"""Simplified HTMX UI for the upload-review-execute workflow.

This module replaces the previous dashboard-style UI with a linear three-step
flow:

1. Upload source files + target schema for a client.
2. Review the LLM-proposed mapping JSON.
3. Review the generated CSV results and pipeline code.
"""

from __future__ import annotations

import io
import json
import uuid
from hashlib import sha256
from html import escape
from typing import Any

import polars as pl
from access_control import require_execution_run_access, require_mapping_spec_access
from auth_service import require_client_membership, require_role, require_ui_auth
from config import get_settings
from db_ops import get_session
from dependencies import get_artifact_store
from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    Request,
    UploadFile,
)
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from google_drive import (
    GoogleDriveError,
    connection_for_user,
    import_drive_file,
    picker_access_token,
)
from google_drive import (
    authorization_url as google_authorization_url,
)
from google_drive import (
    connect_callback as google_connect_callback,
)
from google_drive import (
    disconnect as disconnect_google,
)
from google_drive import (
    new_state as new_google_state,
)
from workflow import (
    PartialProposalError,
    approve_mapping_and_execute,
    approve_result,
    create_organization,
    get_client_file_library,
    get_mapping_review,
    get_organization_key_settings,
    get_result_review,
    ingest_and_propose,
    ingest_and_propose_many,
    invite_organization_reviewer,
    list_organization_runs,
    list_organizations,
    list_visible_clients,
    reject_mapping,
    reject_result,
    rerun_mapping_with_feedback,
    revoke_organization_reviewer_invitation,
    save_mapping_feedback,
)
from workflow import (
    save_organization_key as save_organization_key_record,
)
from workflow import (
    update_mapping_column_from_editor as save_mapping_column_from_editor,
)

router = APIRouter()

templates = Jinja2Templates(directory=str(get_settings().templates_dir))
STATIC_CSS_VERSION = sha256(
    (get_settings().static_dir / "style.css").read_bytes()
).hexdigest()[:12]
templates.env.globals["static_css_version"] = STATIC_CSS_VERSION

CSV_DISP_COUNT = 10
LOCAL_UI_PREVIEW = get_settings().ui_preview_local


def _htmx_redirect(request: Request, url: str) -> Response:
    """Return an HTMX-compatible redirect response."""
    if request.headers.get("HX-Request") == "true":
        return Response(status_code=204, headers={"HX-Redirect": url})
    return RedirectResponse(url=url, status_code=303)


def _user_context(request: Request, user: Any) -> dict[str, Any]:
    """Build the common template context with request and user."""
    return {"request": request, "user": user}


@router.get("/privacy")
def privacy_page(request: Request) -> Any:
    """Render the public privacy notice required by third-party OAuth providers."""
    return templates.TemplateResponse(
        request,
        "privacy.html",
        {"request": request, "user": None},
    )


# ---------------------------------------------------------------------------
# Page 1: Upload
# ---------------------------------------------------------------------------


@router.get("/mapping")
@router.get("/upload", include_in_schema=False)
def mapping_page(request: Request, user: Any = Depends(require_ui_auth)) -> Any:
    """Render the source-selection form for a new mapping."""
    with get_session() as session:
        google_connection = connection_for_user(session, user.id)
    return templates.TemplateResponse(
        request,
        "upload.html",
        {
            **_user_context(request, user),
            "organizations": list_organizations(user),
            "clients": list_visible_clients(user),
            "max_source_files": get_settings().max_source_files,
            "google_connection": google_connection,
            "google_picker_api_key": get_settings().google_picker_api_key,
            "google_picker_app_id": get_settings().google_picker_app_id,
        },
    )


@router.get("/google/connect")
def google_connect(request: Request, user: Any = Depends(require_ui_auth)) -> Response:
    """Begin a Google authorization bound to the current Clerk user session."""
    state = new_google_state()
    request.session["google_oauth_state"] = state
    request.session["google_oauth_user_id"] = str(user.id)
    try:
        return RedirectResponse(google_authorization_url(state), status_code=303)
    except GoogleDriveError as exc:
        raise HTTPException(503, str(exc)) from exc


@router.get("/google/status")
def google_status(user: Any = Depends(require_ui_auth)) -> dict[str, Any]:
    """Return non-sensitive connection status; never return OAuth credentials."""
    with get_session() as session:
        connection = connection_for_user(session, user.id)
    return {
        "connected": connection is not None and connection.status.value == "active",
        "status": connection.status.value if connection is not None else "disconnected",
        "email": connection.google_email if connection is not None else None,
        "scopes": connection.granted_scopes if connection is not None else [],
    }


@router.get("/google/callback")
def google_callback(
    request: Request,
    state: str = "",
    code: str = "",
    user: Any = Depends(require_ui_auth),
) -> Response:
    """Complete Google OAuth only when its state and original Clerk user match."""
    expected_state = request.session.pop("google_oauth_state", None)
    expected_user_id = request.session.pop("google_oauth_user_id", None)
    if (
        not state
        or not code
        or state != expected_state
        or expected_user_id != str(user.id)
    ):
        raise HTTPException(400, "Invalid Google authorization state")
    try:
        google_connect_callback(user.id, code)
    except GoogleDriveError as exc:
        raise HTTPException(422, str(exc)) from exc
    return RedirectResponse("/upload", status_code=303)


@router.post("/google/disconnect")
def google_disconnect(user: Any = Depends(require_ui_auth)) -> Response:
    disconnect_google(user.id)
    return RedirectResponse("/upload", status_code=303)


@router.get("/google/picker-token")
def google_picker_token(user: Any = Depends(require_ui_auth)) -> dict[str, str]:
    """Provide a short-lived access token exclusively to launch Google Picker."""
    try:
        return {"access_token": picker_access_token(user.id)}
    except GoogleDriveError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.post("/clients/{client_id}/google-drive/import")
def google_drive_import(
    client_id: uuid.UUID,
    drive_file_id: str = Form(""),
    user: Any = Depends(require_ui_auth),
) -> Any:
    """Import one explicitly selected Drive file as an immutable client snapshot."""
    if not drive_file_id.strip():
        raise HTTPException(422, "Select a Drive file first")
    with get_session() as session:
        require_client_membership(session, user, client_id)
    try:
        audit = import_drive_file(
            user_id=user.id, client_id=client_id, drive_file_id=drive_file_id.strip()
        )
    except GoogleDriveError as exc:
        raise HTTPException(422, str(exc)) from exc
    return HTMLResponse(
        "<span class='save-note'>Imported Drive snapshot. It is selected below "
        "and ready to include in a mapping.</span>",
        headers={"X-Imported-Raw-File-ID": str(audit.raw_file_id)},
    )


@router.post("/upload")
def upload_submit(
    request: Request,
    client_select: str = Form(""),
    organization_select: str = Form(""),
    new_client_name: str = Form(""),
    source_files: list[UploadFile] = File(default_factory=list),
    stored_raw_file_ids: list[uuid.UUID] = Form(default_factory=list),
    target_schema: list[UploadFile] = File(default_factory=list),
    user: Any = Depends(require_ui_auth),
) -> Any:
    """Ingest files, extract evidence, and propose a mapping."""
    existing_client_id = uuid.UUID(client_select) if client_select else None
    if not organization_select:
        return templates.TemplateResponse(
            request,
            "partials/error.html",
            {**_user_context(request, user), "message": "Select an organization"},
        )
    organization_id = uuid.UUID(organization_select)
    valid_source_files = [f for f in source_files if f.filename]

    if LOCAL_UI_PREVIEW:
        return _htmx_redirect(request, "/mapping/preview")

    try:
        if not target_schema or any(not schema.filename for schema in target_schema):
            raise ValueError("At least one target schema file is required")
        target_schema_files = [schema.file.read() for schema in target_schema]
        settings = get_settings()
        if any(
            len(content) > settings.max_upload_size_bytes
            for content in target_schema_files
        ):
            raise ValueError("Target schema exceeds the individual file limit")
        options = {
            "existing_client_id": existing_client_id,
            "new_client_name": new_client_name or None,
            "organization_id": organization_id,
            "user": user,
            "source_uploads": valid_source_files,
            "stored_raw_file_ids": stored_raw_file_ids,
        }
        if len(target_schema_files) == 1:
            spec_ids = [
                ingest_and_propose(
                    **options, target_schema_bytes=target_schema_files[0]
                )
            ]
        else:
            spec_ids = ingest_and_propose_many(
                **options, target_schema_files=target_schema_files
            )
    except PartialProposalError as exc:
        return templates.TemplateResponse(
            request,
            "partials/mappings_created.html",
            {
                **_user_context(request, user),
                "mappings": list(
                    zip(
                        exc.completed_ids,
                        [s.filename for s in target_schema[: len(exc.completed_ids)]],
                        strict=True,
                    )
                ),
                "error": str(exc),
            },
        )
    except (RuntimeError, ValueError) as exc:
        return templates.TemplateResponse(
            request,
            "partials/error.html",
            {**_user_context(request, user), "message": str(exc)},
        )

    if len(spec_ids) == 1:
        return _htmx_redirect(request, f"/mapping/{spec_ids[0]}")
    return templates.TemplateResponse(
        request,
        "partials/mappings_created.html",
        {
            **_user_context(request, user),
            "mappings": list(
                zip(spec_ids, [s.filename for s in target_schema], strict=True)
            ),
        },
    )


@router.get("/mapping/preview")
def local_mapping_preview_page(
    request: Request,
    user: Any = Depends(require_ui_auth),
) -> Any:
    """Render a static mapping-review screen for local UI development only."""
    if not LOCAL_UI_PREVIEW:
        raise HTTPException(404, "Mapping review not found")
    return templates.TemplateResponse(
        request,
        "mapping_preview.html",
        _user_context(request, user),
    )


@router.get("/clients/{client_id}/files")
def client_files_page(
    request: Request,
    client_id: uuid.UUID,
    user: Any = Depends(require_ui_auth),
) -> Any:
    """Render the authorized file inventory for one client."""
    try:
        library = get_client_file_library(client_id, user)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc
    return templates.TemplateResponse(
        request,
        "files.html",
        {**_user_context(request, user), "library": library},
    )


@router.get("/results/preview")
def local_results_preview_page(
    request: Request,
    user: Any = Depends(require_ui_auth),
) -> Any:
    """Render a static results-review screen for local UI development only."""
    if not LOCAL_UI_PREVIEW:
        raise HTTPException(404, "Results review not found")
    return templates.TemplateResponse(
        request,
        "results_preview.html",
        _user_context(request, user),
    )


@router.get("/client-library")
def clients_page(request: Request, user: Any = Depends(require_ui_auth)) -> Any:
    """Render the clients visible to the current user."""
    return templates.TemplateResponse(
        request,
        "clients.html",
        {**_user_context(request, user), "clients": list_visible_clients(user)},
    )


@router.get("/clients/{client_id}/files/panel")
def client_files_panel(
    request: Request,
    client_id: uuid.UUID,
    user: Any = Depends(require_ui_auth),
) -> Any:
    """Render a compact authorized client-file summary for the upload page."""
    try:
        library = get_client_file_library(client_id, user)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc
    return templates.TemplateResponse(
        request,
        "partials/client_files_panel.html",
        {**_user_context(request, user), "library": library},
    )


@router.get("/runs")
def runs_page(
    request: Request,
    organization_id: uuid.UUID | None = Query(default=None),
    user: Any = Depends(require_ui_auth),
) -> Any:
    """Render an organization's execution-run history."""
    organizations = list_organizations(user)
    selected_organization_id = organization_id or (
        organizations[0].id if organizations else None
    )
    try:
        runs = (
            list_organization_runs(selected_organization_id, user)
            if selected_organization_id is not None
            else []
        )
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc
    return templates.TemplateResponse(
        request,
        "runs.html",
        {
            **_user_context(request, user),
            "organizations": organizations,
            "selected_organization_id": selected_organization_id,
            "runs": runs,
        },
    )


@router.get("/settings")
def organization_settings_page(
    request: Request,
    user: Any = Depends(require_ui_auth),
) -> Any:
    """Render organization-level BYOK settings for an authenticated user."""
    return templates.TemplateResponse(
        request,
        "settings.html",
        {
            **_user_context(request, user),
            "organizations": get_organization_key_settings(user),
        },
    )


@router.get("/documentation")
def documentation_page(request: Request, user: Any = Depends(require_ui_auth)) -> Any:
    """Render the in-app guide for the web workflow."""
    return templates.TemplateResponse(
        request,
        "documentation.html",
        _user_context(request, user),
    )


@router.post("/settings/{client_id}/api-key")
def save_organization_key_endpoint(
    request: Request,
    client_id: uuid.UUID,
    api_key: str = Form(""),
    provider_base_url: str = Form(""),
    user: Any = Depends(require_ui_auth),
) -> Any:
    """Save an encrypted organization API key without rendering it back."""
    try:
        save_organization_key_record(client_id, api_key, provider_base_url, user)
    except (RuntimeError, ValueError) as exc:
        raise HTTPException(422, str(exc)) from exc
    return HTMLResponse("<span class='save-note'>Key saved</span>")


@router.post("/settings/organizations")
def create_organization_endpoint(
    request: Request,
    name: str = Form(""),
    user: Any = Depends(require_ui_auth),
) -> Any:
    """Create an organization before configuring its members and credentials."""
    try:
        create_organization(name, user)
    except ValueError as exc:
        return templates.TemplateResponse(
            request,
            "partials/error.html",
            {**_user_context(request, user), "message": str(exc)},
        )
    return _htmx_redirect(request, "/settings")


@router.post("/settings/{organization_id}/invite")
def invite_user_endpoint(
    request: Request,
    organization_id: uuid.UUID,
    email: str = Form(""),
    user: Any = Depends(require_ui_auth),
) -> Any:
    """Invite a user to an organization."""
    try:
        invite_organization_reviewer(organization_id, email, user)
        return HTMLResponse("<span class='save-note'>Invitation sent</span>")
    except (RuntimeError, ValueError) as exc:
        return HTMLResponse(
            f"<span class='warning-error'>{escape(str(exc))}</span>",
        )


@router.post("/settings/invitations")
def invite_user_from_settings_endpoint(
    request: Request,
    organization_id: str = Form(""),
    email: str = Form(""),
    user: Any = Depends(require_ui_auth),
) -> Any:
    """Invite a user to the selected organization from Settings."""
    try:
        parsed_organization_id = (
            None if organization_id == "create-own" else uuid.UUID(organization_id)
        )
        invite_organization_reviewer(parsed_organization_id, email, user)
        return HTMLResponse("<span class='save-note'>Invitation sent</span>")
    except (RuntimeError, ValueError) as exc:
        return HTMLResponse(
            f"<span class='warning-error'>{escape(str(exc))}</span>",
        )


@router.post("/settings/{organization_id}/invitations/{invitation_id}/revoke")
def revoke_invitation_endpoint(
    request: Request,
    organization_id: uuid.UUID,
    invitation_id: uuid.UUID,
    user: Any = Depends(require_ui_auth),
) -> Any:
    """Revoke a pending invitation."""
    try:
        revoke_organization_reviewer_invitation(organization_id, invitation_id, user)
        return HTMLResponse("<span class='save-note'>Invitation revoked</span>")
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


# ---------------------------------------------------------------------------
# Page 2: Review mapping
# ---------------------------------------------------------------------------


@router.get("/mapping/{spec_id}")
def mapping_review_page(
    request: Request,
    spec_id: uuid.UUID,
    user: Any = Depends(require_ui_auth),
    _: None = Depends(require_mapping_spec_access),
) -> Any:
    """Render the proposed mapping JSON for human review."""
    mapping = get_mapping_review(spec_id)
    mapping_json = json.dumps(mapping, indent=2, default=str)
    source_tables = [
        table
        for catalog in mapping.get("source_catalogs", [])
        for table in catalog.get("tables", [])
    ]
    parse_warnings = [
        {
            **warning,
            "source": catalog.get("original_filename") or "Source file",
        }
        for catalog in mapping.get("source_catalogs", [])
        for warning in catalog.get("warnings", [])
    ]
    parse_warnings.extend(
        {
            **warning,
            "source": table.get("display_name") or table.get("source_table_id"),
        }
        for table in source_tables
        for warning in table.get("warnings", [])
    )
    target_columns = {
        (table.get("name"), column.get("name")): column
        for table in mapping["target_schema_json"].get("tables", [])
        for column in table.get("columns", [])
    }
    catalog_columns = {
        (str(table.get("source_table_id")), str(column.get("source_column_id"))): (
            table,
            column,
        )
        for table in source_tables
        for column in table.get("columns", [])
    }
    review_columns = []
    for mapping_column in mapping.get("columns", []):
        column = dict(mapping_column)
        target = target_columns.get(
            (column["target_table"], column["target_column"]), {}
        )
        source: dict[str, Any] = next(iter(column.get("source_columns", [])), {})
        source_table, source_column = catalog_columns.get(
            (str(source.get("source_table_id")), str(source.get("source_column_id"))),
            ({}, {}),
        )
        column["target_definition"] = target
        column["source"] = source
        column["source_table_display"] = source_table.get("display_name")
        column["source_column_display"] = source_column.get(
            "normalized_name"
        ) or source_column.get("original_name")
        column["source_profile"] = source_column
        column["is_unmapped"] = not source
        review_columns.append(column)
    return templates.TemplateResponse(
        request,
        "mapping.html",
        {
            **_user_context(request, user),
            "spec_id": str(spec_id),
            "mapping_json": mapping_json,
            "target_schema_json": json.dumps(
                mapping["target_schema_json"], indent=2, default=str
            ),
            "mapping_columns": review_columns,
            "source_tables": source_tables,
            "parse_warnings": parse_warnings,
            "mapping": mapping,
        },
    )


@router.get("/mapping/{spec_id}/target-schema.json")
def mapping_target_schema_file(
    spec_id: uuid.UUID,
    user: Any = Depends(require_ui_auth),
    _: None = Depends(require_mapping_spec_access),
) -> Response:
    """Return the target schema file attached to one mapping specification."""
    mapping = get_mapping_review(spec_id)
    return Response(
        content=json.dumps(mapping["target_schema_json"], indent=2),
        media_type="application/json",
        headers={
            "Content-Disposition": (
                f'attachment; filename="{spec_id}_target_schema.json"'
            )
        },
    )


@router.post("/mapping/{spec_id}/columns/{column_id}")
def update_mapping_column_from_editor(
    request: Request,
    spec_id: uuid.UUID,
    column_id: uuid.UUID,
    source_ref: str = Form(""),
    transformation_type: str = Form("expression"),
    transformation_logic: str = Form(""),
    polars_expression: str = Form(""),
    lookup_source_table: str = Form(""),
    lookup_key: str = Form(""),
    lookup_value: str = Form(""),
    user: Any = Depends(require_role("reviewer")),
    _: None = Depends(require_mapping_spec_access),
) -> Any:
    """Save one guided mapping edit after validating catalog selections."""
    try:
        save_mapping_column_from_editor(
            spec_id,
            column_id,
            source_ref=source_ref,
            transformation_type=transformation_type,
            transformation_logic=transformation_logic,
            polars_expression=polars_expression,
            lookup_source_table=lookup_source_table,
            lookup_key=lookup_key,
            lookup_value=lookup_value,
        )
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc

    return HTMLResponse("<span class='save-note'>Saved</span>")


@router.post("/mapping/{spec_id}/columns/{column_id}/feedback")
def save_mapping_feedback_from_review(
    spec_id: uuid.UUID,
    column_id: uuid.UUID,
    comment: str = Form(...),
    user: Any = Depends(require_role("reviewer")),
    _: None = Depends(require_mapping_spec_access),
) -> Any:
    """Persist reviewer feedback for one proposed mapping field."""
    try:
        save_mapping_feedback(
            spec_id,
            column_id,
            author_user_id=user.id,
            comment=comment,
        )
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    return HTMLResponse("<span class='save-note'>Feedback saved</span>")


@router.post("/mapping/{spec_id}/rerun")
def rerun_mapping_from_review(
    request: Request,
    spec_id: uuid.UUID,
    user: Any = Depends(require_role("reviewer")),
    _: None = Depends(require_mapping_spec_access),
) -> Any:
    """Create and propose a revision using the saved field feedback."""
    try:
        revision_id = rerun_mapping_with_feedback(spec_id)
    except (RuntimeError, ValueError) as exc:
        return templates.TemplateResponse(
            request,
            "partials/error.html",
            {**_user_context(request, user), "message": str(exc)},
        )
    return _htmx_redirect(request, f"/mapping/{revision_id}")


@router.post("/mapping/{spec_id}/confirm")
def mapping_confirm(
    request: Request,
    spec_id: uuid.UUID,
    user: Any = Depends(require_role("admin")),
    _: None = Depends(require_mapping_spec_access),
) -> Any:
    """Approve the reviewed mapping, then generate and execute its pipeline."""
    try:
        run_id = approve_mapping_and_execute(spec_id)
    except (RuntimeError, ValueError) as exc:
        return templates.TemplateResponse(
            request,
            "partials/error.html",
            {**_user_context(request, user), "message": str(exc)},
        )
    return _htmx_redirect(request, f"/results/{run_id}")


@router.post("/mapping/{spec_id}/reject")
def mapping_reject(
    request: Request,
    spec_id: uuid.UUID,
    user: Any = Depends(require_ui_auth),
    _: None = Depends(require_mapping_spec_access),
) -> Any:
    """Reject the mapping and return to the upload page."""
    reject_mapping(spec_id)
    return _htmx_redirect(request, "/upload")


# ---------------------------------------------------------------------------
# Page 3: Review results
# ---------------------------------------------------------------------------


@router.get("/results/{run_id}")
def results_review_page(
    request: Request,
    run_id: uuid.UUID,
    user: Any = Depends(require_ui_auth),
    _: None = Depends(require_execution_run_access),
) -> Any:
    """Render the results review page."""
    review = get_result_review(run_id)
    return templates.TemplateResponse(
        request,
        "results.html",
        {
            **_user_context(request, user),
            "run_id": str(run_id),
            "csv_files": review["csv_files"],
        },
    )


@router.get("/results/{run_id}/csv")
def results_csv_page(
    request: Request,
    run_id: uuid.UUID,
    page: int = Query(1, ge=1),
    file: str | None = Query(None),
    search: str = Query(""),
    column: str | None = Query(None),
    user: Any = Depends(require_ui_auth),
    _: None = Depends(require_execution_run_access),
) -> Any:
    """Return a paginated fragment of the results CSV."""
    paths = get_result_review(run_id)
    if file is not None and file not in paths["csv_files"]:
        raise HTTPException(404, "Result CSV not found")
    if file is None:
        csv_content = paths["results_csv"]
    else:
        try:
            csv_content = get_artifact_store().read_artifact(
                paths["run"].mapping_spec_id, file
            )
        except FileNotFoundError as exc:
            raise HTTPException(404, "Result CSV not found") from exc
    if csv_content is None:
        return HTMLResponse("<p class='muted'>No results CSV found.</p>")

    df = pl.read_csv(io.BytesIO(csv_content))
    if column is not None and column not in df.columns:
        raise HTTPException(422, "Selected filter column is not in the result CSV")
    search = search.strip()
    if search:
        searchable_columns = [column] if column else df.columns
        matching_rows = [
            row
            for row in df.iter_rows(named=True)
            if any(
                search.casefold() in str(row[name]).casefold()
                for name in searchable_columns
            )
        ]
        df = (
            pl.DataFrame(matching_rows, schema=df.schema)
            if matching_rows
            else df.head(0)
        )
    total_rows = len(df)
    total_pages = max(1, (total_rows + CSV_DISP_COUNT - 1) // CSV_DISP_COUNT)
    page = min(page, total_pages)
    start = (page - 1) * CSV_DISP_COUNT
    end = min(start + CSV_DISP_COUNT, total_rows)
    page_df = df.slice(start, end - start)

    rows = [dict(row) for row in page_df.iter_rows(named=True)]
    return templates.TemplateResponse(
        request,
        "partials/csv.html",
        {
            **_user_context(request, user),
            "run_id": str(run_id),
            "csv_file": file,
            "columns": df.columns,
            "rows": rows,
            "page": page,
            "total_pages": total_pages,
            "start_row": start + 1,
            "end_row": end,
            "total_rows": total_rows,
            "search": search,
            "selected_column": column,
        },
    )


@router.get("/results/{run_id}/csv/download")
def download_results_csv(
    run_id: uuid.UUID,
    file: str | None = Query(None),
    user: Any = Depends(require_ui_auth),
    _: None = Depends(require_execution_run_access),
) -> Response:
    """Download one generated CSV after validating run access."""
    paths = get_result_review(run_id)
    if file is not None and file not in paths["csv_files"]:
        raise HTTPException(404, "Result CSV not found")
    filename = file or next(iter(paths["csv_files"]), None)
    if filename is None:
        raise HTTPException(404, "No result CSV found")
    try:
        content = get_artifact_store().read_artifact(
            paths["run"].mapping_spec_id, filename
        )
    except FileNotFoundError as exc:
        raise HTTPException(404, "Result CSV not found") from exc
    return Response(
        content=content,
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/results/{run_id}/code")
def results_code_page(
    request: Request,
    run_id: uuid.UUID,
    user: Any = Depends(require_ui_auth),
    _: None = Depends(require_execution_run_access),
) -> Any:
    """Return the generated pipeline.py code fragment."""
    paths = get_result_review(run_id)
    pipeline_content = paths["pipeline_py"]
    if pipeline_content is None:
        return HTMLResponse("<p class='muted'>No generated code found.</p>")

    code = pipeline_content.decode("utf-8")
    return templates.TemplateResponse(
        request,
        "partials/code.html",
        {**_user_context(request, user), "code": code},
    )


@router.post("/results/{run_id}/confirm")
def results_confirm(
    request: Request,
    run_id: uuid.UUID,
    user: Any = Depends(require_role("admin")),
    _: None = Depends(require_execution_run_access),
) -> Any:
    """Confirm the results and finish the workflow."""
    try:
        approve_result(run_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return templates.TemplateResponse(
        request,
        "partials/success.html",
        {
            **_user_context(request, user),
            "message": "Results confirmed. The output folder is ready.",
            "redirect": "/upload",
        },
    )


@router.post("/results/{run_id}/reject")
def results_reject(
    request: Request,
    run_id: uuid.UUID,
    user: Any = Depends(require_ui_auth),
    _: None = Depends(require_execution_run_access),
) -> Any:
    """Reject the results and return to the mapping review page."""
    try:
        spec_id = reject_result(run_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return _htmx_redirect(request, f"/mapping/{spec_id}")
