"""Canonical application workflow and explicit human-gated transitions.

Both JSON API and HTMX routes delegate to this module. It is the only
production owner of upload/proposal, mapping approval, generated execution,
and result approval. HTTP modules validate input and translate responses only.
"""

from __future__ import annotations

import json
import shutil
import uuid
from pathlib import Path
from typing import Any

import polars as pl
from codegen import generate_artifact_set, generate_output_folder
from config import get_settings
from db_ops import (
    approve_mapping_spec,
    create_mapping_spec,
    create_raw_file,
    get_mapping_spec,
    get_raw_file_by_id,
    get_session,
    get_spreadsheet_profile,
    parse_and_record_raw_file,
    update_mapping_spec_status,
)
from dependencies import get_artifact_store, get_object_store
from file_ops import (
    ObjectStore,
    build_storage_key,
    compute_sha256,
    detect_file_type,
    mime_type_for,
)
from mapping import (
    propose_mapping_spec,
)
from mapping_specs import load_mapping_spec, load_target_schema_from_spec
from models import (
    Client,
    ExecutionRun,
    GeneratedArtifact,
    MappingColumn,
    MappingSpec,
    MappingSpecStatus,
    PipelineOutputFolder,
    TargetSchema,
    ValidationResult,
)
from pipeline import (
    compute_quality_profile,
    record_execution_run,
    record_staging_metadata,
    record_validation_results,
    run_validation_tests,
)
from repositories.clients import (
    create_client,
    create_ingestion_batch,
    get_client_by_code,
    get_client_by_id,
    get_ingestion_batch,
)
from repositories.executions import (
    approve_result as approve_result_record,
)
from repositories.executions import (
    reject_result as reject_result_record,
)
from sqlmodel import select


def _json_safe(value: Any) -> Any:
    """Return a JSON-serializable copy of a value for audit-log payloads."""
    if isinstance(value, (uuid.UUID, Path)):
        return str(value)
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(item) for item in value]
    return value


def get_or_create_client(
    session: Any,
    existing_client_id: uuid.UUID | None,
    new_client_name: str | None,
) -> Client:
    """Resolve a client from an existing id or create a new one."""
    if existing_client_id:
        client = get_client_by_id(session, existing_client_id)
        if client is None:
            raise ValueError("Selected client not found")
        return client

    if not new_client_name:
        msg = "Either select an existing client or provide a client name"
        raise ValueError(msg)

    import re

    code = re.sub(r"[^a-z0-9]+", "-", new_client_name.strip().lower()).strip("-")
    if not code:
        code = "client"

    existing = get_client_by_code(session, code)
    if existing is not None:
        import random

        code = f"{code}-{random.randint(1000, 9999)}"

    return create_client(
        session,
        name=new_client_name,
        code=code,
        metadata={},
    )


def list_clients() -> list[Client]:
    """Return clients for workflow entry-point selection."""
    with get_session() as session:
        return list(session.exec(select(Client).order_by(Client.name)).all())


def _save_target_schema(client_code: str, content: bytes) -> TargetSchema:
    """Persist the uploaded target schema JSON and parse it into a model."""
    schema = TargetSchema.model_validate(json.loads(content))
    get_artifact_store().write_target_schema(client_code, schema)
    return schema


def _store_raw_file(
    session: Any,
    client: Client,
    batch_id: uuid.UUID,
    upload: Any,
    object_store: ObjectStore,
    metadata: dict[str, Any] | None = None,
) -> RawFile:
    """Store one uploaded file in object storage and register a RawFile record."""
    filename = upload.filename or "unknown"
    contents = upload.file.read()
    sha256 = compute_sha256(contents)
    file_type = detect_file_type(filename, contents)
    mime_type = mime_type_for(
        file_type, upload.content_type or "application/octet-stream"
    )

    storage_key = build_storage_key(client.code, str(batch_id), filename, sha256)
    object_store.put(storage_key, contents)

    return create_raw_file(
        session=session,
        client_id=client.id,
        ingestion_batch_id=batch_id,
        original_filename=filename,
        storage_key=storage_key,
        sha256=sha256,
        size_bytes=len(contents),
        mime_type=mime_type,
        metadata=metadata,
    )


def store_raw_file_upload(
    client_code: str,
    batch_id: uuid.UUID,
    upload: Any,
    metadata: dict[str, Any] | None = None,
) -> RawFile:
    """Store one uploaded file for an existing client batch.

    Raises:
        ValueError: If the client or batch does not exist.
    """
    with get_session() as session:
        client = get_client_by_code(session, client_code)
        if client is None:
            raise ValueError("Client not found")
        batch = get_ingestion_batch(session, batch_id)
        if batch is None or batch.client_id != client.id:
            raise ValueError("Batch not found")
        return _store_raw_file(
            session, client, batch.id, upload, get_object_store(), metadata
        )


def parse_raw_file(raw_file_id: uuid.UUID) -> dict[str, Any]:
    """Parse a raw file into profiles and evidence, recording the outcome.

    Raises:
        ValueError: If the raw file does not exist.
        RuntimeError: If parsing fails (after the failure is recorded).
    """
    with get_session() as session:
        raw_file = get_raw_file_by_id(session, raw_file_id)
        if raw_file is None:
            raise ValueError("Raw file not found")
        data = get_object_store().get(raw_file.storage_key)
        file_type = detect_file_type(raw_file.original_filename, data)
        try:
            parse_and_record_raw_file(session, raw_file, data, file_type)
        except Exception as exc:
            raise RuntimeError(str(exc)) from exc
        profile = get_spreadsheet_profile(session, raw_file.id)
        return {
            "raw_file_id": str(raw_file.id),
            "status": raw_file.status.value,
            "profile": profile.profile_json if profile else None,
        }


def ingest_and_propose(
    existing_client_id: uuid.UUID | None,
    new_client_name: str | None,
    source_uploads: list[Any],
    target_schema_bytes: bytes,
    model: str | None = None,
) -> uuid.UUID:
    """Ingest uploads, extract evidence, and create a proposed mapping.

    Args:
        existing_client_id: UUID of an existing client, if selected.
        new_client_name: Name for a new client, if creating one.
        source_uploads: List of uploaded source files.
        target_schema_bytes: Raw bytes of the uploaded target schema JSON.
        model: LLM model to use for mapping proposal.

    Returns:
        The UUID of the proposed MappingSpec.
    """
    if not source_uploads:
        raise ValueError("At least one source file is required")

    settings = get_settings()
    object_store = get_object_store()

    with get_session() as session:
        client = get_or_create_client(session, existing_client_id, new_client_name)
        batch = create_ingestion_batch(
            session,
            client_id=client.id,
            label="Wizard upload",
            metadata={},
        )
        session.commit()

        raw_file_ids: list[uuid.UUID] = []
        for upload in source_uploads:
            raw_file = _store_raw_file(session, client, batch.id, upload, object_store)
            raw_file_ids.append(raw_file.id)

        target_schema = _save_target_schema(client.code, target_schema_bytes)

        spec = create_mapping_spec(
            session,
            client_id=client.id,
            source_raw_file_ids=raw_file_ids,
            target_schema=target_schema,
            description="Generated by upload wizard",
        )

        # Parse source files to extract evidence and profiles.
        for raw_file_id in raw_file_ids:
            stored_file = get_raw_file_by_id(session, raw_file_id)
            if stored_file is None:
                continue
            data = object_store.get(stored_file.storage_key)
            file_type = detect_file_type(stored_file.original_filename, data)
            try:
                parse_and_record_raw_file(session, stored_file, data, file_type)
            except Exception:
                continue

        propose_mapping_spec(
            session,
            spec.id,
            target_schema=target_schema,
            model=model or settings.mapping_model,
            api_key=settings.openai_api_key,
            base_url=settings.openai_base_url,
            top_k_evidence=10,
        )
        update_mapping_spec_status(session, spec.id, MappingSpecStatus.PROPOSED)
        return spec.id


def propose_mapping(
    spec_id: uuid.UUID,
    *,
    model: str | None = None,
    api_key: str | None = None,
    base_url: str | None = None,
    top_k_evidence: int = 10,
) -> dict[str, Any]:
    """Propose columns for an existing draft mapping specification."""
    settings = get_settings()
    with get_session() as session:
        spec = get_mapping_spec(session, spec_id)
        if spec is None:
            raise ValueError("Mapping spec not found")
        target_schema = TargetSchema.model_validate(spec.target_schema_json)
        propose_mapping_spec(
            session,
            spec_id,
            target_schema=target_schema,
            model=model or settings.mapping_model,
            api_key=api_key or settings.openai_api_key,
            base_url=base_url or settings.openai_base_url,
            top_k_evidence=top_k_evidence,
        )
    return get_mapping_review(spec_id)


def approve_mapping(
    spec_id: uuid.UUID,
    *,
    reviewer: str,
    notes: str | None = None,
) -> MappingSpec:
    """Apply the mapping-approval transition."""
    with get_session() as session:
        spec = get_mapping_spec(session, spec_id)
        if spec is None:
            raise ValueError("Mapping spec not found")
        approved = approve_mapping_spec(session, spec_id, reviewer, notes)
        if approved is None:
            raise ValueError("Mapping spec not found")
        return approved


def generate_artifacts(
    spec_id: uuid.UUID,
) -> tuple[Path, list[GeneratedArtifact]]:
    """Generate reviewable mapping and Python artifacts."""
    output_folder = get_artifact_store().folder(spec_id)
    return output_folder, generate_artifact_set(spec_id, output_folder)


def create_output_folder(
    spec_id: uuid.UUID,
    output_folder: Path | None = None,
) -> PipelineOutputFolder:
    """Generate the complete deliverable through infrastructure adapters."""
    folder = output_folder or get_artifact_store().folder(spec_id)
    return generate_output_folder(spec_id, folder, get_object_store())


def execute_approved_mapping(
    spec_id: uuid.UUID,
    *,
    target_environment: str = "local",
    output_folder: Path | None = None,
) -> dict[str, Any]:
    """Execute the approved generated artifact, validate, and record the run."""
    folder = output_folder or get_artifact_store().folder(spec_id)
    folder.mkdir(parents=True, exist_ok=True)
    create_output_folder(spec_id, folder)

    mapping_spec = load_mapping_spec(spec_id)
    target_schema = load_target_schema_from_spec(mapping_spec)
    target_tables = {column["target_table"] for column in mapping_spec["columns"]}
    target_dfs: dict[str, pl.DataFrame] = {}
    csv_paths: dict[str, Path] = {}
    for table_name in target_tables:
        csv_path = folder / f"{table_name}.csv"
        if csv_path.exists():
            target_dfs[table_name] = pl.read_csv(csv_path)
            csv_paths[table_name] = csv_path

    if len(csv_paths) == 1:
        shutil.copy2(next(iter(csv_paths.values())), folder / "results.csv")

    with get_session() as session:
        spec = get_mapping_spec(session, spec_id)
        if spec is None:
            raise ValueError("Mapping spec not found")
        client_id = spec.client_id

    run_id = record_execution_run(client_id, spec_id, target_environment)
    test_results = run_validation_tests(
        target_dfs,
        mapping_spec["columns"],
        target_schema,
    )
    record_validation_results(run_id, test_results)
    record_staging_metadata(
        run_id,
        target_dfs,
        mapping_spec["columns"],
        target_schema,
    )
    result = {
        "execution_run_id": str(run_id),
        "spec_id": str(spec_id),
        "target_environment": target_environment,
        "csv_paths": {name: str(path) for name, path in csv_paths.items()},
        "results_csv": (
            str(folder / "results.csv") if (folder / "results.csv").exists() else None
        ),
        "validation_results": test_results,
        "quality_profiles": {
            name: compute_quality_profile(df) for name, df in target_dfs.items()
        },
    }
    get_artifact_store().write_log(
        run_id,
        json.dumps(result, indent=2, default=str).encode("utf-8"),
    )
    return result


def approve_mapping_and_execute(
    spec_id: uuid.UUID,
    *,
    reviewer: str = "wizard-user",
    notes: str | None = "Approved via upload wizard",
) -> uuid.UUID:
    """Approve a mapping and execute it through the canonical runtime."""
    approve_mapping(spec_id, reviewer=reviewer, notes=notes)
    result = execute_approved_mapping(spec_id)
    return uuid.UUID(result["execution_run_id"])


def retry_pipeline_and_execute(
    spec_id: uuid.UUID,
    error_message: str,
) -> uuid.UUID:
    """Retry code generation with the failed pipeline + error, then re-execute."""
    from codegen import _codegen_with_context

    folder = get_artifact_store().folder(spec_id)
    pipeline_path = folder / "pipeline.py"
    if not pipeline_path.exists():
        raise RuntimeError("pipeline.py not found in output folder")
    failed_code = pipeline_path.read_text()

    corrected_code = _codegen_with_context(spec_id, failed_code, error_message)
    pipeline_path.write_text(corrected_code)

    result = execute_approved_mapping(spec_id, output_folder=folder)
    return uuid.UUID(result["execution_run_id"])


def reject_mapping(
    spec_id: uuid.UUID,
    *,
    reason: str = "",
    reviewer: str | None = None,
) -> MappingSpec:
    """Apply the mapping-rejection transition."""
    with get_session() as session:
        spec = get_mapping_spec(session, spec_id)
        if spec is None:
            raise ValueError("Mapping spec not found")
        spec.status = MappingSpecStatus.REJECTED
        if reason:
            actor = f" by {reviewer}" if reviewer else ""
            spec.description = (
                f"{spec.description or ''}\nRejected{actor}: {reason}"
            ).strip()
        session.add(spec)
        session.commit()
        session.refresh(spec)
        return spec


def approve_result(run_id: uuid.UUID) -> ExecutionRun:
    """Record the explicit human approval gate for generated results."""
    with get_session() as session:
        run = approve_result_record(session, run_id)
        session.commit()
        _update_execution_log(run_id, status=run.status.value)
        return run


def reject_result(run_id: uuid.UUID, reason: str = "") -> uuid.UUID:
    """Reject generated results and return their mapping spec for review."""
    with get_session() as session:
        run = reject_result_record(session, run_id, reason)
        session.commit()
        _update_execution_log(
            run_id,
            status=run.status.value,
            rejection_reason=reason,
        )
        return run.mapping_spec_id


def _update_execution_log(run_id: uuid.UUID, **updates: Any) -> None:
    """Merge a state transition into the durable execution log."""
    store = get_artifact_store()
    try:
        current = json.loads(store.read_log(run_id))
    except FileNotFoundError:
        current = {"execution_run_id": str(run_id)}
    current.update(updates)
    store.write_log(
        run_id,
        json.dumps(current, indent=2, default=str).encode("utf-8"),
    )


def get_mapping_review(spec_id: uuid.UUID) -> dict[str, Any]:
    """Return the canonical mapping review projection."""
    return load_mapping_spec(spec_id)


def get_result_review(run_id: uuid.UUID) -> dict[str, Any]:
    """Return the run and durable artifacts needed for result review."""
    with get_session() as session:
        run = session.get(ExecutionRun, run_id)
        if run is None:
            raise ValueError("Execution run not found")
        spec_id = run.mapping_spec_id
        validation_results = list(
            session.exec(
                select(ValidationResult).where(
                    ValidationResult.execution_run_id == run_id
                )
            ).all()
        )

    store = get_artifact_store()
    csv_names = store.list_artifacts(spec_id, suffix=".csv")
    results_name = "results.csv" if "results.csv" in csv_names else None
    if results_name is None and csv_names:
        results_name = csv_names[0]

    def read_optional(filename: str | None) -> bytes | None:
        if filename is None:
            return None
        try:
            return store.read_artifact(spec_id, filename)
        except FileNotFoundError:
            return None

    return {
        "run": run,
        "validation_results": validation_results,
        "results_csv": read_optional(results_name),
        "pipeline_py": read_optional("pipeline.py"),
        "mapping_json": read_optional("mapping.json"),
    }


def refine_mapping(
    spec_id: uuid.UUID,
    feedback: str,
) -> list[dict[str, Any]]:
    """Propose column-level refinements based on user feedback."""
    from feedback import propose_refinements, store_feedback

    proposals = propose_refinements(spec_id, feedback)
    store_feedback(feedback, spec_id)
    return proposals


def apply_refinements(
    spec_id: uuid.UUID,
    changes: list[dict[str, Any]],
    user: Any | None = None,
) -> dict[str, Any]:
    """Apply approved changes to mapping columns and regenerate artifacts.

    Every applied change is appended to the audit log (when a user is given),
    so chat-driven applies and direct mapping edits share one audited path.
    """
    from codegen import generate_artifact_set
    from db_ops import get_session, write_audit_log
    from mapping_specs import load_mapping_spec

    applied: list[tuple[uuid.UUID, str, Any, Any]] = []
    with get_session() as session:
        for change in changes:
            column_id = change.get("column_id")
            field = change.get("field")
            new_value = change.get("new_value")
            if not column_id or not field:
                continue
            column = session.get(MappingColumn, uuid.UUID(str(column_id)))
            if column is None or column.mapping_spec_id != spec_id:
                continue
            old_value = getattr(column, field, None)
            setattr(column, field, new_value)
            session.add(column)
            applied.append((column.id, field, old_value, new_value))
        for column_id, field, old_value, new_value in applied:
            write_audit_log(
                session,
                "mapping_column_edited",
                "MappingColumn",
                column_id,
                getattr(user, "id", None),
                getattr(user, "email", None),
                {
                    "spec_id": str(spec_id),
                    "field": field,
                    "old_value": _json_safe(old_value),
                    "new_value": _json_safe(new_value),
                },
            )
        session.commit()

    output_folder = get_artifact_store().folder(spec_id)
    generate_artifact_set(spec_id, output_folder)
    return load_mapping_spec(spec_id)


def update_mapping_column(
    spec_id: uuid.UUID,
    column_id: uuid.UUID,
    fields: dict[str, Any],
    user: Any | None = None,
) -> MappingColumn:
    """Update one mapping column through the canonical audited refine path.

    Raises:
        ValueError: If the spec or column does not exist.
    """
    with get_session() as session:
        if get_mapping_spec(session, spec_id) is None:
            raise ValueError("Mapping spec not found")
        column = session.get(MappingColumn, column_id)
        if column is None or column.mapping_spec_id != spec_id:
            raise ValueError("Mapping column not found")

    changes = [
        {"column_id": str(column_id), "field": field, "new_value": value}
        for field, value in fields.items()
    ]
    apply_refinements(spec_id, changes, user)

    with get_session() as session:
        column = session.get(MappingColumn, column_id)
        if column is None:
            raise ValueError("Mapping column not found")
        session.refresh(column)
        return column


def refine_from_results(
    run_id: uuid.UUID,
    feedback: str,
) -> list[dict[str, Any]]:
    """Propose refinements with execution context from a results page."""
    from db_ops import get_session
    from feedback import propose_refinements, store_feedback
    from models import ExecutionRun, ValidationResult

    with get_session() as session:
        run = session.get(ExecutionRun, run_id)
        if run is None:
            raise ValueError(f"Execution run not found: {run_id}")
        spec_id = run.mapping_spec_id

        failures = list(
            session.exec(
                select(ValidationResult).where(
                    ValidationResult.execution_run_id == run_id,
                    ValidationResult.passed == False,  # noqa: E712
                )
            ).all()
        )
        validation_context = None
        if failures:
            validation_context = {
                "failed_tests": [
                    {
                        "test_name": f.test_name,
                        "severity": f.severity,
                        "details": f.details,
                    }
                    for f in failures
                ]
            }

    proposals = propose_refinements(spec_id, feedback, validation_context)
    store_feedback(feedback, spec_id, run_id)
    return proposals


def get_column_provenance(
    spec_id: uuid.UUID,
    target_column: str,
) -> dict[str, Any] | None:
    """Return the mapping rule behind a generated output column."""
    from mapping_specs import load_mapping_spec

    spec = load_mapping_spec(spec_id)
    for column in spec.get("columns", []):
        if column.get("target_column") == target_column:
            return dict(column)
    return None


def get_merged_results(
    run_id: uuid.UUID,
) -> dict[str, Any] | None:
    """Return results CSV overlaid with any spec-keyed manual overrides."""
    import io as _io

    from db_ops import get_session, list_result_overrides
    from mapping_specs import load_mapping_spec

    paths = get_result_review(run_id)
    csv_content = paths["results_csv"]
    if csv_content is None:
        return None
    spec_id = paths["run"].mapping_spec_id
    spec = load_mapping_spec(spec_id)
    tables = {c.get("target_table") for c in spec.get("columns", [])}
    target_table = next(iter(tables), None) or "results"

    df = pl.read_csv(_io.BytesIO(csv_content))
    with get_session() as session:
        overrides = list_result_overrides(session, spec_id)

    row_key_col = df.columns[0] if df.columns else ""
    overridden: dict[tuple[str, str], bool] = {}
    override_map: dict[tuple[str, str], dict[str, Any]] = {}
    for o in overrides:
        override_map[(o.row_key, o.target_column)] = {
            "id": str(o.id),
            "target_column": o.target_column,
            "row_key": o.row_key,
            "value": o.value,
            "reason": o.reason,
            "created_by": o.created_by or "",
        }
    merged_rows: list[dict[str, Any]] = []
    if row_key_col:
        for row in df.iter_rows(named=True):
            key = str(row.get(row_key_col, ""))
            merged = dict(row)
            for o in overrides:
                if o.row_key == key and o.target_column in merged:
                    merged[o.target_column] = o.value
                    overridden[(key, o.target_column)] = True
            merged_rows.append(merged)
    else:
        merged_rows = [dict(row) for row in df.iter_rows(named=True)]

    return {
        "target_table": target_table,
        "row_key_column": row_key_col,
        "merged_rows": merged_rows,
        "overridden": overridden,
        "overrides": list(override_map.values()),
        "override_count": len(override_map),
    }


def overwrite_pipeline_code(
    spec_id: uuid.UUID,
    content: str,
    user: Any | None = None,
) -> dict[str, Any]:
    """Overwrite the generated pipeline.py and append an audit event."""
    from db_ops import get_session, write_audit_log

    store = get_artifact_store()
    try:
        store.read_artifact(spec_id, "pipeline.py")
    except FileNotFoundError:
        raise ValueError("pipeline.py not found") from None
    store.write_artifact(spec_id, "pipeline.py", content.encode("utf-8"))
    with get_session() as session:
        write_audit_log(
            session,
            "pipeline_py_edited",
            "GeneratedPipeline",
            spec_id,
            getattr(user, "id", None),
            getattr(user, "email", None),
            {"folder_id": str(spec_id), "bytes": len(content)},
        )
    return {"folder_id": str(spec_id), "bytes": len(content)}


def create_result_override_record(
    run_id: uuid.UUID,
    *,
    target_table: str,
    target_column: str,
    row_key: str,
    value: str,
    reason: str,
    created_by: str | None = None,
) -> None:
    """Create a manual results override keyed to the run's mapping spec."""
    from db_ops import create_result_override, get_session

    with get_session() as session:
        run = session.get(ExecutionRun, run_id)
        if run is None:
            raise ValueError("Execution run not found")
        create_result_override(
            session,
            spec_id=run.mapping_spec_id,
            run_id=run_id,
            target_table=target_table,
            target_column=target_column,
            row_key=row_key,
            value=value,
            reason=reason,
            created_by=created_by,
        )


def delete_result_override_record(run_id: uuid.UUID, override_id: uuid.UUID) -> None:
    """Delete a manual results override scoped to the run's mapping spec."""
    from db_ops import delete_result_override, get_session

    with get_session() as session:
        run = session.get(ExecutionRun, run_id)
        if run is None:
            raise ValueError("Execution run not found")
        delete_result_override(session, override_id, run.mapping_spec_id)


def reset_pipeline_code(
    spec_id: uuid.UUID,
    user: Any | None = None,
) -> str:
    """Regenerate pipeline.py from the mapping contract and append an audit event."""
    from db_ops import get_session, write_audit_log

    store = get_artifact_store()
    folder = store.folder(spec_id)
    generate_artifact_set(spec_id, folder)
    content = store.read_artifact(spec_id, "pipeline.py").decode("utf-8")
    with get_session() as session:
        write_audit_log(
            session,
            "pipeline_py_reset",
            "GeneratedPipeline",
            spec_id,
            getattr(user, "id", None),
            getattr(user, "email", None),
            {"folder_id": str(spec_id)},
        )
    return content


def apply_refinements_and_reexecute(
    run_id: uuid.UUID,
    changes: list[dict[str, Any]],
    user: Any | None = None,
) -> dict[str, Any]:
    """Apply approved changes through the canonical audited path and re-execute."""
    from db_ops import get_session
    from models import ExecutionRun

    with get_session() as session:
        run = session.get(ExecutionRun, run_id)
        if run is None:
            raise ValueError(f"Execution run not found: {run_id}")
        spec_id = run.mapping_spec_id

    apply_refinements(spec_id, changes, user)
    output_folder = get_artifact_store().folder(spec_id)
    result = execute_approved_mapping(spec_id, output_folder=output_folder)
    return result
