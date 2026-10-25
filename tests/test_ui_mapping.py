"""Tests for source diagnostics on the mapping review page."""

from __future__ import annotations

import uuid
from types import SimpleNamespace

from fastapi.testclient import TestClient


def test_settings_omits_reviewer_controls(monkeypatch) -> None:
    """Organization settings only manage organization credentials."""
    import ui
    from app import app

    monkeypatch.setattr(
        ui,
        "get_organization_key_settings",
        lambda user: [
            {
                "id": uuid.uuid4(),
                "name": "Acme",
                "key_suffix": None,
                "provider_base_url": None,
            }
        ],
    )

    response = TestClient(app).get("/settings")

    assert response.status_code == 200
    assert "Provider base URL" in response.text
    assert "Reviewers" not in response.text
    assert "Invite reviewer" not in response.text


def test_upload_uses_client_terminology(monkeypatch) -> None:
    """The upload form distinguishes authorization organizations from clients."""
    import ui
    from app import app

    organization_id = uuid.uuid4()
    monkeypatch.setattr(
        ui,
        "list_organizations",
        lambda user: [SimpleNamespace(id=organization_id, name="Acme")],
    )
    monkeypatch.setattr(ui, "list_visible_clients", lambda user: [])

    response = TestClient(app).get("/upload")

    assert response.status_code == 200
    assert 'label for="client_select">Client<' in response.text
    assert "Client context" not in response.text
    assert "New client name" in response.text
    assert "Select a client to view its files." in response.text


def test_client_files_panel_shows_authorized_summary(monkeypatch) -> None:
    """The upload panel renders a selected client's file summary."""
    import ui
    from app import app

    client_id = uuid.uuid4()
    monkeypatch.setattr(
        ui,
        "get_client_file_library",
        lambda requested_client_id, user: {
            "client": {"id": requested_client_id, "name": "Acme"},
            "target_schema": {"name": "records", "table_count": 2},
            "source_files": [
                {
                    "name": "orders.csv",
                    "mime_type": "text/csv",
                    "size_bytes": 42,
                    "status": "parsed",
                }
            ],
            "mappings": [{"id": uuid.uuid4()}],
        },
    )

    response = TestClient(app).get(f"/clients/{client_id}/files/panel")

    assert response.status_code == 200
    assert "Acme" in response.text
    assert "orders.csv" in response.text
    assert "records (2 tables)" in response.text
    assert f"/clients/{client_id}/files" in response.text


def test_runs_page_lists_organization_history(monkeypatch) -> None:
    """Runs are listed for the selected organization with links to details."""
    import ui
    from app import app

    organization_id = uuid.uuid4()
    run_id = uuid.uuid4()
    monkeypatch.setattr(
        ui,
        "list_organizations",
        lambda user: [SimpleNamespace(id=organization_id, name="Acme")],
    )
    monkeypatch.setattr(
        ui,
        "list_organization_runs",
        lambda requested_organization_id, user: [
            {
                "id": run_id,
                "client_name": "Orders",
                "status": "success",
                "target_environment": "production",
                "started_at": "2026-10-25T10:00:00",
                "finished_at": "2026-10-25T10:01:00",
            }
        ],
    )

    response = TestClient(app).get(f"/runs?organization_id={organization_id}")

    assert response.status_code == 200
    assert "Previous runs" in response.text
    assert "Orders" in response.text
    assert "production" in response.text
    assert f"/results/{run_id}" in response.text
    assert "Organization settings" in response.text


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


def test_client_files_page_lists_workflow_files(monkeypatch) -> None:
    """The client library groups source and generated workflow files."""
    import ui
    from app import app

    client_id = uuid.uuid4()
    mapping_id = uuid.uuid4()
    monkeypatch.setattr(
        ui,
        "get_client_file_library",
        lambda requested_client_id, user: {
            "client": {"id": requested_client_id, "name": "Acme"},
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

    response = TestClient(app).get(f"/clients/{client_id}/files")

    assert response.status_code == 200
    assert "Acme files" in response.text
    assert "orders.csv" in response.text
    assert f"/output-folders/{mapping_id}/pipeline.py" in response.text
    assert f"/output-folders/{mapping_id}/mapping.json" in response.text
    assert f"/output-folders/{mapping_id}/results.csv" in response.text
