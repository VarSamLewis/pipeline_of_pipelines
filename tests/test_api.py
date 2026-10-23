"""Integration tests for the FastAPI endpoints."""

from __future__ import annotations

import re
import uuid
from pathlib import Path

import pytest
from conftest import requires_postgres
from fastapi.testclient import TestClient

pytestmark = requires_postgres


@pytest.fixture
def client_with_schema(
    client: TestClient,
    registered_client: dict,
    simple_schema_file: Path,
) -> dict:
    """Register a target schema for the test client."""
    client_id = registered_client["id"]
    with open(simple_schema_file, "rb") as f:
        response = client.post(
            f"/clients/{client_id}/target-schema",
            files={"schema_file": ("target_schema.json", f, "application/json")},
        )
    assert response.status_code == 200, response.text
    return response.json()


@pytest.fixture
def ingested_batch(
    client: TestClient,
    registered_client: dict,
    simple_data_folder: Path,
    mock_openai: None,
) -> dict:
    """Ingest the simple data folder."""
    client_id = registered_client["id"]
    response = client.post(
        f"/clients/{client_id}/ingest-folder",
        data={"folder_path": str(simple_data_folder)},
        headers={
            "X-OpenAI-Api-Key": "test-byok-key",
            "X-OpenAI-Base-Url": "https://api.openai.com/v1",
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_create_client(registered_client: dict) -> None:
    """Client creation should return a UUID without exposing a code."""
    assert uuid.UUID(registered_client["id"])
    assert "code" not in registered_client


def test_output_folder_override_is_rejected(client: TestClient) -> None:
    """Artifact output locations are always controlled by the server."""
    response = client.post(
        f"/mapping-specs/{uuid.uuid4()}/output-folder",
        json={"output_folder": "/tmp/escape"},
    )
    assert response.status_code == 422


def test_generated_pipeline_is_not_mutable(client: TestClient) -> None:
    """Generated code can be reviewed but not replaced through the API."""
    response = client.put(
        f"/output-folders/{uuid.uuid4()}/pipeline.py",
        json={"content": "raise RuntimeError('unsafe')"},
    )
    assert response.status_code == 405


def test_responses_include_browser_security_headers(client: TestClient) -> None:
    """Every application response includes the baseline browser protections."""
    response = client.get("/health")
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["X-Frame-Options"] == "DENY"
    assert response.headers["Referrer-Policy"] == "strict-origin-when-cross-origin"


def test_health_check(client: TestClient) -> None:
    """The liveness endpoint is available without authentication."""
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_manage_beta_invitation(
    client: TestClient,
    registered_client: dict,
) -> None:
    """Admins can create, inspect, and revoke client invitations."""
    created = client.post(
        "/admin/invitations",
        json={
            "email": "operator@example.com",
            "client_id": registered_client["id"],
            "role": "reviewer",
        },
    )
    assert created.status_code == 200, created.text
    invitation = created.json()
    assert invitation["email"] == "operator@example.com"
    assert invitation["status"] == "pending"

    listed = client.get("/admin/invitations?status=pending")
    assert listed.status_code == 200, listed.text
    assert invitation["id"] in {entry["id"] for entry in listed.json()}

    revoked = client.post(f"/admin/invitations/{invitation['id']}/revoke")
    assert revoked.status_code == 200, revoked.text
    assert revoked.json()["status"] == "revoked"


def test_upload_target_schema(client_with_schema: dict) -> None:
    """Target schema upload should store the schema."""
    schema_payload = client_with_schema["schema"]
    assert schema_payload["name"] == "default"
    assert len(schema_payload["tables"]) == 1
    assert schema_payload["tables"][0]["name"] == "records"


def test_ingest_folder(
    client: TestClient,
    registered_client: dict,
    ingested_batch: dict,
) -> None:
    """Folder ingestion should register raw files and evidence."""
    assert len(ingested_batch["raw_file_ids"]) >= 1
    batch_response = client.get(
        f"/clients/{registered_client['id']}/batches/{ingested_batch['ingestion_batch_id']}"
    )
    assert batch_response.status_code == 200
    batch = batch_response.json()
    assert any(f["original_filename"] == "data.csv" for f in batch.get("files", []))


def test_full_propose_approve_execute_flow(
    client: TestClient,
    mock_openai: None,
    registered_client: dict,
    client_with_schema: dict,
    ingested_batch: dict,
) -> None:
    """End-to-end: propose, approve, generate output folder, and verify CSV."""
    client_id = registered_client["id"]

    schema_response = client.get(f"/clients/{client_id}/target-schema")
    assert schema_response.status_code == 200
    schema = schema_response.json()

    spec_response = client.post(
        f"/clients/{client_id}/mapping-specs",
        json={
            "source_raw_file_ids": ingested_batch["raw_file_ids"],
            "target_schema": schema,
            "description": "integration test mapping",
        },
    )
    assert spec_response.status_code == 200, spec_response.text
    spec_id = spec_response.json()["id"]

    propose_response = client.post(
        f"/mapping-specs/{spec_id}/propose",
        headers={
            "X-OpenAI-Api-Key": "test-byok-key",
            "X-OpenAI-Base-Url": "https://api.openai.com/v1",
        },
    )
    assert propose_response.status_code == 200, propose_response.text
    proposed = propose_response.json()
    columns = proposed["columns"]
    assert len(columns) == 3
    by_target = {c["target_column"]: c for c in columns}
    assert by_target["record_id"]["source_columns"][0]["source_column"] == "id"
    assert by_target["full_name"]["source_columns"][0]["source_column"] == "name"
    assert by_target["score"]["source_columns"][0]["source_column"] == "score"

    approve_response = client.post(
        f"/mapping-specs/{spec_id}/approve", json={"reviewer": "integration-tester"}
    )
    assert approve_response.status_code == 200, approve_response.text
    assert approve_response.json()["status"] == "approved"

    output_response = client.post(f"/mapping-specs/{spec_id}/output-folder")
    assert output_response.status_code == 200, output_response.text
    folder = output_response.json()
    filename_prefix = r".+__records__\d{8}T\d{6}Z"
    assert re.search(filename_prefix + r"_pipeline\.py$", folder["pipeline_py_path"])
    assert re.search(filename_prefix + r"_mapping\.json$", folder["mapping_json_path"])
    assert re.search(filename_prefix + r"_results\.csv$", folder["results_csv_path"])
    assert set(Path(folder["folder_path"]).glob("*")) == {
        Path(folder["pipeline_py_path"]),
        Path(folder["mapping_json_path"]),
        Path(folder["results_csv_path"]),
    }

    mapping_response = client.get(f"/output-folders/{spec_id}/mapping.json")
    assert mapping_response.status_code == 200
    mapping_artifact = mapping_response.json()
    assert mapping_artifact["id"] == spec_id
    assert {column["target_column"] for column in mapping_artifact["columns"]} == {
        "record_id",
        "full_name",
        "score",
    }

    pipeline_response = client.get(f"/output-folders/{spec_id}/pipeline.py")
    assert pipeline_response.status_code == 200
    assert "def build_target_tables()" in pipeline_response.text
    assert "_mapping.json" in pipeline_response.text

    csv_response = client.get(f"/output-folders/{spec_id}/results.csv")
    assert csv_response.status_code == 200
    csv_text = csv_response.text
    assert "record_id,full_name,score" in csv_text
    assert "1,Alice,88.5" in csv_text
    assert "2,Bob,92.0" in csv_text


@pytest.fixture
def client() -> TestClient:
    """Return a FastAPI TestClient."""
    from app import app

    return TestClient(app)


@pytest.fixture
def registered_client(client: TestClient) -> dict:
    """Create a client and return its record."""
    response = client.post(
        "/clients", json={"name": "Test Client"}
    )
    assert response.status_code == 200, response.text
    return response.json()
