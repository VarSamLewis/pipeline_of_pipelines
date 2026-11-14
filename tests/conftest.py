"""Shared pytest fixtures for the pipeline-of-pipelines backend."""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

# Integration tests require a real, dedicated PostgreSQL test database.
os.environ.setdefault(
    "DATABASE_URL",
    (
        "postgresql+psycopg://postgres:postgres@localhost:5432/"
        "pipeline_test?connect_timeout=2"
    ),
)
os.environ.setdefault("AUTH_BYPASS_LOCAL", "true")
os.environ.setdefault("BYOK_ENCRYPTION_KEY", Fernet.generate_key().decode())

from db_ops import get_engine, upgrade_database  # noqa: E402


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


@pytest.fixture(scope="session", autouse=True)
def setup_test_database() -> None:
    """Fail loudly when integration tests cannot reach their test database."""
    if not (get_engine().url.database or "").endswith("_test"):
        pytest.fail("Integration tests require a database name ending in _test")
    if not POSTGRES_AVAILABLE:
        pytest.fail(
            "PostgreSQL test database unavailable. Start `docker compose up -d "
            "postgres` and create `pipeline_test` (see README)."
        )
    upgrade_database()
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

    monkeypatch.setattr(db_ops, "get_embedding", fake_embedding)
    monkeypatch.setattr(mapping, "call_mapping_llm", fake_call_llm)


@pytest.fixture
def simple_schema_file(tmp_path: Path) -> Path:
    """Create a simple target-schema JSON file."""
    import json

    schema = {
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
