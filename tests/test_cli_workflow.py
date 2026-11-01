"""CLI workflow tests using the same HTTP contract as the API."""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import httpx
import pytest
from pop_cli.workflow import Api, _contains_values, document, propose, push, run, upload


def _api(handler) -> Api:
    api = Api.__new__(Api)
    api.client = httpx.Client(
        base_url="https://api.example.com",
        transport=httpx.MockTransport(handler),
    )
    return api


def test_upload_writes_manifest_and_preserves_success_on_failure(
    tmp_path: Path,
) -> None:
    client_id, batch_id, file_id = (str(uuid.uuid4()) for _ in range(3))
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path.endswith("/batches"):
            return httpx.Response(200, json={"id": batch_id})
        if len(calls) == 2:
            return httpx.Response(200, json={"id": file_id})
        return httpx.Response(413, json={"detail": "Too large"})

    paths = [tmp_path / "first.csv", tmp_path / "second.csv"]
    for path in paths:
        path.write_text("id\n1\n")
    manifest = tmp_path / "upload.json"
    with _api(handler) as api, pytest.raises(RuntimeError, match="Too large"):
        upload(api, client_id, paths, manifest)

    assert document(manifest)["files"][0]["id"] == file_id
    assert len(document(manifest)["files"]) == 1
    assert calls[0] == f"/clients/{client_id}/batches"


def test_same_file_can_be_uploaded_again_in_another_batch(tmp_path: Path) -> None:
    client_id = str(uuid.uuid4())
    path = tmp_path / "data.csv"
    path.write_text("id\n1\n")
    next_id = iter(str(uuid.uuid4()) for _ in range(4))

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/batches"):
            return httpx.Response(200, json={"id": next(next_id)})
        return httpx.Response(
            200, json={"id": next(next_id), "uploaded_at": "2026-11-01T12:00:00"}
        )

    with _api(handler) as api:
        first = upload(api, client_id, [path], tmp_path / "first-upload.json")
        second = upload(api, client_id, [path], tmp_path / "second-upload.json")

    assert first["files"][0]["id"] != second["files"][0]["id"]
    assert first["files"][0]["uploaded_at"] == "2026-11-01T12:00:00"
    assert first["files"][0]["sha256"] == second["files"][0]["sha256"]


def test_propose_parses_and_saves_review(tmp_path: Path) -> None:
    client_id, file_id, spec_id = (str(uuid.uuid4()) for _ in range(3))
    manifest = tmp_path / "upload.json"
    manifest.write_text(
        json.dumps({"client_id": client_id, "files": [{"id": file_id}]})
    )
    schema = tmp_path / "schema.json"
    schema.write_text('{"tables": []}')
    output = tmp_path / "mapping.json"
    calls: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path))
        if request.url.path == f"/clients/{client_id}/mapping-specs":
            return httpx.Response(200, json={"id": spec_id})
        if request.url.path == f"/raw-files/{file_id}":
            return httpx.Response(200, json={"status": "received"})
        if request.method == "GET":
            return httpx.Response(200, json={"id": spec_id, "status": "proposed"})
        return httpx.Response(200, json={})

    with _api(handler) as api:
        assert propose(api, client_id, schema, manifest, output)["id"] == spec_id
    assert document(output)["status"] == "proposed"
    assert calls == [
        ("POST", f"/clients/{client_id}/mapping-specs"),
        ("GET", f"/raw-files/{file_id}"),
        ("POST", f"/raw-files/{file_id}/parse"),
        ("POST", f"/mapping-specs/{spec_id}/propose"),
        ("GET", f"/mapping-specs/{spec_id}"),
    ]


def test_propose_can_resume_draft_without_reparsing(tmp_path: Path) -> None:
    client_id, file_id, spec_id = (str(uuid.uuid4()) for _ in range(3))
    manifest = tmp_path / "upload.json"
    manifest.write_text(
        json.dumps({"client_id": client_id, "files": [{"id": file_id}]})
    )
    schema = tmp_path / "schema.json"
    schema.write_text('{"tables": []}')
    calls: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path))
        if request.url.path == f"/raw-files/{file_id}":
            return httpx.Response(200, json={"status": "parsed"})
        if request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "id": spec_id,
                    "client_id": client_id,
                    "source_raw_file_ids": [file_id],
                    "target_schema_json": {"tables": []},
                    "status": "draft",
                },
            )
        return httpx.Response(200, json={})

    with _api(handler) as api:
        propose(
            api, client_id, schema, manifest, tmp_path / "mapping.json", spec_id=spec_id
        )
    assert ("POST", f"/clients/{client_id}/mapping-specs") not in calls
    assert ("POST", f"/raw-files/{file_id}/parse") not in calls
    assert ("POST", f"/mapping-specs/{spec_id}/propose") in calls


def test_retry_accepts_server_schema_defaults() -> None:
    assert _contains_values(
        {"name": "default", "tables": [{"name": "records", "columns": []}]},
        {"tables": [{"name": "records"}]},
    )


def test_push_dry_run_does_not_mutate(tmp_path: Path) -> None:
    spec_id, column_id = (str(uuid.uuid4()) for _ in range(2))
    mapping = tmp_path / "mapping.json"
    column = {
        "id": column_id,
        "target_table": "out",
        "target_column": "id",
        "tests": [],
    }
    mapping.write_text(
        json.dumps({"id": spec_id, "columns": [{**column, "tests": ["not_null"]}]})
    )
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.method)
        return httpx.Response(
            200, json={"id": spec_id, "status": "proposed", "columns": [column]}
        )

    with _api(handler) as api:
        assert push(api, mapping, dry_run=True) == [column_id]
        assert push(api, mapping) == [column_id]
    assert calls == ["GET", "GET", "PATCH"]


def test_run_rejects_unpushed_edits_before_approval(tmp_path: Path) -> None:
    spec_id, column_id = (str(uuid.uuid4()) for _ in range(2))
    mapping = tmp_path / "mapping.json"
    column = {
        "id": column_id,
        "target_table": "out",
        "target_column": "id",
        "tests": [],
    }
    mapping.write_text(
        json.dumps({"id": spec_id, "columns": [{**column, "tests": ["unique"]}]})
    )
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.method)
        return httpx.Response(
            200, json={"id": spec_id, "status": "proposed", "columns": [column]}
        )

    with _api(handler) as api, pytest.raises(ValueError, match="pop push"):
        run(api, mapping, "reviewer@example.com", tmp_path / "deliverables")
    assert calls == ["GET", "GET"]


def test_run_downloads_artifacts_after_approval(tmp_path: Path) -> None:
    spec_id = str(uuid.uuid4())
    mapping = tmp_path / "mapping.json"
    mapping.write_text(json.dumps({"id": spec_id, "columns": []}))
    calls: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path))
        if request.method == "GET" and request.url.path.endswith(
            ("pipeline.py", "mapping.json", "results.csv")
        ):
            return httpx.Response(200, content=b"artifact")
        if request.method == "GET":
            return httpx.Response(
                200, json={"id": spec_id, "status": "proposed", "columns": []}
            )
        if request.url.path.endswith("/execute"):
            return httpx.Response(
                200,
                json={"execution_run_id": "run-1", "csv_paths": {"records": "path"}},
            )
        return httpx.Response(200, json={})

    output = tmp_path / "deliverables"
    with _api(handler) as api:
        assert (
            run(api, mapping, "reviewer@example.com", output)["execution_run_id"]
            == "run-1"
        )
    assert calls[2:4] == [
        ("POST", f"/mapping-specs/{spec_id}/approve"),
        ("POST", f"/mapping-specs/{spec_id}/execute"),
    ]
    assert {path.name for path in output.iterdir()} == {
        "pipeline.py",
        "mapping.json",
        "results.csv",
    }


def test_run_downloads_each_target_table_when_multiple(tmp_path: Path) -> None:
    spec_id = str(uuid.uuid4())
    mapping = tmp_path / "mapping.json"
    mapping.write_text(json.dumps({"id": spec_id, "columns": []}))
    downloads: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.startswith("/output-folders/"):
            downloads.append(request.url.path)
            return httpx.Response(200, content=b"artifact")
        if request.method == "GET":
            return httpx.Response(
                200, json={"id": spec_id, "status": "proposed", "columns": []}
            )
        if request.url.path.endswith("/execute"):
            return httpx.Response(
                200,
                json={
                    "execution_run_id": "run-1",
                    "csv_paths": {"customers": "path", "orders": "path"},
                },
            )
        return httpx.Response(200, json={})

    output = tmp_path / "deliverables"
    with _api(handler) as api:
        run(api, mapping, "reviewer@example.com", output)
    assert {path.name for path in output.iterdir()} == {
        "pipeline.py",
        "mapping.json",
        "customers.csv",
        "orders.csv",
    }
    assert f"/output-folders/{spec_id}/tables/customers.csv" in downloads
    assert f"/output-folders/{spec_id}/tables/orders.csv" in downloads
