"""Single-script clean rerun of the BOM product example.

This script:
1. Ensures Postgres is running.
2. Resets the database and cleans generated artifacts.
3. Generates a few thousand messy product records plus lookups/rules.
4. Ingests the client folder, creates a mapping spec, and asks the LLM to propose
   mappings.
5. Applies deterministic human-review fixes.
6. Approves the spec, generates the output folder, executes the pipeline, and
   prints a summary.

Usage:
    uv run python run_example.py
    uv run python run_example.py --rows 5000
"""

from __future__ import annotations

# ruff: noqa: E402
import argparse
import csv
import json
import random
import shutil
import subprocess
import sys
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import polars as pl
from dotenv import load_dotenv

load_dotenv()

PROJECT_ROOT = Path(__file__).resolve().parent
BACKEND_SRC = PROJECT_ROOT / "backend" / "src"
EXAMPLE_DIR = PROJECT_ROOT / "examples" / "packaging_client"
OBJECT_STORE_DIR = PROJECT_ROOT / "data" / "object-store"
OUTPUT_FOLDERS_DIR = PROJECT_ROOT / "data" / "output-folders"
TARGET_SCHEMAS_DIR = PROJECT_ROOT / "data" / "target-schemas"

CLIENT_CODE = "bom_client"
DEFAULT_ROW_COUNT = 2000

random.seed(42)

DOCKER_AVAILABLE = shutil.which("docker") is not None


# ---------------------------------------------------------------------------
# Postgres orchestration
# ---------------------------------------------------------------------------


def run(command: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    """Run a shell command and stream output."""
    print(f"$ {' '.join(command)}", flush=True)
    return subprocess.run(
        command,
        cwd=PROJECT_ROOT,
        check=check,
        text=True,
    )


def postgres_is_healthy() -> bool:
    """Return True if the Postgres container is healthy."""
    if not DOCKER_AVAILABLE:
        return False
    result = subprocess.run(
        ["docker", "compose", "ps", "postgres"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
    )
    return result.returncode == 0 and "healthy" in result.stdout


def ensure_postgres() -> None:
    """Start the Postgres container if it is not already healthy."""
    if postgres_is_healthy():
        print("Postgres is already healthy.", flush=True)
        return

    if not DOCKER_AVAILABLE:
        print(
            "WARNING: docker not found; assuming Postgres is managed externally.",
            flush=True,
        )
        return

    print("Starting Postgres container...", flush=True)
    run(["docker", "compose", "up", "-d", "postgres"])

    for _ in range(30):
        if postgres_is_healthy():
            print("Postgres is healthy.", flush=True)
            return
        time.sleep(1)

    raise RuntimeError("Postgres did not become healthy within 30 seconds")


def reset_database() -> None:
    """Drop all tables and recreate them."""
    print("\nResetting database...", flush=True)
    sys.path.insert(0, str(BACKEND_SRC))
    from db_ops import create_tables, get_engine
    from sqlmodel import SQLModel

    engine = get_engine()
    SQLModel.metadata.drop_all(engine)
    create_tables(engine)
    print("Database reset complete.", flush=True)


def clean_artifacts() -> None:
    """Remove previous object-store and output-folder artifacts."""
    print("\nCleaning generated artifacts...", flush=True)
    for path in [OBJECT_STORE_DIR, OUTPUT_FOLDERS_DIR, TARGET_SCHEMAS_DIR]:
        if path.exists():
            shutil.rmtree(path)
        path.mkdir(parents=True, exist_ok=True)
    print("Artifacts cleaned.", flush=True)


# ---------------------------------------------------------------------------
# Fixture generation
# ---------------------------------------------------------------------------

MATERIAL_CODES = {
    "AL": "Aluminium",
    "ST": "Steel",
    "PL": "Plastic",
    "CF": "Carbon Fibre",
    "TI": "Titanium",
    "CU": "Copper",
    "RB": "Rubber",
    "GL": "Glass",
    "WD": "Wood",
    "CE": "Ceramic",
}

PRODUCT_LINES = {
    "ELEC": "Electronics",
    "MECH": "Mechanics",
    "HYDR": "Hydraulics",
    "PNEU": "Pneumatics",
    "STRUC": "Structural",
    "SOFT": "Software",
}

PRODUCT_NAMES = [
    ("Widget 3000", "Widget"),
    ("Bracket Assembly", "Bracket"),
    ("Control Module X1", "Control Board"),
    ("Hydraulic Pump P-50", "Hydraulic Pump"),
    ("Sensor Array", "Sensor"),
    ("Actuator Rod", "Actuator"),
    ("Mounting Plate", "Mount Bracket"),
    ("Gearbox Kit", "Gearbox"),
    ("Heat Sink", "Heatsink"),
    ("Cable Harness", "Cable"),
    ("Seal Ring Kit", "Seal Kit"),
    ("Piston Head", "Piston"),
    ("Bus Bar", "Busbar"),
    ("Filter Cartridge", "Filter"),
    ("Throttle Valve", "Valve"),
    ("Widgit Pro", "Widget"),  # deliberate misspelling in name
]

STATUS_VALUES = [
    ("active", "Active"),
    ("ACTIVE", "Active"),
    ("Active", "Active"),
    ("discontinued", "Discontinued"),
    ("DISCONTINUED", "Discontinued"),
    ("discont", "Discontinued"),
    ("pending", "Pending"),
    ("PENDING", "Pending"),
    ("in dev", "Pending"),
]

DATE_FORMATS = [
    "%d/%m/%Y",
    "%Y-%m-%d",
    "%Y%m%d",
    "%d-%b-%Y",
]

WEIGHT_UNITS = ["kg", "g"]


FIELDNAMES = [
    "SKU",
    "Product Name",
    "Product Line Code",
    "Weight",
    "Weight Unit",
    "Material Code",
    "Component Qty",
    "Lead Time Days",
    "Status",
    "Last Updated",
    "Data Quality Flag",
    "Comment",
]


def make_source_rows(count: int) -> list[dict[str, str]]:
    """Create messy BOM product source rows with deterministic noise."""
    rows: list[dict[str, str]] = []
    base_date = datetime(2025, 1, 15)

    for i in range(1, count + 1):
        name_variant, _ = random.choice(PRODUCT_NAMES)
        material_code = random.choice(list(MATERIAL_CODES.keys()))
        line_code = random.choice(list(PRODUCT_LINES.keys()))
        weight = round(random.uniform(0.05, 250.0), 3)
        unit = random.choice(WEIGHT_UNITS)
        status_raw, _ = random.choice(STATUS_VALUES)
        component_qty = random.randint(0, 50)
        lead_time = random.randint(1, 120)
        date_fmt = random.choice(DATE_FORMATS)
        record_date = (base_date + timedelta(days=random.randint(0, 540))).strftime(
            date_fmt
        )

        row: dict[str, str] = {
            "SKU": f"SKU-{i:05d}",
            "Product Name": name_variant,
            "Product Line Code": line_code,
            "Weight": str(weight),
            "Weight Unit": unit,
            "Material Code": material_code,
            "Component Qty": str(component_qty),
            "Lead Time Days": str(lead_time),
            "Status": status_raw,
            "Last Updated": record_date,
            "Data Quality Flag": "VALID",
            "Comment": "",
        }
        rows.append(row)

    # Inject deterministic noise at low, fixed rates.
    for i, row in enumerate(rows):
        bucket = i % 100
        if bucket == 0:
            row["Product Line Code"] = "elec"  # lowercase
        elif bucket == 2:
            row["Weight Unit"] = "G"  # uppercase g
        elif bucket == 4:
            row["Status"] = "Discont"  # shorthand
        elif bucket == 6:
            row["Last Updated"] = "20250115"  # compact format
        elif bucket == 8:
            row["Data Quality Flag"] = "TEST"
            row["Comment"] = "test record - exclude"
        elif bucket == 10:
            row["Data Quality Flag"] = "DUPLICATE"
        elif bucket == 12:
            row["Weight"] = ""  # missing weight
        elif bucket == 14:
            row["Material Code"] = "XX"  # unknown material
        elif bucket == 16:
            row["Component Qty"] = "-1"  # negative quantity
        elif bucket == 18:
            row["Product Line Code"] = "ZZ"  # unknown line code

    return rows


def write_source_csv(rows: list[dict[str, str]]) -> None:
    """Write the main product source CSV."""
    path = EXAMPLE_DIR / "packaging_data.csv"
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
        writer.writeheader()
        writer.writerows(rows)


def write_material_reference() -> None:
    """Write material code lookup table."""
    path = EXAMPLE_DIR / "material_reference.csv"
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["material_code", "material_name"])
        writer.writeheader()
        for code, name in MATERIAL_CODES.items():
            writer.writerow({"material_code": code, "material_name": name})


def write_product_lines() -> None:
    """Write product line code lookup table."""
    path = EXAMPLE_DIR / "product_lines.csv"
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["line_code", "line_name"])
        writer.writeheader()
        for code, name in PRODUCT_LINES.items():
            writer.writerow({"line_code": code, "line_name": name})


def write_requirements_text() -> None:
    """Write a plain-text requirements document for the BOM product data."""
    text = """Product BOM Data Requirements

Target table: product_bom

sku (String, required, unique):
    A unique product SKU identifier. Map directly from 'SKU', trim
    whitespace, and convert to uppercase.

product_name (String, required):
    Product name. Trim whitespace and convert to title case.
    Known correction: 'Widgit' must be corrected to 'Widget'.

product_line (String, required):
    Lookup 'Product Line Code' in product_lines.csv keyed on
    line_code and return line_name.
    Allowed values: Electronics, Mechanics, Hydraulics, Pneumatics,
    Structural, Software.
    If a code is not found, default to 'Other'.

weight_kg (Float64, required):
    Convert Weight to kilograms. If Weight Unit is 'g', divide by 1000.
    If Weight Unit is 'kg', keep the value as-is. Round to 3 decimal
    places. Missing weights should result in null and fail validation.

primary_material (String, required):
    Lookup 'Material Code' in material_reference.csv keyed on
    material_code returning material_name.
    Allowed values: Aluminium, Steel, Plastic, Carbon Fibre, Titanium,
    Copper, Rubber, Glass, Wood, Ceramic.
    If a code is not found, default to 'Plastic'.

component_qty (Int64):
    Number of sub-components. Map directly from 'Component Qty'.
    Negative values must be set to null. Missing values remain null.

lead_time_days (Int64):
    Manufacturing lead time in calendar days. Map directly from
    'Lead Time Days'. Missing values remain null.

status (String, required):
    Normalise 'Status' case-insensitively to one of:
    'active' / 'ACTIVE' -> 'Active'
    'discontinued' / 'DISCONTINUED' / 'discont' -> 'Discontinued'
    'pending' / 'PENDING' / 'in dev' -> 'Pending'
    Anything else defaults to 'Pending'.

last_updated (Date):
    Normalise 'Last Updated' to a date. Try formats:
    d/m/Y, Y-m-d, YYYYMMDD, d-M-Y using coalesce.

Data quality rules:
- Exclude rows where Data Quality Flag is 'TEST' or 'DUPLICATE'.
- Exclude rows with missing SKU or product_name.
"""
    (EXAMPLE_DIR / "ea_submission_requirements.txt").write_text(text, encoding="utf-8")


def write_business_rules_email() -> None:
    """Write an email containing additional business rules."""
    email_text = """From: engineering@example.com
To: data-team@example.com
Subject: BOM product data rules for manufacturing catalog
MIME-Version: 1.0
Content-Type: text/plain; charset="utf-8"

Hi team,

Please ensure the BOM product data follows these business rules:

1. All weights must be reported in kilograms. If the source unit is 'g',
   divide by 1000. Round final weight to 3 decimal places.

2. Product status must be one of Active, Discontinued, or Pending.
   Treat "in dev" as Pending and "discont" as Discontinued.

3. Product line is determined by line code using the product_lines.csv
   lookup. Any code not in the lookup should default to "Other".

4. Filter out any rows flagged as TEST or DUPLICATE in the Data Quality
   Flag column before loading.

5. Material code is determined by material_reference.csv lookup. Any
   unknown material codes should default to "Plastic".

6. The product name must be title-cased. There's a known misspelling:
   "Widgit" should always be corrected to "Widget".

Thanks,
Engineering
"""
    (EXAMPLE_DIR / "business_rules.eml").write_text(email_text, encoding="utf-8")


def write_target_schema() -> None:
    """Write the supplied target schema JSON."""
    schema = {
        "client_code": CLIENT_CODE,
        "name": "product_bom",
        "description": "Product Bill of Materials data for manufacturing catalog",
        "tables": [
            {
                "name": "product_bom",
                "description": "Curated product BOM records with materials, weights and components",
                "columns": [
                    {
                        "name": "sku",
                        "dtype": "String",
                        "description": "Unique product SKU identifier. Direct map from SKU column, upper-cased and trimmed.",
                        "required": True,
                        "unique": True,
                    },
                    {
                        "name": "product_name",
                        "dtype": "String",
                        "description": (
                            "Product name in title case. Trim whitespace and normalise "
                            "to title case. Apply manual override for known misspellings "
                            "(e.g. 'Widget' should never be 'Widgit')."
                        ),
                        "required": True,
                    },
                    {
                        "name": "product_line",
                        "dtype": "String",
                        "description": (
                            "Product line name. Use a lookup transformation with "
                            "product_lines.csv keyed on line_code returning line_name."
                        ),
                        "required": True,
                        "allowed_values": [
                            "Electronics",
                            "Mechanics",
                            "Hydraulics",
                            "Pneumatics",
                            "Structural",
                            "Software",
                        ],
                    },
                    {
                        "name": "weight_kg",
                        "dtype": "Float64",
                        "description": (
                            "Product weight in kilograms. If Weight Unit is 'g' divide "
                            "Weight by 1000, otherwise keep as kg. Cast to Float64."
                        ),
                        "required": True,
                    },
                    {
                        "name": "primary_material",
                        "dtype": "String",
                        "description": (
                            "Primary manufacturing material. Use a lookup transformation "
                            "with material_reference.csv keyed on material_code returning "
                            "material_name. Source column is Material Code."
                        ),
                        "required": True,
                        "allowed_values": [
                            "Aluminium",
                            "Steel",
                            "Plastic",
                            "Carbon Fibre",
                            "Titanium",
                            "Copper",
                            "Rubber",
                            "Glass",
                            "Wood",
                            "Ceramic",
                        ],
                    },
                    {
                        "name": "component_qty",
                        "dtype": "Int64",
                        "description": (
                            "Number of sub-components / parts in the BOM. "
                            "Cast to Int64. Negative values should be set to null."
                        ),
                        "required": False,
                    },
                    {
                        "name": "lead_time_days",
                        "dtype": "Int64",
                        "description": (
                            "Manufacturing lead time in calendar days. Cast to Int64."
                        ),
                        "required": False,
                    },
                    {
                        "name": "status",
                        "dtype": "String",
                        "description": (
                            "Product lifecycle status. Normalise case-insensitively: "
                            "'active'/'ACTIVE' -> 'Active', "
                            "'discontinued'/'DISCONTINUED'/'discont' -> 'Discontinued', "
                            "'pending'/'PENDING'/'in dev' -> 'Pending'. "
                            "Anything else defaults to 'Pending'."
                        ),
                        "required": True,
                        "allowed_values": [
                            "Active",
                            "Discontinued",
                            "Pending",
                        ],
                    },
                    {
                        "name": "last_updated",
                        "dtype": "Date",
                        "description": (
                            "Date the product record was last updated. Try formats "
                            "'%d/%m/%Y', '%Y-%m-%d', '%Y%m%d', '%d-%b-%Y' using coalesce."
                        ),
                        "required": False,
                    },
                ],
            }
        ],
    }
    (EXAMPLE_DIR / "target_schema.json").write_text(
        json.dumps(schema, indent=2), encoding="utf-8"
    )


def generate_fixtures(row_count: int) -> None:
    """Generate the BOM product example files."""
    print(f"\nGenerating {row_count} BOM product example rows...", flush=True)
    EXAMPLE_DIR.mkdir(parents=True, exist_ok=True)
    rows = make_source_rows(row_count)
    write_source_csv(rows)
    write_material_reference()
    write_product_lines()
    write_requirements_text()
    write_business_rules_email()
    write_target_schema()
    print(f"Generated BOM example in {EXAMPLE_DIR}", flush=True)


# ---------------------------------------------------------------------------
# Pipeline driver
# ---------------------------------------------------------------------------

sys.path.insert(0, str(BACKEND_SRC))

from codegen import generate_output_folder, load_mapping_spec  # noqa: E402
from db_ops import (  # noqa: E402
    approve_mapping_spec,
    create_client,
    create_mapping_spec,
    get_client_by_code,
    get_mapping_columns,
    get_session,
    ingest_client_folder,
)
from file_ops import LocalObjectStore, load_target_schema  # noqa: E402
from mapping import propose_mapping_spec  # noqa: E402
from models import BusinessRule, ExtractedEvidence, RawFile  # noqa: E402
from pipeline import load_target_schema_from_spec, run_validation_tests  # noqa: E402


def ensure_client() -> uuid.UUID:
    """Create the client if it does not exist."""
    with get_session() as session:
        client = get_client_by_code(session, CLIENT_CODE)
        if client is None:
            client = create_client(
                session,
                name="BOM Manufacturing Ltd",
                code=CLIENT_CODE,
                metadata={"sector": "manufacturing", "domain": "bom"},
            )
            print(f"Created client {client.id} ({CLIENT_CODE})", flush=True)
        else:
            print(f"Using existing client {client.id} ({CLIENT_CODE})", flush=True)
        return client.id


def ingest_folder(client_id: uuid.UUID) -> dict[str, object]:
    """Ingest the BOM product client folder."""
    object_store = LocalObjectStore(str(OBJECT_STORE_DIR))
    with get_session() as session:
        result = ingest_client_folder(
            session,
            client_id=client_id,
            folder_path=str(EXAMPLE_DIR),
            object_store=object_store,
            label="BOM product catalog 2025",
        )
        print(f"Ingested batch {result.ingestion_batch_id}", flush=True)
        print(f"  raw files: {len(result.raw_file_ids)}", flush=True)
        print(
            f"  parsed: {result.parsed_count}, failed: {result.failed_count}",
            flush=True,
        )
        return {
            "batch_id": result.ingestion_batch_id,
            "raw_file_ids": result.raw_file_ids,
        }


def create_spec(client_id: uuid.UUID, raw_file_ids: list[uuid.UUID]) -> uuid.UUID:
    """Create a mapping spec from the target schema and source files."""
    target_schema = load_target_schema(EXAMPLE_DIR / "target_schema.json")
    with get_session() as session:
        spec = create_mapping_spec(
            session,
            client_id=client_id,
            source_raw_file_ids=raw_file_ids,
            target_schema=target_schema,
            description="BOM product catalog mapping",
        )
        print(f"Created mapping spec {spec.id}", flush=True)
        return spec.id





def propose(spec_id: uuid.UUID) -> None:
    """Ask the LLM to propose mappings for the spec."""
    with get_session() as session:
        spec = load_mapping_spec(spec_id)
        target_schema = load_target_schema_from_spec(spec)
        propose_mapping_spec(
            session,
            spec_id,
            target_schema=target_schema,
            model="gpt-4o-mini",
            top_k_evidence=10,
        )
        print(f"Proposed mappings for spec {spec_id}", flush=True)


def approve(spec_id: uuid.UUID) -> None:
    """Approve the proposed mapping spec."""
    with get_session() as session:
        spec = approve_mapping_spec(
            session,
            spec_id,
            reviewer="human-reviewer-1",
            notes="Approved after LLM proposal review",
        )
        print(f"Approved spec {spec.id} by {spec.approved_by}", flush=True)


def generate_and_execute(spec_id: uuid.UUID) -> Path:
    """Generate the output folder and execute the pipeline."""
    object_store = LocalObjectStore(str(OBJECT_STORE_DIR))
    output_folder = OUTPUT_FOLDERS_DIR / str(spec_id)
    folder = generate_output_folder(spec_id, output_folder, object_store)
    print(f"Generated output folder: {folder.folder_path}", flush=True)
    print(f"  pipeline.py: {folder.pipeline_py_path}", flush=True)
    print(f"  mapping.json: {folder.mapping_json_path}", flush=True)
    print(f"  results.csv: {folder.results_csv_path}", flush=True)
    return folder.folder_path


def _load_results_dataframe(folder_path: Path) -> pl.DataFrame:
    """Read the generated results CSV."""
    results_path = folder_path / "product_bom.csv"
    if not results_path.exists():
        results_path = folder_path / "results.csv"
    if not results_path.exists():
        raise FileNotFoundError(f"No results CSV found in {folder_path}")
    return pl.read_csv(results_path)


def analyse_results(folder_path: Path) -> None:
    """Read and summarise the generated results."""
    df = _load_results_dataframe(folder_path)
    print("\n=== Generated results summary ===", flush=True)
    print(f"Rows: {len(df)}", flush=True)
    print(f"Columns: {df.columns}", flush=True)
    print("\nSchema:", flush=True)
    for name in df.columns:
        print(f"  {name}: {df[name].dtype} (nulls={df[name].null_count()})", flush=True)

    print("\nSample rows:", flush=True)
    print(df.head(10).to_pandas().to_string(index=False), flush=True)

    print("\nValue counts:", flush=True)
    for col in ["product_line", "primary_material", "status"]:
        if col in df.columns:
            print(f"\n{col}:", flush=True)
            print(df[col].value_counts().to_pandas().to_string(index=False), flush=True)

    print(f"\nTotal weight (kg): {df['weight_kg'].sum():.3f}", flush=True)

    mapping_spec = load_mapping_spec(uuid.UUID(folder_path.name))
    target_schema = load_target_schema_from_spec(mapping_spec)
    test_results = run_validation_tests(
        {"product_bom": df}, mapping_spec["columns"], target_schema
    )
    print("\nValidation results:", flush=True)
    error_count = sum(
        1 for t in test_results if t["severity"] == "error" and not t["passed"]
    )
    warning_count = sum(
        1 for t in test_results if t["severity"] == "warning" and not t["passed"]
    )
    for tr in test_results:
        status = "PASS" if tr["passed"] else "FAIL"
        print(f"  [{status}] {tr['test_name']} ({tr['severity']})", flush=True)
    print(
        f"\nValidation summary: {error_count} errors, {warning_count} warnings",
        flush=True,
    )


def show_lineage(spec_id: uuid.UUID, folder_path: Path) -> None:
    """Write and summarise the provenance of every generated mapping."""
    report_path = folder_path / "lineage_report.txt"
    lines: list[str] = []

    with get_session() as session:
        columns = get_mapping_columns(session, spec_id)

        evidence_ids = {eid for col in columns for eid in (col.evidence_ids or [])}
        rule_ids = {rid for col in columns for rid in (col.business_rule_ids or [])}
        raw_file_ids = {
            ref.get("raw_file_id")
            for col in columns
            for ref in (col.source_columns_json or [])
            if ref.get("raw_file_id")
        }

        for eid in evidence_ids:
            ev = session.get(ExtractedEvidence, uuid.UUID(eid))
            if ev and ev.raw_file_id:
                raw_file_ids.add(str(ev.raw_file_id))

        evidence_by_id: dict[str, ExtractedEvidence] = {}
        for eid in evidence_ids:
            ev = session.get(ExtractedEvidence, uuid.UUID(eid))
            if ev:
                evidence_by_id[eid] = ev

        rules_by_id: dict[str, BusinessRule] = {}
        for rid in rule_ids:
            rule = session.get(BusinessRule, uuid.UUID(rid))
            if rule:
                rules_by_id[rid] = rule

        raw_files_by_id: dict[str, RawFile] = {}
        for rid in raw_file_ids:
            if rid is None:
                continue
            rf = session.get(RawFile, uuid.UUID(rid))
            if rf:
                raw_files_by_id[str(rid)] = rf

        lines.append("=" * 70)
        lines.append("Mapping lineage / LLM provenance")
        lines.append("=" * 70)
        lines.append("")

        for col in sorted(columns, key=lambda c: c.sort_order):
            lines.append(f"Target: {col.target_table}.{col.target_column}")
            lines.append(f"  type: {col.transformation_type}")
            lines.append(f"  logic: {col.transformation_logic}")
            if col.polars_expression:
                lines.append(f"  polars: {col.polars_expression}")

            if col.source_columns_json:
                lines.append("  source columns:")
                for ref in col.source_columns_json:
                    raw_file_id = ref.get("raw_file_id")
                    filename = ""
                    if raw_file_id and raw_file_id in raw_files_by_id:
                        filename = raw_files_by_id[raw_file_id].original_filename
                    lines.append(
                        f"    - {ref.get('source_table')}.{ref.get('source_column')}"
                        f"{f' ({filename})' if filename else ''}"
                    )

            if col.evidence_ids:
                lines.append("  evidence cited:")
                for eid in col.evidence_ids:
                    ev = evidence_by_id.get(str(eid))
                    if ev:
                        rf = raw_files_by_id.get(str(ev.raw_file_id))
                        lines.append(f"    - evidence {ev.id}")
                        source_file = rf.original_filename if rf else "unknown"
                        lines.append(f"      from raw file: {source_file}")
                        snippet = ev.content[:300].replace("\n", " ")
                        lines.append(f"      content: {snippet}")

            if col.business_rule_ids:
                lines.append("  business rules cited:")
                for rid in col.business_rule_ids:
                    rule = rules_by_id.get(str(rid))
                    if rule:
                        lines.append(f"    - rule {rule.id}: {rule.rule_text}")
            lines.append("")

        lines.append("=" * 70)
        lines.append("Generated pipeline.py")
        lines.append("=" * 70)
        lines.append("")
        pipeline_py = folder_path / "pipeline.py"
        lines.append(pipeline_py.read_text())

        lines.append("")
        lines.append("=" * 70)
        lines.append("Generated mapping.json (mappings only)")
        lines.append("=" * 70)
        lines.append("")
        mapping_json = json.loads((folder_path / "mapping.json").read_text())
        lines.append(json.dumps(mapping_json.get("columns", []), indent=2, default=str))

        report_text = "\n".join(lines)
        report_path.write_text(report_text, encoding="utf-8")

        print("\n=== Mapping lineage / LLM provenance summary ===", flush=True)
        print(f"Full report written to: {report_path}", flush=True)
        print(f"  mappings: {len(columns)}", flush=True)
        print(f"  evidence chunks cited: {len(evidence_by_id)}", flush=True)
        print(f"  business rules cited: {len(rules_by_id)}", flush=True)
        print(f"  source raw files: {len(raw_files_by_id)}", flush=True)
        print("\nCited raw files:", flush=True)
        seen_files: set[str] = set()
        for rf in raw_files_by_id.values():
            if rf.original_filename not in seen_files:
                seen_files.add(rf.original_filename)
                print(f"  - {rf.original_filename}", flush=True)
        print("\nCited evidence snippets (unique content):", flush=True)
        seen_content: set[str] = set()
        for ev in evidence_by_id.values():
            snippet = ev.content[:200].replace("\n", " ")
            if snippet in seen_content:
                continue
            seen_content.add(snippet)
            rf = raw_files_by_id.get(str(ev.raw_file_id))
            source = rf.original_filename if rf else "unknown"
            print(f"  - {source}: {snippet}...", flush=True)


def main() -> None:
    """Run the full example end-to-end."""
    parser = argparse.ArgumentParser(
        description="Clean rerun of the BOM product example."
    )
    parser.add_argument(
        "--rows",
        type=int,
        default=DEFAULT_ROW_COUNT,
        help=f"Number of source rows to generate (default: {DEFAULT_ROW_COUNT})",
    )
    args = parser.parse_args()

    ensure_postgres()
    reset_database()
    clean_artifacts()
    generate_fixtures(args.rows)

    client_id = ensure_client()
    ingest_result = ingest_folder(client_id)
    spec_id = create_spec(client_id, ingest_result["raw_file_ids"])
    propose(spec_id)
    approve(spec_id)
    folder_path = generate_and_execute(spec_id)
    analyse_results(folder_path)
    show_lineage(spec_id, folder_path)

    print("\n=== Done ===", flush=True)
    print(f"Latest output folder: {folder_path}", flush=True)
    for name in (
        "product_bom.csv",
        "lineage_report.txt",
        "pipeline.py",
        "mapping.json",
    ):
        print(f"  {name}: {folder_path / name}", flush=True)


if __name__ == "__main__":
    main()
