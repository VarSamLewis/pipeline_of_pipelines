"""Database operations for metadata, evidence search, and lineage.

This module handles all interactions with the database. For the initial
implementation it uses SQLite (via SQLModel) so the backend can run without
a Postgres container. The schema is designed to migrate to PostgreSQL + pgvector
later.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC
from typing import Any, cast

from config import get_settings
from file_ops import (
    ObjectStore,
    build_storage_key,
    compute_sha256,
    detect_file_type,
)
from models import (
    AuditLog,
    BusinessRule,
    BusinessRuleStatus,
    ExtractedEvidence,
    FileStatus,
    FolderIngestionResult,
    GeneratedArtifact,
    MappingColumn,
    MappingSpec,
    MappingSpecStatus,
    RawFile,
    ResultOverride,
    SpreadsheetProfile,
    TargetSchema,
)
from parser import (
    discover_source_tables,
    extract_evidence_chunks,
    parse_email_to_dict,
    parse_pdf_to_text,
    parse_text_document,
)
from repositories.clients import (
    create_ingestion_batch,
    get_client_by_id,
)
from sqlalchemy import Engine, text
from sqlmodel import Session, SQLModel, create_engine, select

_settings = get_settings()
DEFAULT_DATABASE_URL = _settings.database_url
OPENAI_API_KEY = _settings.openai_api_key
OPENAI_BASE_URL = _settings.openai_base_url


def get_embedding(
    content: str,
    api_key: str | None = None,
    base_url: str | None = None,
    model: str | None = None,
) -> list[float]:
    """Generate an embedding vector for a text chunk using OpenAI."""
    api_key = api_key or OPENAI_API_KEY
    if not api_key:
        raise RuntimeError(
            "OPENAI_API_KEY is not set. Embeddings are required for the knowledge base."
        )

    from openai import OpenAI

    client = OpenAI(api_key=api_key, base_url=base_url or OPENAI_BASE_URL)
    response = client.embeddings.create(
        model=model or _settings.embedding_model,
        input=content,
    )
    return response.data[0].embedding


_engine: Engine | None = None


def get_engine(database_url: str | None = None) -> Engine:
    """Create and return a SQLAlchemy engine for the given database URL."""
    global _engine
    url = database_url or DEFAULT_DATABASE_URL
    if _engine is None:
        _engine = create_engine(url)
    return _engine


def create_pgvector_extension(engine: Engine | None = None) -> None:
    """Enable the pgvector extension in PostgreSQL."""
    if engine is None:
        engine = get_engine()
    with engine.connect() as connection:
        connection.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        connection.commit()


def create_tables(engine: Engine | None = None) -> None:
    """Create all SQLModel tables in the database."""
    if engine is None:
        engine = get_engine()
    create_pgvector_extension(engine)
    SQLModel.metadata.create_all(engine)


def get_session(engine: Engine | None = None) -> Session:
    """Yield a new SQLModel database session."""
    if engine is None:
        engine = get_engine()
    return Session(engine)


# ---------------------------------------------------------------------------
# Client and ingestion batch operations
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Raw file operations
# ---------------------------------------------------------------------------


def create_raw_file(
    session: Session,
    client_id: uuid.UUID,
    ingestion_batch_id: uuid.UUID,
    original_filename: str,
    storage_key: str,
    sha256: str,
    size_bytes: int,
    mime_type: str,
    metadata: dict[str, Any] | None = None,
) -> RawFile:
    """Register an immutable raw file record."""
    raw_file = RawFile(
        client_id=client_id,
        ingestion_batch_id=ingestion_batch_id,
        original_filename=original_filename,
        storage_key=storage_key,
        sha256=sha256,
        size_bytes=size_bytes,
        mime_type=mime_type,
        status=FileStatus.RECEIVED,
        meta=metadata or {},
    )
    session.add(raw_file)
    session.commit()
    session.refresh(raw_file)
    return raw_file


def get_raw_file_by_id(session: Session, raw_file_id: uuid.UUID) -> RawFile | None:
    """Fetch a raw file by UUID."""
    return session.get(RawFile, raw_file_id)


def update_raw_file_status(
    session: Session,
    raw_file_id: uuid.UUID,
    status: FileStatus,
) -> RawFile | None:
    """Update the processing status of a raw file."""
    raw_file = session.get(RawFile, raw_file_id)
    if raw_file is None:
        return None
    raw_file.status = status
    session.add(raw_file)
    session.commit()
    session.refresh(raw_file)
    return raw_file


def list_raw_files_by_batch(
    session: Session,
    ingestion_batch_id: uuid.UUID,
) -> Sequence[RawFile]:
    """List all raw files in an ingestion batch."""
    return session.exec(
        select(RawFile).where(RawFile.ingestion_batch_id == ingestion_batch_id)
    ).all()


# ---------------------------------------------------------------------------
# Folder ingestion
# ---------------------------------------------------------------------------


def ingest_client_folder(
    session: Session,
    client_id: uuid.UUID,
    folder_path: str,
    object_store: ObjectStore,
    label: str | None = None,
) -> FolderIngestionResult:
    """Ingest an entire client folder of heterogeneous files as one batch."""
    from pathlib import Path

    folder = Path(folder_path)
    if not folder.exists():
        raise ValueError(f"Folder not found: {folder_path}")

    batch = create_ingestion_batch(session, client_id, label or folder.name)
    client = get_client_by_id(session, client_id)
    if client is None:
        raise ValueError(f"Client not found: {client_id}")

    raw_file_ids: list[uuid.UUID] = []
    evidence_ids: list[uuid.UUID] = []
    parsed_count = 0
    failed_count = 0

    from file_ops import discover_client_files

    files = discover_client_files(folder)
    for file_path in files:
        file_bytes = file_path.read_bytes()
        sha256 = compute_sha256(file_bytes)
        mime_type = "application/octet-stream"
        file_type = detect_file_type(file_path.name, file_bytes)
        if file_type == "csv":
            mime_type = "text/csv"
        elif file_type == "xlsx":
            mime_type = (
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            )
        elif file_type == "pdf":
            mime_type = "application/pdf"
        elif file_type == "eml":
            mime_type = "message/rfc822"
        elif file_type == "txt":
            mime_type = "text/plain"
        elif file_type == "md":
            mime_type = "text/markdown"
        elif file_type == "docx":
            mime_type = (
                "application/vnd.openxmlformats-officedocument."
                "wordprocessingml.document"
            )

        storage_key = build_storage_key(
            client.code, str(batch.id), file_path.name, sha256
        )
        object_store.put(storage_key, file_bytes)

        raw_file = create_raw_file(
            session=session,
            client_id=client_id,
            ingestion_batch_id=batch.id,
            original_filename=file_path.name,
            storage_key=storage_key,
            sha256=sha256,
            size_bytes=len(file_bytes),
            mime_type=mime_type,
        )
        raw_file_ids.append(raw_file.id)

        try:
            _parse_raw_file(session, raw_file, file_bytes, file_type)
            update_raw_file_status(session, raw_file.id, FileStatus.PARSED)
            parsed_count += 1
        except Exception as exc:
            update_raw_file_status(session, raw_file.id, FileStatus.FAILED)
            raw_file.meta = {"error": str(exc)}
            session.add(raw_file)
            session.commit()
            failed_count += 1

    return FolderIngestionResult(
        client_id=client_id,
        ingestion_batch_id=batch.id,
        raw_file_ids=raw_file_ids,
        parsed_count=parsed_count,
        failed_count=failed_count,
        evidence_ids=evidence_ids,
    )


def _parse_raw_file(
    session: Session,
    raw_file: RawFile,
    file_bytes: bytes,
    file_type: str,
) -> None:
    """Parse a raw file and store profiles/evidence."""
    if file_type in {"csv", "xlsx"}:
        catalog = discover_source_tables(
            file_bytes,
            file_type,
            raw_file_id=raw_file.id,
            file_sha256=raw_file.sha256,
            original_filename=raw_file.original_filename,
        )
        create_spreadsheet_profile(
            session,
            raw_file.id,
            catalog.model_dump(mode="json"),
        )

    parsed: dict[str, Any] = {}
    if file_type == "pdf":
        parsed = parse_pdf_to_text(file_bytes)
    elif file_type == "eml":
        parsed = parse_email_to_dict(file_bytes)
    elif file_type in {"txt", "md", "docx"}:
        parsed = parse_text_document(file_bytes, raw_file.mime_type)
    else:
        parsed = {"text": "", "note": f"no text parser for {file_type}"}

    chunks = extract_evidence_chunks(parsed, str(raw_file.id))
    for chunk in chunks:
        embedding = get_embedding(chunk["content"])
        create_extracted_evidence(
            session=session,
            client_id=raw_file.client_id,
            raw_file_id=raw_file.id,
            evidence_type=chunk["evidence_type"],
            content=chunk["content"],
            embedding=embedding,
            page_ref=chunk.get("page_ref"),
            chunk_index=chunk.get("chunk_index"),
            metadata=chunk.get("metadata") or {},
        )


# ---------------------------------------------------------------------------
# Profile and evidence operations
# ---------------------------------------------------------------------------


def create_spreadsheet_profile(
    session: Session,
    raw_file_id: uuid.UUID,
    profile_json: dict[str, Any],
) -> SpreadsheetProfile:
    """Store a spreadsheet profile extracted from a raw file."""
    profile = get_spreadsheet_profile(session, raw_file_id)
    if profile is None:
        profile = SpreadsheetProfile(raw_file_id=raw_file_id, profile_json=profile_json)
    else:
        profile.profile_json = profile_json
    session.add(profile)
    session.commit()
    session.refresh(profile)
    return profile


def get_spreadsheet_profile(
    session: Session,
    raw_file_id: uuid.UUID,
) -> SpreadsheetProfile | None:
    """Fetch the spreadsheet profile for a raw file."""
    return session.exec(
        select(SpreadsheetProfile).where(SpreadsheetProfile.raw_file_id == raw_file_id)
    ).first()


def create_extracted_evidence(
    session: Session,
    client_id: uuid.UUID,
    raw_file_id: uuid.UUID,
    evidence_type: str,
    content: str,
    embedding: list[float] | None,
    page_ref: str | None,
    chunk_index: int | None,
    metadata: dict[str, Any],
) -> ExtractedEvidence:
    """Store a piece of extracted evidence and optionally its vector embedding."""
    evidence = ExtractedEvidence(
        client_id=client_id,
        raw_file_id=raw_file_id,
        evidence_type=evidence_type,
        content=content,
        embedding=embedding,
        page_ref=page_ref,
        chunk_index=chunk_index,
        meta=metadata,
    )
    session.add(evidence)
    session.commit()
    session.refresh(evidence)
    return evidence


def search_evidence(
    session: Session,
    query_embedding: list[float],
    client_id: uuid.UUID | None,
    top_k: int = 5,
) -> Sequence[ExtractedEvidence]:
    """Perform vector similarity search over extracted evidence using pgvector."""
    embedding_column = cast(Any, ExtractedEvidence.embedding)
    distance = embedding_column.cosine_distance(query_embedding)
    statement = select(ExtractedEvidence, distance.label("distance")).order_by(distance)
    if client_id:
        statement = statement.where(ExtractedEvidence.client_id == client_id)
    statement = statement.limit(top_k)
    results = session.exec(statement).all()
    return [r[0] for r in results]


def search_evidence_by_text(
    session: Session,
    query: str,
    client_id: uuid.UUID | None,
    top_k: int = 5,
) -> Sequence[ExtractedEvidence]:
    """Embed a text query and search extracted evidence by vector similarity."""
    query_embedding = get_embedding(query)
    return search_evidence(session, query_embedding, client_id, top_k=top_k)


# ---------------------------------------------------------------------------
# Business rule operations
# ---------------------------------------------------------------------------


def create_business_rule(
    session: Session,
    client_id: uuid.UUID,
    rule_text: str,
    evidence_ids: list[uuid.UUID],
    metadata: dict[str, Any] | None = None,
) -> BusinessRule:
    """Create a new business rule draft."""
    rule = BusinessRule(
        client_id=client_id,
        rule_text=rule_text,
        evidence_ids=evidence_ids,
        meta=metadata or {},
    )
    session.add(rule)
    session.commit()
    session.refresh(rule)
    return rule


def approve_business_rule(
    session: Session,
    rule_id: uuid.UUID,
    reviewer: str,
) -> BusinessRule | None:
    """Approve a business rule."""
    from datetime import datetime

    rule = session.get(BusinessRule, rule_id)
    if rule is None:
        return None
    rule.status = BusinessRuleStatus.APPROVED
    rule.approved_by = reviewer
    rule.approved_at = datetime.now(UTC)
    session.add(rule)
    session.commit()
    session.refresh(rule)
    return rule


def list_business_rules(
    session: Session,
    client_id: uuid.UUID,
    status: str | None = None,
) -> Sequence[BusinessRule]:
    """List business rules for a client, optionally filtered by status."""
    statement = select(BusinessRule).where(BusinessRule.client_id == client_id)
    if status:
        statement = statement.where(BusinessRule.status == status)
    return session.exec(statement).all()


# ---------------------------------------------------------------------------
# Mapping specification operations
# ---------------------------------------------------------------------------


def create_mapping_spec(
    session: Session,
    client_id: uuid.UUID,
    source_raw_file_ids: list[uuid.UUID],
    target_schema: TargetSchema,
    description: str | None,
) -> MappingSpec:
    """Create a new mapping specification draft."""
    spec = MappingSpec(
        client_id=client_id,
        source_raw_file_ids=[str(x) for x in source_raw_file_ids],
        target_schema_json=target_schema.model_dump(mode="json"),
        description=description,
    )
    session.add(spec)
    session.commit()
    session.refresh(spec)
    return spec


def get_mapping_spec(
    session: Session,
    mapping_spec_id: uuid.UUID,
) -> MappingSpec | None:
    """Fetch a mapping specification by UUID."""
    return session.get(MappingSpec, mapping_spec_id)


def approve_mapping_spec(
    session: Session,
    mapping_spec_id: uuid.UUID,
    reviewer: str,
    notes: str | None,
) -> MappingSpec | None:
    """Approve a proposed mapping specification."""
    from datetime import datetime

    spec = session.get(MappingSpec, mapping_spec_id)
    if spec is None:
        return None
    spec.status = MappingSpecStatus.APPROVED
    spec.approved_by = reviewer
    spec.approved_at = datetime.now(UTC)
    if notes:
        spec.description = (spec.description or "") + f"\nApproved notes: {notes}"
    session.add(spec)
    session.commit()
    session.refresh(spec)
    return spec


def update_mapping_spec_status(
    session: Session,
    mapping_spec_id: uuid.UUID,
    status: MappingSpecStatus,
) -> MappingSpec | None:
    """Update the status of a mapping specification."""
    spec = session.get(MappingSpec, mapping_spec_id)
    if spec is None:
        return None
    spec.status = status
    session.add(spec)
    session.commit()
    session.refresh(spec)
    return spec


def create_mapping_columns(
    session: Session,
    mapping_spec_id: uuid.UUID,
    columns: list[dict[str, Any]],
) -> Sequence[MappingColumn]:
    """Bulk-create column mappings within a specification."""
    records = []
    for idx, col in enumerate(columns):
        record = MappingColumn(
            mapping_spec_id=mapping_spec_id,
            target_table=col["target_table"],
            target_column=col["target_column"],
            source_columns_json=col.get("source_columns", []),
            transformation_logic=col.get("transformation_logic", ""),
            polars_expression=col.get("polars_expression"),
            transformation_type=col.get("transformation_type", "expression"),
            aggregation_source_table=col.get("aggregation_source_table"),
            aggregation_expression=col.get("aggregation_expression"),
            aggregation_group_key=col.get("aggregation_group_key"),
            lookup_source_table=col.get("lookup_source_table"),
            lookup_key=col.get("lookup_key"),
            lookup_value=col.get("lookup_value"),
            filter_expression=col.get("filter_expression"),
            tests=col.get("tests", []),
            evidence_ids=col.get("evidence_ids", []),
            business_rule_ids=col.get("business_rule_ids", []),
            sort_order=idx,
        )
        session.add(record)
        records.append(record)
    session.commit()
    for record in records:
        session.refresh(record)
    return records


def get_mapping_columns(
    session: Session,
    mapping_spec_id: uuid.UUID,
) -> Sequence[MappingColumn]:
    """Fetch all column mappings for a specification."""
    return session.exec(
        select(MappingColumn).where(MappingColumn.mapping_spec_id == mapping_spec_id)
    ).all()


def delete_mapping_columns(session: Session, mapping_spec_id: uuid.UUID) -> None:
    """Remove all column mappings for a specification."""
    for col in get_mapping_columns(session, mapping_spec_id):
        session.delete(col)
    session.commit()


# ---------------------------------------------------------------------------
# Artifact and execution operations
# ---------------------------------------------------------------------------


def create_generated_artifact(
    session: Session,
    mapping_spec_id: uuid.UUID,
    artifact_type: str,
    file_path: str,
    content: str,
    mapping_column_ids: list[uuid.UUID],
) -> GeneratedArtifact:
    """Store a generated transformation artifact."""
    artifact = GeneratedArtifact(
        mapping_spec_id=mapping_spec_id,
        artifact_type=artifact_type,
        file_path=file_path,
        content=content,
        mapping_column_ids=[str(x) for x in mapping_column_ids],
    )
    session.add(artifact)
    session.commit()
    session.refresh(artifact)
    return artifact


# ---------------------------------------------------------------------------
# Audit and lineage operations
# ---------------------------------------------------------------------------


def write_audit_log(
    session: Session,
    event_type: str,
    entity_type: str,
    entity_id: uuid.UUID,
    actor_user_id: uuid.UUID | None,
    actor: str | None,
    payload: dict[str, Any],
) -> AuditLog:
    """Append an audit event to the database."""
    log = AuditLog(
        event_type=event_type,
        entity_type=entity_type,
        entity_id=entity_id,
        actor_user_id=actor_user_id,
        actor=actor,
        payload=payload,
    )
    session.add(log)
    session.commit()
    session.refresh(log)
    return log


def record_lineage_edge(
    session: Session,
    source_type: str,
    source_id: uuid.UUID,
    target_type: str,
    target_id: uuid.UUID,
    edge_type: str,
    metadata: dict[str, Any] | None = None,
) -> None:
    """Record a single provenance edge between two entities."""
    from models import LineageEdge

    edge = LineageEdge(
        source_type=source_type,
        source_id=source_id,
        target_type=target_type,
        target_id=target_id,
        edge_type=edge_type,
        meta=metadata or {},
    )
    session.add(edge)
    session.commit()


def get_lineage_for_staging_column(
    session: Session,
    staging_column_id: uuid.UUID,
) -> dict[str, Any]:
    """Return the full provenance graph for a staging column."""
    from models import LineageEdge

    edges = session.exec(
        select(LineageEdge).where(
            (LineageEdge.source_id == staging_column_id)
            | (LineageEdge.target_id == staging_column_id)
        )
    ).all()
    return {
        "staging_column_id": staging_column_id,
        "edges": [
            {
                "source_type": e.source_type,
                "source_id": str(e.source_id),
                "target_type": e.target_type,
                "target_id": str(e.target_id),
                "edge_type": e.edge_type,
            }
            for e in edges
        ],
    }


# ---------------------------------------------------------------------------
# Result overrides
# ---------------------------------------------------------------------------


def create_result_override(
    session: Session,
    *,
    spec_id: uuid.UUID,
    run_id: uuid.UUID | None,
    target_table: str,
    target_column: str,
    row_key: str,
    value: str,
    reason: str,
    created_by: str | None = None,
) -> ResultOverride:
    """Create an audited manual override for a generated output cell."""
    record = ResultOverride(
        spec_id=spec_id,
        run_id=run_id,
        target_table=target_table,
        target_column=target_column,
        row_key=row_key,
        value=value,
        reason=reason,
        created_by=created_by,
    )
    session.add(record)
    session.commit()
    session.refresh(record)
    return record


def list_result_overrides(
    session: Session,
    spec_id: uuid.UUID,
) -> Sequence[ResultOverride]:
    """Return all overrides for a mapping spec (oldest first)."""
    records = session.exec(
        select(ResultOverride).where(ResultOverride.spec_id == spec_id)
    ).all()
    return sorted(records, key=lambda o: o.created_at)


def delete_result_override(
    session: Session,
    override_id: uuid.UUID,
    spec_id: uuid.UUID,
) -> bool:
    """Delete an override, refusing to delete overrides of another spec."""
    record = session.get(ResultOverride, override_id)
    if record is None or record.spec_id != spec_id:
        return False
    session.delete(record)
    session.commit()
    return True
