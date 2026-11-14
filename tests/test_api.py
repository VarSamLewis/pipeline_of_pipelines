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
        assert all(
            re.search(r"/raw/same_\d{8}T\d{12}Z\.csv$", item.storage_key)
            for item in stored
        )

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
    command = [
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
    ]
    result = CliRunner().invoke(cli.app, command)
    assert result.exit_code == 0, result.output
    mappings = sorted(outputs.glob("*-mapping.json"))
    assert len(mappings) == 2
    for path in mappings:
        saved = json.loads(path.read_text())
        spec = client.get(f"/mapping-specs/{saved['id']}")
        assert spec.status_code == 200
        assert spec.json()["status"] == "proposed"

    # Repeat the same command: reuse each proposed review instead of failing
    # because the output files already exist.
    previous_ids = [json.loads(path.read_text())["id"] for path in mappings]
    rerun = CliRunner().invoke(cli.app, command)
    assert rerun.exit_code == 0, rerun.output
    assert [json.loads(path.read_text())["id"] for path in mappings] == previous_ids

    approved = client.post(
        f"/mapping-specs/{previous_ids[0]}/approve", json={"reviewer": "tester"}
    )
    assert approved.status_code == 200, approved.text
    rerun = CliRunner().invoke(cli.app, command)
    assert rerun.exit_code == 0, rerun.output
    assert json.loads(mappings[0].read_text())["id"] != previous_ids[0]
    assert json.loads(mappings[1].read_text())["id"] == previous_ids[1]
    assert client.get(f"/mapping-specs/{previous_ids[0]}").json()["status"] == (
        "approved"
    )


def test_failed_parse_rolls_back_chunks_and_can_be_retried(
    client: TestClient, registered_client: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed embedding cannot publish half of an upload's context."""
    import db_ops
    from db_ops import get_session
    from models import ExtractedEvidence
    from sqlmodel import select

    client_id = registered_client["id"]
    batch = client.post(f"/clients/{client_id}/batches", json={"label": "retry"})
    assert batch.status_code == 200
    uploaded = client.post(
        f"/clients/{client_id}/batches/{batch.json()['id']}/files",
        files={"file": ("notes.txt", b"a" * 1200, "text/plain")},
    )
    assert uploaded.status_code == 200
    file_id = uploaded.json()["id"]
    attempts = 0

    def embedding(_content: str, **_kwargs) -> list[float]:
        nonlocal attempts
        attempts += 1
        if attempts == 2:
            raise RuntimeError("Embedding service temporarily unavailable")
        return [1.0] + [0.0] * 1535

    monkeypatch.setattr(db_ops, "get_embedding", embedding)
    headers = {
        "X-OpenAI-Api-Key": "test-byok-key",
        "X-OpenAI-Base-Url": "https://api.openai.com/v1",
    }
    failed = client.post(f"/raw-files/{file_id}/parse", headers=headers)
    assert failed.status_code == 500
    assert client.get(f"/raw-files/{file_id}").json()["status"] == "failed"
    with get_session() as session:
        evidence = session.exec(
            select(ExtractedEvidence).where(
                ExtractedEvidence.raw_file_id == uuid.UUID(file_id)
            )
        ).all()
        assert evidence == []

    retry = client.post(f"/raw-files/{file_id}/parse", headers=headers)
    assert retry.status_code == 200, retry.text
    assert retry.json()["status"] == "parsed"
    with get_session() as session:
        evidence = session.exec(
            select(ExtractedEvidence).where(
                ExtractedEvidence.raw_file_id == uuid.UUID(file_id)
            )
        ).all()
        assert len(evidence) == 2
    assert (
        client.post(f"/raw-files/{file_id}/parse", headers=headers).status_code == 200
    )
    with get_session() as session:
        assert (
            len(
                session.exec(
                    select(ExtractedEvidence).where(
                        ExtractedEvidence.raw_file_id == uuid.UUID(file_id)
                    )
                ).all()
            )
            == 2
        )


def test_evidence_search_is_scoped_to_one_client(
    client: TestClient, registered_client: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Matching vectors from another client must never enter retrieval results."""
    import db_ops

    monkeypatch.setattr(
        db_ops, "get_embedding", lambda *_args, **_kwargs: [1.0] + [0.0] * 1535
    )
    second = client.post(
        "/clients",
        json={
            "name": "Other client",
            "organization_id": registered_client["organization_id"],
        },
    )
    assert second.status_code == 200
    client_ids = [registered_client["id"], second.json()["id"]]
    headers = {
        "X-OpenAI-Api-Key": "test-byok-key",
        "X-OpenAI-Base-Url": "https://api.openai.com/v1",
    }
    file_ids = []
    for client_id in client_ids:
        batch = client.post(f"/clients/{client_id}/batches", json={"label": "scope"})
        assert batch.status_code == 200
        uploaded = client.post(
            f"/clients/{client_id}/batches/{batch.json()['id']}/files",
            files={"file": ("notes.txt", b"shared topic", "text/plain")},
        )
        assert uploaded.status_code == 200
        file_id = uploaded.json()["id"]
        file_ids.append(file_id)
        assert (
            client.post(f"/raw-files/{file_id}/parse", headers=headers).status_code
            == 200
        )

    for client_id, file_id in zip(client_ids, file_ids, strict=True):
        response = client.get(
            "/evidence/search",
            params={"query": "shared topic", "client_id": client_id},
            headers=headers,
        )
        assert response.status_code == 200, response.text
        assert response.json()
        assert {item["raw_file_id"] for item in response.json()} == {file_id}
    assert (
        client.get("/evidence/search?query=shared%20topic", headers=headers).status_code
        == 422
    )


def test_chunk_partial_updates_and_ambiguous_matches(
    client: TestClient, registered_client: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    """New chunks replace only confident matches among this client's active chunks."""
    import math

    import db_ops
    from db_ops import get_session
    from models import ExtractedEvidence
    from sqlmodel import select

    client_id = registered_client["id"]

    def embedding(text: str, **_kwargs) -> list[float]:
        text = text.lower()
        angle = (
            90
            if "policy" in text
            else 20
            if "right" in text
            else 10
            if "new value" in text
            else 0
        )
        radians = math.radians(angle)
        return [math.cos(radians), math.sin(radians)] + [0.0] * 1534

    monkeypatch.setattr(db_ops, "get_embedding", embedding)
    headers = {
        "X-OpenAI-Api-Key": "test-byok-key",
        "X-OpenAI-Base-Url": "https://api.openai.com/v1",
    }

    def upload(text: str, selected_client: str = client_id) -> str:
        batch = client.post(
            f"/clients/{selected_client}/batches", json={"label": "context"}
        )
        assert batch.status_code == 200, batch.text
        response = client.post(
            f"/clients/{selected_client}/batches/{batch.json()['id']}/files",
            files={"file": ("context.txt", text.encode(), "text/plain")},
        )
        assert response.status_code == 200, response.text
        file_id = response.json()["id"]
        parsed = client.post(f"/raw-files/{file_id}/parse", headers=headers)
        assert parsed.status_code == 200, parsed.text
        return file_id

    old_file = upload("Product price is 10")
    policy_file = upload("Policy: retain records")
    new_file = upload("Product price is 20")
    with get_session() as session:
        chunks = session.exec(
            select(ExtractedEvidence).where(
                ExtractedEvidence.raw_file_id.in_(
                    [uuid.UUID(old_file), uuid.UUID(policy_file), uuid.UUID(new_file)]
                )
            )
        ).all()
        by_file = {str(chunk.raw_file_id): chunk for chunk in chunks}
        assert not by_file[old_file].active
        assert by_file[old_file].superseded_by_id == by_file[new_file].id
        assert by_file[new_file].active and by_file[policy_file].active

    found = client.get(
        "/evidence/search",
        params={"query": "price", "client_id": client_id, "top_k": 10},
        headers=headers,
    )
    assert found.status_code == 200, found.text
    assert old_file not in {item["raw_file_id"] for item in found.json()}
    assert new_file in {item["raw_file_id"] for item in found.json()}

    # Both candidates are equally close to the new chunk, so neither retires.
    separate = client.post(
        "/clients",
        json={
            "name": "Ambiguous context",
            "organization_id": registered_client["organization_id"],
        },
    )
    assert separate.status_code == 200
    separate_client = separate.json()["id"]
    left = upload("left value", separate_client)
    right = upload("right value", separate_client)
    ambiguous = upload("new value", separate_client)
    with get_session() as session:
        by_file = {
            str(chunk.raw_file_id): chunk
            for chunk in session.exec(
                select(ExtractedEvidence).where(
                    ExtractedEvidence.raw_file_id.in_(
                        [uuid.UUID(left), uuid.UUID(right), uuid.UUID(ambiguous)]
                    )
                )
            ).all()
        }
        assert by_file[left].active and by_file[right].active
        assert by_file[ambiguous].active


def test_mapping_keeps_its_context_snapshot_after_supersession(
    client: TestClient,
    registered_client: dict,
    ingested_batch: dict,
    simple_schema_file: Path,
    mock_openai: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Old reviews keep the evidence they saw; new retrieval hides it."""
    import json

    import db_ops
    from db_ops import get_session
    from models import ExtractedEvidence
    from sqlmodel import select

    monkeypatch.setattr(
        db_ops, "get_embedding", lambda *_args, **_kwargs: [1.0] + [0.0] * 1535
    )
    client_id = registered_client["id"]
    headers = {
        "X-OpenAI-Api-Key": "test-byok-key",
        "X-OpenAI-Base-Url": "https://api.openai.com/v1",
    }

    def upload(text: str) -> str:
        batch = client.post(f"/clients/{client_id}/batches", json={"label": "snapshot"})
        uploaded = client.post(
            f"/clients/{client_id}/batches/{batch.json()['id']}/files",
            files={"file": ("facts.txt", text.encode(), "text/plain")},
        )
        assert uploaded.status_code == 200, uploaded.text
        file_id = uploaded.json()["id"]
        parsed = client.post(f"/raw-files/{file_id}/parse", headers=headers)
        assert parsed.status_code == 200, parsed.text
        return file_id

    original_file = upload("Customer account fee is 10")
    with get_session() as session:
        old = session.exec(
            select(ExtractedEvidence).where(
                ExtractedEvidence.raw_file_id == uuid.UUID(original_file)
            )
        ).one()
        old_id = str(old.id)
    created = client.post(
        f"/clients/{client_id}/mapping-specs",
        json={
            "source_raw_file_ids": ingested_batch["raw_file_ids"],
            "target_schema": json.loads(simple_schema_file.read_text()),
        },
    )
    assert created.status_code == 200, created.text
    spec_id = created.json()["id"]
    proposed = client.post(f"/mapping-specs/{spec_id}/propose", headers=headers)
    assert proposed.status_code == 200, proposed.text
    assert old_id in client.get(f"/mapping-specs/{spec_id}").json()[
        "context_evidence_ids"
    ]

    upload("Customer account fee is 20")
    with get_session() as session:
        assert session.get(ExtractedEvidence, uuid.UUID(old_id)).active is False
    assert old_id in client.get(f"/mapping-specs/{spec_id}").json()[
        "context_evidence_ids"
    ]


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
