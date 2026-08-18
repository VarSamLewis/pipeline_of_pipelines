"""Integration tests for the FastAPI endpoints."""

from __future__ import annotations

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
    code = registered_client["code"]
    with open(simple_schema_file, "rb") as f:
        response = client.post(
            f"/clients/{code}/target-schema",
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
    code = registered_client["code"]
    response = client.post(
        f"/clients/{code}/ingest-folder",
        data={"folder_path": str(simple_data_folder)},
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_create_client(registered_client: dict) -> None:
    """Client creation should return a code and id."""
    assert registered_client["code"].startswith("testclient-")
    assert "id" in registered_client


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
        f"/clients/{registered_client['code']}/batches/{ingested_batch['ingestion_batch_id']}"
    )
    assert batch_response.status_code == 200
    batch = batch_response.json()
    assert any(f["original_filename"] == "data.csv" for f in batch.get("files", []))


def test_full_propose_approve_execute_flow(
    client: TestClient,
    registered_client: dict,
    client_with_schema: dict,
    ingested_batch: dict,
) -> None:
    """End-to-end: propose, approve, generate output folder, and verify CSV."""
    code = registered_client["code"]

    schema_response = client.get(f"/clients/{code}/target-schema")
    assert schema_response.status_code == 200
    schema = schema_response.json()

    spec_response = client.post(
        f"/clients/{code}/mapping-specs",
        json={
            "source_raw_file_ids": ingested_batch["raw_file_ids"],
            "target_schema": schema,
            "description": "integration test mapping",
        },
    )
    assert spec_response.status_code == 200, spec_response.text
    spec_id = spec_response.json()["id"]

    propose_response = client.post(f"/mapping-specs/{spec_id}/propose")
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
    assert folder["pipeline_py_path"].endswith("pipeline.py")
    assert folder["mapping_json_path"].endswith("mapping.json")
    assert folder["results_csv_path"].endswith("results.csv")
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
    assert "def main()" in pipeline_response.text
    assert "mapping.json" in pipeline_response.text

    csv_response = client.get(f"/output-folders/{spec_id}/results.csv")
    assert csv_response.status_code == 200
    csv_text = csv_response.text
    assert "record_id,full_name,score" in csv_text
    assert "1,Alice,88.5" in csv_text
    assert "2,Bob,92.0" in csv_text


@pytest.fixture
def proposed_spec(
    client: TestClient,
    registered_client: dict,
    client_with_schema: dict,
    ingested_batch: dict,
) -> dict:
    """Create and propose a mapping spec, returning its id and columns."""
    code = registered_client["code"]
    schema_response = client.get(f"/clients/{code}/target-schema")
    assert schema_response.status_code == 200
    schema = schema_response.json()

    spec_response = client.post(
        f"/clients/{code}/mapping-specs",
        json={
            "source_raw_file_ids": ingested_batch["raw_file_ids"],
            "target_schema": schema,
            "description": "test mapping",
        },
    )
    assert spec_response.status_code == 200, spec_response.text
    spec_id = spec_response.json()["id"]

    propose_response = client.post(f"/mapping-specs/{spec_id}/propose")
    assert propose_response.status_code == 200, propose_response.text
    return {"spec_id": spec_id, **propose_response.json()}


def test_upload_raw_file(
    client: TestClient,
    registered_client: dict,
) -> None:
    """Uploading a raw file should register it with metadata."""
    code = registered_client["code"]
    batch_response = client.post(
        f"/clients/{code}/batches",
        json={"label": "manual upload"},
    )
    assert batch_response.status_code == 200, batch_response.text
    batch_id = batch_response.json()["id"]

    response = client.post(
        f"/clients/{code}/batches/{batch_id}/files",
        files={"file": ("notes.txt", b"hello world", "text/plain")},
        data={"metadata": '{"source": "test"}'},
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["status"] == "received"
    assert payload["original_filename"] == "notes.txt"
    assert payload["sha256"]

    raw_response = client.get(f"/raw-files/{payload['id']}")
    assert raw_response.status_code == 200
    assert raw_response.json()["metadata"] == {"source": "test"}


def test_upload_raw_file_not_found(
    client: TestClient,
    registered_client: dict,
) -> None:
    """Uploading to a missing client or batch should return 404."""
    code = registered_client["code"]
    files = {"file": ("notes.txt", b"hello", "text/plain")}

    missing_client = client.post(
        f"/clients/does-not-exist/batches/{uuid.uuid4()}/files", files=files
    )
    assert missing_client.status_code == 404

    missing_batch = client.post(
        f"/clients/{code}/batches/{uuid.uuid4()}/files", files=files
    )
    assert missing_batch.status_code == 404


def test_parse_raw_file(
    client: TestClient,
    registered_client: dict,
    mock_openai: None,
) -> None:
    """Parsing a raw file should mark it parsed and produce a profile."""
    code = registered_client["code"]
    batch_response = client.post(f"/clients/{code}/batches", json={"label": "x"})
    batch_id = batch_response.json()["id"]
    upload_response = client.post(
        f"/clients/{code}/batches/{batch_id}/files",
        files={"file": ("data.csv", b"id,name\n1,Alice\n", "text/csv")},
    )
    assert upload_response.status_code == 200, upload_response.text
    raw_file_id = upload_response.json()["id"]

    response = client.post(f"/raw-files/{raw_file_id}/parse")
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["status"] == "parsed"
    assert payload["profile"] is not None

    raw_response = client.get(f"/raw-files/{raw_file_id}")
    assert raw_response.json()["status"] == "parsed"


def test_parse_raw_file_failure(
    client: TestClient,
    registered_client: dict,
    mock_openai: None,
) -> None:
    """A failing parse should mark the file failed with structured metadata."""
    code = registered_client["code"]
    batch_response = client.post(f"/clients/{code}/batches", json={"label": "x"})
    batch_id = batch_response.json()["id"]
    upload_response = client.post(
        f"/clients/{code}/batches/{batch_id}/files",
        files={
            "file": (
                "broken.xlsx",
                b"not a real workbook",
                "application/octet-stream",
            )
        },
    )
    assert upload_response.status_code == 200, upload_response.text
    raw_file_id = upload_response.json()["id"]

    response = client.post(f"/raw-files/{raw_file_id}/parse")
    assert response.status_code == 500, response.text

    raw_response = client.get(f"/raw-files/{raw_file_id}")
    assert raw_response.status_code == 200
    payload = raw_response.json()
    assert payload["status"] == "failed"
    assert payload["metadata"]["parse_error"]["code"] == "parse_failed"


def test_parse_raw_file_not_found(client: TestClient) -> None:
    """Parsing a missing raw file should return 404."""
    response = client.post(f"/raw-files/{uuid.uuid4()}/parse")
    assert response.status_code == 404


def test_update_mapping_column_audited(
    client: TestClient,
    proposed_spec: dict,
) -> None:
    """PATCHing a mapping column should persist the change and audit it."""
    column = proposed_spec["columns"][0]
    spec_id = proposed_spec["spec_id"]
    column_id = column["id"]

    response = client.patch(
        f"/mapping-specs/{spec_id}/columns/{column_id}",
        json={"polars_expression": "pl.col('id')", "tests": ["not_null"]},
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["polars_expression"] == "pl.col('id')"
    assert payload["tests"] == ["not_null"]

    audit_response = client.get(
        f"/audit-log?event_type=mapping_column_edited&entity_id={column_id}"
    )
    assert audit_response.status_code == 200
    assert len(audit_response.json()) >= 1


def test_update_mapping_column_not_found(
    client: TestClient,
    proposed_spec: dict,
) -> None:
    """PATCHing a missing spec or column should return 404."""
    spec_id = proposed_spec["spec_id"]
    missing_spec = client.patch(
        f"/mapping-specs/{uuid.uuid4()}/columns/{uuid.uuid4()}",
        json={"polars_expression": "pl.col('x')"},
    )
    assert missing_spec.status_code == 404

    missing_column = client.patch(
        f"/mapping-specs/{spec_id}/columns/{uuid.uuid4()}",
        json={"polars_expression": "pl.col('x')"},
    )
    assert missing_column.status_code == 404


def test_update_generated_pipeline_py(
    client: TestClient,
    proposed_spec: dict,
) -> None:
    """PUTting pipeline.py should overwrite the artifact and audit it."""
    spec_id = proposed_spec["spec_id"]
    approve_response = client.post(
        f"/mapping-specs/{spec_id}/approve",
        json={"reviewer": "integration-tester"},
    )
    assert approve_response.status_code == 200, approve_response.text
    output_response = client.post(f"/mapping-specs/{spec_id}/output-folder")
    assert output_response.status_code == 200, output_response.text

    content = "# edited pipeline\n"
    response = client.put(
        f"/output-folders/{spec_id}/pipeline.py",
        json={"content": content},
    )
    assert response.status_code == 200, response.text
    assert response.json()["bytes"] == len(content)

    get_response = client.get(f"/output-folders/{spec_id}/pipeline.py")
    assert get_response.status_code == 200
    assert get_response.text == content

    audit_response = client.get(
        f"/audit-log?event_type=pipeline_py_edited&entity_id={spec_id}"
    )
    assert audit_response.status_code == 200
    assert len(audit_response.json()) >= 1


def test_update_generated_pipeline_py_not_found(client: TestClient) -> None:
    """PUTting pipeline.py for a missing folder should return 404."""
    response = client.put(
        f"/output-folders/{uuid.uuid4()}/pipeline.py",
        json={"content": "x"},
    )
    assert response.status_code == 404


@pytest.fixture
def client() -> TestClient:
    """Return a FastAPI TestClient."""
    from app import app

    return TestClient(app)


@pytest.fixture
def registered_client(client: TestClient, unique_client_code: str) -> dict:
    """Create a client and return its record."""
    response = client.post(
        "/clients", json={"name": "Test Client", "code": unique_client_code}
    )
    assert response.status_code == 200, response.text
    return response.json()
