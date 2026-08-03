"""Tests for the results direct-edit surfaces (provenance, code, overrides)."""

from __future__ import annotations

import uuid
from types import SimpleNamespace

from fastapi.testclient import TestClient


def _run_view(spec_id: str) -> dict:
    return {
        "run": SimpleNamespace(mapping_spec_id=uuid.UUID(spec_id)),
        "results_csv": b"id,name\n1,Alice\n",
        "pipeline_py": b"# generated\nimport polars as pl\n",
        "mapping_json": b"{}",
    }


def test_column_provenance_renders_mapping_rule(monkeypatch) -> None:
    """A column header should expose the mapping rule behind it."""
    import ui
    import workflow
    from app import app

    spec_id = str(uuid.uuid4())
    run_id = str(uuid.uuid4())
    monkeypatch.setattr(ui, "get_result_review", lambda run_id: _run_view(spec_id))
    monkeypatch.setattr(
        workflow,
        "get_column_provenance",
        lambda spec_id, column: {
            "target_table": "customers",
            "target_column": column,
            "transformation_type": "expression",
            "polars_expression": "pl.col('name')",
            "source_columns": [
                {"source_table": "clients.csv::Sheet1", "source_column": "name"}
            ],
            "tests": ["not_null"],
        },
    )

    response = TestClient(app).get(
        f"/results/{run_id}/columns/name/provenance",
        headers={"HX-Request": "true"},
    )

    assert response.status_code == 200
    assert "Column provenance" in response.text
    assert "clients.csv::Sheet1" in response.text
    assert "not_null" in response.text


def test_code_save_overwrites_pipeline(monkeypatch) -> None:
    """Saving the code editor should write pipeline.py and audit."""
    import workflow
    from app import app

    spec_id = str(uuid.uuid4())
    run_id = str(uuid.uuid4())
    monkeypatch.setattr(
        workflow, "get_result_review", lambda run_id: _run_view(spec_id)
    )

    written: dict = {}

    def fake_overwrite(spec_id, content, user=None):
        written["spec_id"] = spec_id
        written["content"] = content
        return {"folder_id": str(spec_id), "bytes": len(content)}

    monkeypatch.setattr(workflow, "overwrite_pipeline_code", fake_overwrite)

    response = TestClient(app).post(
        f"/results/{run_id}/code",
        data={"code": "# manual edit\nprint('hi')\n"},
        headers={"HX-Request": "true"},
    )

    assert response.status_code == 204
    assert response.headers["HX-Redirect"] == f"/results/{run_id}/code"
    assert written["content"] == "# manual edit\nprint('hi')\n"


def test_code_reset_regenerates(monkeypatch) -> None:
    """Resetting should regenerate pipeline.py from the mapping contract."""
    import workflow
    from app import app

    spec_id = str(uuid.uuid4())
    run_id = str(uuid.uuid4())
    monkeypatch.setattr(
        workflow, "get_result_review", lambda run_id: _run_view(spec_id)
    )
    monkeypatch.setattr(
        workflow,
        "reset_pipeline_code",
        lambda spec_id, user=None: "# regenerated\n",
    )

    response = TestClient(app).post(
        f"/results/{run_id}/code/reset",
        headers={"HX-Request": "true"},
    )

    assert response.status_code == 200
    assert "# regenerated" in response.text


def test_override_modal_renders(monkeypatch) -> None:
    """Clicking a results cell should open the override modal with a reason box."""
    from app import app

    run_id = str(uuid.uuid4())
    response = TestClient(app).get(
        f"/results/{run_id}/overrides/new?column=name&row=1",
        headers={"HX-Request": "true"},
    )

    assert response.status_code == 200
    assert "Override cell" in response.text
    assert 'name="column" value="name"' in response.text
    assert 'name="row_key" value="1"' in response.text
    assert 'name="reason"' in response.text


def test_override_create_requires_reason(monkeypatch) -> None:
    """An override without a reason should be rejected."""
    import workflow
    from app import app

    spec_id = str(uuid.uuid4())
    run_id = str(uuid.uuid4())
    monkeypatch.setattr(
        workflow, "get_result_review", lambda run_id: _run_view(spec_id)
    )
    created: dict = {}

    def fake_create(
        run_id,
        *,
        target_table,
        target_column,
        row_key,
        value,
        reason,
        created_by=None,
    ):
        created["column"] = target_column
        created["reason"] = reason
        return None

    monkeypatch.setattr(workflow, "create_result_override_record", fake_create)

    response = TestClient(app).post(
        f"/results/{run_id}/overrides",
        data={
            "target_table": "customers",
            "column": "name",
            "row_key": "1",
            "value": "Alicia",
            "reason": "Typo in source; corrected after reconciliation.",
        },
        headers={"HX-Request": "true"},
    )

    assert response.status_code == 204
    assert response.headers["HX-Redirect"] == f"/results/{run_id}/csv"
    assert created["column"] == "name"

    rejected = TestClient(app).post(
        f"/results/{run_id}/overrides",
        data={
            "target_table": "customers",
            "column": "name",
            "row_key": "1",
            "value": "Alicia",
            "reason": "  ",
        },
        headers={"HX-Request": "true"},
    )
    assert rejected.status_code == 200
    assert "A reason is required" in rejected.text


def test_get_merged_results_applies_overrides(monkeypatch) -> None:
    """Overrides keyed by row/column should win over generated values."""
    import db_ops
    import mapping_specs
    import workflow

    spec_id = str(uuid.uuid4())
    run_id = str(uuid.uuid4())
    monkeypatch.setattr(workflow, "get_result_review", lambda rid: _run_view(spec_id))
    monkeypatch.setattr(
        mapping_specs,
        "load_mapping_spec",
        lambda sid: {"columns": [{"target_table": "customers"}]},
    )

    class FakeOverride:
        def __init__(self, row_key, target_column, value, reason, created_by):
            self.id = uuid.uuid4()
            self.row_key = row_key
            self.target_column = target_column
            self.value = value
            self.reason = reason
            self.created_by = created_by

    monkeypatch.setattr(
        db_ops,
        "list_result_overrides",
        lambda session, sid: [
            FakeOverride("1", "name", "Alicia", "source typo", "tester@example.com")
        ],
    )

    class FakeSession:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    monkeypatch.setattr(db_ops, "get_session", lambda: FakeSession())

    merged = workflow.get_merged_results(uuid.UUID(run_id))

    assert merged is not None
    assert merged["target_table"] == "customers"
    assert merged["merged_rows"][0]["name"] == "Alicia"
    assert merged["overridden"][("1", "name")] is True
    assert merged["override_count"] == 1
    assert merged["overrides"][0]["reason"] == "source typo"
