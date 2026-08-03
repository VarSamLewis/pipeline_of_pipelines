"""Tests for the direct-edit mapping drawer."""

from __future__ import annotations

import uuid

from fastapi.testclient import TestClient


def _mapping_review(column_id: str) -> dict:
    return {
        "id": str(uuid.uuid4()),
        "columns": [
            {
                "id": column_id,
                "target_table": "customers",
                "target_column": "name",
                "transformation_type": "expression",
                "transformation_logic": "Pass through the name column.",
                "polars_expression": "pl.col('name')",
                "source_columns": [
                    {"source_table": "clients.csv::Sheet1", "source_column": "name"}
                ],
                "tests": ["not_null"],
                "sort_order": 0,
            }
        ],
        "source_catalogs": [
            {
                "original_filename": "clients.csv",
                "warnings": [],
                "tables": [
                    {
                        "source_table_id": "file-1",
                        "sheet_name": "Sheet1",
                        "display_name": "clients.csv::Sheet1",
                        "columns": [
                            {
                                "source_file_id": "file-1",
                                "name": "name",
                                "normalized_name": "name",
                            }
                        ],
                    }
                ],
            }
        ],
    }


def test_mapping_column_edit_drawer_renders(monkeypatch) -> None:
    """Reviewers should see a pre-filled edit drawer for a mapping column."""
    import ui
    from app import app

    column_id = str(uuid.uuid4())
    mapping = _mapping_review(column_id)
    monkeypatch.setattr(ui, "get_mapping_review", lambda spec_id: mapping)

    response = TestClient(app).get(
        f"/mapping/{mapping['id']}/columns/{column_id}/edit",
        headers={"HX-Request": "true"},
    )

    assert response.status_code == 200
    assert "Edit mapping column" in response.text
    assert 'name="target_column" value="name"' in response.text
    assert 'value="expression" selected' in response.text
    assert "file-1::Sheet1::name" in response.text
    assert "not_null" in response.text


def test_mapping_column_edit_drawer_404_for_unknown_column(monkeypatch) -> None:
    """Editing a column that does not belong to the spec returns 404."""
    import ui
    from app import app

    mapping = _mapping_review(str(uuid.uuid4()))
    monkeypatch.setattr(ui, "get_mapping_review", lambda spec_id: mapping)

    response = TestClient(app).get(
        f"/mapping/{mapping['id']}/columns/{uuid.uuid4()}/edit",
        headers={"HX-Request": "true"},
    )

    assert response.status_code == 404
