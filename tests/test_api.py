"""Integration tests for the FastAPI endpoints."""

from __future__ import annotations

import re
import uuid
from datetime import datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


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
    assert response.headers["X-Request-ID"]


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
            "organization_id": registered_client["organization_id"],
            "role": "member",
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


def test_repeat_upload_is_preserved_and_visible_on_website(
    client: TestClient, registered_client: dict
) -> None:
    """Two identical uploads remain separate records and website inventory rows."""
    from db_ops import get_session
    from models import RawFile

    client_id = registered_client["id"]
    uploads = []
    for _ in range(2):
        batch = client.post(f"/clients/{client_id}/batches", json={"label": "repeat"})
        assert batch.status_code == 200, batch.text
        response = client.post(
            f"/clients/{client_id}/batches/{batch.json()['id']}/files",
            files={"file": ("same.csv", b"id,name\n1,Alice\n", "text/csv")},
        )
        assert response.status_code == 200, response.text
        uploads.append(response.json())

    assert uploads[0]["id"] != uploads[1]["id"]
    assert uploads[0]["sha256"] == uploads[1]["sha256"]
    assert all(item["original_filename"] == "same.csv" for item in uploads)
    for item in uploads:
        assert datetime.fromisoformat(item["uploaded_at"])
        fetched = client.get(f"/raw-files/{item['id']}")
        assert fetched.status_code == 200
        assert fetched.json()["uploaded_at"] == item["uploaded_at"]

    with get_session() as session:
        stored = [session.get(RawFile, uuid.UUID(item["id"])) for item in uploads]
        assert all(item is not None for item in stored)
        assert stored[0].storage_key != stored[1].storage_key

    files_page = client.get(f"/clients/{client_id}/files")
    assert files_page.status_code == 200, files_page.text
    assert files_page.text.count("same.csv") >= 2
    assert all(item["uploaded_at"][:10] in files_page.text for item in uploads)


def test_two_schemas_create_independent_reviewable_mappings(
    client: TestClient,
    registered_client: dict,
    ingested_batch: dict,
    simple_schema_file: Path,
    mock_openai: None,
) -> None:
    """Shared source files produce two independently proposed and reviewed specs."""
    import json

    client_id = registered_client["id"]
    schema = json.loads(simple_schema_file.read_text())
    other_schema = {**schema, "name": "another-output"}
    created = client.post(
        f"/clients/{client_id}/mapping-specs",
        json={
            "source_raw_file_ids": ingested_batch["raw_file_ids"],
            "target_schemas": [schema, other_schema],
        },
    )
    assert created.status_code == 200, created.text
    drafts = created.json()["mapping_specs"]
    assert len(drafts) == 2
    assert drafts[0]["id"] != drafts[1]["id"]
    assert [draft["target_schema"]["name"] for draft in drafts] == [
        "default",
        "another-output",
    ]
    for draft in drafts:
        spec_id = draft["id"]
        spec = client.get(f"/mapping-specs/{spec_id}")
        assert spec.status_code == 200
        assert spec.json()["source_raw_file_ids"] == ingested_batch["raw_file_ids"]
        proposed = client.post(
            f"/mapping-specs/{spec_id}/propose",
            headers={
                "X-OpenAI-Api-Key": "test-byok-key",
                "X-OpenAI-Base-Url": "https://api.openai.com/v1",
            },
        )
        assert proposed.status_code == 200, proposed.text
        review = client.get(f"/mapping/{spec_id}")
        assert review.status_code == 200, review.text
        target = client.get(f"/mapping/{spec_id}/target-schema.json")
        assert target.status_code == 200
        assert target.json()["name"] == draft["target_schema"]["name"]

    approved = client.post(
        f"/mapping-specs/{drafts[0]['id']}/approve",
        json={"reviewer": "integration-tester"},
    )
    assert approved.status_code == 200, approved.text
    assert client.get(f"/mapping-specs/{drafts[1]['id']}").json()["status"] == (
        "proposed"
    )


def test_bulk_schema_validation_does_not_leave_partial_drafts(
    client: TestClient, registered_client: dict, simple_schema_file: Path
) -> None:
    import json

    client_id = registered_client["id"]
    before = client.get(f"/mapping-specs?client_id={client_id}").json()
    invalid = client.post(
        f"/clients/{client_id}/mapping-specs",
        json={
            "target_schemas": [
                json.loads(simple_schema_file.read_text()),
                {"name": "broken", "tables": "not-a-list"},
            ]
        },
    )
    assert invalid.status_code == 422
    assert client.get(f"/mapping-specs?client_id={client_id}").json() == before


def test_website_uploads_two_schemas_and_links_both_reviews(
    client: TestClient,
    registered_client: dict,
    simple_schema_file: Path,
    mock_openai: None,
) -> None:
    """Exercise the real upload form, source parser, and mapping proposals."""
    import json

    from byok import save_organization_api_key
    from db_ops import get_session
    from models import OrganizationMembership, OrganizationRole, User

    org_id = uuid.UUID(registered_client["organization_id"])
    local_id = uuid.UUID(int=0)
    with get_session() as session:
        if session.get(User, local_id) is None:
            session.add(
                User(
                    id=local_id,
                    external_user_id="local-dev",
                    email="local-dev@example.com",
                )
            )
            session.commit()
        session.add(
            OrganizationMembership(
                organization_id=org_id,
                user_id=local_id,
                role=OrganizationRole.ORG_ADMIN,
            )
        )
        session.commit()
        save_organization_api_key(
            session, org_id, "test-byok-key", "https://api.openai.com/v1"
        )

    schema = json.loads(simple_schema_file.read_text())
    second = {**schema, "name": "second-output"}
    response = client.post(
        "/upload",
        data={
            "organization_select": str(org_id),
            "client_select": registered_client["id"],
        },
        files=[
            (
                "source_files",
                ("data.csv", b"id,name,score\n1,Alice,88.5\n", "text/csv"),
            ),
            ("target_schema", ("first.json", json.dumps(schema), "application/json")),
            ("target_schema", ("second.json", json.dumps(second), "application/json")),
        ],
    )
    assert response.status_code == 200, response.text
    import re

    review_ids = re.findall(r"/mapping/([0-9a-f-]{36})", response.text)
    assert len(review_ids) == 2 and review_ids[0] != review_ids[1]
    assert "first.json" in response.text and "second.json" in response.text
    for spec_id in review_ids:
        assert client.get(f"/mapping/{spec_id}").status_code == 200
    first = client.get(f"/mapping-specs/{review_ids[0]}").json()
    second_spec = client.get(f"/mapping-specs/{review_ids[1]}").json()
    assert first["source_raw_file_ids"] == second_spec["source_raw_file_ids"]
    assert first["status"] == second_spec["status"] == "proposed"


def test_multi_table_outputs_download_and_preview_independently(
    client: TestClient,
    registered_client: dict,
    ingested_batch: dict,
    simple_schema_file: Path,
    mock_openai: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An executed mapping exposes one real CSV per target table in API and UI."""
    import json

    import mapping

    schema = json.loads(simple_schema_file.read_text())
    schema["tables"].append({**schema["tables"][0], "name": "records_copy"})
    original_call = mapping.call_mapping_llm

    def two_table_response(*args, **kwargs):
        response = original_call(*args, **kwargs)
        response["mappings"].extend(
            {**column, "target_table": "records_copy"}
            for column in list(response["mappings"])
        )
        return response

    monkeypatch.setattr(mapping, "call_mapping_llm", two_table_response)
    client_id = registered_client["id"]
    created = client.post(
        f"/clients/{client_id}/mapping-specs",
        json={
            "source_raw_file_ids": ingested_batch["raw_file_ids"],
            "target_schema": schema,
        },
    )
    assert created.status_code == 200, created.text
    spec_id = created.json()["id"]
    proposed = client.post(
        f"/mapping-specs/{spec_id}/propose",
        headers={
            "X-OpenAI-Api-Key": "test-byok-key",
            "X-OpenAI-Base-Url": "https://api.openai.com/v1",
        },
    )
    assert proposed.status_code == 200, proposed.text
    assert (
        client.post(
            f"/mapping-specs/{spec_id}/approve", json={"reviewer": "integration-tester"}
        ).status_code
        == 200
    )
    generated = client.post(f"/mapping-specs/{spec_id}/output-folder")
    assert generated.status_code == 200, generated.text
    for table in ("records", "records_copy"):
        download = client.get(f"/output-folders/{spec_id}/tables/{table}.csv")
        assert download.status_code == 200, download.text
        assert "1,Alice,88.5" in download.text
    assert (
        client.get(f"/output-folders/{spec_id}/tables/unknown.csv").status_code == 404
    )

    # Execute to get a run ID for the website's result-review screen.
    executed = client.post(f"/mapping-specs/{spec_id}/execute")
    assert executed.status_code == 200, executed.text
    run_id = executed.json()["execution_run_id"]
    page = client.get(f"/results/{run_id}")
    assert page.status_code == 200, page.text
    assert "View records.csv" in page.text
    assert "View records_copy.csv" in page.text
    for table in ("records", "records_copy"):
        preview = client.get(f"/results/{run_id}/csv?file={table}.csv")
        assert preview.status_code == 200, preview.text
        assert "Alice" in preview.text


def test_cli_proposes_two_mappings_against_real_api(
    client: TestClient,
    registered_client: dict,
    ingested_batch: dict,
    simple_schema_file: Path,
    mock_openai: None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CLI arguments lead to two persisted API mappings, not fake client calls."""
    import json

    import pop_cli.main as cli
    from byok import save_organization_api_key
    from db_ops import get_session
    from pop_cli.workflow import Api
    from typer.testing import CliRunner

    with get_session() as session:
        save_organization_api_key(
            session,
            uuid.UUID(registered_client["organization_id"]),
            "test-byok-key",
            "https://api.openai.com/v1",
        )

    schema_b = tmp_path / "second.json"
    schema = json.loads(simple_schema_file.read_text())
    schema_b.write_text(json.dumps({**schema, "name": "second-output"}))
    manifest = tmp_path / "upload.json"
    manifest.write_text(
        json.dumps(
            {
                "client_id": registered_client["id"],
                "files": [{"id": raw_id} for raw_id in ingested_batch["raw_file_ids"]],
            }
        )
    )

    def local_api():
        api = Api.__new__(Api)
        api.client = TestClient(client.app)
        return api

    monkeypatch.setattr(cli, "Api", local_api)
    outputs = tmp_path / "mappings"
    result = CliRunner().invoke(
        cli.app,
        [
            "propose",
            "--client",
            registered_client["id"],
            "--manifest",
            str(manifest),
            "--schema",
            str(simple_schema_file),
            "--schema",
            str(schema_b),
            "--output-dir",
            str(outputs),
        ],
    )
    assert result.exit_code == 0, result.output
    mappings = sorted(outputs.glob("*-mapping.json"))
    assert len(mappings) == 2
    for path in mappings:
        saved = json.loads(path.read_text())
        spec = client.get(f"/mapping-specs/{saved['id']}")
        assert spec.status_code == 200
        assert spec.json()["status"] == "proposed"


@pytest.fixture
def client() -> TestClient:
    """Return a FastAPI TestClient."""
    from app import app

    return TestClient(app)


@pytest.fixture
def registered_client(client: TestClient) -> dict:
    """Create a client and return its record."""
    from db_ops import get_session
    from models import Organization

    with get_session() as session:
        organization = Organization(name=f"Test organization {uuid.uuid4()}")
        session.add(organization)
        session.commit()
        session.refresh(organization)
    response = client.post(
        "/clients",
        json={"name": "Test Client", "organization_id": str(organization.id)},
    )
    assert response.status_code == 200, response.text
    return {**response.json(), "organization_id": str(organization.id)}
