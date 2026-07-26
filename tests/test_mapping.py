"""Unit tests for the LLM-assisted mapping module."""

from __future__ import annotations

import uuid

import pytest
from mapping import (
    build_mapping_prompt,
    check_target_schema_coverage,
    parse_llm_mapping_response,
    validate_mapping_columns,
)
from models import ExtractedEvidence, ProposedMapping, SourceColumnRef, TargetSchema


def test_parse_llm_mapping_response_builds_proposed_mappings() -> None:
    """LLM JSON responses should convert into ProposedMapping objects."""
    spec_id = uuid.uuid4()
    target_schema = TargetSchema(
        client_code="test",
        name="default",
        description="test",
        tables=[
            {
                "name": "records",
                "description": "test table",
                "columns": [
                    {"name": "customer_id", "dtype": "Int64", "required": True},
                    {"name": "total_spend", "dtype": "Float64"},
                ],
            }
        ],
    )
    raw_file_id = uuid.uuid4()
    response = {
        "mappings": [
            {
                "target_table": "records",
                "target_column": "customer_id",
                "source_columns": [
                    {
                        "source_table_id": "table-1",
                        "source_column_id": "column-1",
                        "raw_file_id": str(raw_file_id),
                        "source_table": "data",
                        "source_column": "cust_id",
                    }
                ],
                "transformation_logic": "Direct map",
                "polars_expression": None,
                "tests": ["not_null"],
                "evidence_ids": [str(uuid.uuid4())],
                "business_rule_ids": [],
                "confidence": 0.95,
            }
        ]
    }

    mappings = parse_llm_mapping_response(response, spec_id, target_schema)

    assert len(mappings) == 1
    mapping = mappings[0]
    assert mapping.target_table == "records"
    assert mapping.target_column == "customer_id"
    assert mapping.source_columns[0] == SourceColumnRef(
        source_table_id="table-1",
        source_column_id="column-1",
        raw_file_id=raw_file_id,
        source_table="data",
        source_column="cust_id",
    )
    assert mapping.confidence == 0.95


def test_parse_llm_mapping_response_requires_target_fields() -> None:
    """LLM responses missing required fields should raise validation errors."""
    response = {"mappings": [{"target_table": "records"}]}
    with pytest.raises((KeyError, ValueError)):
        parse_llm_mapping_response(
            response,
            uuid.uuid4(),
            TargetSchema(client_code="test", name="default", tables=[]),
        )


def test_validate_mapping_columns_reports_missing_sources() -> None:
    """Validation should flag source columns that do not exist in profiles."""
    target_schema = TargetSchema(
        client_code="test",
        name="default",
        tables=[
            {
                "name": "records",
                "description": "",
                "columns": [
                    {"name": "customer_id", "dtype": "Int64", "required": True}
                ],
            }
        ],
    )
    mappings = [
        ProposedMapping(
            target_table="records",
            target_column="customer_id",
            source_columns=[
                SourceColumnRef(source_table="data", source_column="missing_col")
            ],
        )
    ]
    profiles = [
        {
            "source_table": "data",
            "columns": [{"column": "cust_id", "inferred_type": "integer"}],
        }
    ]

    results = validate_mapping_columns(mappings, target_schema, profiles)

    assert len(results) == 1
    assert any("missing_col" in err for err in results[0]["validation_errors"])


def test_validate_mapping_columns_accepts_catalog_ids() -> None:
    """Catalog-grounded references should resolve by stable IDs."""
    raw_file_id = uuid.uuid4()
    target_schema = TargetSchema(
        client_code="test",
        tables=[
            {
                "name": "records",
                "columns": [{"name": "customer_id", "dtype": "Int64"}],
            }
        ],
    )
    mappings = [
        ProposedMapping(
            target_table="records",
            target_column="customer_id",
            source_columns=[
                SourceColumnRef(
                    source_table_id="table-1",
                    source_column_id="column-1",
                    raw_file_id=raw_file_id,
                    source_table="Customers",
                    source_column="Customer ID",
                )
            ],
        )
    ]
    catalogs = [
        {
            "schema_version": 1,
            "tables": [
                {
                    "source_table_id": "table-1",
                    "raw_file_id": str(raw_file_id),
                    "columns": [
                        {
                            "source_column_id": "column-1",
                            "original_name": "Customer ID",
                        }
                    ],
                }
            ],
        }
    ]

    results = validate_mapping_columns(mappings, target_schema, catalogs)

    assert results[0]["validation_errors"] == []


def test_validate_mapping_columns_rejects_cross_table_column_id() -> None:
    """A real column ID cannot be paired with the wrong source table ID."""
    raw_file_id = uuid.uuid4()
    target_schema = TargetSchema(
        client_code="test",
        tables=[{"name": "records", "columns": [{"name": "customer_id"}]}],
    )
    mapping = ProposedMapping(
        target_table="records",
        target_column="customer_id",
        source_columns=[
            SourceColumnRef(
                source_table_id="table-2",
                source_column_id="column-1",
                raw_file_id=raw_file_id,
                source_table="Wrong table",
                source_column="Customer ID",
            )
        ],
    )
    catalogs = [
        {
            "tables": [
                {
                    "source_table_id": "table-1",
                    "raw_file_id": str(raw_file_id),
                    "columns": [{"source_column_id": "column-1"}],
                },
                {
                    "source_table_id": "table-2",
                    "raw_file_id": str(raw_file_id),
                    "columns": [{"source_column_id": "column-2"}],
                },
            ]
        }
    ]

    results = validate_mapping_columns([mapping], target_schema, catalogs)

    assert any(
        "does not belong" in error for error in results[0]["validation_errors"]
    )


def test_check_target_schema_coverage_reports_missing_columns() -> None:
    """Coverage check should separate required and optional missing columns."""
    target_schema = TargetSchema(
        client_code="test",
        name="default",
        tables=[
            {
                "name": "records",
                "description": "",
                "columns": [
                    {"name": "customer_id", "dtype": "Int64", "required": True},
                    {"name": "total_spend", "dtype": "Float64"},
                ],
            }
        ],
    )
    mappings = [
        ProposedMapping(
            target_table="records",
            target_column="customer_id",
            source_columns=[
                SourceColumnRef(source_table="data", source_column="cust_id")
            ],
        )
    ]

    coverage = check_target_schema_coverage(mappings, target_schema)

    assert coverage["covered_required"] == [("records", "customer_id")]
    assert coverage["missing_optional"] == [("records", "total_spend")]


def test_build_mapping_prompt_includes_evidence_ids() -> None:
    """The prompt should expose evidence IDs so the LLM can cite them."""
    target_schema = TargetSchema(
        client_code="test",
        name="default",
        tables=[
            {
                "name": "records",
                "description": "",
                "columns": [
                    {"name": "customer_id", "dtype": "Int64", "required": True}
                ],
            }
        ],
    )
    evidence_id = uuid.uuid4()
    evidence = [
        ExtractedEvidence(
            id=evidence_id,
            client_id=uuid.uuid4(),
            raw_file_id=uuid.uuid4(),
            evidence_type="text_chunk",
            content="Map cust_id to customer_id.",
        )
    ]

    raw_file_id = uuid.uuid4()
    catalogs = [
        {
            "schema_version": 1,
            "tables": [
                {
                    "source_table_id": "table-1",
                    "raw_file_id": str(raw_file_id),
                    "columns": [{"source_column_id": "column-1"}],
                }
            ],
        }
    ]
    messages = build_mapping_prompt(
        target_schema, catalogs, evidence, [], [{"filename": "data.csv"}]
    )

    user_content = messages[1]["content"]
    assert str(evidence_id) in user_content
    assert "Evidence ID" in user_content
    assert "Canonical source catalogs" in user_content
    assert "source_table_id" in user_content
    assert "source_column_id" in user_content
