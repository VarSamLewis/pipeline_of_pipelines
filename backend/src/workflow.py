"""Simplified end-to-end workflow orchestration for the upload-review-execute UI.

This module hides the multi-table pipeline behind three coarse operations:

1. ``process_upload`` — create/select a client, ingest source files and a target
   schema, parse files, extract evidence, and ask the LLM to propose a mapping.
2. ``approve_and_execute`` — approve a proposed mapping, generate the output
   folder, execute the pipeline, and record the run.
3. ``reject_mapping`` — mark a mapping spec as rejected.
"""

from __future__ import annotations

import contextlib
import json
import os
import uuid
from pathlib import Path
from typing import Any

import polars as pl
from codegen import generate_output_folder, load_mapping_spec
from db_ops import (
    _parse_raw_file,
    approve_mapping_spec,
    create_client,
    create_ingestion_batch,
    create_mapping_spec,
    create_raw_file,
    get_client_by_code,
    get_client_by_id,
    get_mapping_spec,
    get_raw_file_by_id,
    get_session,
    update_mapping_spec_status,
)
from file_ops import (
    LocalObjectStore,
    build_storage_key,
    compute_sha256,
    detect_file_type,
)
from mapping import propose_mapping_spec
from models import Client, MappingSpecStatus, TargetSchema
from pipeline import (
    load_target_schema_from_spec,
    record_execution_run,
    record_staging_metadata,
    record_validation_results,
    run_validation_tests,
)

PROJECT_ROOT = Path(__file__).parent.parent.parent
OBJECT_STORE_DIR = PROJECT_ROOT / "data" / "object-store"
TARGET_SCHEMAS_DIR = PROJECT_ROOT / "data" / "target-schemas"
OUTPUT_FOLDERS_DIR = PROJECT_ROOT / "data" / "output-folders"


def _get_object_store() -> LocalObjectStore:
    """Return the singleton local object store."""
    return LocalObjectStore(str(OBJECT_STORE_DIR))


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


def _save_target_schema(client_code: str, content: bytes) -> TargetSchema:
    """Persist the uploaded target schema JSON and parse it into a model."""
    schema_dir = TARGET_SCHEMAS_DIR / client_code
    schema_dir.mkdir(parents=True, exist_ok=True)
    schema_path = schema_dir / "target_schema.json"
    schema_path.write_bytes(content)
    return TargetSchema.model_validate(json.loads(content))


def _store_raw_file(
    session: Any,
    client: Client,
    batch_id: uuid.UUID,
    upload: Any,
    object_store: LocalObjectStore,
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


def process_upload(
    existing_client_id: uuid.UUID | None,
    new_client_name: str | None,
    new_client_code: str | None,
    source_uploads: list[Any],
    target_schema_bytes: bytes,
    model: str = "gpt-4o-mini",
) -> uuid.UUID:
    """Run the upload step and return the created mapping spec id.

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

    object_store = _get_object_store()

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
            model=model,
            api_key=os.getenv("OPENAI_API_KEY"),
            base_url=os.getenv("OPENAI_BASE_URL"),
            top_k_evidence=10,
        )
        update_mapping_spec_status(session, spec.id, MappingSpecStatus.PROPOSED)
        return spec.id


def approve_and_execute(spec_id: uuid.UUID) -> uuid.UUID:
    """Approve a mapping spec and run the pipeline.

    Returns:
        The UUID of the recorded ExecutionRun.
    """
    object_store = _get_object_store()
    output_folder = OUTPUT_FOLDERS_DIR / str(spec_id)
    output_folder.mkdir(parents=True, exist_ok=True)

    with get_session() as session:
        spec = get_mapping_spec(session, spec_id)
        if spec is None:
            raise ValueError("Mapping spec not found")

        approve_mapping_spec(
            session,
            spec_id,
            reviewer="wizard-user",
            notes="Approved via upload wizard",
        )

    # Generate pipeline.py, mapping.json, and execute to produce CSVs.
    generate_output_folder(spec_id, output_folder, object_store)

    # Read the generated CSVs back into DataFrames for validation/staging metadata.
    mapping_spec = load_mapping_spec(spec_id)
    target_schema = load_target_schema_from_spec(mapping_spec)
    target_tables = {c["target_table"] for c in mapping_spec["columns"]}
    target_dfs: dict[str, Any] = {}
    for table_name in target_tables:
        csv_path = output_folder / f"{table_name}.csv"
        if csv_path.exists():
            target_dfs[table_name] = pl.read_csv(csv_path)

    # Record an ExecutionRun and validation/staging metadata for the UI.
    client_id = spec.client_id
    run_id = record_execution_run(client_id, spec_id, "local")
    test_results = run_validation_tests(
        target_dfs, mapping_spec["columns"], target_schema
    )
    record_validation_results(run_id, test_results)
    record_staging_metadata(run_id, target_dfs, mapping_spec["columns"], target_schema)

    return run_id


def reject_mapping(spec_id: uuid.UUID) -> None:
    """Mark a mapping spec as rejected."""
    with get_session() as session:
        update_mapping_spec_status(session, spec_id, MappingSpecStatus.REJECTED)


def load_mapping_json(spec_id: uuid.UUID) -> dict[str, Any]:
    """Load a mapping spec and its columns as a plain dict."""
    from codegen import load_mapping_spec

    return load_mapping_spec(spec_id)


def get_run_output_paths(run_id: uuid.UUID) -> dict[str, Path]:
    """Return filesystem paths for a recorded run's deliverables."""
    with get_session() as session:
        from models import ExecutionRun

        run = session.get(ExecutionRun, run_id)
        if run is None:
            raise ValueError("Execution run not found")
        spec_id = run.mapping_spec_id

    folder = OUTPUT_FOLDERS_DIR / str(spec_id)
    return {
        "folder": folder,
        "results_csv": folder / "results.csv",
        "pipeline_py": folder / "pipeline.py",
        "mapping_json": folder / "mapping.json",
    }
