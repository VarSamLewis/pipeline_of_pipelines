"""Single-file Polars target transformation pipeline.

This module is the dedicated runtime for transforming approved mapping
specifications into curated staging tables using Polars. It is intentionally
self-contained: given an approved spec and an object store, it loads sources,
applies column-level expressions, runs validation tests, publishes staging
tables, and records execution lineage.

All outputs are shaped by the supplied TargetSchema, and multi-to-one source
mappings are supported.
"""

from __future__ import annotations

import io
import uuid
from datetime import UTC
from pathlib import Path
from typing import Any

import polars as pl
from db_ops import (
    get_mapping_spec,
    get_raw_file_by_id,
    get_session,
)
from file_ops import LocalObjectStore, detect_file_type
from models import TargetSchema

# ---------------------------------------------------------------------------
# Source loading
# ---------------------------------------------------------------------------


def load_mapping_spec(spec_id: uuid.UUID) -> dict[str, Any]:
    """Load an approved mapping specification, its columns, and target schema."""
    with get_session() as session:
        spec = get_mapping_spec(session, spec_id)
        if spec is None:
            raise ValueError(f"Mapping spec not found: {spec_id}")
        from db_ops import get_mapping_columns

        columns = get_mapping_columns(session, spec_id)
        return {
            "id": str(spec.id),
            "client_id": str(spec.client_id),
            "version": spec.version,
            "status": spec.status.value,
            "source_raw_file_ids": [str(x) for x in spec.source_raw_file_ids],
            "target_schema_json": spec.target_schema_json,
            "description": spec.description,
            "approved_by": spec.approved_by,
            "columns": [
                {
                    "id": str(c.id),
                    "target_table": c.target_table,
                    "target_column": c.target_column,
                    "source_columns": c.source_columns_json,
                    "transformation_logic": c.transformation_logic,
                    "polars_expression": c.polars_expression,
                    "tests": c.tests,
                    "evidence_ids": [str(x) for x in c.evidence_ids],
                    "business_rule_ids": [str(x) for x in c.business_rule_ids],
                    "confidence": c.confidence,
                    "sort_order": c.sort_order,
                }
                for c in columns
            ],
        }


def load_target_schema_from_spec(mapping_spec: dict[str, Any]) -> TargetSchema:
    """Deserialize the target schema stored inside a mapping spec."""
    return TargetSchema.model_validate(mapping_spec["target_schema_json"])


def load_source_dataframes(
    mapping_spec: dict[str, Any],
    object_store: LocalObjectStore,
) -> dict[str, pl.DataFrame]:
    """Load each source table referenced by the spec into a Polars DataFrame."""
    source_tables: dict[str, pl.DataFrame] = {}
    seen: set[str] = set()

    with get_session() as session:
        for raw_file_id in mapping_spec["source_raw_file_ids"]:
            raw_file = get_raw_file_by_id(session, uuid.UUID(raw_file_id))
            if raw_file is None:
                continue
            file_type = detect_file_type(raw_file.original_filename)
            if file_type not in {"csv", "xlsx"}:
                continue
            key = raw_file.original_filename
            if key in seen:
                continue
            seen.add(key)
            data = object_store.get(raw_file.storage_key)
            if file_type == "csv":
                df = pl.read_csv(io.BytesIO(data))
            else:
                df = pl.read_excel(
                    io.BytesIO(data), sheet_id=0, engine="openpyxl"
                )
            source_tables[key] = df

    return source_tables


# ---------------------------------------------------------------------------
# Column-level transformations
# ---------------------------------------------------------------------------


def _concat_str(*args: Any) -> pl.Expr:
    """Concatenate a variadic list of strings/expressions with pl.concat_str."""
    exprs = [pl.lit(a) if not isinstance(a, pl.Expr) else a for a in args]
    return pl.concat_str(exprs)


def apply_column_expression(
    df: pl.DataFrame,
    source_columns: list[dict[str, Any]],
    polars_expression: str | None,
    target_column: str,
) -> pl.DataFrame:
    """Apply a column transformation to a Polars DataFrame."""
    if not source_columns:
        return df.with_columns(pl.lit(None).alias(target_column))

    first_source = source_columns[0]["source_column"]
    if polars_expression:
        local_vars = {
            ref["source_column"]: df[ref["source_column"]]
            for ref in source_columns
            if ref["source_column"] in df.columns
        }
        eval_globals = {
            "pl": pl,
            "col": pl.col,
            "when": pl.when,
            "concat": _concat_str,
            "coalesce": pl.coalesce,
            "null": None,
            "Int64": pl.Int64,
            "Float64": pl.Float64,
            "String": pl.String,
            "Date": pl.Date,
            "Datetime": pl.Datetime,
            "Boolean": pl.Boolean,
        }
        try:
            result = eval(polars_expression, eval_globals, local_vars)
            return df.with_columns(result.alias(target_column))
        except Exception:
            return df.with_columns(pl.lit(None).alias(target_column))

    if first_source in df.columns:
        return df.with_columns(pl.col(first_source).alias(target_column))
    return df.with_columns(pl.lit(None).alias(target_column))


def enforce_target_schema_dtypes(
    df: pl.DataFrame,
    target_table: str,
    target_schema: TargetSchema,
) -> pl.DataFrame:
    """Cast DataFrame columns to the dtypes declared in the target schema."""
    table = next((t for t in target_schema.tables if t.name == target_table), None)
    if table is None:
        return df

    casts = []
    for col in table.columns:
        if col.dtype and col.name in df.columns:
            try:
                if col.dtype == "Date":
                    casts.append(
                        pl.col(col.name)
                        .cast(pl.String, strict=False)
                        .str.to_date(strict=False)
                        .alias(col.name)
                    )
                elif col.dtype == "Datetime":
                    casts.append(
                        pl.col(col.name)
                        .cast(pl.String, strict=False)
                        .str.to_datetime(strict=False)
                        .alias(col.name)
                    )
                else:
                    polars_dtype = getattr(pl, col.dtype)
                    casts.append(pl.col(col.name).cast(polars_dtype, strict=False))
            except AttributeError:
                pass
    if casts:
        df = df.with_columns(casts)
    return df


def build_target_dataframe(
    source_dfs: dict[str, pl.DataFrame],
    target_table: str,
    mapping_columns: list[dict[str, Any]],
    target_schema: TargetSchema,
) -> pl.DataFrame:
    """Build one curated staging DataFrame for a target table."""
    if not source_dfs:
        raise ValueError("No source DataFrames available")

    base_df = next(iter(source_dfs.values()))
    for col in mapping_columns:
        base_df = apply_column_expression(
            base_df,
            col["source_columns"],
            col.get("polars_expression"),
            col["target_column"],
        )

    target_cols = [c["target_column"] for c in mapping_columns]
    available_cols = [c for c in target_cols if c in base_df.columns]
    df = base_df.select(available_cols)
    return enforce_target_schema_dtypes(df, target_table, target_schema)


# ---------------------------------------------------------------------------
# Full pipeline execution
# ---------------------------------------------------------------------------


def run_pipeline(
    spec_id: uuid.UUID,
    object_store: LocalObjectStore,
    target_environment: str = "local",
) -> dict[str, pl.DataFrame]:
    """Execute the full Polars transformation pipeline."""
    mapping_spec = load_mapping_spec(spec_id)
    target_schema = load_target_schema_from_spec(mapping_spec)
    source_dfs = load_source_dataframes(mapping_spec, object_store)

    columns_by_table: dict[str, list[dict[str, Any]]] = {}
    for c in mapping_spec["columns"]:
        columns_by_table.setdefault(c["target_table"], []).append(c)

    result: dict[str, pl.DataFrame] = {}
    for target_table, cols in columns_by_table.items():
        result[target_table] = build_target_dataframe(
            source_dfs, target_table, cols, target_schema
        )
    return result


def persist_staging_tables(
    target_dfs: dict[str, pl.DataFrame],
    output_folder: str | Path,
    format: str = "csv",
) -> dict[str, Path]:
    """Write curated staging DataFrames to durable storage."""
    output_folder = Path(output_folder)
    output_folder.mkdir(parents=True, exist_ok=True)
    paths: dict[str, Path] = {}
    for name, df in target_dfs.items():
        path = output_folder / f"{name}.{format}"
        if format == "csv":
            df.write_csv(path)
        elif format == "parquet":
            df.write_parquet(path)
        else:
            raise ValueError(f"Unsupported format: {format}")
        paths[name] = path
    return paths


def write_results_csv(
    df: pl.DataFrame,
    output_path: str | Path,
) -> Path:
    """Write a single Polars DataFrame to a CSV file."""
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    df.write_csv(output_path)
    return output_path


# ---------------------------------------------------------------------------
# Validation and quality
# ---------------------------------------------------------------------------


def build_validation_tests(
    mapping_columns: list[dict[str, Any]],
    target_schema: TargetSchema,
) -> list[dict[str, Any]]:
    """Translate mapping-column tests and target schema into Polars checks."""
    target_col_map = {
        c.name: c for t in target_schema.tables for c in t.columns
    }
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


def build_lineage_for_run(
    execution_run_id: uuid.UUID,
    mapping_spec: dict[str, Any],
    target_dfs: dict[str, pl.DataFrame],
    target_schema: TargetSchema,
) -> None:
    """Record all lineage edges produced by an execution run."""
    from db_ops import record_lineage_edge

    with get_session() as session:
        for col in mapping_spec["columns"]:
            for ref in col["source_columns"]:
                raw_file_id = ref.get("raw_file_id")
                if raw_file_id:
                    record_lineage_edge(
                        session,
                        "RawFile",
                        uuid.UUID(raw_file_id),
                        "MappingColumn",
                        uuid.UUID(col["id"]),
                        "derived_from",
                    )
            record_lineage_edge(
                session,
                "MappingColumn",
                uuid.UUID(col["id"]),
                "ExecutionRun",
                execution_run_id,
                "produced_by",
            )
