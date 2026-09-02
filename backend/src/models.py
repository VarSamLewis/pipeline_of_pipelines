"""Domain models for the auditable data-transformation platform.

This module defines the shared Pydantic and SQLModel schemas used across the
ingestion, parsing, mapping, code-generation, and execution layers. All models
are designed to support immutable raw-file storage, versioned mapping specs,
and full column-level lineage.
"""

from __future__ import annotations

import datetime
import enum
import uuid
from pathlib import Path
from typing import Any

from pgvector.sqlalchemy import Vector
from pydantic import BaseModel, Field
from sqlmodel import JSON, Column, SQLModel, Text
from sqlmodel import Field as SQLField

# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------


class FileStatus(str, enum.Enum):
    """Lifecycle states for a raw file."""

    RECEIVED = "received"
    PARSING = "parsing"
    PARSED = "parsed"
    FAILED = "failed"
    ARCHIVED = "archived"


class MappingSpecStatus(str, enum.Enum):
    """Lifecycle states for a mapping specification version."""

    DRAFT = "draft"
    PROPOSED = "proposed"
    APPROVED = "approved"
    REJECTED = "rejected"
    SUPERSEDED = "superseded"


class BusinessRuleStatus(str, enum.Enum):
    """Lifecycle states for a business rule."""

    DRAFT = "draft"
    APPROVED = "approved"
    DEPRECATED = "deprecated"


class ExecutionStatus(str, enum.Enum):
    """Lifecycle states for an execution run."""

    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    PARTIAL = "partial"
    FAILED = "failed"


class UserRole(str, enum.Enum):
    """Authorization roles enforced by the Entra ID-backed auth layer."""

    CREATOR = "creator"
    REVIEWER = "reviewer"
    APPROVER = "approver"
    ADMIN = "admin"


# ---------------------------------------------------------------------------
# Pydantic schema models
# ---------------------------------------------------------------------------


class TargetSchemaColumn(BaseModel):
    """A single column inside a target schema table."""

    name: str = Field(..., description="Target column name.")
    dtype: str | None = Field(
        default=None,
        description="Expected Polars dtype, e.g. 'String', 'Int64', 'Float64'.",
    )
    description: str | None = Field(
        default=None,
        description="Human-readable meaning of the column.",
    )
    required: bool = Field(
        default=False, description="Whether the column must be present."
    )
    unique: bool = Field(default=False, description="Whether values must be unique.")
    allowed_values: list[str] | None = Field(
        default=None,
        description="Optional enumeration of allowed values.",
    )


class TargetSchemaTable(BaseModel):
    """A target schema table definition."""

    name: str = Field(..., description="Target table name.")
    description: str | None = Field(
        default=None,
        description="Human-readable meaning of the table.",
    )
    columns: list[TargetSchemaColumn] = Field(
        default_factory=list,
        description="Expected columns for this target table.",
    )


class TargetSchema(BaseModel):
    """Supplied target schema loaded from a JSON file.

    The target schema is the shape the client wants their data transformed
    into. It is loaded from a JSON file for now and is always treated as the
    latest version for a client.
    """

    client_code: str = Field(..., description="Short code of the owning client.")
    name: str = Field(default="default", description="Schema name.")
    description: str | None = Field(default=None)
    tables: list[TargetSchemaTable] = Field(
        default_factory=list,
        description="Target tables to map source data into.",
    )


class ParseWarning(BaseModel):
    """A structured, reviewable warning emitted during source discovery."""

    code: str
    message: str
    severity: str = Field(default="warning", pattern="^(info|warning|error)$")
    location: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)


class SourceLocation(BaseModel):
    """Physical location and parser settings for a discovered source table."""

    sheet_name: str | None = None
    cell_range: str | None = None
    header_row: int
    data_start_row: int
    encoding: str | None = None
    delimiter: str | None = None
    quote_char: str | None = None
    newline: str | None = None


class SourceColumn(BaseModel):
    """Profile of one column in a discovered source table."""

    source_column_id: str
    ordinal: int
    original_name: str
    normalized_name: str
    inferred_type: str
    examples: list[str] = Field(default_factory=list)
    null_count: int
    null_rate: float
    cardinality: int
    candidate_key_score: float


class SourceTable(BaseModel):
    """Canonical description and profile of one discovered tabular region."""

    source_table_id: str
    raw_file_id: uuid.UUID | None = None
    file_sha256: str
    original_filename: str | None = None
    display_name: str
    location: SourceLocation
    row_count: int
    columns: list[SourceColumn] = Field(default_factory=list)
    confidence: float = Field(ge=0.0, le=1.0)
    warnings: list[ParseWarning] = Field(default_factory=list)


class SourceCatalog(BaseModel):
    """Versioned catalog of every table discovered in one raw source file."""

    schema_version: int = 1
    raw_file_id: uuid.UUID | None = None
    file_sha256: str
    original_filename: str | None = None
    file_type: str
    tables: list[SourceTable] = Field(default_factory=list)
    warnings: list[ParseWarning] = Field(default_factory=list)


class SourceColumnRef(BaseModel):
    """Reference to one source column used in a mapping."""

    source_table_id: str | None = Field(
        default=None,
        description="Stable identifier from the canonical source catalog.",
    )
    source_column_id: str | None = Field(
        default=None,
        description="Stable column identifier from the canonical source catalog.",
    )
    raw_file_id: uuid.UUID | None = Field(
        default=None,
        description="UUID of the source raw file when known.",
    )
    source_table: str = Field(..., description="Logical source table/sheet name.")
    source_column: str = Field(..., description="Source column name or letter.")


class ProposedMapping(BaseModel):
    """A single column-level mapping proposed by the LLM.

    Supports multi-to-one mappings: several source columns can feed a single
    target column via one Polars expression.
    """

    target_table: str = Field(..., description="Target staging table name.")
    target_column: str = Field(..., description="Target staging column name.")
    source_columns: list[SourceColumnRef] = Field(
        default_factory=list,
        description="One or more source columns feeding this target column.",
    )
    transformation_logic: str = Field(
        default="",
        description="Description or expression of the transformation.",
    )
    polars_expression: str | None = Field(
        default=None,
        description="Polars expression string to compute the target column.",
    )
    transformation_type: str = Field(
        default="expression",
        description="Type of transformation: expression, aggregation, lookup, filter.",
    )
    aggregation_source_table: str | None = Field(
        default=None,
        description="Source table to aggregate for aggregation transformations.",
    )
    aggregation_expression: str | None = Field(
        default=None,
        description="Polars aggregation expression, e.g. pl.col('qty').sum().",
    )
    aggregation_group_key: str | None = Field(
        default=None,
        description="Column to group by when aggregating, e.g. 'cust_id'.",
    )
    lookup_source_table: str | None = Field(
        default=None,
        description="Source table to look up values from for lookup transformations.",
    )
    lookup_key: str | None = Field(
        default=None,
        description="Join key for lookup transformations.",
    )
    lookup_value: str | None = Field(
        default=None,
        description="Value column to return from lookup transformations.",
    )
    filter_expression: str | None = Field(
        default=None,
        description="Polars filter expression to apply before mapping.",
    )
    tests: list[str] = Field(
        default_factory=list,
        description="Validation tests for the column.",
    )
    evidence_ids: list[uuid.UUID] = Field(
        default_factory=list,
        description="Evidence items cited for this mapping.",
    )
    business_rule_ids: list[uuid.UUID] = Field(
        default_factory=list,
        description="Business rules cited for this mapping.",
    )


class PipelineOutputFolder(BaseModel):
    """Paths to the deliverables produced for a client batch.

    A client-supplied folder + target schema produces an output folder containing:
    - pipeline.py: single-file Polars transformation script
    - mapping.json: human- and machine-readable mapping specification
    - results.csv: CSV output from executing pipeline.py against the source data
    """

    folder_path: Path
    pipeline_py_path: Path
    mapping_json_path: Path
    results_csv_path: Path
    generated_at: datetime.datetime


class GeneratedPipelineScript(BaseModel):
    """A generated single-file Polars pipeline script and its metadata."""

    file_path: Path
    content: str
    target_tables: list[str]


class MappingFile(BaseModel):
    """The mapping file written alongside the generated pipeline."""

    file_path: Path
    content: dict[str, Any]


class FolderIngestionResult(BaseModel):
    """Summary returned after ingesting a client folder of heterogeneous files."""

    client_id: uuid.UUID
    ingestion_batch_id: uuid.UUID
    raw_file_ids: list[uuid.UUID]
    parsed_count: int
    failed_count: int
    evidence_ids: list[uuid.UUID]


# ---------------------------------------------------------------------------
# SQLModel table definitions (Postgres + pgvector)
# ---------------------------------------------------------------------------


class Client(SQLModel, table=True):
    """A tenant/client onboarded into the platform."""

    id: uuid.UUID = SQLField(default_factory=uuid.uuid4, primary_key=True)
    name: str = SQLField(index=True)
    code: str = SQLField(unique=True, index=True)
    meta: dict[str, Any] = SQLField(default={}, sa_column=Column("meta", JSON))
    created_at: datetime.datetime = SQLField(
        default_factory=datetime.datetime.utcnow,
    )


class IngestionBatch(SQLModel, table=True):
    """A logical grouping of raw files uploaded together."""

    id: uuid.UUID = SQLField(default_factory=uuid.uuid4, primary_key=True)
    client_id: uuid.UUID = SQLField(foreign_key="client.id", index=True)
    label: str | None = SQLField(default=None)
    meta: dict[str, Any] = SQLField(default={}, sa_column=Column("meta", JSON))
    created_at: datetime.datetime = SQLField(
        default_factory=datetime.datetime.utcnow,
    )


class RawFile(SQLModel, table=True):
    """Immutable record of a raw file stored in the object store."""

    id: uuid.UUID = SQLField(default_factory=uuid.uuid4, primary_key=True)
    client_id: uuid.UUID = SQLField(foreign_key="client.id", index=True)
    ingestion_batch_id: uuid.UUID = SQLField(
        foreign_key="ingestionbatch.id",
        index=True,
    )
    original_filename: str
    storage_key: str = SQLField(index=True)
    sha256: str = SQLField(index=True)
    size_bytes: int
    mime_type: str
    status: FileStatus = SQLField(default=FileStatus.RECEIVED)
    meta: dict[str, Any] = SQLField(default={}, sa_column=Column("meta", JSON))
    uploaded_at: datetime.datetime = SQLField(
        default_factory=datetime.datetime.utcnow,
    )


class SpreadsheetProfile(SQLModel, table=True):
    """Profiling metadata extracted from a spreadsheet raw file."""

    id: uuid.UUID = SQLField(default_factory=uuid.uuid4, primary_key=True)
    raw_file_id: uuid.UUID = SQLField(foreign_key="rawfile.id", index=True)
    profile_json: dict[str, Any] = SQLField(sa_column=Column(JSON))
    created_at: datetime.datetime = SQLField(
        default_factory=datetime.datetime.utcnow,
    )


class ExtractedEvidence(SQLModel, table=True):
    """Searchable evidence extracted from raw files."""

    id: uuid.UUID = SQLField(default_factory=uuid.uuid4, primary_key=True)
    client_id: uuid.UUID = SQLField(
        foreign_key="client.id",
        index=True,
        description="Owning client; allows tenant filtering without joining RawFile.",
    )
    raw_file_id: uuid.UUID = SQLField(foreign_key="rawfile.id", index=True)
    evidence_type: str = SQLField(
        description="Type: text_chunk, table, kv_pair, email_header, etc.",
    )
    content: str = SQLField(sa_column=Column(Text))
    embedding: list[float] | None = SQLField(
        default=None,
        sa_column=Column("embedding", Vector(1536)),
    )
    page_ref: str | None = SQLField(default=None)
    chunk_index: int | None = SQLField(default=None)
    meta: dict[str, Any] = SQLField(default={}, sa_column=Column("meta", JSON))
    created_at: datetime.datetime = SQLField(
        default_factory=datetime.datetime.utcnow,
    )


class BusinessRule(SQLModel, table=True):
    """Approved business rule with version and evidence linkage."""

    id: uuid.UUID = SQLField(default_factory=uuid.uuid4, primary_key=True)
    client_id: uuid.UUID = SQLField(foreign_key="client.id", index=True)
    rule_text: str = SQLField(sa_column=Column(Text))
    status: BusinessRuleStatus = SQLField(default=BusinessRuleStatus.DRAFT)
    version: int = SQLField(default=1)
    evidence_ids: list[uuid.UUID] = SQLField(
        default_factory=list,
        sa_column=Column(JSON),
    )
    approved_by: str | None = SQLField(default=None)
    approved_at: datetime.datetime | None = SQLField(default=None)
    meta: dict[str, Any] = SQLField(default={}, sa_column=Column("meta", JSON))
    created_at: datetime.datetime = SQLField(
        default_factory=datetime.datetime.utcnow,
    )


class MappingSpec(SQLModel, table=True):
    """Versioned, human-approved source-to-target mapping contract."""

    id: uuid.UUID = SQLField(default_factory=uuid.uuid4, primary_key=True)
    client_id: uuid.UUID = SQLField(foreign_key="client.id", index=True)
    version: int = SQLField(default=1)
    status: MappingSpecStatus = SQLField(default=MappingSpecStatus.DRAFT)
    source_raw_file_ids: list[uuid.UUID] = SQLField(
        default_factory=list,
        sa_column=Column(JSON),
    )
    target_schema_json: dict[str, Any] = SQLField(
        default={},
        sa_column=Column(JSON),
        description="Snapshot of the supplied target schema at spec creation time.",
    )
    description: str | None = SQLField(default=None, sa_column=Column(Text))
    approved_by: str | None = SQLField(default=None)
    approved_at: datetime.datetime | None = SQLField(default=None)
    created_at: datetime.datetime = SQLField(
        default_factory=datetime.datetime.utcnow,
    )


class MappingColumn(SQLModel, table=True):
    """Granular column-level mapping entry within a mapping specification."""

    id: uuid.UUID = SQLField(default_factory=uuid.uuid4, primary_key=True)
    mapping_spec_id: uuid.UUID = SQLField(
        foreign_key="mappingspec.id",
        index=True,
    )
    target_table: str
    target_column: str
    source_columns_json: list[dict[str, Any]] = SQLField(
        default_factory=list,
        sa_column=Column(JSON),
        description="List of source column references; supports multi-to-one mappings.",
    )
    transformation_logic: str = SQLField(default="", sa_column=Column(Text))
    polars_expression: str | None = SQLField(default=None, sa_column=Column(Text))
    transformation_type: str = SQLField(default="expression")
    aggregation_source_table: str | None = SQLField(default=None)
    aggregation_expression: str | None = SQLField(default=None, sa_column=Column(Text))
    aggregation_group_key: str | None = SQLField(default=None)
    lookup_source_table: str | None = SQLField(default=None)
    lookup_key: str | None = SQLField(default=None)
    lookup_value: str | None = SQLField(default=None)
    filter_expression: str | None = SQLField(default=None, sa_column=Column(Text))
    tests: list[str] = SQLField(default_factory=list, sa_column=Column(JSON))
    evidence_ids: list[uuid.UUID] = SQLField(
        default_factory=list,
        sa_column=Column(JSON),
    )
    business_rule_ids: list[uuid.UUID] = SQLField(
        default_factory=list,
        sa_column=Column(JSON),
    )
    sort_order: int = SQLField(default=0)


class GeneratedArtifact(SQLModel, table=True):
    """Artifact generated from an approved mapping specification."""

    id: uuid.UUID = SQLField(default_factory=uuid.uuid4, primary_key=True)
    mapping_spec_id: uuid.UUID = SQLField(
        foreign_key="mappingspec.id",
        index=True,
    )
    artifact_type: str
    file_path: str
    content: str = SQLField(sa_column=Column(Text))
    mapping_column_ids: list[uuid.UUID] = SQLField(
        default_factory=list,
        sa_column=Column(JSON),
    )
    generated_at: datetime.datetime = SQLField(
        default_factory=datetime.datetime.utcnow,
    )


class ExecutionRun(SQLModel, table=True):
    """A run of generated artifacts against the target environment."""

    id: uuid.UUID = SQLField(default_factory=uuid.uuid4, primary_key=True)
    client_id: uuid.UUID = SQLField(
        foreign_key="client.id",
        index=True,
        description="Owning client for direct tenant filtering.",
    )
    mapping_spec_id: uuid.UUID = SQLField(
        foreign_key="mappingspec.id",
        index=True,
    )
    artifact_set_id: uuid.UUID | None = SQLField(
        default=None,
        foreign_key="generatedartifact.id",
    )
    target_environment: str = SQLField(default="local")
    status: ExecutionStatus = SQLField(default=ExecutionStatus.PENDING)
    logs: dict[str, Any] = SQLField(default={}, sa_column=Column(JSON))
    started_at: datetime.datetime | None = SQLField(default=None)
    finished_at: datetime.datetime | None = SQLField(default=None)


class ValidationResult(SQLModel, table=True):
    """Per-test validation result captured during an execution run."""

    id: uuid.UUID = SQLField(default_factory=uuid.uuid4, primary_key=True)
    execution_run_id: uuid.UUID = SQLField(
        foreign_key="executionrun.id",
        index=True,
    )
    mapping_column_id: uuid.UUID | None = SQLField(
        default=None,
        foreign_key="mappingcolumn.id",
    )
    test_name: str
    severity: str
    passed: bool
    details: dict[str, Any] = SQLField(default={}, sa_column=Column(JSON))
    recorded_at: datetime.datetime = SQLField(
        default_factory=datetime.datetime.utcnow,
    )


class ResultOverride(SQLModel, table=True):
    """Audited manual override of a generated output cell that survives re-runs.

    Keyed by spec + column + row key so a re-execution of the same mapping spec
    keeps the override. The reason is required so every override is auditable.
    """

    id: uuid.UUID = SQLField(default_factory=uuid.uuid4, primary_key=True)
    spec_id: uuid.UUID = SQLField(foreign_key="mappingspec.id", index=True)
    run_id: uuid.UUID | None = SQLField(default=None, index=True)
    target_table: str = SQLField(default="results")
    target_column: str = SQLField(index=True)
    row_key: str = SQLField(index=True)
    value: str = SQLField(default="")
    reason: str = SQLField(default="")
    created_by: str | None = SQLField(default=None)
    created_at: datetime.datetime = SQLField(
        default_factory=datetime.datetime.utcnow,
    )


class StagingTable(SQLModel, table=True):
    """A curated staging table published by an execution run."""

    id: uuid.UUID = SQLField(default_factory=uuid.uuid4, primary_key=True)
    execution_run_id: uuid.UUID = SQLField(
        foreign_key="executionrun.id",
        index=True,
    )
    table_name: str
    row_count: int | None = SQLField(default=None)
    published_at: datetime.datetime = SQLField(
        default_factory=datetime.datetime.utcnow,
    )


class StagingColumn(SQLModel, table=True):
    """A column within a published staging table."""

    id: uuid.UUID = SQLField(default_factory=uuid.uuid4, primary_key=True)
    staging_table_id: uuid.UUID = SQLField(
        foreign_key="stagingtable.id",
        index=True,
    )
    mapping_column_id: uuid.UUID | None = SQLField(
        default=None,
        foreign_key="mappingcolumn.id",
    )
    column_name: str
    polars_dtype: str | None = SQLField(default=None)
    null_count: int | None = SQLField(default=None)
    unique_count: int | None = SQLField(default=None)


class User(SQLModel, table=True):
    """Platform user provisioned from Microsoft Entra ID.

    Entra ID remains the source of truth for identity and role (app roles) data.
    The local record caches the latest role for fast permission checks and audit
    lineage.
    """

    id: uuid.UUID = SQLField(default_factory=uuid.uuid4, primary_key=True)
    external_user_id: str = SQLField(unique=True, index=True)
    email: str = SQLField(index=True)
    name: str | None = SQLField(default=None)
    role: UserRole = SQLField(default=UserRole.CREATOR)
    last_login_at: datetime.datetime | None = SQLField(default=None)
    created_at: datetime.datetime = SQLField(
        default_factory=datetime.datetime.utcnow,
    )


class AuditLog(SQLModel, table=True):
    """Append-only audit log of all significant platform events."""

    id: uuid.UUID = SQLField(default_factory=uuid.uuid4, primary_key=True)
    event_type: str = SQLField(index=True)
    entity_type: str = SQLField(index=True)
    entity_id: uuid.UUID = SQLField(index=True)
    actor_user_id: uuid.UUID | None = SQLField(
        default=None,
        foreign_key="user.id",
        index=True,
    )
    actor: str | None = SQLField(default=None)
    payload: dict[str, Any] = SQLField(default={}, sa_column=Column("payload", JSON))
    recorded_at: datetime.datetime = SQLField(
        default_factory=datetime.datetime.utcnow,
    )


class LineageEdge(SQLModel, table=True):
    """Generic lineage edge connecting two entities in the provenance graph."""

    id: uuid.UUID = SQLField(default_factory=uuid.uuid4, primary_key=True)
    source_type: str = SQLField(index=True)
    source_id: uuid.UUID = SQLField(index=True)
    target_type: str = SQLField(index=True)
    target_id: uuid.UUID = SQLField(index=True)
    edge_type: str = SQLField(
        description="Relationship type: derived_from, approved_by, tested_by, etc.",
    )
    meta: dict[str, Any] = SQLField(default={}, sa_column=Column("meta", JSON))
    recorded_at: datetime.datetime = SQLField(
        default_factory=datetime.datetime.utcnow,
    )


# Rebuild Pydantic models that reference Path so forward references resolve.
PipelineOutputFolder.model_rebuild()
GeneratedPipelineScript.model_rebuild()
MappingFile.model_rebuild()
