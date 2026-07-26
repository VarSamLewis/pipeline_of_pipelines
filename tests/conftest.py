"""Shared pytest fixtures for the pipeline-of-pipelines backend."""

from __future__ import annotations

import os
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
    yield


@pytest.fixture
def mock_openai(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub OpenAI embeddings and chat completions for deterministic tests."""
    import db_ops
    import mapping

    def fake_embedding(content: str, **kwargs: object) -> list[float]:
        """Return a deterministic 1536-dimension zero vector."""
        return [0.0] * 1536

    def fake_call_llm(messages: list[dict[str, str]], **kwargs: object) -> dict:
        """Return a deterministic mapping proposal for the simple test schema."""
        return {
            "mappings": [
                {
                    "target_table": "records",
                    "target_column": "record_id",
                    "source_columns": [
                        {"source_table": "data.csv", "source_column": "id"}
                    ],
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
                    "source_columns": [
                        {"source_table": "data.csv", "source_column": "name"}
                    ],
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
                    "source_columns": [
                        {"source_table": "data.csv", "source_column": "score"}
                    ],
                    "transformation_logic": "Direct map",
                    "polars_expression": None,
                    "tests": [],
                    "evidence_ids": [],
                    "business_rule_ids": [],
                    "confidence": 0.95,
                },
            ]
        }

    monkeypatch.setattr(db_ops, "get_embedding", fake_embedding)
    monkeypatch.setattr(mapping, "call_mapping_llm", fake_call_llm)


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
