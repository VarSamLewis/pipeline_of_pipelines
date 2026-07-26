"""JSON API routes for the auditable data-transformation platform.

This module exposes HTTP endpoints for:
- Client and ingestion-batch management
- Raw file upload and registration
- File parsing, profiling, and evidence extraction
- Vector evidence search
- LLM-assisted mapping proposal and human approval
- Artifact generation and Polars pipeline execution
- Audit log and lineage queries

Every significant step is exposed as an endpoint so a human reviewer can
inspect and approve before moving to the next stage:
    1. Is it mapped correctly?
       -> /mapping-specs/{id}/propose + /approve
    2. Does the code make sense?
       -> /mapping-specs/{id}/generate + /output-folder/.../pipeline.py
    3. Do the results match?
       -> /mapping-specs/{id}/execute + /output-folder/.../results.csv
"""

# ruff: noqa: E402
# Load environment variables before any module-level env reads.
from __future__ import annotations

from dotenv import load_dotenv

load_dotenv()

import json
import secrets
import uuid
from pathlib import Path
from typing import Any, cast

from auth_service import (
    AUTH_BYPASS_LOCAL,
    clear_session,
    create_session,
    get_authkit_url,
    require_auth,
    require_role,
    update_user_role,
)
from db_ops import (
    approve_business_rule,
    create_business_rule,
    create_mapping_spec,
    create_raw_file,
    get_mapping_spec,
    get_raw_file_by_id,
    get_session,
    get_spreadsheet_profile,
    ingest_client_folder,
    list_raw_files_by_batch,
    search_evidence_by_text,
    write_audit_log,
)
from dependencies import get_artifact_store, get_object_store
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
from fastapi.responses import PlainTextResponse, RedirectResponse, Response
from file_ops import (
    build_storage_key,
    compute_sha256,
    detect_file_type,
)
from models import PipelineOutputFolder, TargetSchema
from repositories.clients import (
    create_client,
    create_ingestion_batch,
    get_client_by_code,
    get_ingestion_batch,
)
from workflow import (
    approve_mapping,
    approve_result,
    create_output_folder,
    execute_approved_mapping,
    generate_artifacts,
    get_mapping_review,
    get_result_review,
    propose_mapping,
    reject_mapping,
    reject_result,
)

app = APIRouter()

@app.get("/login")
def login_page(request: Request) -> RedirectResponse:
    """Redirect to the upload page or WorkOS AuthKit login."""
    if AUTH_BYPASS_LOCAL:
        return RedirectResponse(url="/upload")
    return RedirectResponse(url="/auth/login")


# ---------------------------------------------------------------------------
# Root redirect
# ---------------------------------------------------------------------------


@app.get("/")
def root(request: Request) -> RedirectResponse:
    """Redirect the root URL to the upload page."""
    return RedirectResponse(url="/upload")


# ---------------------------------------------------------------------------
# WorkOS AuthKit routes
# ---------------------------------------------------------------------------


@app.get("/auth/login")
def login(request: Request) -> RedirectResponse:
    """Redirect the browser to WorkOS AuthKit for authentication."""
    if AUTH_BYPASS_LOCAL:
        return RedirectResponse(url="/upload")
    state = secrets.token_urlsafe(32)
    request.session["auth_state"] = state
    url = get_authkit_url(state)
    return RedirectResponse(url=url)


@app.get("/auth/callback")
def auth_callback(
    request: Request,
    code: str,
    state: str | None = None,
) -> RedirectResponse:
    """Handle the WorkOS AuthKit callback and establish a session."""
    if AUTH_BYPASS_LOCAL:
        return RedirectResponse(url="/upload")

    stored_state = request.session.pop("auth_state", None)
    if state is None or stored_state is None or state != stored_state:
        raise HTTPException(status_code=400, detail="Invalid OAuth state")

    try:
        user, _created = __import__("auth_service").authenticate_with_workos(code)
    except Exception as exc:
        msg = f"Authentication failed: {exc}"
        raise HTTPException(status_code=401, detail=msg) from exc

    response = RedirectResponse(url="/upload")
    create_session(request, user.id)
    return response


@app.post("/auth/logout")
def logout(request: Request) -> RedirectResponse:
    """Clear the session and return to the login page."""
    clear_session(request)
    response = RedirectResponse(url="/login")
    return response


@app.get("/auth/me")
def me(user: Any = Depends(require_auth)) -> dict[str, Any]:
    """Return the currently authenticated user."""
    return {
        "id": str(user.id),
        "email": user.email,
        "name": user.name,
        "role": user.role.value,
    }


@app.get("/admin/users")
def list_users(
    user: Any = Depends(require_role("admin")),
    limit: int = Query(100, ge=1, le=500),
) -> list[dict[str, Any]]:
    """List provisioned platform users (admin only)."""
    from models import User
    from sqlmodel import select

    with get_session() as session:
        statement = select(User).order_by(User.email).limit(limit)
        users = session.exec(statement).all()
        return [
            {
                "id": str(u.id),
                "email": u.email,
                "name": u.name,
                "role": u.role.value,
                "last_login_at": (
                    u.last_login_at.isoformat() if u.last_login_at else None
                ),
            }
            for u in users
        ]


@app.post("/admin/users/{user_id}/role")
def set_user_role(
    user_id: uuid.UUID,
    payload: dict[str, Any],
    user: Any = Depends(require_role("admin")),
) -> dict[str, Any]:
    """Update a user's role in WorkOS and locally (admin only)."""
    updated = update_user_role(user_id, payload["role"])
    return {
        "id": str(updated.id),
        "email": updated.email,
        "name": updated.name,
        "role": updated.role.value,
    }


# ---------------------------------------------------------------------------
# Client and ingestion batch endpoints
# ---------------------------------------------------------------------------


@app.post("/clients")
def create_client_endpoint(
    payload: dict[str, Any],
    user: Any = Depends(require_role("creator")),
) -> dict[str, Any]:
    """Register a new client/tenant."""
    with get_session() as session:
        client = create_client(
            session,
            name=payload["name"],
            code=payload["code"],
            metadata=payload.get("metadata", {}),
        )
        write_audit_log(
            session,
            "client_created",
            "Client",
            client.id,
            user.id,
            user.email,
            payload,
        )
        return {
            "id": str(client.id),
            "name": client.name,
            "code": client.code,
            "created_at": client.created_at.isoformat(),
        }


@app.get("/clients")
def list_clients_endpoint(
    user: Any = Depends(require_auth),
    limit: int = Query(100, ge=1, le=500),
) -> list[dict[str, Any]]:
    """List all clients."""
    from models import Client
    from sqlmodel import select

    with get_session() as session:
        statement = select(Client).order_by(
            cast(Any, Client.created_at).desc()
        ).limit(limit)
        clients = session.exec(statement).all()
        return [
            {
                "id": str(c.id),
                "name": c.name,
                "code": c.code,
                "created_at": c.created_at.isoformat(),
            }
            for c in clients
        ]


@app.get("/clients/{client_code}")
def get_client(
    client_code: str,
    user: Any = Depends(require_auth),
) -> dict[str, Any]:
    """Fetch a client by its short code."""
    with get_session() as session:
        client = get_client_by_code(session, client_code)
        if client is None:
            raise HTTPException(status_code=404, detail="Client not found")
        return {
            "id": str(client.id),
            "name": client.name,
            "code": client.code,
            "metadata": client.meta,
            "created_at": client.created_at.isoformat(),
        }


@app.post("/clients/{client_code}/batches")
def create_ingestion_batch_endpoint(
    client_code: str,
    payload: dict[str, Any],
    user: Any = Depends(require_role("creator")),
) -> dict[str, Any]:
    """Create a new ingestion batch for a client."""
    with get_session() as session:
        client = get_client_by_code(session, client_code)
        if client is None:
            raise HTTPException(status_code=404, detail="Client not found")
        batch = create_ingestion_batch(
            session,
            client_id=client.id,
            label=payload.get("label"),
            metadata=payload.get("metadata", {}),
        )
        session.commit()
        return {
            "id": str(batch.id),
            "client_id": str(batch.client_id),
            "label": batch.label,
            "created_at": batch.created_at.isoformat(),
        }


@app.get("/clients/{client_code}/batches")
def list_ingestion_batches_endpoint(
    client_code: str,
    user: Any = Depends(require_auth),
    limit: int = Query(100, ge=1, le=500),
) -> list[dict[str, Any]]:
    """List ingestion batches for a client."""
    from models import IngestionBatch
    from sqlmodel import select

    with get_session() as session:
        client = get_client_by_code(session, client_code)
        if client is None:
            raise HTTPException(status_code=404, detail="Client not found")
        statement = (
            select(IngestionBatch)
            .where(IngestionBatch.client_id == client.id)
            .order_by(cast(Any, IngestionBatch.created_at).desc())
            .limit(limit)
        )
        batches = session.exec(statement).all()
        return [
            {
                "id": str(b.id),
                "label": b.label,
                "created_at": b.created_at.isoformat(),
            }
            for b in batches
        ]


@app.get("/clients/{client_code}/batches/{batch_id}")
def get_ingestion_batch_endpoint(
    client_code: str,
    batch_id: uuid.UUID,
    user: Any = Depends(require_auth),
) -> dict[str, Any]:
    """Fetch an ingestion batch and its files."""
    with get_session() as session:
        client = get_client_by_code(session, client_code)
        if client is None:
            raise HTTPException(status_code=404, detail="Client not found")
        batch = get_ingestion_batch(session, batch_id)
        if batch is None or batch.client_id != client.id:
            raise HTTPException(status_code=404, detail="Batch not found")
        raw_files = list_raw_files_by_batch(session, batch.id)
        return {
            "id": str(batch.id),
            "client_id": str(batch.client_id),
            "label": batch.label,
            "files": [
                {
                    "id": str(f.id),
                    "original_filename": f.original_filename,
                    "mime_type": f.mime_type,
                    "status": f.status.value,
                    "sha256": f.sha256,
                }
                for f in raw_files
            ],
        }


# ---------------------------------------------------------------------------
# Raw file endpoints
# ---------------------------------------------------------------------------


@app.post("/clients/{client_code}/batches/{batch_id}/files")
def upload_raw_file(
    client_code: str,
    batch_id: uuid.UUID,
    file: UploadFile = File(...),
    metadata: str = Form("{}"),
    user: Any = Depends(require_role("creator")),
) -> dict[str, Any]:
    """Upload a raw file into immutable object storage and register metadata."""
    with get_session() as session:
        client = get_client_by_code(session, client_code)
        if client is None:
            raise HTTPException(status_code=404, detail="Client not found")
        batch = get_ingestion_batch(session, batch_id)
        if batch is None or batch.client_id != client.id:
            raise HTTPException(status_code=404, detail="Batch not found")

        contents = file.file.read()
        sha256 = compute_sha256(contents)
        mime_type = file.content_type or "application/octet-stream"
        file_type = detect_file_type(file.filename or "unknown", contents)
        if file_type == "csv":
            mime_type = "text/csv"
        elif file_type == "xlsx":
            mime_type = (
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            )

        storage_key = build_storage_key(
            client.code, str(batch.id), file.filename or "unknown", sha256
        )
        object_store = get_object_store()
        object_store.put(storage_key, contents)

        raw_file = create_raw_file(
            session=session,
            client_id=client.id,
            ingestion_batch_id=batch.id,
            original_filename=file.filename or "unknown",
            storage_key=storage_key,
            sha256=sha256,
            size_bytes=len(contents),
            mime_type=mime_type,
            metadata=json.loads(metadata),
        )
        return {
            "id": str(raw_file.id),
            "original_filename": raw_file.original_filename,
            "storage_key": raw_file.storage_key,
            "sha256": raw_file.sha256,
            "status": raw_file.status.value,
        }


@app.get("/raw-files/{raw_file_id}")
def get_raw_file(
    raw_file_id: uuid.UUID,
    user: Any = Depends(require_auth),
) -> dict[str, Any]:
    """Fetch a raw file registration record."""
    with get_session() as session:
        raw_file = get_raw_file_by_id(session, raw_file_id)
        if raw_file is None:
            raise HTTPException(status_code=404, detail="Raw file not found")
        return {
            "id": str(raw_file.id),
            "original_filename": raw_file.original_filename,
            "mime_type": raw_file.mime_type,
            "status": raw_file.status.value,
            "sha256": raw_file.sha256,
            "size_bytes": raw_file.size_bytes,
            "metadata": raw_file.meta,
        }


@app.post("/raw-files/{raw_file_id}/parse")
def parse_raw_file(
    raw_file_id: uuid.UUID,
    user: Any = Depends(require_role("creator")),
) -> dict[str, Any]:
    """Parse a raw file into structured facts, profiles, and evidence."""
    from db_ops import _parse_raw_file, update_raw_file_status
    from models import FileStatus

    with get_session() as session:
        raw_file = get_raw_file_by_id(session, raw_file_id)
        if raw_file is None:
            raise HTTPException(status_code=404, detail="Raw file not found")

        object_store = get_object_store()
        file_bytes = object_store.get(raw_file.storage_key)
        file_type = detect_file_type(raw_file.original_filename, file_bytes)

        try:
            _parse_raw_file(session, raw_file, file_bytes, file_type)
            update_raw_file_status(session, raw_file.id, FileStatus.PARSED)
        except Exception as exc:
            update_raw_file_status(session, raw_file.id, FileStatus.FAILED)
            raw_file.meta = {"error": str(exc)}
            session.add(raw_file)
            session.commit()
            raise HTTPException(status_code=500, detail=str(exc)) from exc

        profile = get_spreadsheet_profile(session, raw_file.id)
        return {
            "raw_file_id": str(raw_file.id),
            "status": raw_file.status.value,
            "profile": profile.profile_json if profile else None,
        }


@app.post("/clients/{client_code}/ingest-folder")
def ingest_client_folder_endpoint(
    client_code: str,
    folder_path: str = Form(
        ..., description="Absolute or relative path to the client folder."
    ),
    label: str | None = Form(None, description="Optional batch label."),
    user: Any = Depends(require_role("creator")),
) -> dict[str, Any]:
    """Ingest an entire client folder of heterogeneous files as one batch."""
    with get_session() as session:
        client = get_client_by_code(session, client_code)
        if client is None:
            raise HTTPException(status_code=404, detail="Client not found")

        object_store = get_object_store()
        result = ingest_client_folder(
            session,
            client_id=client.id,
            folder_path=folder_path,
            object_store=object_store,
            label=label,
        )
        return {
            "client_id": str(result.client_id),
            "ingestion_batch_id": str(result.ingestion_batch_id),
            "raw_file_ids": [str(rid) for rid in result.raw_file_ids],
            "parsed_count": result.parsed_count,
            "failed_count": result.failed_count,
        }


@app.post("/clients/{client_code}/target-schema")
def upload_target_schema(
    client_code: str,
    schema_file: UploadFile = File(
        ..., description="JSON file describing the target schema."
    ),
    user: Any = Depends(require_role("creator")),
) -> dict[str, Any]:
    """Upload and persist a target-schema JSON file for a client."""
    with get_session() as session:
        client = get_client_by_code(session, client_code)
        if client is None:
            raise HTTPException(status_code=404, detail="Client not found")

        content = schema_file.file.read()
        schema = TargetSchema.model_validate_json(content.decode("utf-8"))
        schema.client_code = client_code

        get_artifact_store().write_target_schema(client_code, schema)

        return {
            "client_code": client_code,
            "schema_path": (
                f"target-schemas/{client_code}/target_schema.json"
            ),
            "schema": schema.model_dump(mode="json"),
        }


@app.get("/clients/{client_code}/target-schema")
def get_target_schema(
    client_code: str,
    user: Any = Depends(require_auth),
) -> TargetSchema:
    """Return the latest target schema for a client."""
    with get_session() as session:
        client = get_client_by_code(session, client_code)
        if client is None:
            raise HTTPException(status_code=404, detail="Client not found")

    try:
        return get_artifact_store().read_target_schema(client_code)
    except FileNotFoundError:
        raise HTTPException(
            status_code=404,
            detail="Target schema not found",
        ) from None


# ---------------------------------------------------------------------------
# Evidence and profiling endpoints
# ---------------------------------------------------------------------------


@app.get("/raw-files/{raw_file_id}/profile")
def get_spreadsheet_profile_endpoint(
    raw_file_id: uuid.UUID,
    user: Any = Depends(require_auth),
) -> dict[str, Any]:
    """Return the stored spreadsheet profile for a raw file."""
    with get_session() as session:
        raw_file = get_raw_file_by_id(session, raw_file_id)
        if raw_file is None:
            raise HTTPException(status_code=404, detail="Raw file not found")
        profile = get_spreadsheet_profile(session, raw_file_id)
        if profile is None:
            raise HTTPException(status_code=404, detail="Profile not found")
        return {
            "raw_file_id": str(raw_file_id),
            "profile": profile.profile_json,
        }


@app.get("/evidence/search")
def search_evidence_endpoint(
    query: str = Query(..., description="Plain-text query string."),
    client_code: str | None = Query(None, description="Optional client filter."),
    top_k: int = Query(5, ge=1, le=100),
    user: Any = Depends(require_auth),
) -> list[dict[str, Any]]:
    """Search extracted evidence by full text."""
    with get_session() as session:
        client_id = None
        if client_code:
            client = get_client_by_code(session, client_code)
            if client is None:
                raise HTTPException(status_code=404, detail="Client not found")
            client_id = client.id
        items = search_evidence_by_text(session, query, client_id, top_k=top_k)
        return [
            {
                "id": str(item.id),
                "raw_file_id": str(item.raw_file_id),
                "evidence_type": item.evidence_type,
                "content": item.content[:500],
                "page_ref": item.page_ref,
                "chunk_index": item.chunk_index,
            }
            for item in items
        ]


# ---------------------------------------------------------------------------
# Business rule endpoints
# ---------------------------------------------------------------------------


@app.post("/clients/{client_code}/rules")
def create_business_rule_endpoint(
    client_code: str,
    payload: dict[str, Any],
    user: Any = Depends(require_role("creator")),
) -> dict[str, Any]:
    """Create a new business rule draft."""
    with get_session() as session:
        client = get_client_by_code(session, client_code)
        if client is None:
            raise HTTPException(status_code=404, detail="Client not found")
        rule = create_business_rule(
            session,
            client_id=client.id,
            rule_text=payload["rule_text"],
            evidence_ids=[uuid.UUID(x) for x in payload.get("evidence_ids", [])],
            metadata=payload.get("metadata", {}),
        )
        return {
            "id": str(rule.id),
            "rule_text": rule.rule_text,
            "status": rule.status.value,
            "created_at": rule.created_at.isoformat(),
        }


@app.post("/rules/{rule_id}/approve")
def approve_business_rule_endpoint(
    rule_id: uuid.UUID,
    payload: dict[str, Any],
    user: Any = Depends(require_role("reviewer")),
) -> dict[str, Any]:
    """Approve a business rule."""
    with get_session() as session:
        rule = approve_business_rule(session, rule_id, payload["reviewer"])
        if rule is None:
            raise HTTPException(status_code=404, detail="Rule not found")
        return {
            "id": str(rule.id),
            "status": rule.status.value,
            "approved_by": rule.approved_by,
            "approved_at": rule.approved_at.isoformat() if rule.approved_at else None,
        }


@app.get("/clients/{client_code}/rules")
def list_business_rules_endpoint(
    client_code: str,
    user: Any = Depends(require_auth),
    limit: int = Query(100, ge=1, le=500),
) -> list[dict[str, Any]]:
    """List business rules for a client."""
    from db_ops import list_business_rules

    with get_session() as session:
        client = get_client_by_code(session, client_code)
        if client is None:
            raise HTTPException(status_code=404, detail="Client not found")
        rules = list_business_rules(session, client.id)
        return [
            {
                "id": str(r.id),
                "rule_text": r.rule_text,
                "status": r.status.value,
                "approved_by": r.approved_by,
                "created_at": r.created_at.isoformat(),
            }
            for r in rules[:limit]
        ]


# ---------------------------------------------------------------------------
# Mapping specification endpoints
# ---------------------------------------------------------------------------


@app.post("/clients/{client_code}/mapping-specs")
def create_mapping_spec_endpoint(
    client_code: str,
    payload: dict[str, Any],
    user: Any = Depends(require_role("creator")),
) -> dict[str, Any]:
    """Create a new mapping specification draft against a supplied target schema."""
    with get_session() as session:
        client = get_client_by_code(session, client_code)
        if client is None:
            raise HTTPException(status_code=404, detail="Client not found")

        target_schema = TargetSchema.model_validate(payload["target_schema"])
        target_schema.client_code = client_code

        spec = create_mapping_spec(
            session,
            client_id=client.id,
            source_raw_file_ids=[
                uuid.UUID(x) for x in payload.get("source_raw_file_ids", [])
            ],
            target_schema=target_schema,
            description=payload.get("description"),
        )
        return {
            "id": str(spec.id),
            "client_id": str(spec.client_id),
            "status": spec.status.value,
            "target_schema": spec.target_schema_json,
            "description": spec.description,
        }


@app.get("/mapping-specs")
def list_mapping_specs_endpoint(
    user: Any = Depends(require_auth),
    client_code: str | None = Query(None),
    status: str | None = Query(None),
    limit: int = Query(100, ge=1, le=500),
) -> list[dict[str, Any]]:
    """List mapping specs, optionally filtered by client or status."""
    from models import MappingSpec
    from sqlmodel import select

    with get_session() as session:
        statement = select(MappingSpec).order_by(
            cast(Any, MappingSpec.created_at).desc()
        )
        if client_code:
            client = get_client_by_code(session, client_code)
            if client is None:
                raise HTTPException(status_code=404, detail="Client not found")
            statement = statement.where(MappingSpec.client_id == client.id)
        if status:
            statement = statement.where(MappingSpec.status == status)
        specs = session.exec(statement.limit(limit)).all()
        return [
            {
                "id": str(s.id),
                "client_id": str(s.client_id),
                "status": s.status.value,
                "description": s.description,
                "created_at": s.created_at.isoformat(),
            }
            for s in specs
        ]


@app.get("/mapping-specs/{spec_id}")
def get_mapping_spec_endpoint(
    spec_id: uuid.UUID,
    user: Any = Depends(require_auth),
) -> dict[str, Any]:
    """Fetch a mapping specification and its columns."""
    try:
        review = get_mapping_review(spec_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {
        **review,
        "target_schema": review["target_schema_json"],
    }


@app.post("/mapping-specs/{spec_id}/propose")
def propose_mapping_spec_endpoint(
    spec_id: uuid.UUID,
    payload: dict[str, Any] | None = None,
    user: Any = Depends(require_role("reviewer")),
) -> dict[str, Any]:
    """Ask the LLM to propose mappings for a draft specification."""
    payload = payload or {}
    try:
        mapping = propose_mapping(
            spec_id,
            model=payload.get("model"),
            api_key=payload.get("api_key"),
            base_url=payload.get("base_url"),
            top_k_evidence=payload.get("top_k_evidence", 10),
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return mapping


@app.post("/mapping-specs/{spec_id}/approve")
def approve_mapping_spec_endpoint(
    spec_id: uuid.UUID,
    payload: dict[str, Any],
    user: Any = Depends(require_role("approver")),
) -> dict[str, Any]:
    """Approve a proposed mapping specification."""
    try:
        spec = approve_mapping(
            spec_id,
            reviewer=payload["reviewer"],
            notes=payload.get("notes"),
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {
        "id": str(spec.id),
        "status": spec.status.value,
        "approved_by": spec.approved_by,
        "approved_at": spec.approved_at.isoformat() if spec.approved_at else None,
    }


@app.post("/mapping-specs/{spec_id}/reject")
def reject_mapping_spec_endpoint(
    spec_id: uuid.UUID,
    payload: dict[str, Any],
    user: Any = Depends(require_role("reviewer")),
) -> dict[str, Any]:
    """Reject a mapping specification."""
    try:
        spec = reject_mapping(
            spec_id,
            reason=payload.get("reason", ""),
            reviewer=user.email,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"id": str(spec.id), "status": spec.status.value}


@app.patch("/mapping-specs/{spec_id}/columns/{column_id}")
def update_mapping_column_endpoint(
    spec_id: uuid.UUID,
    column_id: uuid.UUID,
    payload: dict[str, Any],
    user: Any = Depends(require_role("reviewer")),
) -> dict[str, Any]:
    """Update a single mapping column (reviewer+)."""
    from models import MappingColumn

    with get_session() as session:
        spec = get_mapping_spec(session, spec_id)
        if spec is None:
            raise HTTPException(status_code=404, detail="Mapping spec not found")
        column = session.get(MappingColumn, column_id)
        if column is None or column.mapping_spec_id != spec_id:
            raise HTTPException(status_code=404, detail="Mapping column not found")

        if "transformation_logic" in payload:
            column.transformation_logic = payload["transformation_logic"]
        if "polars_expression" in payload:
            column.polars_expression = payload["polars_expression"]
        if "source_columns" in payload:
            column.source_columns_json = payload["source_columns"]
        if "tests" in payload:
            column.tests = payload["tests"]
        session.add(column)
        session.commit()
        session.refresh(column)
        return {
            "id": str(column.id),
            "target_table": column.target_table,
            "target_column": column.target_column,
            "transformation_logic": column.transformation_logic,
            "polars_expression": column.polars_expression,
            "tests": column.tests,
        }


# ---------------------------------------------------------------------------
# Code generation and execution endpoints
# ---------------------------------------------------------------------------


@app.post("/mapping-specs/{spec_id}/generate")
def generate_artifacts_endpoint(
    spec_id: uuid.UUID,
    user: Any = Depends(require_role("approver")),
) -> dict[str, Any]:
    """Generate Polars artifacts from an approved mapping spec."""
    output_folder, artifacts = generate_artifacts(spec_id)
    return {
        "spec_id": str(spec_id),
        "output_folder": str(output_folder),
        "artifacts": [
            {
                "id": str(a.id),
                "type": a.artifact_type,
                "file_path": a.file_path,
            }
            for a in artifacts
        ],
    }


@app.post("/mapping-specs/{spec_id}/output-folder")
def generate_output_folder_endpoint(
    spec_id: uuid.UUID,
    payload: dict[str, Any] | None = None,
    user: Any = Depends(require_role("approver")),
) -> PipelineOutputFolder:
    """Generate the complete client deliverable folder."""
    payload = payload or {}
    output_folder_value = payload.get("output_folder")
    output_folder = (
        Path(output_folder_value) if output_folder_value is not None else None
    )
    return create_output_folder(spec_id, output_folder)


@app.get("/output-folders/{folder_id}/pipeline.py")
def get_generated_pipeline_py(
    folder_id: uuid.UUID,
    user: Any = Depends(require_auth),
) -> PlainTextResponse:
    """Return the generated single-file Polars pipeline for human review."""
    try:
        content = get_artifact_store().read_artifact(folder_id, "pipeline.py")
    except FileNotFoundError:
        raise HTTPException(
            status_code=404,
            detail="pipeline.py not found",
        ) from None
    return PlainTextResponse(content.decode("utf-8"), media_type="text/x-python")


@app.put("/output-folders/{folder_id}/pipeline.py")
def update_generated_pipeline_py(
    folder_id: uuid.UUID,
    payload: dict[str, Any],
    user: Any = Depends(require_role("reviewer")),
) -> dict[str, Any]:
    """Overwrite the generated pipeline.py for advanced code review.

    Warning: editing the generated pipeline directly bypasses the mapping
    contract for the edited file. The mapping.json and audit log remain
    unchanged; the edited script is used on the next execution.
    """
    store = get_artifact_store()
    try:
        store.read_artifact(folder_id, "pipeline.py")
    except FileNotFoundError:
        raise HTTPException(
            status_code=404,
            detail="pipeline.py not found",
        ) from None
    content = payload.get("content", "")
    store.write_artifact(folder_id, "pipeline.py", content.encode("utf-8"))
    with get_session() as session:
        write_audit_log(
            session,
            "pipeline_py_edited",
            "GeneratedPipeline",
            folder_id,
            user.id,
            user.email,
            {"folder_id": str(folder_id)},
        )
    return {"folder_id": str(folder_id), "bytes": len(content)}


@app.get("/output-folders/{folder_id}/mapping.json")
def get_generated_mapping_json(
    folder_id: uuid.UUID,
    user: Any = Depends(require_auth),
) -> dict[str, Any]:
    """Return the generated mapping.json for human review."""
    try:
        content = get_artifact_store().read_artifact(folder_id, "mapping.json")
    except FileNotFoundError:
        raise HTTPException(
            status_code=404,
            detail="mapping.json not found",
        ) from None
    return cast(dict[str, Any], json.loads(content))


@app.get("/output-folders/{folder_id}/results.csv")
def get_generated_results_csv(
    folder_id: uuid.UUID,
    user: Any = Depends(require_auth),
) -> Response:
    """Return the generated results.csv for human review."""
    try:
        content = get_artifact_store().read_artifact(folder_id, "results.csv")
    except FileNotFoundError:
        raise HTTPException(
            status_code=404,
            detail="results.csv not found",
        ) from None
    return Response(
        content=content,
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="results.csv"'},
    )


@app.post("/mapping-specs/{spec_id}/execute")
def execute_pipeline(
    spec_id: uuid.UUID,
    payload: dict[str, Any] | None = None,
    user: Any = Depends(require_role("approver")),
) -> dict[str, Any]:
    """Execute the Polars transformation pipeline for an approved spec."""
    payload = payload or {}
    output_folder_value = payload.get("output_folder")
    output_folder = (
        Path(output_folder_value) if output_folder_value is not None else None
    )
    try:
        return execute_approved_mapping(
            spec_id,
            target_environment=payload.get("target_environment", "local"),
            output_folder=output_folder,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/execution-runs/{run_id}")
def get_execution_run(
    run_id: uuid.UUID,
    user: Any = Depends(require_auth),
) -> dict[str, Any]:
    """Fetch an execution run with validation results."""
    try:
        review = get_result_review(run_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    run = review["run"]
    return {
        "id": str(run.id),
        "mapping_spec_id": str(run.mapping_spec_id),
        "status": run.status.value,
        "started_at": run.started_at.isoformat() if run.started_at else None,
        "finished_at": run.finished_at.isoformat() if run.finished_at else None,
        "validation_results": [
            {
                "id": str(result.id),
                "test_name": result.test_name,
                "severity": result.severity,
                "passed": result.passed,
                "details": result.details,
            }
            for result in review["validation_results"]
        ],
    }


@app.get("/execution-runs")
def list_execution_runs_endpoint(
    user: Any = Depends(require_auth),
    spec_id: uuid.UUID | None = Query(None),
    status: str | None = Query(None),
    limit: int = Query(100, ge=1, le=500),
) -> list[dict[str, Any]]:
    """List execution runs, optionally filtered by spec or status."""
    from models import ExecutionRun
    from sqlmodel import select

    with get_session() as session:
        statement = select(ExecutionRun).order_by(
            cast(Any, ExecutionRun.started_at).desc()
        )
        if spec_id:
            statement = statement.where(ExecutionRun.mapping_spec_id == spec_id)
        if status:
            statement = statement.where(ExecutionRun.status == status)
        runs = session.exec(statement.limit(limit)).all()
        return [
            {
                "id": str(r.id),
                "mapping_spec_id": str(r.mapping_spec_id),
                "status": r.status.value,
                "target_environment": r.target_environment,
                "started_at": r.started_at.isoformat() if r.started_at else None,
                "finished_at": r.finished_at.isoformat() if r.finished_at else None,
            }
            for r in runs
        ]


@app.post("/execution-runs/{run_id}/approve")
def approve_execution_run_endpoint(
    run_id: uuid.UUID,
    user: Any = Depends(require_role("approver")),
) -> dict[str, Any]:
    """Publish an execution run (approver only)."""
    try:
        run = approve_result(run_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {
        "id": str(run.id),
        "status": run.status.value,
        "approved_by": user.email,
    }


@app.post("/execution-runs/{run_id}/reject")
def reject_execution_run_endpoint(
    run_id: uuid.UUID,
    payload: dict[str, Any],
    user: Any = Depends(require_role("reviewer")),
) -> dict[str, Any]:
    """Reject an execution run and return it for correction."""
    try:
        reject_result(run_id, payload.get("reason", ""))
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"id": str(run_id), "status": "failed"}


# ---------------------------------------------------------------------------
# Audit and lineage endpoints
# ---------------------------------------------------------------------------


@app.get("/audit-log")
def query_audit_log(
    entity_type: str | None = Query(None),
    entity_id: uuid.UUID | None = Query(None),
    event_type: str | None = Query(None),
    limit: int = Query(50, ge=1, le=500),
    user: Any = Depends(require_auth),
) -> list[dict[str, Any]]:
    """Query the append-only audit log."""
    from models import AuditLog as ALModel

    with get_session() as session:
        statement = __import__("sqlmodel").select(ALModel)
        if entity_type:
            statement = statement.where(ALModel.entity_type == entity_type)
        if entity_id:
            statement = statement.where(ALModel.entity_id == entity_id)
        if event_type:
            statement = statement.where(ALModel.event_type == event_type)
        statement = statement.order_by(
            cast(Any, ALModel.recorded_at).desc()
        ).limit(limit)
        logs = session.exec(statement).all()
        return [
            {
                "id": str(log.id),
                "event_type": log.event_type,
                "entity_type": log.entity_type,
                "entity_id": str(log.entity_id),
                "actor": log.actor,
                "payload": log.payload,
                "recorded_at": log.recorded_at.isoformat(),
            }
            for log in logs
        ]


@app.get("/lineage/staging-columns/{staging_column_id}")
def get_staging_column_lineage(
    staging_column_id: uuid.UUID,
    user: Any = Depends(require_auth),
) -> dict[str, Any]:
    """Return full provenance for a published staging column."""
    from db_ops import get_lineage_for_staging_column

    with get_session() as session:
        return get_lineage_for_staging_column(session, staging_column_id)
