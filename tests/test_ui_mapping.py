"""Tests for source diagnostics on the mapping review page."""

from __future__ import annotations

import uuid

from fastapi.testclient import TestClient


def test_mapping_review_surfaces_catalog_warnings(
    monkeypatch,
) -> None:
    """Users should see parse warnings before the approval control."""
    import ui
    from app import app

    monkeypatch.setattr(
        ui,
        "get_mapping_review",
        lambda spec_id: {
            "id": str(spec_id),
            "columns": [
                {
                    "id": str(uuid.uuid4()),
                    "target_table": "records",
                    "target_column": "client_name",
                    "source_columns": [],
                    "transformation_logic": "Direct map",
                    "polars_expression": None,
                    "transformation_type": "expression",
                    "lookup_source_table": None,
                    "lookup_key": None,
                    "lookup_value": None,
                    "tests": ["not_null"],
                    "confidence": 0.8,
                }
            ],
            "source_catalogs": [
                {
                    "original_filename": "clients.csv",
                    "warnings": [
                        {
                            "code": "encoding_fallback",
                            "message": "Decoded using Windows-1252.",
                            "severity": "warning",
                        }
                    ],
                    "tables": [
                        {
                            "source_table_id": "table-1",
                            "display_name": "clients.csv",
                            "original_filename": "clients.csv",
                            "row_count": 2,
                            "confidence": 0.8,
                            "location": {
                                "sheet_name": None,
                                "cell_range": None,
                                "header_row": 2,
                            },
                            "warnings": [
                                {
                                    "code": "preamble_detected",
                                    "message": "Detected one title row.",
                                    "severity": "warning",
                                }
                            ],
                        }
                    ],
                }
            ],
        },
    )

    response = TestClient(app).get(f"/mapping/{uuid.uuid4()}")

    assert response.status_code == 200
    assert "Mapping editor" in response.text
    assert "Raw mapping JSON" in response.text
    assert "Discovered source tables" in response.text
    assert "encoding_fallback" in response.text
    assert "preamble_detected" in response.text
    assert response.text.index("Parsing warnings") < response.text.index(
        "Approve mapping &amp; generate output"
        if "Approve mapping &amp; generate output" in response.text
        else "Approve mapping & generate output"
    )
    assert '<label>Tests' not in response.text


def test_mapping_confirmation_displays_execution_errors(monkeypatch) -> None:
    """Pipeline execution failures are returned to the HTMX target."""
    import ui
    from app import app

    def fail(_: uuid.UUID) -> uuid.UUID:
        raise RuntimeError("Pipeline execution exceeded the configured time limit")

    monkeypatch.setattr(ui, "approve_mapping_and_execute", fail)

    response = TestClient(app).post(f"/mapping/{uuid.uuid4()}/confirm")

    assert response.status_code == 200
    assert "Pipeline execution exceeded the configured time limit" in response.text


def test_organization_files_page_lists_workflow_files(monkeypatch) -> None:
    """The organization library groups source and generated workflow files."""
    import ui
    from app import app

    organization_id = uuid.uuid4()
    mapping_id = uuid.uuid4()
    monkeypatch.setattr(
        ui,
        "get_organization_file_library",
        lambda client_id, user: {
            "organization": {"id": organization_id, "name": "Acme"},
            "target_schema": {"name": "records", "table_count": 1},
            "source_files": [
                {
                    "name": "orders.csv",
                    "mime_type": "text/csv",
                    "size_bytes": 42,
                    "status": "parsed",
                    "uploaded_at": "2026-01-01",
                }
            ],
            "mappings": [
                {
                    "id": mapping_id,
                    "status": "approved",
                    "description": "Orders mapping",
                    "created_at": "2026-01-01",
                    "execution_status": "success",
                    "files": [
                        {"name": "records_pipeline.py", "artifact_type": "pipeline"},
                        {"name": "records_mapping.json", "artifact_type": "mapping"},
                        {"name": "records_results.csv", "artifact_type": "results"},
                    ],
                }
            ],
        },
    )

    response = TestClient(app).get(f"/organizations/{organization_id}/files")

    assert response.status_code == 200
    assert "Acme files" in response.text
    assert "orders.csv" in response.text
    assert f"/output-folders/{mapping_id}/pipeline.py" in response.text
    assert f"/output-folders/{mapping_id}/mapping.json" in response.text
    assert f"/output-folders/{mapping_id}/results.csv" in response.text
