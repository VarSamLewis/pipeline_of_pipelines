"""Canonical application workflow and explicit human-gated transitions.

Both JSON API and HTMX routes delegate to this module. It is the only
production owner of upload/proposal, mapping approval, generated execution,
and result approval. HTTP modules validate input and translate responses only.
"""

from __future__ import annotations

import contextlib
import json
import shutil
import uuid
from pathlib import Path
from typing import Any

import polars as pl
from codegen import generate_artifact_set, generate_output_folder
from config import get_settings
from db_ops import (
    _parse_raw_file,
    approve_mapping_spec,
    create_mapping_spec,
    create_raw_file,
    get_mapping_spec,
    get_raw_file_by_id,
    get_session,
    update_mapping_spec_status,
)
from dependencies import get_artifact_store, get_object_store
from file_ops import (
    ObjectStore,
    build_storage_key,
    compute_sha256,
    detect_file_type,
)
from mapping import propose_mapping_spec
from mapping_specs import load_mapping_spec, load_target_schema_from_spec
from models import (
    Client,
    ExecutionRun,
    GeneratedArtifact,
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
)
from repositories.executions import (
    approve_result as approve_result_record,
)
from repositories.executions import (
    reject_result as reject_result_record,
)
from sqlmodel import select


def get_or_create_client(
    session: Any,
    existing_client_id: uuid.UUID | None,
    new_client_name: str | None,
    new_client_code: str | None,
) -> Client:
    """Resolve a client from an existing id or create a new one."""
    if existing_client_id:
        client = get_client_by_id(session, existing_client_id)
        if client is None:
            raise ValueError("Selected client not found")
        return client

    if not new_client_name or not new_client_code:
        msg = "Either select an existing client or provide new client details"
        raise ValueError(msg)

    existing = get_client_by_code(session, new_client_code)
    if existing is not None:
        raise ValueError(f"Client code '{new_client_code}' already exists")

    return create_client(
        session,
        name=new_client_name,
        code=new_client_code,
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
) -> uuid.UUID:
    """Store one uploaded file in object storage and register a RawFile record."""
    filename = upload.filename or "unknown"
    contents = upload.file.read()
    sha256 = compute_sha256(contents)
    file_type = detect_file_type(filename, contents)
    mime_type = upload.content_type or "application/octet-stream"
    if file_type == "csv":
        mime_type = "text/csv"
    elif file_type == "xlsx":
        mime_type = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

    storage_key = build_storage_key(client.code, str(batch_id), filename, sha256)
    object_store.put(storage_key, contents)

    raw_file = create_raw_file(
        session=session,
        client_id=client.id,
        ingestion_batch_id=batch_id,
        original_filename=filename,
        storage_key=storage_key,
        sha256=sha256,
        size_bytes=len(contents),
        mime_type=mime_type,
        metadata={},
    )
    return raw_file.id


def ingest_and_propose(
    existing_client_id: uuid.UUID | None,
    new_client_name: str | None,
    new_client_code: str | None,
    source_uploads: list[Any],
    target_schema_bytes: bytes,
    model: str | None = None,
) -> uuid.UUID:
    """Ingest uploads, extract evidence, and create a proposed mapping.

    Args:
        existing_client_id: UUID of an existing client, if selected.
        new_client_name: Name for a new client, if creating one.
        new_client_code: Short code for a new client, if creating one.
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
        client = get_or_create_client(
            session, existing_client_id, new_client_name, new_client_code
        )
        batch = create_ingestion_batch(
            session,
            client_id=client.id,
            label="Wizard upload",
            metadata={},
        )
        session.commit()

        raw_file_ids: list[uuid.UUID] = []
        for upload in source_uploads:
            raw_file_id = _store_raw_file(
                session, client, batch.id, upload, object_store
            )
            raw_file_ids.append(raw_file_id)

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
            raw_file = get_raw_file_by_id(session, raw_file_id)
            if raw_file is None:
                continue
            file_type = detect_file_type(raw_file.original_filename)
            data = object_store.get(raw_file.storage_key)
            # Continue even if one file fails to parse; the LLM will work with
            # whatever profiles and evidence were extracted.
            with contextlib.suppress(Exception):
                _parse_raw_file(session, raw_file, data, file_type)

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
            str(folder / "results.csv")
            if (folder / "results.csv").exists()
            else None
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
