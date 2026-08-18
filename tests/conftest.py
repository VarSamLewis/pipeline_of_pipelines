"""Shared pytest fixtures for the pipeline-of-pipelines backend."""

from __future__ import annotations

import os
import re
import uuid
from pathlib import Path

import pytest

# Use the PostgreSQL test database by default. Tests that require a live DB are
# skipped automatically when Postgres is not reachable.
os.environ.setdefault(
    "DATABASE_URL",
    (
        "postgresql+psycopg://postgres:postgres@localhost:5432/"
        "pipeline_test?connect_timeout=2"
    ),
)
os.environ.setdefault("AUTH_BYPASS_LOCAL", "true")

from db_ops import create_tables, get_engine  # noqa: E402


def _postgres_available() -> bool:
    """Return True if the configured PostgreSQL database is reachable."""
    from sqlalchemy import text

    try:
        engine = get_engine()
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


POSTGRES_AVAILABLE = _postgres_available()


requires_postgres = pytest.mark.skipif(
    not POSTGRES_AVAILABLE,
    reason="PostgreSQL is not available; start it with `docker compose up -d postgres`",
)


@pytest.fixture(scope="session", autouse=True)
def setup_test_database() -> None:
    """Create tables once for the test suite when Postgres is available."""
    if POSTGRES_AVAILABLE:
        create_tables()
        _seed_bypass_user()
    yield


def _seed_bypass_user() -> None:
    """Persist the synthetic local-dev user so audit-log foreign keys resolve."""
    import uuid as _uuid

    from db_ops import get_session
    from models import User, UserRole

    user_id = _uuid.UUID("00000000-0000-0000-0000-000000000000")
    with get_session() as session:
        if session.get(User, user_id) is None:
            session.add(
                User(
                    id=user_id,
                    workos_user_id="local-dev",
                    email="local-dev@example.com",
                    name="Local Developer",
                    role=UserRole.ADMIN,
                )
            )
            session.commit()


@pytest.fixture
def mock_openai(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub OpenAI embeddings and chat completions for deterministic tests."""
    import db_ops
    import mapping

    def fake_embedding(content: str, **kwargs: object) -> list[float]:
        """Return a deterministic 1536-dimension zero vector."""
        return [0.0] * 1536

    def fake_call_llm(
        messages: list[dict[str, str]],
        *args: object,
        **kwargs: object,
    ) -> dict:
        """Return a deterministic mapping proposal for the simple test schema."""
        prompt = messages[-1]["content"]
        table_ids = re.findall(r'"source_table_id": "([^"]+)"', prompt)
        column_ids = re.findall(r'"source_column_id": "([^"]+)"', prompt)
        raw_file_ids = re.findall(r'"raw_file_id": "([^"]+)"', prompt)
        table_id = table_ids[0] if table_ids else None
        raw_file_id = raw_file_ids[0] if raw_file_ids else None

        def source_ref(index: int, column: str) -> dict[str, str | None]:
            return {
                "source_table_id": table_id,
                "source_column_id": (
                    column_ids[index] if index < len(column_ids) else None
                ),
                "raw_file_id": raw_file_id,
                "source_table": "data.csv",
                "source_column": column,
            }

        return {
            "mappings": [
                {
                    "target_table": "records",
                    "target_column": "record_id",
                    "source_columns": [source_ref(0, "id")],
                    "transformation_logic": "Direct map",
                    "polars_expression": None,
                    "tests": ["not_null"],
                    "evidence_ids": [],
                    "business_rule_ids": [],
                    "confidence": 0.95,
                },
                {
                    "target_table": "records",
                    "target_column": "full_name",
                    "source_columns": [source_ref(1, "name")],
                    "transformation_logic": "Direct map",
                    "polars_expression": None,
                    "tests": [],
                    "evidence_ids": [],
                    "business_rule_ids": [],
                    "confidence": 0.95,
                },
                {
                    "target_table": "records",
                    "target_column": "score",
                    "source_columns": [source_ref(2, "score")],
                    "transformation_logic": "Direct map",
                    "polars_expression": None,
                    "tests": [],
                    "evidence_ids": [],
                    "business_rule_ids": [],
                    "confidence": 0.95,
                },
            ]
        }

    def fake_call_codegen_llm(
        messages: list[dict[str, str]],
        *args: object,
        **kwargs: object,
    ) -> str:
        """Return a deterministic pipeline script for the simple test schema."""
        return _FAKE_PIPELINE_SCRIPT

    monkeypatch.setattr(db_ops, "get_embedding", fake_embedding)
    monkeypatch.setattr(mapping, "call_mapping_llm", fake_call_llm)
    monkeypatch.setattr(mapping, "call_codegen_llm", fake_call_codegen_llm)


_FAKE_PIPELINE_SCRIPT = '''\
"""Auto-generated test pipeline. Uses mapping.json for source resolution."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import polars as pl

null = None
Int64 = pl.Int64
Float64 = pl.Float64
String = pl.String
Date = pl.Date
Datetime = pl.Datetime
Boolean = pl.Boolean


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-folder", required=True, type=Path)
    parser.add_argument("--output-folder", default=".", type=Path)
    args = parser.parse_args()

    df: pl.DataFrame | None = None
    for p in sorted(args.source_folder.iterdir()):
        if p.is_file() and p.suffix == ".csv":
            df = pl.read_csv(p, infer_schema_length=1000)
            break

    if df is None:
        return

    df = df.select([
        pl.col("id").cast(pl.Int64).alias("record_id"),
        pl.col("name").alias("full_name"),
        pl.col("score").alias("score"),
    ])

    args.output_folder.mkdir(parents=True, exist_ok=True)
    df.write_csv(args.output_folder / "records.csv")


if __name__ == "__main__":
    main()
'''


@pytest.fixture
def unique_client_code() -> str:
    """Return a unique client code for a test."""
    return f"testclient-{uuid.uuid4().hex[:8]}"


@pytest.fixture
def simple_schema_file(tmp_path: Path, unique_client_code: str) -> Path:
    """Create a simple target-schema JSON file."""
    import json

    schema = {
        "client_code": unique_client_code,
        "name": "default",
        "description": "Simple schema for tests",
        "tables": [
            {
                "name": "records",
                "description": "Simple record table",
                "columns": [
                    {
                        "name": "record_id",
                        "dtype": "Int64",
                        "required": True,
                        "unique": True,
                    },
                    {"name": "full_name", "dtype": "String", "required": True},
                    {"name": "score", "dtype": "Float64", "required": False},
                ],
            }
        ],
    }
    path = tmp_path / "target_schema.json"
    path.write_text(json.dumps(schema))
    return path


@pytest.fixture
def simple_data_folder(tmp_path: Path) -> Path:
    """Create a simple CSV data folder."""
    folder = tmp_path / "simple_data"
    folder.mkdir()
    (folder / "data.csv").write_text("id,name,score\n1,Alice,88.5\n2,Bob,92.0\n")
    return folder
