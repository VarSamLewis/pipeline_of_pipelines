"""FastAPI application for the auditable data-transformation platform.

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
import uuid
from pathlib import Path
from typing import Any

from codegen import (
    generate_artifact_set,
    generate_output_folder,
    load_mapping_spec,
)
from db_ops import (
    approve_business_rule,
    approve_mapping_spec,
    create_business_rule,
    create_client,
    create_ingestion_batch,
    create_mapping_spec,
    create_raw_file,
    create_tables,
    get_client_by_code,
    get_engine,
    get_ingestion_batch,
    get_mapping_columns,
    get_mapping_spec,
    get_raw_file_by_id,
    get_session,
    get_spreadsheet_profile,
    ingest_client_folder,
    list_raw_files_by_batch,
    search_evidence_by_text,
    write_audit_log,
)
from fastapi import FastAPI, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, PlainTextResponse
from file_ops import (
    LocalObjectStore,
    build_storage_key,
    compute_sha256,
    detect_file_type,
    load_target_schema,
    save_target_schema,
)
from mapping import propose_mapping_spec as propose_mapping_spec_service
from models import PipelineOutputFolder, TargetSchema
from pipeline import (
    compute_quality_profile,
    load_target_schema_from_spec,
    persist_staging_tables,
    record_execution_run,
    record_staging_metadata,
    record_validation_results,
    run_pipeline,
    run_validation_tests,
)

app = FastAPI(
    title="Pipeline of Pipelines",
    description=(
        "Auditable data-transformation platform for heterogeneous client files."
    ),
    version="0.1.0",
)

# Global state (replaced by proper dependency injection in production)
_object_store: LocalObjectStore | None = None
_output_folders: dict[uuid.UUID, Path] = {}


def get_object_store() -> LocalObjectStore:
    """Return the singleton local object store."""
    global _object_store
    if _object_store is None:
        _object_store = LocalObjectStore("data/object-store")
    return _object_store


@app.on_event("startup")
def startup() -> None:
    """Create database tables on startup."""
    create_tables(get_engine())


# ---------------------------------------------------------------------------
# Client and ingestion batch endpoints
# ---------------------------------------------------------------------------


@app.post("/clients")
def create_client_endpoint(payload: dict[str, Any]) -> dict[str, Any]:
    """Register a new client/tenant."""
    with get_session() as session:
        client = create_client(
            session,
            name=payload["name"],
            code=payload["code"],
            metadata=payload.get("metadata", {}),
        )
        write_audit_log(
            session, "client_created", "Client", client.id, None, payload
        )
        return {
            "id": str(client.id),
            "name": client.name,
            "code": client.code,
            "created_at": client.created_at.isoformat(),
        }


@app.get("/clients/{client_code}")
def get_client(client_code: str) -> dict[str, Any]:
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
    client_code: str, payload: dict[str, Any]
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
        return {
            "id": str(batch.id),
            "client_id": str(batch.client_id),
            "label": batch.label,
            "created_at": batch.created_at.isoformat(),
        }


@app.get("/clients/{client_code}/batches/{batch_id}")
def get_ingestion_batch_endpoint(
    client_code: str, batch_id: uuid.UUID
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
def get_raw_file(raw_file_id: uuid.UUID) -> dict[str, Any]:
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
def parse_raw_file(raw_file_id: uuid.UUID) -> dict[str, Any]:
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
) -> dict[str, Any]:
    """Upload and persist a target-schema JSON file for a client."""
    with get_session() as session:
        client = get_client_by_code(session, client_code)
        if client is None:
            raise HTTPException(status_code=404, detail="Client not found")

        content = schema_file.file.read()
        schema = TargetSchema.model_validate_json(content.decode("utf-8"))
        schema.client_code = client_code

        schema_dir = Path("data/target-schemas") / client_code
        schema_dir.mkdir(parents=True, exist_ok=True)
        schema_path = schema_dir / "target_schema.json"
        save_target_schema(schema, schema_path)

        return {
            "client_code": client_code,
            "schema_path": str(schema_path),
            "schema": schema.model_dump(mode="json"),
        }


@app.get("/clients/{client_code}/target-schema")
def get_target_schema(client_code: str) -> TargetSchema:
    """Return the latest target schema for a client."""
    with get_session() as session:
        client = get_client_by_code(session, client_code)
        if client is None:
            raise HTTPException(status_code=404, detail="Client not found")

    schema_path = Path("data/target-schemas") / client_code / "target_schema.json"
    if not schema_path.exists():
        raise HTTPException(status_code=404, detail="Target schema not found")
    return load_target_schema(schema_path)


# ---------------------------------------------------------------------------
# Evidence and profiling endpoints
# ---------------------------------------------------------------------------


@app.get("/raw-files/{raw_file_id}/profile")
def get_spreadsheet_profile_endpoint(raw_file_id: uuid.UUID) -> dict[str, Any]:
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
    client_code: str, payload: dict[str, Any]
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
    rule_id: uuid.UUID, payload: dict[str, Any]
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


# ---------------------------------------------------------------------------
# Mapping specification endpoints
# ---------------------------------------------------------------------------


@app.post("/clients/{client_code}/mapping-specs")
def create_mapping_spec_endpoint(
    client_code: str, payload: dict[str, Any]
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


@app.get("/mapping-specs/{spec_id}")
def get_mapping_spec_endpoint(spec_id: uuid.UUID) -> dict[str, Any]:
    """Fetch a mapping specification and its columns."""
    with get_session() as session:
        spec = get_mapping_spec(session, spec_id)
        if spec is None:
            raise HTTPException(status_code=404, detail="Mapping spec not found")
        columns = get_mapping_columns(session, spec_id)
        return {
            "id": str(spec.id),
            "client_id": str(spec.client_id),
            "status": spec.status.value,
            "target_schema": spec.target_schema_json,
            "description": spec.description,
            "approved_by": spec.approved_by,
            "columns": [
                {
                    "id": str(c.id),
                    "target_table": c.target_table,
                    "target_column": c.target_column,
                    "source_columns": c.source_columns_json,
                    "transformation_logic": c.transformation_logic,
                    "polars_expression": c.polars_expression,
                    "tests": c.tests,
                    "confidence": c.confidence,
                }
                for c in columns
            ],
        }


@app.post("/mapping-specs/{spec_id}/propose")
def propose_mapping_spec_endpoint(
    spec_id: uuid.UUID,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Ask the LLM to propose mappings for a draft specification."""
    payload = payload or {}
    with get_session() as session:
        spec = get_mapping_spec(session, spec_id)
        if spec is None:
            raise HTTPException(status_code=404, detail="Mapping spec not found")

        target_schema = load_target_schema_from_spec(
            {"target_schema_json": spec.target_schema_json}
        )
        propose_mapping_spec_service(
            session,
            spec_id,
            target_schema=target_schema,
            model=payload.get("model", "gpt-4o-mini"),
            api_key=payload.get("api_key"),
            base_url=payload.get("base_url"),
            top_k_evidence=payload.get("top_k_evidence", 10),
        )

        columns = get_mapping_columns(session, spec_id)
        return {
            "id": str(spec_id),
            "status": "proposed",
            "columns": [
                {
                    "id": str(c.id),
                    "target_table": c.target_table,
                    "target_column": c.target_column,
                    "source_columns": c.source_columns_json,
                    "transformation_logic": c.transformation_logic,
                    "polars_expression": c.polars_expression,
                    "tests": c.tests,
                    "confidence": c.confidence,
                }
                for c in columns
            ],
        }


@app.post("/mapping-specs/{spec_id}/approve")
def approve_mapping_spec_endpoint(
    spec_id: uuid.UUID,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """Approve a proposed mapping specification."""
    with get_session() as session:
        spec = approve_mapping_spec(
            session,
            spec_id,
            reviewer=payload["reviewer"],
            notes=payload.get("notes"),
        )
        if spec is None:
            raise HTTPException(status_code=404, detail="Mapping spec not found")
        return {
            "id": str(spec.id),
            "status": spec.status.value,
            "approved_by": spec.approved_by,
            "approved_at": spec.approved_at.isoformat() if spec.approved_at else None,
        }


# ---------------------------------------------------------------------------
# Code generation and execution endpoints
# ---------------------------------------------------------------------------


@app.post("/mapping-specs/{spec_id}/generate")
def generate_artifacts_endpoint(spec_id: uuid.UUID) -> dict[str, Any]:
    """Generate Polars artifacts from an approved mapping spec."""
    output_folder = Path("data/output-folders") / str(spec_id)
    artifacts = generate_artifact_set(spec_id, output_folder)
    _output_folders[spec_id] = output_folder
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
) -> PipelineOutputFolder:
    """Generate the complete client deliverable folder."""
    payload = payload or {}
    output_folder = Path(payload.get("output_folder", f"data/output-folders/{spec_id}"))
    folder = generate_output_folder(spec_id, output_folder, get_object_store())
    _output_folders[spec_id] = folder.folder_path
    return folder


@app.get("/output-folders/{folder_id}/pipeline.py")
def get_generated_pipeline_py(folder_id: uuid.UUID) -> PlainTextResponse:
    """Return the generated single-file Polars pipeline for human review."""
    folder = _output_folders.get(folder_id)
    if folder is None:
        # Try deterministic path
        folder = Path("data/output-folders") / str(folder_id)
    path = folder / "pipeline.py"
    if not path.exists():
        raise HTTPException(status_code=404, detail="pipeline.py not found")
    return PlainTextResponse(path.read_text(), media_type="text/x-python")


@app.get("/output-folders/{folder_id}/mapping.json")
def get_generated_mapping_json(folder_id: uuid.UUID) -> dict[str, Any]:
    """Return the generated mapping.json for human review."""
    folder = _output_folders.get(folder_id)
    if folder is None:
        folder = Path("data/output-folders") / str(folder_id)
    path = folder / "mapping.json"
    if not path.exists():
        raise HTTPException(status_code=404, detail="mapping.json not found")
    return json.loads(path.read_text())


@app.get("/output-folders/{folder_id}/results.csv")
def get_generated_results_csv(folder_id: uuid.UUID) -> FileResponse:
    """Return the generated results.csv for human review."""
    folder = _output_folders.get(folder_id)
    if folder is None:
        folder = Path("data/output-folders") / str(folder_id)
    path = folder / "results.csv"
    if not path.exists():
        raise HTTPException(status_code=404, detail="results.csv not found")
    return FileResponse(path, media_type="text/csv", filename="results.csv")


@app.post("/mapping-specs/{spec_id}/execute")
def execute_pipeline(
    spec_id: uuid.UUID,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Execute the Polars transformation pipeline for an approved spec."""
    payload = payload or {}
    target_environment = payload.get("target_environment", "local")
    output_folder = Path(payload.get("output_folder", f"data/output-folders/{spec_id}"))
    output_folder.mkdir(parents=True, exist_ok=True)

    object_store = get_object_store()
    target_dfs = run_pipeline(spec_id, object_store, target_environment)
    csv_paths = persist_staging_tables(target_dfs, output_folder, format="csv")

    # Rename single-table output to results.csv
    if len(csv_paths) == 1:
        src = next(iter(csv_paths.values()))
        results_path = output_folder / "results.csv"
        import shutil

        shutil.copy2(src, results_path)

    mapping_spec = load_mapping_spec(spec_id)
    target_schema = load_target_schema_from_spec(mapping_spec)
    run_id = record_execution_run(spec_id, None, target_environment)
    test_results = run_validation_tests(
        target_dfs, mapping_spec["columns"], target_schema
    )
    record_validation_results(run_id, test_results)
    record_staging_metadata(run_id, target_dfs, mapping_spec["columns"], target_schema)

    return {
        "execution_run_id": str(run_id),
        "spec_id": str(spec_id),
        "target_environment": target_environment,
        "csv_paths": {name: str(path) for name, path in csv_paths.items()},
        "results_csv": str(output_folder / "results.csv")
        if (output_folder / "results.csv").exists()
        else None,
        "validation_results": test_results,
        "quality_profiles": {
            name: compute_quality_profile(df) for name, df in target_dfs.items()
        },
    }


@app.get("/execution-runs/{run_id}")
def get_execution_run(run_id: uuid.UUID) -> dict[str, Any]:
    """Fetch an execution run with validation results."""
    from models import ExecutionRun
    from models import ValidationResult as VRModel

    with get_session() as session:
        run = session.get(ExecutionRun, run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="Execution run not found")
        results = session.exec(
            __import__("sqlmodel").select(VRModel).where(
                VRModel.execution_run_id == run_id
            )
        ).all()
        return {
            "id": str(run.id),
            "mapping_spec_id": str(run.mapping_spec_id),
            "status": run.status.value,
            "started_at": run.started_at.isoformat() if run.started_at else None,
            "finished_at": run.finished_at.isoformat() if run.finished_at else None,
            "validation_results": [
                {
                    "id": str(r.id),
                    "test_name": r.test_name,
                    "severity": r.severity,
                    "passed": r.passed,
                    "details": r.details,
                }
                for r in results
            ],
        }


# ---------------------------------------------------------------------------
# Audit and lineage endpoints
# ---------------------------------------------------------------------------


@app.get("/audit-log")
def query_audit_log(
    entity_type: str | None = Query(None),
    entity_id: uuid.UUID | None = Query(None),
    event_type: str | None = Query(None),
    limit: int = Query(50, ge=1, le=500),
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
        statement = statement.order_by(ALModel.recorded_at.desc()).limit(limit)
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
def get_staging_column_lineage(staging_column_id: uuid.UUID) -> dict[str, Any]:
    """Return full provenance for a published staging column."""
    from db_ops import get_lineage_for_staging_column

    with get_session() as session:
        return get_lineage_for_staging_column(session, staging_column_id)
