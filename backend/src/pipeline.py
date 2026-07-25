"""Output validation, execution recording, and staging metadata.

Generated Python/Polars execution is owned by :mod:`codegen`; this module
evaluates its outputs and persists validation and lineage metadata.
"""

from __future__ import annotations

import uuid
from datetime import UTC
from typing import Any

import polars as pl
from db_ops import get_session
from models import TargetSchema

# ---------------------------------------------------------------------------
# Validation and quality
# ---------------------------------------------------------------------------


def build_validation_tests(
    mapping_columns: list[dict[str, Any]],
    target_schema: TargetSchema,
) -> list[dict[str, Any]]:
    """Translate mapping-column tests and target schema into Polars checks."""
    target_col_map = {c.name: c for t in target_schema.tables for c in t.columns}
    tests = []
    for col in mapping_columns:
        tc = target_col_map.get(col["target_column"])
        if tc and tc.required:
            tests.append(
                {
                    "name": f"{col['target_table']}.{col['target_column']}.not_null",
                    "severity": "error",
                    "column": col["target_column"],
                    "expression": (
                        f"pl.col('{col['target_column']}').is_not_null().all()"
                    ),
                }
            )
        if tc and tc.unique:
            tests.append(
                {
                    "name": f"{col['target_table']}.{col['target_column']}.unique",
                    "severity": "error",
                    "column": col["target_column"],
                    "expression": f"pl.col('{col['target_column']}').is_unique().all()",
                }
            )
        for test in col.get("tests", []):
            if test == "not_null":
                expression = f"pl.col('{col['target_column']}').is_not_null().all()"
            elif test == "unique":
                expression = f"pl.col('{col['target_column']}').is_unique().all()"
            elif test == "positive":
                expression = f"(pl.col('{col['target_column']}') > 0).all()"
            else:
                expression = test
            tests.append(
                {
                    "name": f"{col['target_table']}.{col['target_column']}.{test}",
                    "severity": "warning",
                    "column": col["target_column"],
                    "expression": expression,
                }
            )
    return tests


def run_validation_tests(
    target_dfs: dict[str, pl.DataFrame],
    mapping_columns: list[dict[str, Any]],
    target_schema: TargetSchema,
) -> list[dict[str, Any]]:
    """Run validation tests against curated DataFrames."""
    tests = build_validation_tests(mapping_columns, target_schema)
    results = []
    for test in tests:
        passed = True
        details: dict[str, Any] = {"expression": test["expression"]}
        for _table_name, df in target_dfs.items():
            if test["column"] not in df.columns:
                continue
            try:
                expr = eval(
                    test["expression"],
                    {"pl": pl, "col": pl.col, "when": pl.when},
                    {test["column"]: df[test["column"]]},
                )
                # Polars expressions are lazy; evaluate them against the DataFrame.
                result = df.select(expr).item() if isinstance(expr, pl.Expr) else expr
                passed = bool(result)
            except Exception as exc:
                passed = False
                details["error"] = str(exc)
        results.append(
            {
                "test_name": test["name"],
                "severity": test["severity"],
                "passed": passed,
                "details": details,
            }
        )
    return results


def compute_quality_profile(df: pl.DataFrame) -> dict[str, Any]:
    """Compute a basic quality profile for a Polars DataFrame."""
    return {
        "row_count": len(df),
        "columns": {
            name: {
                "null_count": int(df[name].null_count()),
                "unique_count": int(df[name].n_unique()),
                "dtype": str(df[name].dtype),
            }
            for name in df.columns
        },
    }


# ---------------------------------------------------------------------------
# Audit and lineage
# ---------------------------------------------------------------------------


def record_execution_run(
    client_id: uuid.UUID,
    spec_id: uuid.UUID,
    target_environment: str,
) -> uuid.UUID:
    """Create an ExecutionRun record and return its UUID."""
    from datetime import datetime

    from models import ExecutionRun, ExecutionStatus

    run = ExecutionRun(
        client_id=client_id,
        mapping_spec_id=spec_id,
        target_environment=target_environment,
        status=ExecutionStatus.SUCCESS,
        started_at=datetime.now(UTC),
        finished_at=datetime.now(UTC),
    )
    with get_session() as session:
        session.add(run)
        session.commit()
        session.refresh(run)
        return run.id


def record_validation_results(
    execution_run_id: uuid.UUID,
    test_results: list[dict[str, Any]],
) -> list[uuid.UUID]:
    """Persist validation results linked to an execution run."""
    from datetime import datetime

    from models import ValidationResult as VRModel

    ids: list[uuid.UUID] = []
    with get_session() as session:
        for result in test_results:
            vr = VRModel(
                execution_run_id=execution_run_id,
                test_name=result["test_name"],
                severity=result["severity"],
                passed=result["passed"],
                details=result["details"],
                recorded_at=datetime.now(UTC),
            )
            session.add(vr)
            session.commit()
            session.refresh(vr)
            ids.append(vr.id)
    return ids


def record_staging_metadata(
    execution_run_id: uuid.UUID,
    target_dfs: dict[str, pl.DataFrame],
    mapping_columns: list[dict[str, Any]],
    target_schema: TargetSchema,
) -> list[uuid.UUID]:
    """Persist staging table and column metadata with lineage links."""
    from models import StagingColumn, StagingTable

    ids: list[uuid.UUID] = []
    with get_session() as session:
        for table_name, df in target_dfs.items():
            st = StagingTable(
                execution_run_id=execution_run_id,
                table_name=table_name,
                row_count=len(df),
            )
            session.add(st)
            session.commit()
            session.refresh(st)
            ids.append(st.id)

            for col in df.columns:
                mc = next(
                    (m for m in mapping_columns if m["target_column"] == col), None
                )
                sc = StagingColumn(
                    staging_table_id=st.id,
                    mapping_column_id=uuid.UUID(mc["id"]) if mc else None,
                    column_name=col,
                    polars_dtype=str(df[col].dtype),
                    null_count=int(df[col].null_count()),
                    unique_count=int(df[col].n_unique()),
                )
                session.add(sc)
            session.commit()
    return ids
