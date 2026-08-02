"""Unit tests for the LLM-assisted mapping module."""

from __future__ import annotations

import uuid

import pytest
from mapping import (
    _build_catalog_index,
    _build_table_index,
    build_mapping_prompt,
    check_target_schema_coverage,
    parse_llm_mapping_response,
    resolve_composite_keys,
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


def test_parse_llm_mapping_response_requires_target_fields() -> None:
    """LLM responses missing required fields should raise validation errors."""
    response = {"mappings": [{"target_table": "records"}]}
    with pytest.raises((KeyError, ValueError)):
        parse_llm_mapping_response(
            response,
            uuid.uuid4(),
            TargetSchema(client_code="test", name="default", tables=[]),
        )


def test_resolve_composite_keys_populates_ids() -> None:
    """Resolver should populate source_table_id and source_column_id."""
    raw_file_id = uuid.uuid4()
    catalogs = [
        {
            "tables": [
                {
                    "source_table_id": "table-1",
                    "raw_file_id": str(raw_file_id),
                    "original_filename": "data.csv",
                    "display_name": "Orders",
                    "columns": [
                        {
                            "source_column_id": "column-1",
                            "normalized_name": "revenue",
                        }
                    ],
                }
            ]
        }
    ]
    mappings = [
        ProposedMapping(
            target_table="records",
            target_column="revenue",
            source_columns=[
                SourceColumnRef(
                    source_table="data.csv::Orders",
                    source_column="revenue",
                )
            ],
        )
    ]

    errors = resolve_composite_keys(mappings, catalogs)

    assert errors == []
    ref = mappings[0].source_columns[0]
    assert ref.source_table_id == "table-1"
    assert ref.source_column_id == "column-1"
    assert ref.raw_file_id == raw_file_id
    assert ref.source_table == "Orders"
    assert ref.source_column == "revenue"


def test_resolve_composite_keys_case_insensitive_column() -> None:
    """Resolver should match columns case-insensitively."""
    catalogs = [
        {
            "tables": [
                {
                    "source_table_id": "t1",
                    "original_filename": "data.csv",
                    "display_name": "Sheet1",
                    "columns": [
                        {
                            "source_column_id": "c1",
                            "normalized_name": "CustomerID",
                        }
                    ],
                }
            ]
        }
    ]
    mappings = [
        ProposedMapping(
            target_table="out",
            target_column="id",
            source_columns=[
                SourceColumnRef(
                    source_table="data.csv::Sheet1",
                    source_column="customerid",
                )
            ],
        )
    ]

    errors = resolve_composite_keys(mappings, catalogs)

    assert errors == []
    assert mappings[0].source_columns[0].source_column_id == "c1"


def test_resolve_composite_keys_display_name_fallback() -> None:
    """Resolver should fall back to display_name when composite key fails."""
    catalogs = [
        {
            "tables": [
                {
                    "source_table_id": "t1",
                    "original_filename": "data.csv",
                    "display_name": "Orders",
                    "columns": [
                        {
                            "source_column_id": "c1",
                            "normalized_name": "Revenue",
                        }
                    ],
                }
            ]
        }
    ]
    mappings = [
        ProposedMapping(
            target_table="out",
            target_column="rev",
            source_columns=[
                SourceColumnRef(
                    source_table="Orders",
                    source_column="Revenue",
                )
            ],
        )
    ]

    errors = resolve_composite_keys(mappings, catalogs)

    assert errors == []
    assert mappings[0].source_columns[0].source_table_id == "t1"
    assert mappings[0].source_columns[0].source_column_id == "c1"


def test_resolve_composite_keys_errors_on_missing_column() -> None:
    """Resolver should return an error when a column cannot be resolved."""
    catalogs = [
        {
            "tables": [
                {
                    "source_table_id": "t1",
                    "original_filename": "data.csv",
                    "display_name": "Orders",
                    "columns": [
                        {
                            "source_column_id": "c1",
                            "normalized_name": "Revenue",
                        }
                    ],
                }
            ]
        }
    ]
    mappings = [
        ProposedMapping(
            target_table="out",
            target_column="rev",
            source_columns=[
                SourceColumnRef(
                    source_table="data.csv::Orders",
                    source_column="MissingCol",
                )
            ],
        )
    ]

    errors = resolve_composite_keys(mappings, catalogs)

    assert len(errors) == 1
    assert "MissingCol" in errors[0]


def test_resolve_composite_keys_resolves_lookup_table() -> None:
    """Resolver should resolve lookup_source_table composite keys to source_table_id."""
    catalogs = [
        {
            "tables": [
                {
                    "source_table_id": "base-tbl",
                    "original_filename": "data.csv",
                    "display_name": "Main",
                    "columns": [
                        {
                            "source_column_id": "bc1",
                            "normalized_name": "code",
                        }
                    ],
                },
                {
                    "source_table_id": "lookup-tbl",
                    "original_filename": "lookup.csv",
                    "display_name": "Lookups",
                    "columns": [
                        {
                            "source_column_id": "lc1",
                            "normalized_name": "code",
                        }
                    ],
                },
            ]
        }
    ]
    mappings = [
        ProposedMapping(
            target_table="out",
            target_column="name",
            source_columns=[
                SourceColumnRef(
                    source_table="data.csv::Main",
                    source_column="code",
                )
            ],
            transformation_type="lookup",
            lookup_source_table="lookup.csv::Lookups",
            lookup_key="code",
            lookup_value="name",
        )
    ]

    errors = resolve_composite_keys(mappings, catalogs)

    assert errors == []
    assert mappings[0].lookup_source_table == "lookup-tbl"


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


def test_build_mapping_prompt_includes_composite_keys() -> None:
    """The prompt should expose composite keys so the LLM can reference sources."""
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
                    "original_filename": "data.csv",
                    "display_name": "Customers",
                    "columns": [
                        {
                            "source_column_id": "column-1",
                            "normalized_name": "cust_id",
                        }
                    ],
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
    assert "Available source columns" in user_content
    assert "data.csv::Customers::cust_id" in user_content


def test_build_catalog_index_builds_correct_keys() -> None:
    """Catalog index should map composite keys to (table, column) tuples."""
    catalogs = [
        {
            "tables": [
                {
                    "original_filename": "data.csv",
                    "display_name": "Orders",
                    "columns": [
                        {"normalized_name": "Revenue"},
                        {"normalized_name": "Qty"},
                    ],
                }
            ]
        }
    ]

    index = _build_catalog_index(catalogs)

    assert "data.csv::Orders::Revenue" in index
    assert "data.csv::Orders::Qty" in index
    assert index["data.csv::Orders::Revenue"][1]["normalized_name"] == "Revenue"


def test_build_table_index_builds_correct_keys() -> None:
    """Table index should map composite keys to table dicts."""
    catalogs = [
        {
            "tables": [
                {
                    "source_table_id": "t1",
                    "original_filename": "data.csv",
                    "display_name": "Orders",
                }
            ]
        }
    ]

    index = _build_table_index(catalogs)

    assert "data.csv::Orders" in index
    assert index["data.csv::Orders"]["source_table_id"] == "t1"


def test_validate_lookup_accepts_valid_key() -> None:
    """Valid lookup keys should produce no errors."""
    target_schema = TargetSchema(
        client_code="test",
        tables=[{"name": "out", "columns": [{"name": "material"}]}],
    )
    mapping = ProposedMapping(
        target_table="out",
        target_column="material",
        source_columns=[
            SourceColumnRef(
                source_table_id="base-tbl",
                source_column_id="col-1",
                source_table="Main",
                source_column="code",
            )
        ],
        transformation_type="lookup",
        lookup_source_table="lookup-tbl",
        lookup_key="internal_code",
        lookup_value="material_name",
    )
    catalogs = [
        {
            "tables": [
                {
                    "source_table_id": "base-tbl",
                    "columns": [{"source_column_id": "col-1"}],
                },
                {
                    "source_table_id": "lookup-tbl",
                    "columns": [
                        {
                            "source_column_id": "lc1",
                            "normalized_name": "internal_code",
                        },
                        {
                            "source_column_id": "lc2",
                            "normalized_name": "material_name",
                        },
                    ],
                },
            ]
        }
    ]

    results = validate_mapping_columns(
        [mapping], target_schema, catalogs
    )

    assert results[0]["validation_errors"] == []


def test_validate_aggregation_group_key_not_found() -> None:
    """Validation should reject a group key that doesn't exist in the agg table."""
    target_schema = TargetSchema(
        client_code="test",
        tables=[{"name": "out", "columns": [{"name": "total"}]}],
    )
    mapping = ProposedMapping(
        target_table="out",
        target_column="total",
        source_columns=[
            SourceColumnRef(
                source_table_id="base-tbl",
                source_column_id="col-1",
                source_table="Main",
                source_column="cust_id",
            )
        ],
        transformation_type="aggregation",
        aggregation_source_table="agg-tbl",
        aggregation_group_key="wrong_key",
        aggregation_expression="col('qty').sum()",
    )
    catalogs = [
        {
            "tables": [
                {
                    "source_table_id": "base-tbl",
                    "columns": [{"source_column_id": "col-1"}],
                },
                {
                    "source_table_id": "agg-tbl",
                    "columns": [
                        {
                            "source_column_id": "ac1",
                            "normalized_name": "cust_id",
                        },
                        {
                            "source_column_id": "ac2",
                            "normalized_name": "qty",
                        },
                    ],
                },
            ]
        }
    ]

    results = validate_mapping_columns(
        [mapping], target_schema, catalogs
    )

    errors = results[0]["validation_errors"]
    assert any(
        "aggregation_group_key" in e and "wrong_key" in e for e in errors
    )
