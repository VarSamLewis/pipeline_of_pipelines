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
from typing import Any

import polars as pl
from access_control import require_execution_run_access, require_mapping_spec_access
from auth_service import require_auth, require_role
from config import get_settings
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
from workflow import (
    approve_mapping_and_execute,
    approve_result,
    create_organization,
    get_client_file_library,
    get_mapping_review,
    get_organization_key_settings,
    get_result_review,
    ingest_and_propose,
    invite_organization_reviewer,
    list_organization_runs,
    list_organizations,
    list_visible_clients,
    reject_mapping,
    reject_result,
    revoke_organization_reviewer_invitation,
)
from workflow import (
    save_organization_key as save_organization_key_record,
)
from workflow import (
    update_mapping_column_from_editor as save_mapping_column_from_editor,
)

router = APIRouter()

templates = Jinja2Templates(directory=str(get_settings().templates_dir))

CSV_DISP_COUNT = 10


def _htmx_redirect(request: Request, url: str) -> Response:
    """Return an HTMX-compatible redirect response."""
    if request.headers.get("HX-Request") == "true":
        return Response(status_code=204, headers={"HX-Redirect": url})
    return RedirectResponse(url=url, status_code=303)


def _user_context(request: Request, user: Any) -> dict[str, Any]:
    """Build the common template context with request and user."""
    return {"request": request, "user": user}


# ---------------------------------------------------------------------------
# Page 1: Upload
# ---------------------------------------------------------------------------


@router.get("/upload")
def upload_page(request: Request, user: Any = Depends(require_auth)) -> Any:
    """Render the upload form with a client dropdown."""
    return templates.TemplateResponse(
        request,
        "upload.html",
        {
            **_user_context(request, user),
            "organizations": list_organizations(user),
            "clients": list_visible_clients(user),
            "max_source_files": get_settings().max_source_files,
        },
    )


@router.post("/upload")
def upload_submit(
    request: Request,
    client_select: str = Form(""),
    organization_select: str = Form(""),
    new_client_name: str = Form(""),
    source_files: list[UploadFile] = File(default_factory=list),
    target_schema: UploadFile = File(...),
    user: Any = Depends(require_auth),
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

    try:
        target_schema_bytes = target_schema.file.read()
        settings = get_settings()
        if len(target_schema_bytes) > settings.max_upload_size_bytes:
            raise ValueError("Target schema exceeds the individual file limit")
        spec_id = ingest_and_propose(
            existing_client_id=existing_client_id,
            new_client_name=new_client_name or None,
            organization_id=organization_id,
            user=user,
            source_uploads=valid_source_files,
            target_schema_bytes=target_schema_bytes,
        )
    except (RuntimeError, ValueError) as exc:
        return templates.TemplateResponse(
            request,
            "partials/error.html",
            {**_user_context(request, user), "message": str(exc)},
        )

    return _htmx_redirect(request, f"/mapping/{spec_id}")


@router.get("/clients/{client_id}/files")
def client_files_page(
    request: Request,
    client_id: uuid.UUID,
    user: Any = Depends(require_auth),
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


@router.get("/clients/{client_id}/files/panel")
def client_files_panel(
    request: Request,
    client_id: uuid.UUID,
    user: Any = Depends(require_auth),
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
    user: Any = Depends(require_auth),
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
    user: Any = Depends(require_auth),
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


@router.post("/settings/{client_id}/api-key")
def save_organization_key_endpoint(
    request: Request,
    client_id: uuid.UUID,
    api_key: str = Form(""),
    provider_base_url: str = Form(""),
    user: Any = Depends(require_auth),
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
    user: Any = Depends(require_role("admin")),
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
    user: Any = Depends(require_auth),
) -> Any:
    """Invite a user to an organization."""
    try:
        invite_organization_reviewer(organization_id, email, user)
        return HTMLResponse("<span class='save-note'>Invitation sent</span>")
    except (RuntimeError, ValueError) as exc:
        raise HTTPException(422, str(exc)) from exc


@router.post("/settings/invitations")
def invite_user_from_settings_endpoint(
    request: Request,
    organization_id: uuid.UUID = Form(...),
    email: str = Form(""),
    user: Any = Depends(require_auth),
) -> Any:
    """Invite a user to the selected organization from Settings."""
    try:
        invite_organization_reviewer(organization_id, email, user)
        return HTMLResponse("<span class='save-note'>Invitation sent</span>")
    except (RuntimeError, ValueError) as exc:
        raise HTTPException(422, str(exc)) from exc


@router.post("/settings/{organization_id}/invitations/{invitation_id}/revoke")
def revoke_invitation_endpoint(
    request: Request,
    organization_id: uuid.UUID,
    invitation_id: uuid.UUID,
    user: Any = Depends(require_auth),
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
    user: Any = Depends(require_auth),
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
    return templates.TemplateResponse(
        request,
        "mapping.html",
        {
            **_user_context(request, user),
            "spec_id": str(spec_id),
            "mapping_json": mapping_json,
            "mapping_columns": mapping.get("columns", []),
            "source_tables": source_tables,
            "parse_warnings": parse_warnings,
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
    user: Any = Depends(require_auth),
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
    user: Any = Depends(require_auth),
    _: None = Depends(require_execution_run_access),
) -> Any:
    """Render the results review page."""
    return templates.TemplateResponse(
        request,
        "results.html",
        {**_user_context(request, user), "run_id": str(run_id)},
    )


@router.get("/results/{run_id}/csv")
def results_csv_page(
    request: Request,
    run_id: uuid.UUID,
    page: int = Query(1, ge=1),
    user: Any = Depends(require_auth),
    _: None = Depends(require_execution_run_access),
) -> Any:
    """Return a paginated fragment of the results CSV."""
    paths = get_result_review(run_id)
    csv_content = paths["results_csv"]
    if csv_content is None:
        return HTMLResponse("<p class='muted'>No results CSV found.</p>")

    df = pl.read_csv(io.BytesIO(csv_content))
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
            "columns": df.columns,
            "rows": rows,
            "page": page,
            "total_pages": total_pages,
            "start_row": start + 1,
            "end_row": end,
            "total_rows": total_rows,
        },
    )


@router.get("/results/{run_id}/code")
def results_code_page(
    request: Request,
    run_id: uuid.UUID,
    user: Any = Depends(require_auth),
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
    user: Any = Depends(require_auth),
    _: None = Depends(require_execution_run_access),
) -> Any:
    """Reject the results and return to the mapping review page."""
    try:
        spec_id = reject_result(run_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return _htmx_redirect(request, f"/mapping/{spec_id}")
