"""Canonical mapping-spec query representation.

Proposal, code generation, execution, and review all consume the dictionary
returned here.  Keeping the database-to-artifact projection in one module
prevents subtly different production representations from emerging.
"""

from __future__ import annotations

import uuid
from typing import Any

from db_ops import (
    get_mapping_columns,
    get_mapping_spec,
    get_raw_file_by_id,
    get_session,
    get_spreadsheet_profile,
)
from mapping import _normalize_polars_expression
from models import MappingColumn, TargetSchema


def _column_to_dict(column: MappingColumn) -> dict[str, Any]:
    return {
        "id": str(column.id),
        "target_table": column.target_table,
        "target_column": column.target_column,
        "source_columns": column.source_columns_json,
        "transformation_logic": column.transformation_logic,
        "polars_expression": _normalize_polars_expression(
            column.polars_expression
        ),
        "transformation_type": column.transformation_type,
        "aggregation_source_table": column.aggregation_source_table,
        "aggregation_expression": _normalize_polars_expression(
            column.aggregation_expression
        ),
        "aggregation_group_key": column.aggregation_group_key,
        "lookup_source_table": column.lookup_source_table,
        "lookup_key": column.lookup_key,
        "lookup_value": column.lookup_value,
        "filter_expression": _normalize_polars_expression(
            column.filter_expression
        ),
        "tests": column.tests,
        "evidence_ids": [str(value) for value in column.evidence_ids],
        "business_rule_ids": [
            str(value) for value in column.business_rule_ids
        ],
        "confidence": column.confidence,
        "sort_order": column.sort_order,
    }


def _normalize_catalog(
    profile_json: dict[str, Any],
    raw_file_id: uuid.UUID,
    original_filename: str,
    file_sha256: str,
    file_type: str,
) -> dict[str, Any]:
    """Ensure a profile has the SourceCatalog structure expected by generated pipelines.

    Old profiles stored a flat ``{"source_table": ..., "columns": [...]}``
    dict.  Newer profiles use the full ``SourceCatalog`` schema with a
    ``tables`` list.  This helper up-grades the old format so the generated
    ``pipeline.py`` can find source files and column metadata.
    """
    if "tables" in profile_json:
        return profile_json

    source_table = profile_json.get("source_table", "")
    location: dict[str, Any] = {}
    if file_type == "csv":
        location = {"header_row": 1, "data_start_row": 2}
    elif file_type == "xlsx":
        location = {
            "sheet_name": "Sheet1",
            "cell_range": "A1:Z10000",
            "header_row": 1,
            "data_start_row": 2,
        }

    normalized_columns: list[dict[str, Any]] = []
    for col in profile_json.get("columns", []):
        normalized_columns.append({
            "source_column_id": col.get("column", ""),
            "original_name": col.get("column", ""),
            "normalized_name": col.get("column", ""),
            "inferred_type": col.get("dtype", "String"),
            "null_count": col.get("null_count", 0),
            "null_rate": 0.0,
            "cardinality": col.get("unique_count", 0),
            "candidate_key_score": 0.0,
        })

    table = {
        "source_table_id": source_table,
        "raw_file_id": str(raw_file_id),
        "file_sha256": file_sha256,
        "original_filename": original_filename,
        "display_name": source_table or original_filename,
        "location": location,
        "row_count": profile_json.get("row_count", 0),
        "columns": normalized_columns,
        "confidence": 1.0,
    }
    return {
        "schema_version": 1,
        "raw_file_id": str(raw_file_id),
        "file_sha256": file_sha256,
        "original_filename": original_filename,
        "file_type": file_type,
        "tables": [table],
        "warnings": [],
    }


def load_mapping_spec(spec_id: uuid.UUID) -> dict[str, Any]:
    """Load a mapping specification and its columns for downstream stages."""
    with get_session() as session:
        spec = get_mapping_spec(session, spec_id)
        if spec is None:
            raise ValueError(f"Mapping spec not found: {spec_id}")
        columns = get_mapping_columns(session, spec_id)
        source_catalogs = []
        for raw_file_id in spec.source_raw_file_ids:
            profile = get_spreadsheet_profile(session, raw_file_id)
            if profile is None:
                continue
            raw_file = get_raw_file_by_id(session, raw_file_id)
            original_filename = raw_file.original_filename if raw_file else ""
            file_sha256 = raw_file.sha256 if raw_file else ""
            file_ext = (
                original_filename.rsplit(".", 1)[-1].lower()
                if "." in original_filename
                else ""
            )
            source_catalogs.append(
                _normalize_catalog(
                    profile.profile_json,
                    raw_file_id,
                    original_filename,
                    file_sha256,
                    file_ext,
                )
            )
        return {
            "id": str(spec.id),
            "client_id": str(spec.client_id),
            "version": spec.version,
            "status": spec.status.value,
            "source_raw_file_ids": [
                str(value) for value in spec.source_raw_file_ids
            ],
            "target_schema_json": spec.target_schema_json,
            "source_catalogs": source_catalogs,
            "description": spec.description,
            "approved_by": spec.approved_by,
            "columns": [_column_to_dict(column) for column in columns],
        }


def load_target_schema_from_spec(
    mapping_spec: dict[str, Any],
) -> TargetSchema:
    """Deserialize the target schema embedded in a mapping specification."""
    return TargetSchema.model_validate(mapping_spec["target_schema_json"])
