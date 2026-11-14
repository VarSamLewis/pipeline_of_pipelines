"""Reviewable candidate previews never silently exceed their execution budget."""

from __future__ import annotations

import base64
import json
import uuid
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient


def test_plan_may_not_change_approved_source_identity(simple_schema_file) -> None:
    from adaptive_pipeline import _compile_candidate

    mapping = {
        "target_schema_json": json.loads(simple_schema_file.read_text()),
        "columns": [
            {
                "target_table": "records",
                "target_column": "record_id",
                "source_columns": [{"source_column": "id"}],
            }
        ],
    }
    with pytest.raises(ValueError, match="source or target identities"):
        _compile_candidate(
            {
                "mode": "plan",
                "overrides": [
                    {
                        "target_table": "records",
                        "target_column": "record_id",
                        "source_columns": [{"source_column": "another"}],
                    }
                ],
            },
            mapping,
        )
    script, _, mode = _compile_candidate(
        {"mode": "script", "script": "print('candidate')"}, mapping
    )
    assert mode == "script"
    assert script == "print('candidate')"


def test_worker_rejects_unconfigured_and_unsafe_sources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import sandbox_worker

    monkeypatch.setattr(sandbox_worker, "_exit_worker", lambda: None)
    app = sandbox_worker.app

    worker = TestClient(app)
    monkeypatch.delenv("SANDBOX_WORKER", raising=False)
    assert (
        worker.post(
            "/execute", json={"script": "pass", "mapping": {}, "sources": {}}
        ).status_code
        == 503
    )
    monkeypatch.setenv("SANDBOX_WORKER", "true")
    assert (
        worker.post(
            "/execute",
            json={"script": "pass", "mapping": {}, "sources": {"../bad": "YQ=="}},
        ).status_code
        == 422
    )


def test_worker_exits_after_a_completed_preview(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import sandbox_worker

    exits: list[bool] = []
    monkeypatch.setattr(sandbox_worker, "_exit_worker", lambda: exits.append(True))
    monkeypatch.setenv("SANDBOX_WORKER", "true")
    response = TestClient(sandbox_worker.app).post(
        "/execute",
        json={
            "script": "raise RuntimeError('failed')",
            "mapping": {},
            "sources": {f"{uuid.uuid4()}_input.csv": "aWQKMQo="},
        },
    )
    assert response.status_code == 200
    assert response.json()["passed"] is False
    assert exits == [True]


def test_worker_executes_without_inherited_secrets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import sandbox_worker

    monkeypatch.setattr(sandbox_worker, "_exit_worker", lambda: None)
    app = sandbox_worker.app

    monkeypatch.setenv("SANDBOX_WORKER", "true")
    monkeypatch.setenv("BYOK_ENCRYPTION_KEY", "do-not-inherit")
    script = """\
import os
from pathlib import Path
assert 'BYOK_ENCRYPTION_KEY' not in os.environ
destination = Path(os.sys.argv[os.sys.argv.index('--output-folder') + 1])
(destination / 'records.csv').write_text('record_id\\n1\\n')
"""
    response = TestClient(app).post(
        "/execute",
        json={
            "script": script,
            "mapping": {},
            "sources": {
                f"{uuid.uuid4()}_input.csv": base64.b64encode(b"id\n1\n").decode()
            },
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["passed"] is True
    assert (
        base64.b64decode(response.json()["outputs"]["records.csv"]) == b"record_id\n1\n"
    )
    failure = TestClient(app).post(
        "/execute",
        json={
            "script": "raise KeyError('sensitive row')",
            "mapping": {},
            "sources": {f"{uuid.uuid4()}_input.csv": "aWQKMQo="},
        },
    )
    assert failure.json()["passed"] is False
    assert failure.json()["failure_type"] == "KeyError"


def test_three_candidate_previews_then_explicit_review(
    client: TestClient,
    registered_client: dict,
    client_with_schema: dict,
    ingested_batch: dict,
    mock_openai: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import adaptive_pipeline
    from byok import save_organization_api_key
    from config import get_settings
    from db_ops import get_session
    from sandbox_worker import CandidateRequest, CandidateResult, _execute

    monkeypatch.setattr(
        adaptive_pipeline,
        "get_settings",
        lambda: replace(get_settings(), candidate_worker_url="http://127.0.0.1:8001"),
    )

    with get_session() as session:
        save_organization_api_key(
            session,
            uuid.UUID(registered_client["organization_id"]),
            "test-byok-key",
            "https://api.openai.com/v1",
        )
    spec = client.post(
        f"/clients/{registered_client['id']}/mapping-specs",
        json={
            "source_raw_file_ids": ingested_batch["raw_file_ids"],
            "target_schema": client_with_schema["schema"],
        },
    )
    assert spec.status_code == 200, spec.text
    spec_id = spec.json()["id"]
    assert client.post(f"/mapping-specs/{spec_id}/propose").status_code == 200
    assert (
        client.post(
            f"/mapping-specs/{spec_id}/approve", json={"reviewer": "tester"}
        ).status_code
        == 200
    )

    calls = 0
    prompts: list[str] = []

    def propose(messages: list[dict[str, str]], **_kwargs: object) -> dict[str, object]:
        prompts.append(messages[-1]["content"])
        return {"mode": "plan", "overrides": []}

    def preview(
        script: str, mapping: dict, sources: dict[str, str]
    ) -> CandidateResult:
        nonlocal calls
        calls += 1
        if calls == 1:
            return CandidateResult(
                passed=False,
                error="sensitive source row in failure detail",
                failure_type="ColumnNotFoundError",
            )
        if calls == 2:
            failed_validation = b"record_id,full_name,score\n,Alice,88.5\n"
            return CandidateResult(
                passed=True,
                outputs={
                    "records.csv": base64.b64encode(failed_validation).decode()
                },
            )
        monkeypatch.setenv("SANDBOX_WORKER", "true")
        return _execute(
            CandidateRequest(script=script, mapping=mapping, sources=sources)
        )

    monkeypatch.setattr(adaptive_pipeline, "call_mapping_llm", propose)
    monkeypatch.setattr(adaptive_pipeline, "_request_preview", preview)
    response = client.post(f"/mapping-specs/{spec_id}/candidates")
    assert response.status_code == 200, response.text
    assert response.json()["attempt"] == 3
    assert response.json()["status"] == "passed"
    assert calls == 3
    assert "ColumnNotFoundError" in prompts[1]
    assert "sensitive source row" not in prompts[1]
    assert "records.record_id.not_null" in prompts[2]
    candidates = client.get(f"/mapping-specs/{spec_id}/candidates").json()
    assert [candidate["status"] for candidate in candidates] == [
        "failed",
        "failed",
        "passed",
    ]
    assert "def build_target_tables" in candidates[2]["script"]
    assert client.post(f"/mapping-specs/{spec_id}/candidates").status_code == 422
    assert calls == 3

    review_page = client.get(
        f"/mapping/{spec_id}/candidate/{response.json()['id']}"
    )
    assert review_page.status_code == 200, review_page.text
    assert "Review candidate 3 of 3" in review_page.text
    assert "Download records.csv preview" in review_page.text
    preview_csv = client.get(
        f"/mapping/{spec_id}/candidate/{response.json()['id']}/outputs/records.csv"
    )
    assert preview_csv.status_code == 200
    assert "Alice" in preview_csv.text

    from dependencies import get_artifact_store

    store = get_artifact_store()
    candidate_id = uuid.UUID(response.json()["id"])
    source_name = candidates[2]["result"]["source_snapshots"][0]
    saved_source = store.read_artifact(candidate_id, f"source_{source_name}")
    store.write_artifact(candidate_id, f"source_{source_name}", b"changed")
    assert (
        client.post(
            f"/mapping-specs/{spec_id}/candidates/{candidate_id}/approve"
        ).status_code
        == 422
    )
    store.write_artifact(candidate_id, f"source_{source_name}", saved_source)
    saved_preview = store.read_artifact(candidate_id, "records.csv")
    store.write_artifact(candidate_id, "records.csv", b"record_id\n999\n")
    assert (
        client.post(
            f"/mapping-specs/{spec_id}/candidates/{candidate_id}/approve"
        ).status_code
        == 422
    )
    store.write_artifact(candidate_id, "records.csv", saved_preview)

    from routers import api

    monkeypatch.setattr(
        api,
        "get_settings",
        lambda: replace(get_settings(), candidate_worker_url="http://127.0.0.1:8001"),
    )
    for path in ("generate", "execute", "output-folder"):
        assert client.post(f"/mapping-specs/{spec_id}/{path}").status_code == 409

    approval = client.post(
        f"/mapping-specs/{spec_id}/candidates/{response.json()['id']}/approve"
    )
    assert approval.status_code == 200, approval.text
    run_id = approval.json()["execution_run_id"]
    assert client.get(f"/execution-runs/{run_id}").json()["status"] == "success"
    assert "Alice" in client.get(f"/output-folders/{spec_id}/results.csv").text
    assert (
        client.post(
            f"/mapping-specs/{spec_id}/candidates/{response.json()['id']}/approve"
        ).status_code
        == 422
    )


def test_candidate_budget_cannot_be_extended_after_three_failures(
    client: TestClient,
    registered_client: dict,
    client_with_schema: dict,
    ingested_batch: dict,
    mock_openai: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import adaptive_pipeline
    from byok import save_organization_api_key
    from config import get_settings
    from db_ops import get_session
    from sandbox_worker import CandidateResult

    with get_session() as session:
        save_organization_api_key(
            session,
            uuid.UUID(registered_client["organization_id"]),
            "test-byok-key",
            "https://api.openai.com/v1",
        )
    spec = client.post(
        f"/clients/{registered_client['id']}/mapping-specs",
        json={
            "source_raw_file_ids": ingested_batch["raw_file_ids"],
            "target_schema": client_with_schema["schema"],
        },
    )
    spec_id = spec.json()["id"]
    assert client.post(f"/mapping-specs/{spec_id}/propose").status_code == 200
    assert client.post(
        f"/mapping-specs/{spec_id}/approve", json={"reviewer": "tester"}
    ).status_code == 200
    monkeypatch.setattr(
        adaptive_pipeline,
        "get_settings",
        lambda: replace(get_settings(), candidate_worker_url="http://127.0.0.1:8001"),
    )
    monkeypatch.setattr(
        adaptive_pipeline,
        "call_mapping_llm",
        lambda *_args, **_kwargs: {"mode": "plan"},
    )
    executions = 0

    def fail_preview(*_args: object, **_kwargs: object) -> CandidateResult:
        nonlocal executions
        executions += 1
        return CandidateResult(passed=False, failure_type="ComputeError")

    monkeypatch.setattr(adaptive_pipeline, "_request_preview", fail_preview)
    result = client.post(f"/mapping-specs/{spec_id}/candidates")
    assert result.status_code == 200, result.text
    assert result.json()["status"] == "failed"
    assert result.json()["attempt"] == 3
    assert executions == 3
    assert client.post(f"/mapping-specs/{spec_id}/candidates").status_code == 422
    assert executions == 3
    assert (
        client.post(
            f"/mapping-specs/{spec_id}/candidates/{result.json()['id']}/approve"
        ).status_code
        == 422
    )
