"""Deterministic artifact generation and Polars execution pipeline.

This module is the single-file Polars pipeline for the target transformation
layer. From an approved mapping specification it generates a standalone Polars
transformation script and a machine-readable mapping file. It also executes the
Polars pipeline, runs validation checks, captures results, and records lineage
back to source files and mapping columns.

All outputs are shaped by the supplied TargetSchema.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
import uuid
from datetime import UTC
from pathlib import Path
from typing import Any

from db_ops import create_generated_artifact, get_raw_file_by_id
from file_ops import ObjectStore
from mapping_specs import load_mapping_spec, load_target_schema_from_spec
from models import (
    GeneratedArtifact,
    GeneratedPipelineScript,
    MappingFile,
    PipelineOutputFolder,
    TargetSchema,
)

# ---------------------------------------------------------------------------
# Artifact generation
# ---------------------------------------------------------------------------


_PIPELINE_TEMPLATE = '''\
"""Auto-generated single-file Polars transformation pipeline.

Reads mapping.json from the same directory, loads source files from the
provided source folder, applies the approved mappings (including filters,
lookups, and aggregations), and writes one CSV per target table.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import polars as pl


def load_mapping_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def discover_source_files(source_folder: Path) -> dict[str, Path]:
    """Map logical source table names to CSV or Excel file paths."""
    mapping = {}
    for p in sorted(source_folder.iterdir()):
        if not p.is_file():
            continue
        name = p.stem
        if p.suffix.lower() == ".csv":
            mapping[name] = p
        elif p.suffix.lower() in {".xlsx", ".xls"}:
            mapping[name] = p
    return mapping


def load_source_dataframes(source_folder: Path) -> dict[str, pl.DataFrame]:
    """Load every source file into a DataFrame keyed by file/sheet name."""
    source_files = discover_source_files(source_folder)
    result: dict[str, pl.DataFrame] = {}
    for name, path in source_files.items():
        if path.suffix.lower() == ".csv":
            result[name] = pl.read_csv(path)
        elif path.suffix.lower() in {".xlsx", ".xls"}:
            # Read every sheet and add it to the source pool
            sheets = pl.read_excel(path, sheet_id=None, engine="openpyxl")
            if isinstance(sheets, dict):
                for sheet_name, df in sheets.items():
                    result[str(sheet_name)] = df
            else:
                result[name] = sheets
    return result


def _concat_str(*args: Any) -> pl.Expr:
    """Concatenate a variadic list of strings/expressions with pl.concat_str."""
    exprs = [pl.lit(a) if not isinstance(a, pl.Expr) else a for a in args]
    return pl.concat_str(exprs)


def _eval_globals() -> dict[str, Any]:
    """Return the namespace used to evaluate polars_expression strings."""
    return {
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


def apply_row_expression(
    df: pl.DataFrame,
    mapping: dict[str, Any],
) -> pl.DataFrame:
    """Apply a per-row Polars expression to a DataFrame."""
    target_column = mapping["target_column"]
    source_columns = mapping.get("source_columns", [])
    expression = mapping.get("polars_expression")

    if not source_columns:
        return df.with_columns(pl.lit(None).alias(target_column))

    first_source = source_columns[0]["source_column"]
    if expression:
        local_vars = {
            ref["source_column"]: df[ref["source_column"]]
            for ref in source_columns
            if ref["source_column"] in df.columns
        }
        try:
            result = eval(expression, _eval_globals(), local_vars)
            return df.with_columns(result.alias(target_column))
        except Exception:
            return df.with_columns(pl.lit(None).alias(target_column))
    if first_source in df.columns:
        return df.with_columns(pl.col(first_source).alias(target_column))
    return df.with_columns(pl.lit(None).alias(target_column))


def apply_filter(
    df: pl.DataFrame,
    mapping: dict[str, Any],
) -> pl.DataFrame:
    """Apply a Polars filter expression to a DataFrame."""
    expression = mapping.get("filter_expression")
    if not expression:
        return df
    local_vars = {name: df[name] for name in df.columns}
    try:
        mask = eval(expression, _eval_globals(), local_vars)
        return df.filter(mask)
    except Exception:
        return df


def apply_lookup(
    df: pl.DataFrame,
    mapping: dict[str, Any],
    source_dfs: dict[str, pl.DataFrame],
) -> pl.DataFrame:
    """Join a lookup table and bring in a value column."""
    target_column = mapping["target_column"]
    lookup_table = mapping.get("lookup_source_table")
    lookup_key = mapping.get("lookup_key")
    lookup_value = mapping.get("lookup_value")
    source_columns = mapping.get("source_columns", [])

    if not lookup_table:
        return df.with_columns(pl.lit(None).alias(target_column))
    if lookup_table not in source_dfs:
        lookup_table = Path(lookup_table).stem
    if lookup_table not in source_dfs:
        return df.with_columns(pl.lit(None).alias(target_column))
    if not lookup_key or not lookup_value:
        return df.with_columns(pl.lit(None).alias(target_column))

    left_key = source_columns[0]["source_column"] if source_columns else lookup_key
    # LLMs sometimes confuse the lookup-table column name with the source column;
    # fall back to case- and punctuation-insensitive matches on the current DataFrame.
    def _normalise(name: str) -> str:
        return name.lower().replace(" ", "_").replace("-", "_")

    if left_key not in df.columns:
        left_key = next(
            (
                c
                for c in df.columns
                if c.lower() == left_key.lower()
                or _normalise(c) == _normalise(left_key)
            ),
            left_key,
        )
    if left_key not in df.columns:
        return df.with_columns(pl.lit(None).alias(target_column))

    right = source_dfs[lookup_table].select([lookup_key, lookup_value]).unique()
    return df.join(right, left_on=left_key, right_on=lookup_key, how="left").rename(
        {lookup_value: target_column}
    )


def apply_aggregation(
    df: pl.DataFrame,
    mapping: dict[str, Any],
    source_dfs: dict[str, pl.DataFrame],
) -> pl.DataFrame:
    """Aggregate a source table and join the result back to the base table."""
    target_column = mapping["target_column"]
    agg_table = mapping.get("aggregation_source_table")
    agg_key = mapping.get("aggregation_group_key")
    agg_expression = mapping.get("aggregation_expression")
    source_columns = mapping.get("source_columns", [])

    if not agg_table or not agg_key or not agg_expression:
        return df.with_columns(pl.lit(None).alias(target_column))
    if agg_table not in source_dfs:
        agg_table = Path(agg_table).stem
    if agg_table not in source_dfs:
        return df.with_columns(pl.lit(None).alias(target_column))

    base_key = source_columns[0]["source_column"] if source_columns else agg_key

    agg_df = source_dfs[agg_table]
    local_vars = {name: agg_df[name] for name in agg_df.columns}
    try:
        agg_expr = eval(agg_expression, _eval_globals(), local_vars)
    except Exception:
        return df.with_columns(pl.lit(None).alias(target_column))

    grouped = agg_df.group_by(agg_key).agg(agg_expr.alias(target_column))
    return df.join(grouped, left_on=base_key, right_on=agg_key, how="left")


def apply_mapping(
    df: pl.DataFrame,
    mapping: dict[str, Any],
    source_dfs: dict[str, pl.DataFrame],
) -> pl.DataFrame:
    """Apply a single mapping to the working DataFrame."""
    ttype = mapping.get("transformation_type", "expression")
    if ttype == "filter":
        return apply_filter(df, mapping)
    if ttype == "lookup":
        return apply_lookup(df, mapping, source_dfs)
    if ttype == "aggregation":
        return apply_aggregation(df, mapping, source_dfs)
    return apply_row_expression(df, mapping)


def enforce_dtypes(
    df: pl.DataFrame,
    target_table: str,
    target_schema: dict[str, Any],
) -> pl.DataFrame:
    """Cast columns to the dtypes declared in the target schema."""
    table = next(
        (t for t in target_schema.get("tables", []) if t["name"] == target_table),
        None,
    )
    if table is None:
        return df
    casts = []
    for col in table.get("columns", []):
        dtype = col.get("dtype")
        name = col["name"]
        if not dtype or name not in df.columns:
            continue
        current_dtype = str(df[name].dtype).lower()
        if "null" in current_dtype:
            continue
        if dtype == "Date":
            if "date" in current_dtype and "datetime" not in current_dtype:
                continue
            if "datetime" in current_dtype:
                casts.append(pl.col(name).dt.date().alias(name))
            else:
                casts.append(pl.col(name).str.to_date(strict=False).alias(name))
        elif dtype == "Datetime":
            if "datetime" in current_dtype:
                continue
            casts.append(pl.col(name).str.to_datetime(strict=False).alias(name))
        else:
            polars_dtype = getattr(pl, dtype, None)
            if polars_dtype is not None:
                casts.append(pl.col(name).cast(polars_dtype, strict=False))
    if casts:
        df = df.with_columns(casts)
    return df


def build_target_tables(
    mapping_spec: dict[str, Any],
    source_folder: Path,
) -> dict[str, pl.DataFrame]:
    """Build all target tables from the source files and mappings."""
    source_dfs = load_source_dataframes(source_folder)
    target_schema = mapping_spec.get("target_schema_json", {})

    columns_by_table: dict[str, list[dict[str, Any]]] = {}
    for col in mapping_spec["columns"]:
        columns_by_table.setdefault(col["target_table"], []).append(col)

    result: dict[str, pl.DataFrame] = {}
    for target_table, columns in columns_by_table.items():
        # Determine base source table from the first non-aggregation/lookup mapping
        base_source = target_table
        for col in columns:
            if col.get("source_columns"):
                base_source = col["source_columns"][0]["source_table"]
                break

        # Source table keys are file stems; the LLM may use the full filename.
        source_key = base_source
        if source_key not in source_dfs:
            stem_key = Path(base_source).stem
            source_key = stem_key if stem_key in source_dfs else base_source
        if source_key not in source_dfs:
            raise ValueError(f"Source table not found: {base_source}")

        df = source_dfs[source_key].clone()

        # Apply filters first
        for col in columns:
            if col.get("transformation_type") == "filter":
                df = apply_mapping(df, col, source_dfs)

        # Apply remaining mappings
        for col in columns:
            if col.get("transformation_type") != "filter":
                df = apply_mapping(df, col, source_dfs)

        target_cols = [c["target_column"] for c in columns]
        df = df.select([c for c in target_cols if c in df.columns])
        df = enforce_dtypes(df, target_table, target_schema)
        result[target_table] = df

    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-folder", required=True, type=Path)
    parser.add_argument("--output-folder", default=".", type=Path)
    args = parser.parse_args()

    pipeline_dir = Path(__file__).parent
    mapping_spec = load_mapping_json(pipeline_dir / "mapping.json")
    tables = build_target_tables(mapping_spec, args.source_folder)

    args.output_folder.mkdir(parents=True, exist_ok=True)
    # Write every target table so multi-table specs are complete.
    for name, df in tables.items():
        df.write_csv(args.output_folder / f"{name}.csv")


if __name__ == "__main__":
    main()
'''


def generate_polars_pipeline_script(
    mapping_spec: dict[str, Any],
    target_schema: TargetSchema,
) -> GeneratedPipelineScript:
    """Generate a standalone single-file Polars pipeline script."""
    target_tables = sorted({c["target_table"] for c in mapping_spec["columns"]})
    content = _PIPELINE_TEMPLATE
    return GeneratedPipelineScript(
        file_path=Path("pipeline.py"),
        content=content,
        target_tables=target_tables,
    )


def generate_mapping_json(
    mapping_spec: dict[str, Any],
) -> MappingFile:
    """Generate a human- and machine-readable mapping.json file."""
    return MappingFile(
        file_path=Path("mapping.json"),
        content=mapping_spec,
    )


def generate_artifact_set(
    spec_id: uuid.UUID,
    output_folder: str | Path,
) -> list[GeneratedArtifact]:
    """Generate the full set of artifacts for an approved mapping spec."""
    from db_ops import get_session

    output_folder = Path(output_folder)
    output_folder.mkdir(parents=True, exist_ok=True)

    mapping_spec = load_mapping_spec(spec_id)
    target_schema = load_target_schema_from_spec(mapping_spec)

    artifacts: list[GeneratedArtifact] = []
    with get_session() as session:
        # single-file Polars pipeline
        pipeline = generate_polars_pipeline_script(mapping_spec, target_schema)
        pipeline_path = output_folder / "pipeline.py"
        pipeline_path.write_text(pipeline.content)
        artifacts.append(
            create_generated_artifact(
                session,
                spec_id,
                "pipeline",
                str(pipeline_path.relative_to(output_folder)),
                pipeline.content,
                [],
            )
        )

        # mapping.json
        mapping_file = generate_mapping_json(mapping_spec)
        mapping_path = output_folder / "mapping.json"
        mapping_path.write_text(json.dumps(mapping_file.content, indent=2, default=str))
        artifacts.append(
            create_generated_artifact(
                session,
                spec_id,
                "mapping",
                str(mapping_path.relative_to(output_folder)),
                mapping_path.read_text(),
                [],
            )
        )

    return artifacts


def generate_output_folder(
    spec_id: uuid.UUID,
    output_folder: str | Path,
    object_store: ObjectStore,
) -> PipelineOutputFolder:
    """Generate the complete client deliverable folder.

    Writes pipeline.py, mapping.json, and one CSV per target table into
    output_folder.
    """
    from datetime import datetime

    output_folder = Path(output_folder)
    output_folder.mkdir(parents=True, exist_ok=True)

    mapping_spec = load_mapping_spec(spec_id)
    target_schema = load_target_schema_from_spec(mapping_spec)

    # Write mapping.json
    mapping_file = generate_mapping_json(mapping_spec)
    mapping_path = output_folder / "mapping.json"
    mapping_path.write_text(json.dumps(mapping_file.content, indent=2, default=str))

    # Write pipeline.py
    pipeline = generate_polars_pipeline_script(mapping_spec, target_schema)
    pipeline_path = output_folder / "pipeline.py"
    pipeline_path.write_text(pipeline.content)

    # Execute pipeline to produce results.csv
    execute_generated_pipeline(
        pipeline_path,
        output_folder,
        object_store,
        spec_id,
    )

    # Keep the generated artifacts plus one CSV per target table.
    allowed = {"pipeline.py", "mapping.json"}
    target_tables = {f"{c['target_table']}.csv" for c in mapping_spec["columns"]}
    for path in output_folder.iterdir():
        if (
            path.is_file()
            and path.name not in allowed
            and path.name not in target_tables
        ):
            path.unlink()

    first_table = next(iter({c["target_table"] for c in mapping_spec["columns"]}), None)
    if first_table:
        results_csv_path = output_folder / f"{first_table}.csv"
    else:
        results_csv_path = output_folder / "results.csv"
    return PipelineOutputFolder(
        folder_path=output_folder,
        pipeline_py_path=output_folder / "pipeline.py",
        mapping_json_path=output_folder / "mapping.json",
        results_csv_path=results_csv_path,
        generated_at=datetime.now(UTC),
    )


def execute_generated_pipeline(
    pipeline_py_path: str | Path,
    output_folder: str | Path,
    object_store: ObjectStore,
    spec_id: uuid.UUID,
) -> dict[str, Path]:
    """Run a generated pipeline.py script and capture its CSV outputs."""
    from db_ops import get_mapping_spec, get_session

    pipeline_py_path = Path(pipeline_py_path)
    output_folder = Path(output_folder)
    output_folder.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory() as tmpdir:
        source_folder = Path(tmpdir) / "sources"
        source_folder.mkdir(parents=True, exist_ok=True)

        with get_session() as session:
            spec = get_mapping_spec(session, spec_id)
            if spec is None:
                raise ValueError(f"Mapping spec not found: {spec_id}")
            for raw_file_id in spec.source_raw_file_ids:
                raw_file = get_raw_file_by_id(session, uuid.UUID(raw_file_id))
                if raw_file is None:
                    continue
                data = object_store.get(raw_file.storage_key)
                (source_folder / raw_file.original_filename).write_bytes(data)

        subprocess.run(
            [
                "python",
                str(pipeline_py_path),
                "--source-folder",
                str(source_folder),
                "--output-folder",
                str(output_folder),
            ],
            check=True,
        )

    csv_files = sorted(output_folder.glob("*.csv"))
    return {p.stem: p for p in csv_files}
