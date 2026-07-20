"""Single-script clean rerun of the packaging-client example.

This script:
1. Ensures Postgres is running.
2. Resets the database and cleans generated artifacts.
3. Generates a few thousand messy packaging records plus lookups/rules.
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

CLIENT_CODE = "packaging_client"
DEFAULT_ROW_COUNT = 3000

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
    "PL01": "Plastic",
    "PL02": "Plastic",
    "GL01": "Glass",
    "AL01": "Aluminium",
    "ST01": "Steel",
    "PC01": "Paper/card",
    "FB01": "Fibre-based composite",
    "WD01": "Wood",
    "OT01": "Other",
}

SITE_NATIONS = {
    "SITE-A": "England",
    "SITE-B": "Scotland",
    "SITE-C": "Wales",
    "SITE-D": "Northern Ireland",
    "SITE-E": "England",
}

PACKAGING_TYPES = [
    ("Primary Bottle", "Primary"),
    ("primary jar", "Primary"),
    ("Secondary Carton", "Secondary"),
    ("secondary tray", "Secondary"),
    ("Shipment Pallet", "Shipment"),
    ("tertiary crate", "Tertiary"),
    ("Tertiary Wrap", "Tertiary"),
]

ACTIVITIES = [
    ("Supplied", "Supplied as goods"),
    ("supplied", "Supplied as goods"),
    ("Import", "Imported"),
    ("Imported", "Imported"),
    ("Export", "Exported"),
    ("Exported", "Exported"),
]

DATE_FORMATS = [
    "%d/%m/%Y",
    "%Y-%m-%d",
    "%d-%b-%Y",
]

PERIOD_FORMATS = [
    "2024-Q1",
    "Q2 2024",
    "2024-Q3",
    "Q4 2024",
]

UNITS = ["kg", "tonnes"]


FIELDNAMES = [
    "Record ID",
    "Period",
    "Org Code",
    "Site Ref",
    "Packaging Material Code",
    "Packaging Type",
    "Activity Type",
    "Weight",
    "Unit",
    "Record Date",
    "Data Quality Flag",
    "Comment",
]


def make_source_rows(count: int) -> list[dict[str, str]]:
    """Create messy packaging source rows with deterministic noise."""
    rows: list[dict[str, str]] = []
    base_date = datetime(2024, 1, 15)

    for i in range(1, count + 1):
        material_code = random.choice(list(MATERIAL_CODES.keys()))
        packaging_type, _packaging_class = random.choice(PACKAGING_TYPES)
        activity_raw, _activity_normalised = random.choice(ACTIVITIES)
        weight = round(random.uniform(50, 5000), 2)
        unit = random.choice(UNITS)
        site = random.choice(list(SITE_NATIONS.keys()))
        period = random.choice(PERIOD_FORMATS)
        date_fmt = random.choice(DATE_FORMATS)
        record_date = (base_date + timedelta(days=random.randint(0, 360))).strftime(
            date_fmt
        )

        row: dict[str, str] = {
            "Record ID": f"REC-{i:04d}",
            "Period": period,
            "Org Code": "ORG-12345",
            "Site Ref": site,
            "Packaging Material Code": material_code,
            "Packaging Type": packaging_type,
            "Activity Type": activity_raw,
            "Weight": str(weight),
            "Unit": unit,
            "Record Date": record_date,
            "Data Quality Flag": "VALID",
            "Comment": "",
        }
        rows.append(row)

    # Inject deterministic noise at low, fixed rates so the example stays messy
    # but remains parseable.
    for i, row in enumerate(rows):
        bucket = i % 100
        if bucket == 0:
            row["Period"] = "2024Q2"  # missing dash but still parseable
        elif bucket == 2:
            row["Packaging Type"] = "PRIMARY bottle"  # mixed case
        elif bucket == 4:
            row["Unit"] = "KG"  # uppercase
        elif bucket == 6:
            row["Record Date"] = "15-Mar-2024"  # different standard format
        elif bucket == 8:
            row["Data Quality Flag"] = "TEST"
            row["Comment"] = "test record - exclude"
        elif bucket == 10:
            row["Data Quality Flag"] = "DUPLICATE"
        elif bucket == 12:
            row["Weight"] = ""  # missing weight
        elif bucket == 14:
            row["Packaging Material Code"] = "UNKNOWN"  # unknown material
        elif bucket == 16:
            row["Site Ref"] = "SITE-Z"  # unknown site

    return rows


def write_source_csv(rows: list[dict[str, str]]) -> None:
    """Write the main packaging source CSV."""
    path = EXAMPLE_DIR / "packaging_data.csv"
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
        writer.writeheader()
        writer.writerows(rows)


def write_material_reference() -> None:
    """Write material code lookup table."""
    path = EXAMPLE_DIR / "material_reference.csv"
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["internal_code", "ea_material_name"])
        for code, name in MATERIAL_CODES.items():
            writer.writerow([code, name])


def write_site_locations() -> None:
    """Write site-to-nation lookup table."""
    path = EXAMPLE_DIR / "site_locations.csv"
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["site_ref", "nation"])
        for site, nation in SITE_NATIONS.items():
            writer.writerow([site, nation])


def write_requirements_text() -> None:
    """Write a plain-text requirements document for the EA submission."""
    text = """Environment Agency Packaging Data Submission Requirements

Target schema: packaging_submission

submission_id (String, required, unique):
    A unique identifier for each record. Combine Record ID, reporting period,
    site_id and packaging material code, separated by hyphens.

reporting_period (String, required):
    Normalise all period values to the format YYYY-QN, e.g. 2024-Q1.
    Acceptable source formats include '2024-Q1', 'Q2 2024', '2024Q2'.
    Use case-insensitive regex matching and always output 'YYYY-QN'.

organisation_id (String, required):
    Map directly from 'Org Code'.

site_id (String, required):
    Map directly from 'Site Ref'.

nation (String, required):
    Lookup 'Site Ref' in site_locations.csv and return the nation column.
    Allowed values: England, Scotland, Wales, Northern Ireland.
    If a site is not found, default to 'England'.

packaging_material (String, required):
    Lookup 'Packaging Material Code' in material_reference.csv and return
    ea_material_name. Allowed values: Plastic, Glass, Aluminium, Steel,
    Paper/card, Fibre-based composite, Wood, Other.

packaging_class (String, required):
    Normalise 'Packaging Type' to one of: Primary, Secondary, Shipment,
    Tertiary. Case-insensitive match on the first word. 'tertiary crate'
    maps to 'Tertiary'.

activity (String, required):
    Normalise 'Activity Type' case-insensitively:
    'supplied' -> 'Supplied as goods', 'import'/'imported' -> 'Imported',
    'export'/'exported' -> 'Exported'.

weight_tonnes (Float64, required):
    Convert Weight to tonnes. If Unit is 'kg' or 'KG', divide by 1000.
    If Unit is 'tonnes', keep the value as-is. Missing weights should
    result in null and fail validation.

submission_date (Date):
    Normalise 'Record Date' to a date. Try formats d/m/Y, Y-m-d, d-M-Y
    using coalesce.

Data quality rules:
- Exclude rows where Data Quality Flag is 'TEST' or 'DUPLICATE'.
- Exclude rows with unknown sites or materials if they cannot be defaulted.
"""
    (EXAMPLE_DIR / "ea_submission_requirements.txt").write_text(text, encoding="utf-8")


def write_business_rules_email() -> None:
    """Write an email containing additional business rules."""
    email_text = """From: compliance@example.com
To: data-team@example.com
Subject: EA packaging submission rules for 2024
MIME-Version: 1.0
Content-Type: text/plain; charset="utf-8"

Hi team,

Please ensure the 2024 packaging submission follows these business rules:

1. All weights must be reported in tonnes. If the source unit is kilograms,
   divide by 1000.

2. Packaging class must be one of Primary, Secondary, Shipment, or Tertiary.
   Treat "transport" as Tertiary.

3. Activity "supplied" should be normalised to "Supplied as goods".

4. Filter out any rows flagged as TEST or DUPLICATE in the Data Quality Flag
   column before submission.

5. Nation is determined by site reference using the site_locations.csv lookup.
   Any site not in the lookup should default to England.

6. The submission_id should be a concatenation of Record ID, reporting period,
   site_id and packaging material code, separated by hyphens.

Thanks,
Compliance
"""
    (EXAMPLE_DIR / "business_rules.eml").write_text(email_text, encoding="utf-8")


def write_target_schema() -> None:
    """Write the supplied target schema JSON."""
    schema = {
        "client_code": CLIENT_CODE,
        "name": "ea_packaging_submission",
        "description": "UK Environment Agency EPR packaging data submission",
        "tables": [
            {
                "name": "packaging_submission",
                "description": "Curated packaging records ready for EA submission",
                "columns": [
                    {
                        "name": "submission_id",
                        "dtype": "String",
                        "description": (
                            "Unique record identifier. Build with concat of "
                            "Record ID, Period, Site Ref and Packaging Material Code."
                        ),
                        "required": True,
                        "unique": True,
                    },
                    {
                        "name": "reporting_period",
                        "dtype": "String",
                        "description": (
                            "Normalised reporting period YYYY-QN. Source formats: "
                            "'2024-Q1', 'Q2 2024', '2024Q3'. Use case-insensitive "
                            "regex matching and always output 'YYYY-QN'."
                        ),
                        "required": True,
                    },
                    {
                        "name": "organisation_id",
                        "dtype": "String",
                        "description": (
                            "Organisation identifier. Direct map from Org Code."
                        ),
                        "required": True,
                    },
                    {
                        "name": "site_id",
                        "dtype": "String",
                        "description": "Site reference. Direct map from Site Ref.",
                        "required": True,
                    },
                    {
                        "name": "nation",
                        "dtype": "String",
                        "description": (
                            "Nation where packaging was handled. Use a lookup "
                            "transformation with site_locations.csv keyed on "
                            "site_ref returning nation. Source column is Site Ref."
                        ),
                        "required": True,
                        "allowed_values": [
                            "England",
                            "Scotland",
                            "Wales",
                            "Northern Ireland",
                        ],
                    },
                    {
                        "name": "packaging_material",
                        "dtype": "String",
                        "description": (
                            "EA packaging material category. Use a lookup "
                            "transformation with material_reference.csv keyed on "
                            "internal_code returning ea_material_name. "
                            "Source column is Packaging Material Code."
                        ),
                        "required": True,
                        "allowed_values": [
                            "Plastic",
                            "Glass",
                            "Aluminium",
                            "Steel",
                            "Paper/card",
                            "Fibre-based composite",
                            "Wood",
                            "Other",
                        ],
                    },
                    {
                        "name": "packaging_class",
                        "dtype": "String",
                        "description": (
                            "Packaging class. Map case-insensitively from the "
                            "first word of Packaging Type: Primary, Secondary, "
                            "Shipment, Tertiary."
                        ),
                        "required": True,
                        "allowed_values": [
                            "Primary",
                            "Secondary",
                            "Shipment",
                            "Tertiary",
                        ],
                    },
                    {
                        "name": "activity",
                        "dtype": "String",
                        "description": (
                            "Packaging activity. Normalise case-insensitively: "
                            "supplied -> 'Supplied as goods', "
                            "import/imported -> 'Imported', "
                            "export/exported -> 'Exported'."
                        ),
                        "required": True,
                    },
                    {
                        "name": "weight_tonnes",
                        "dtype": "Float64",
                        "description": (
                            "Weight in tonnes. If Unit is 'kg' or 'KG' divide "
                            "Weight by 1000, otherwise keep Weight as tonnes. "
                            "Cast to Float64."
                        ),
                        "required": True,
                    },
                    {
                        "name": "submission_date",
                        "dtype": "Date",
                        "description": (
                            "Date of record. Try formats '%d/%m/%Y', '%Y-%m-%d', "
                            "'%d-%b-%Y' using coalesce."
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
    """Generate the packaging_client example files."""
    print(f"\nGenerating {row_count} packaging example rows...", flush=True)
    EXAMPLE_DIR.mkdir(parents=True, exist_ok=True)
    rows = make_source_rows(row_count)
    write_source_csv(rows)
    write_material_reference()
    write_site_locations()
    write_requirements_text()
    write_business_rules_email()
    write_target_schema()
    print(f"Generated packaging example in {EXAMPLE_DIR}", flush=True)


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
from models import BusinessRule, ExtractedEvidence, MappingColumn, RawFile  # noqa: E402
from pipeline import load_target_schema_from_spec, run_validation_tests  # noqa: E402


def ensure_client() -> uuid.UUID:
    """Create the client if it does not exist."""
    with get_session() as session:
        client = get_client_by_code(session, CLIENT_CODE)
        if client is None:
            client = create_client(
                session,
                name="Packaging Client Ltd",
                code=CLIENT_CODE,
                metadata={"sector": "packaging", "regulator": "EA"},
            )
            print(f"Created client {client.id} ({CLIENT_CODE})", flush=True)
        else:
            print(f"Using existing client {client.id} ({CLIENT_CODE})", flush=True)
        return client.id


def ingest_folder(client_id: uuid.UUID) -> dict[str, object]:
    """Ingest the packaging client folder."""
    object_store = LocalObjectStore(str(OBJECT_STORE_DIR))
    with get_session() as session:
        result = ingest_client_folder(
            session,
            client_id=client_id,
            folder_path=str(EXAMPLE_DIR),
            object_store=object_store,
            label="EA packaging submission 2024",
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
            description="EA EPR packaging submission mapping",
        )
        print(f"Created mapping spec {spec.id}", flush=True)
        return spec.id


def _fix_mapping_columns(session: Any, spec_id: uuid.UUID) -> None:
    """Apply human review fixes to LLM-proposed column mappings."""
    columns = get_mapping_columns(session, spec_id)
    for col in columns:
        if col.target_column == "reporting_period":
            col.polars_expression = (
                "when(col('Period').str.contains(r'(?i)Q1'))"
                ".then(pl.lit('2024-Q1'))"
                ".when(col('Period').str.contains(r'(?i)Q2'))"
                ".then(pl.lit('2024-Q2'))"
                ".when(col('Period').str.contains(r'(?i)Q3'))"
                ".then(pl.lit('2024-Q3'))"
                ".when(col('Period').str.contains(r'(?i)Q4'))"
                ".then(pl.lit('2024-Q4')).otherwise(null)"
            )
            col.transformation_logic = (
                "Normalise period formats to YYYY-QN case-insensitively"
            )
        if col.target_column == "packaging_class":
            col.polars_expression = (
                "when(col('Packaging Type').str.contains(r'(?i)Primary'))"
                ".then(pl.lit('Primary'))"
                ".when(col('Packaging Type').str.contains(r'(?i)Secondary'))"
                ".then(pl.lit('Secondary'))"
                ".when(col('Packaging Type').str.contains(r'(?i)Shipment'))"
                ".then(pl.lit('Shipment'))"
                ".when(col('Packaging Type').str.contains(r'(?i)Tertiary'))"
                ".then(pl.lit('Tertiary')).otherwise(null)"
            )
            col.transformation_logic = (
                "Map packaging type case-insensitively to allowed class"
            )
        if col.target_column == "activity":
            col.polars_expression = (
                "when(col('Activity Type').str.strip_chars()"
                ".str.contains(r'(?i)supplied'))"
                ".then(pl.lit('Supplied as goods'))"
                ".when(col('Activity Type').str.strip_chars()"
                ".str.contains(r'(?i)import'))"
                ".then(pl.lit('Imported'))"
                ".when(col('Activity Type').str.strip_chars()"
                ".str.contains(r'(?i)export'))"
                ".then(pl.lit('Exported')).otherwise(null)"
            )
            col.transformation_logic = "Trim and map activity case-insensitively"
        if col.target_column == "weight_tonnes":
            col.polars_expression = (
                "when(col('Unit').str.strip_chars()"
                ".str.contains(r'(?i)^kg$'))"
                ".then(col('Weight').cast(pl.Float64) / 1000)"
                ".when(col('Unit').str.strip_chars()"
                ".str.contains(r'(?i)tonnes'))"
                ".then(col('Weight').cast(pl.Float64)).otherwise(null)"
            )
            col.transformation_logic = "Convert kg to tonnes, keep tonnes as-is"
        if col.target_column == "submission_date":
            col.polars_expression = (
                "coalesce("
                "col('Record Date').str.to_date('%d/%m/%Y', strict=False),"
                "col('Record Date').str.to_date('%Y-%m-%d', strict=False),"
                "col('Record Date').str.to_date('%d-%b-%Y', strict=False)"
                ")"
            )
            col.transformation_logic = "Parse multiple date formats using coalesce"
        session.add(col)
    session.commit()

    # Add a filter mapping for data quality flags (TEST / DUPLICATE).
    filter_exists = any(c.target_column == "_data_quality_filter" for c in columns)
    if not filter_exists:
        filter_col = MappingColumn(
            mapping_spec_id=spec_id,
            target_table="packaging_submission",
            target_column="_data_quality_filter",
            source_columns_json=[
                {
                    "source_table": "packaging_data.csv",
                    "source_column": "Data Quality Flag",
                }
            ],
            transformation_logic="Exclude rows flagged TEST or DUPLICATE",
            transformation_type="filter",
            filter_expression=(
                "~col('Data Quality Flag').str.strip_chars()"
                ".str.to_uppercase().is_in(['TEST', 'DUPLICATE'])"
            ),
            tests=[],
            sort_order=-1,
        )
        session.add(filter_col)
        session.commit()
    print("Applied human review fixes to proposed mappings", flush=True)


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
        _fix_mapping_columns(session, spec_id)


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
    results_path = folder_path / "packaging_submission.csv"
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
    for col in ["nation", "packaging_material", "packaging_class", "activity"]:
        if col in df.columns:
            print(f"\n{col}:", flush=True)
            print(df[col].value_counts().to_pandas().to_string(index=False), flush=True)

    print(f"\nTotal weight (tonnes): {df['weight_tonnes'].sum():.4f}", flush=True)

    mapping_spec = load_mapping_spec(uuid.UUID(folder_path.name))
    target_schema = load_target_schema_from_spec(mapping_spec)
    test_results = run_validation_tests(
        {"packaging_submission": df}, mapping_spec["columns"], target_schema
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

        evidence_ids = {
            eid for col in columns for eid in (col.evidence_ids or [])
        }
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
        lines.append(
            json.dumps(mapping_json.get("columns", []), indent=2, default=str)
        )

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
        description="Clean rerun of the packaging-client example."
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
        "packaging_submission.csv",
        "lineage_report.txt",
        "pipeline.py",
        "mapping.json",
    ):
        print(f"  {name}: {folder_path / name}", flush=True)


if __name__ == "__main__":
    main()
