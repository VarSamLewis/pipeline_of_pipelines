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
from datetime import UTC, datetime
from typing import Any

import polars as pl
from auth_service import require_auth
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
    return templates.TemplateResponse(
        request,
        "mapping.html",
        {
            **_user_context(request, user),
            "spec_id": str(spec_id),
            "columns_by_table": columns_by_table,
            "target_column_info": target_column_info,
            "mapping_json": json.dumps(mapping, indent=2, default=str),
            "source_tables": source_tables,
            "parse_warnings": parse_warnings,
            "generated_at": datetime.now(UTC).strftime("%d %b %Y at %H:%M UTC"),
        },
    )


@router.post("/mapping/{spec_id}")
def mapping_post(
    request: Request,
    spec_id: uuid.UUID,
    user: Any = Depends(require_auth),
    changes_json: str = Form(None),
) -> Any:
    """Handle POST to the mapping page (apply chat refinements or redirect)."""
    if changes_json:
        from workflow import apply_refinements

        try:
            changes = json.loads(changes_json)
            apply_refinements(spec_id, changes)
        except (ValueError, json.JSONDecodeError) as exc:
            return templates.TemplateResponse(
                request,
                "partials/error.html",
                {**_user_context(request, user), "message": str(exc)},
            )
        return _htmx_redirect(request, f"/mapping/{spec_id}")
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
        apply_refinements(spec_id, changes)
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
        apply_refinements_and_reexecute(run_id, changes)
    except (ValueError, _json.JSONDecodeError) as exc:
        return templates.TemplateResponse(
            request,
            "partials/error.html",
            {**_user_context(request, user), "message": str(exc)},
        )

    return _htmx_redirect(request, f"/results/{run_id}")
