"""Simplified HTMX UI for the upload-review-execute workflow.

This module replaces the previous dashboard-style UI with a linear three-step
flow:

1. Upload source files + target schema for a client.
2. Review the LLM-proposed mapping JSON.
3. Review the generated CSV results and pipeline code.
"""

from __future__ import annotations

import contextlib
import json
import uuid
from datetime import UTC, datetime
from typing import Any

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
    get_mapping_review,
    get_result_review,
    ingest_and_propose,
    list_clients,
    reject_mapping,
    reject_result,
    retry_pipeline_and_execute,
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
        {**_user_context(request, user), "clients": list_clients()},
    )


@router.post("/upload")
def upload_submit(
    request: Request,
    client_select: str = Form(""),
    new_client_name: str = Form(""),
    source_files: list[UploadFile] = File(default_factory=list),
    target_schema: UploadFile = File(...),
    user: Any = Depends(require_auth),
) -> Any:
    """Ingest files, extract evidence, and propose a mapping."""
    existing_client_id = uuid.UUID(client_select) if client_select else None
    valid_source_files = [f for f in source_files if f.filename]

    try:
        target_schema_bytes = target_schema.file.read()
        spec_id = ingest_and_propose(
            existing_client_id=existing_client_id,
            new_client_name=new_client_name or None,
            source_uploads=valid_source_files,
            target_schema_bytes=target_schema_bytes,
        )
    except ValueError as exc:
        return templates.TemplateResponse(
            request,
            "partials/error.html",
            {**_user_context(request, user), "message": str(exc)},
        )

    return _htmx_redirect(request, f"/mapping/{spec_id}")


# ---------------------------------------------------------------------------
# Page 2: Review mapping
# ---------------------------------------------------------------------------


@router.get("/mapping/{spec_id}")
def mapping_review_page(
    request: Request,
    spec_id: uuid.UUID,
    user: Any = Depends(require_auth),
) -> Any:
    """Render the proposed mapping for human review."""
    context = _mapping_review_context(spec_id)
    return templates.TemplateResponse(
        request,
        "mapping.html",
        {
            **_user_context(request, user),
            "spec_id": str(spec_id),
            **context,
        },
    )


def _mapping_review_context(spec_id: uuid.UUID) -> dict[str, Any]:
    """Build the shared mapping-review context (page and drawer re-renders)."""
    from models import TargetSchema

    mapping = get_mapping_review(spec_id)

    columns = mapping.get("columns", [])
    columns_by_table: dict[str, list[dict[str, Any]]] = {}
    for col in columns:
        columns_by_table.setdefault(col["target_table"], []).append(col)

    target_column_info: dict[tuple[str, str], dict[str, Any]] = {}
    try:
        ts = mapping.get("target_schema_json")
        if ts:
            schema = TargetSchema.model_validate(ts)
            for table in schema.tables:
                for col in table.columns:
                    target_column_info[(table.name, col.name)] = {
                        "dtype": col.dtype,
                        "required": col.required,
                        "description": col.description,
                        "allowed_values": col.allowed_values,
                        "unique": col.unique,
                    }
    except Exception:
        pass

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
    return {
        "columns_by_table": columns_by_table,
        "target_column_info": target_column_info,
        "mapping_json": json.dumps(mapping, indent=2, default=str),
        "source_tables": source_tables,
        "source_catalogs": mapping.get("source_catalogs", []),
        "parse_warnings": parse_warnings,
        "generated_at": datetime.now(UTC).strftime("%d %b %Y at %H:%M UTC"),
    }


@router.post("/mapping/{spec_id}")
def mapping_post(
    request: Request,
    spec_id: uuid.UUID,
    user: Any = Depends(require_auth),
) -> Any:
    """Handle POST to the mapping page.

    Chat refinements are applied exclusively through the dedicated
    ``/mapping/{spec_id}/chat/apply`` endpoint; a bare POST here just
    re-renders the review page.
    """
    return _htmx_redirect(request, f"/mapping/{spec_id}")


def _mapping_edit_context(
    spec_id: uuid.UUID,
    column: dict[str, Any],
) -> dict[str, Any]:
    """Build the drawer context for editing a single mapping column."""
    context = _mapping_review_context(spec_id)
    source_options: list[dict[str, str]] = []
    for table in context["source_tables"]:
        for src_col in table.get("columns", []):
            file_id = str(
                src_col.get("source_file_id") or table.get("source_table_id") or ""
            )
            sheet_name = str(table.get("sheet_name") or "")
            col_name = str(src_col.get("name") or "")
            key = "::".join([file_id, sheet_name, col_name])
            source_options.append(
                {
                    "value": key,
                    "label": f"{file_id}::{sheet_name}::{col_name}",
                }
            )

    def _ref_to_key(ref: Any) -> str:
        if isinstance(ref, dict):
            source_table = ref.get("source_table") or ""
            source_column = ref.get("source_column") or ""
            return "::".join(part for part in [source_table, source_column] if part)
        return ""

    selected = ",".join(
        _ref_to_key(ref) for ref in (column.get("source_columns_json") or [])
    )
    if not selected and isinstance(column.get("source_columns"), list):
        selected = ",".join(_ref_to_key(ref) for ref in column["source_columns"])
    return {
        "spec_id": str(spec_id),
        "column": column,
        "col": column,
        "target_column_info": context["target_column_info"],
        "source_options": source_options,
        "source_columns": selected,
        "mapping_done": True,
    }


@router.get("/mapping/{spec_id}/columns/{column_id}/edit")
def mapping_column_edit(
    request: Request,
    spec_id: uuid.UUID,
    column_id: uuid.UUID,
    user: Any = Depends(require_role("reviewer")),
) -> Any:
    """Render the edit drawer for one mapping column."""
    target_id = str(column_id)
    for column in get_mapping_review(spec_id).get("columns", []):
        if column.get("id") == target_id:
            return templates.TemplateResponse(
                request,
                "partials/mapping_edit.html",
                {
                    **_user_context(request, user),
                    **_mapping_edit_context(spec_id, column),
                },
            )
    raise HTTPException(status_code=404, detail="Column not found")


@router.post("/mapping/{spec_id}/columns/{column_id}")
def mapping_column_save(
    request: Request,
    spec_id: uuid.UUID,
    column_id: uuid.UUID,
    user: Any = Depends(require_role("reviewer")),
    target_column: str = Form(""),
    transformation_type: str = Form(""),
    source_columns: str = Form(""),
    polars_expression: str = Form(""),
    lookup_source: str = Form(""),
    lookup_key: str = Form(""),
    lookup_value: str = Form(""),
    aggregation_source: str = Form(""),
    aggregation_group_key: str = Form(""),
    aggregation_expression: str = Form(""),
    filter_expression: str = Form(""),
    tests: str = Form(""),
    sort_order: str = Form(""),
    rationale: str = Form(""),
) -> Any:
    """Save an edited mapping column through the canonical refine path."""
    import json as _json

    from workflow import apply_refinements

    def _clean(text: str) -> str:
        return text.strip()

    source_columns_clean = _clean(source_columns)
    source_list = [
        part.strip() for part in source_columns_clean.split(",") if part.strip()
    ]
    if source_list:
        source_json: list[dict[str, str]] = []
        for key in source_list:
            parts = [part for part in key.split("::") if part]
            if len(parts) >= 2:
                source_json.append(
                    {
                        "source_table": "::".join(parts[:-1]),
                        "source_column": parts[-1],
                    }
                )
    else:
        source_json = []

    fields: dict[str, Any] = {}
    if _clean(target_column):
        fields["target_column"] = _clean(target_column)
    if _clean(transformation_type):
        fields["transformation_type"] = _clean(transformation_type)
    if source_json:
        fields["source_columns_json"] = source_json
    if _clean(polars_expression):
        fields["polars_expression"] = _clean(polars_expression)
    if _clean(lookup_source):
        fields["lookup_source_table"] = _clean(lookup_source)
    if _clean(lookup_key):
        fields["lookup_key"] = _clean(lookup_key)
    if _clean(lookup_value):
        fields["lookup_value"] = _clean(lookup_value)
    if _clean(aggregation_source):
        fields["aggregation_source_table"] = _clean(aggregation_source)
    if _clean(aggregation_group_key):
        fields["aggregation_group_key"] = _clean(aggregation_group_key)
    if _clean(aggregation_expression):
        fields["aggregation_expression"] = _clean(aggregation_expression)
    if _clean(filter_expression):
        fields["filter_expression"] = _clean(filter_expression)
    if _clean(tests):
        try:
            tests_list = _json.loads(tests)
            fields["tests"] = (
                tests_list if isinstance(tests_list, list) else [str(tests)]
            )
        except _json.JSONDecodeError:
            fields["tests"] = [
                line.strip() for line in tests.splitlines() if line.strip()
            ]
    if _clean(sort_order):
        with contextlib.suppress(ValueError):
            fields["sort_order"] = int(_clean(sort_order))
    if _clean(rationale):
        fields["rationale"] = _clean(rationale)

    changes = [
        {"column_id": str(column_id), "field": field, "new_value": value}
        for field, value in fields.items()
    ]
    try:
        apply_refinements(spec_id, changes, user)
    except Exception as exc:
        return templates.TemplateResponse(
            request,
            "partials/error.html",
            {**_user_context(request, user), "message": f"Save failed: {exc}"},
        )

    return _htmx_redirect(request, f"/mapping/{spec_id}")


@router.post("/mapping/{spec_id}/confirm")
def mapping_confirm(
    request: Request,
    spec_id: uuid.UUID,
    user: Any = Depends(require_auth),
) -> Any:
    """Approve the mapping and run codegen + execution."""
    try:
        run_id = approve_mapping_and_execute(spec_id)
    except ValueError as exc:
        return templates.TemplateResponse(
            request,
            "partials/error.html",
            {**_user_context(request, user), "message": str(exc)},
        )
    except RuntimeError as exc:
        return templates.TemplateResponse(
            request,
            "partials/pipeline_error.html",
            {
                **_user_context(request, user),
                "error": str(exc),
                "spec_id": str(spec_id),
            },
        )
    return _htmx_redirect(request, f"/results/{run_id}")


@router.post("/mapping/{spec_id}/retry")
def mapping_retry(
    request: Request,
    spec_id: uuid.UUID,
    user: Any = Depends(require_auth),
    error: str = Form(default=""),
) -> Any:
    """Retry pipeline code generation with the runtime error as feedback."""
    try:
        run_id = retry_pipeline_and_execute(spec_id, error)
    except (ValueError, RuntimeError) as exc:
        return templates.TemplateResponse(
            request,
            "partials/pipeline_error.html",
            {
                **_user_context(request, user),
                "error": str(exc),
                "spec_id": str(spec_id),
            },
        )
    return _htmx_redirect(request, f"/results/{run_id}")


@router.post("/mapping/{spec_id}/reject")
def mapping_reject(
    request: Request,
    spec_id: uuid.UUID,
    user: Any = Depends(require_auth),
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
) -> Any:
    """Return a paginated fragment of the results CSV with overrides applied."""
    from workflow import get_merged_results

    merged = get_merged_results(run_id)
    if merged is None:
        return HTMLResponse("<p class='muted'>No results CSV found.</p>")

    total_rows = len(merged["merged_rows"])
    total_pages = max(1, (total_rows + CSV_DISP_COUNT - 1) // CSV_DISP_COUNT)
    page = min(page, total_pages)
    start = (page - 1) * CSV_DISP_COUNT
    end = min(start + CSV_DISP_COUNT, total_rows)
    page_rows = merged["merged_rows"][start:end]

    row_key_col = merged["row_key_column"]
    rows: list[dict[str, Any]] = []
    for row in page_rows:
        key = str(row.get(row_key_col, "")) if row_key_col else ""
        row_meta = {
            "cells": row,
            "overridden": {
                col: bool(merged["overridden"].get((key, col)))
                for col in merged["merged_rows"][0]
            },
            "row_key": key,
        }
        rows.append(row_meta)

    columns = list(merged["merged_rows"][0].keys()) if merged["merged_rows"] else []
    return templates.TemplateResponse(
        request,
        "partials/csv.html",
        {
            **_user_context(request, user),
            "run_id": str(run_id),
            "spec_id": _run_spec_id(run_id),
            "target_table": merged["target_table"],
            "row_key_column": row_key_col,
            "columns": columns,
            "rows": rows,
            "page": page,
            "total_pages": total_pages,
            "start_row": start + 1,
            "end_row": end,
            "total_rows": total_rows,
            "overrides": merged["overrides"],
            "override_count": merged["override_count"],
        },
    )


@router.get("/results/{run_id}/code")
def results_code_page(
    request: Request,
    run_id: uuid.UUID,
    user: Any = Depends(require_auth),
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
        {
            **_user_context(request, user),
            "run_id": str(run_id),
            "spec_id": str(paths["run"].mapping_spec_id),
            "code": code,
        },
    )


@router.post("/results/{run_id}/code")
def results_code_save(
    request: Request,
    run_id: uuid.UUID,
    user: Any = Depends(require_role("reviewer")),
    code: str = Form(...),
) -> Any:
    """Overwrite the generated pipeline.py with reviewer edits."""
    from workflow import get_result_review, overwrite_pipeline_code

    try:
        paths = get_result_review(run_id)
        spec_id = paths["run"].mapping_spec_id
        overwrite_pipeline_code(spec_id, code, user)
    except Exception as exc:
        return templates.TemplateResponse(
            request,
            "partials/error.html",
            {**_user_context(request, user), "message": f"Save failed: {exc}"},
        )
    return _htmx_redirect(request, f"/results/{run_id}/code")


@router.post("/results/{run_id}/code/reset")
def results_code_reset(
    request: Request,
    run_id: uuid.UUID,
    user: Any = Depends(require_role("reviewer")),
) -> Any:
    """Regenerate pipeline.py from the mapping contract."""
    from workflow import get_result_review, reset_pipeline_code

    try:
        paths = get_result_review(run_id)
        spec_id = paths["run"].mapping_spec_id
        code = reset_pipeline_code(spec_id, user)
    except Exception as exc:
        return templates.TemplateResponse(
            request,
            "partials/error.html",
            {**_user_context(request, user), "message": f"Reset failed: {exc}"},
        )
    return templates.TemplateResponse(
        request,
        "partials/code.html",
        {
            **_user_context(request, user),
            "run_id": str(run_id),
            "spec_id": str(spec_id),
            "code": code,
        },
    )


def _run_spec_id(run_id: uuid.UUID) -> str:
    """Resolve the mapping spec id behind an execution run."""
    try:
        return str(get_result_review(run_id)["run"].mapping_spec_id)
    except Exception:
        return ""


@router.get("/results/{run_id}/columns/{column}/provenance")
def column_provenance(
    request: Request,
    run_id: uuid.UUID,
    column: str,
    user: Any = Depends(require_auth),
) -> Any:
    """Return the mapping rule / lineage behind a results column."""
    from workflow import get_column_provenance

    spec_id = _run_spec_id(run_id)
    rule = get_column_provenance(uuid.UUID(spec_id), column) if spec_id else None
    return templates.TemplateResponse(
        request,
        "partials/lineage.html",
        {
            **_user_context(request, user),
            "run_id": str(run_id),
            "column": column,
            "rule": rule,
            "source_catalogs": rule.get("source_catalogs", []) if rule else [],
        },
    )


@router.get("/results/{run_id}/overrides/new")
def override_new(
    request: Request,
    run_id: uuid.UUID,
    column: str = Query(...),
    row: str = Query(...),
    user: Any = Depends(require_role("reviewer")),
) -> Any:
    """Render the override modal for a single results cell."""
    return templates.TemplateResponse(
        request,
        "partials/override_modal.html",
        {
            **_user_context(request, user),
            "run_id": str(run_id),
            "target_table": "results",
            "column": column,
            "row_key": row,
        },
    )


@router.post("/results/{run_id}/overrides")
def override_create(
    request: Request,
    run_id: uuid.UUID,
    user: Any = Depends(require_role("reviewer")),
    target_table: str = Form(...),
    column: str = Form(...),
    row_key: str = Form(...),
    value: str = Form(...),
    reason: str = Form(...),
) -> Any:
    """Create a manual override for a results cell (keyed by mapping spec)."""
    from workflow import create_result_override_record

    if not reason.strip():
        return templates.TemplateResponse(
            request,
            "partials/error.html",
            {
                **_user_context(request, user),
                "message": "A reason is required for every override.",
            },
        )
    try:
        create_result_override_record(
            run_id,
            target_table=target_table,
            target_column=column,
            row_key=row_key,
            value=value,
            reason=reason.strip(),
            created_by=getattr(user, "email", None),
        )
    except Exception as exc:
        return templates.TemplateResponse(
            request,
            "partials/error.html",
            {
                **_user_context(request, user),
                "message": f"Override failed: {exc}",
            },
        )
    return _htmx_redirect(request, f"/results/{run_id}/csv")


@router.delete("/results/{run_id}/overrides/{override_id}")
def override_delete(
    request: Request,
    run_id: uuid.UUID,
    override_id: uuid.UUID,
    user: Any = Depends(require_role("reviewer")),
) -> Any:
    """Delete a manual override."""
    from workflow import delete_result_override_record

    try:
        delete_result_override_record(run_id, override_id)
    except Exception as exc:
        return templates.TemplateResponse(
            request,
            "partials/error.html",
            {
                **_user_context(request, user),
                "message": f"Delete failed: {exc}",
            },
        )
    return _htmx_redirect(request, f"/results/{run_id}/csv")


@router.post("/results/{run_id}/confirm")
def results_confirm(
    request: Request,
    run_id: uuid.UUID,
    user: Any = Depends(require_auth),
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
) -> Any:
    """Reject the results and return to the mapping review page."""
    try:
        spec_id = reject_result(run_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return _htmx_redirect(request, f"/mapping/{spec_id}")


# ---------------------------------------------------------------------------
# Feedback chat (mapping review and results pages)
# ---------------------------------------------------------------------------


@router.post("/mapping/{spec_id}/chat")
def mapping_chat(
    request: Request,
    spec_id: uuid.UUID,
    user: Any = Depends(require_auth),
    feedback: str = Form(...),
) -> Any:
    """Send feedback on the mapping review page and get proposed changes."""
    from workflow import refine_mapping

    try:
        proposals = refine_mapping(spec_id, feedback)
    except ValueError as exc:
        return templates.TemplateResponse(
            request,
            "partials/error.html",
            {**_user_context(request, user), "message": str(exc)},
        )

    return templates.TemplateResponse(
        request,
        "partials/chat_diff.html",
        {
            **_user_context(request, user),
            "proposals": proposals,
            "feedback": feedback,
            "spec_id": str(spec_id),
            "sidebar_id": "mapping",
            "page": "mapping",
        },
    )


@router.post("/mapping/{spec_id}/chat/apply")
def mapping_chat_apply(
    request: Request,
    spec_id: uuid.UUID,
    user: Any = Depends(require_auth),
    changes_json: str = Form(...),
) -> Any:
    """Apply approved changes from mapping chat feedback."""
    import json as _json

    from workflow import apply_refinements

    try:
        changes = _json.loads(changes_json)
        apply_refinements(spec_id, changes, user)
    except (ValueError, _json.JSONDecodeError) as exc:
        return templates.TemplateResponse(
            request,
            "partials/error.html",
            {**_user_context(request, user), "message": str(exc)},
        )

    return _htmx_redirect(request, f"/mapping/{spec_id}")


@router.post("/results/{run_id}/chat")
def results_chat(
    request: Request,
    run_id: uuid.UUID,
    user: Any = Depends(require_auth),
    feedback: str = Form(...),
) -> Any:
    """Send feedback on the results page and get proposed changes."""
    from workflow import refine_from_results

    try:
        proposals = refine_from_results(run_id, feedback)
    except ValueError as exc:
        return templates.TemplateResponse(
            request,
            "partials/error.html",
            {**_user_context(request, user), "message": str(exc)},
        )

    return templates.TemplateResponse(
        request,
        "partials/chat_diff.html",
        {
            **_user_context(request, user),
            "proposals": proposals,
            "feedback": feedback,
            "run_id": str(run_id),
            "sidebar_id": "results",
            "page": "results",
        },
    )


@router.post("/results/{run_id}/chat/apply")
def results_chat_apply(
    request: Request,
    run_id: uuid.UUID,
    user: Any = Depends(require_auth),
    changes_json: str = Form(...),
) -> Any:
    """Apply approved changes from results chat feedback and re-execute."""
    import json as _json

    from workflow import apply_refinements_and_reexecute

    try:
        changes = _json.loads(changes_json)
        apply_refinements_and_reexecute(run_id, changes, user)
    except (ValueError, _json.JSONDecodeError) as exc:
        return templates.TemplateResponse(
            request,
            "partials/error.html",
            {**_user_context(request, user), "message": str(exc)},
        )

    return _htmx_redirect(request, f"/results/{run_id}")
