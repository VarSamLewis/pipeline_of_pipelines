"""Tests for output validation behaviour."""

from __future__ import annotations

import polars as pl
from models import TargetSchema
from pipeline import run_validation_tests


def test_validation_uses_the_mapping_target_table() -> None:
    """Same-named columns in another output table cannot satisfy a test."""
    schema = TargetSchema(
        tables=[
            {
                "name": "customers",
                "columns": [{"name": "id", "dtype": "Int64", "required": True}],
            },
            {
                "name": "orders",
                "columns": [{"name": "id", "dtype": "Int64", "required": True}],
            },
        ]
    )

    results = run_validation_tests(
        {
            "customers": pl.DataFrame({"id": [None]}),
            "orders": pl.DataFrame({"id": [1]}),
        },
        [{"target_table": "customers", "target_column": "id"}],
        schema,
    )

    assert results == [
        {
            "test_name": "customers.id.not_null",
            "severity": "error",
            "passed": False,
            "details": {"expression": "pl.col('id').is_not_null().all()"},
        }
    ]
