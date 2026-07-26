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
    get_session,
    get_spreadsheet_profile,
)
from models import MappingColumn, TargetSchema


def _column_to_dict(column: MappingColumn) -> dict[str, Any]:
    return {
        "id": str(column.id),
        "target_table": column.target_table,
        "target_column": column.target_column,
        "source_columns": column.source_columns_json,
        "transformation_logic": column.transformation_logic,
        "polars_expression": column.polars_expression,
        "transformation_type": column.transformation_type,
        "aggregation_source_table": column.aggregation_source_table,
        "aggregation_expression": column.aggregation_expression,
        "aggregation_group_key": column.aggregation_group_key,
        "lookup_source_table": column.lookup_source_table,
        "lookup_key": column.lookup_key,
        "lookup_value": column.lookup_value,
        "filter_expression": column.filter_expression,
        "tests": column.tests,
        "evidence_ids": [str(value) for value in column.evidence_ids],
        "business_rule_ids": [
            str(value) for value in column.business_rule_ids
        ],
        "confidence": column.confidence,
        "sort_order": column.sort_order,
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
            if profile is not None:
                source_catalogs.append(profile.profile_json)
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
