"""Tests for stage-owned application and infrastructure boundaries."""

from __future__ import annotations

import ast
import uuid
from pathlib import Path

import pytest
from artifact_store import LocalArtifactStore
from models import TargetSchema


def test_local_artifact_store_is_deterministic_across_instances(
    tmp_path: Path,
) -> None:
    spec_id = uuid.uuid4()
    first = LocalArtifactStore(tmp_path)
    second = LocalArtifactStore(tmp_path)

    first.write_artifact(spec_id, "results.csv", b"id\n1\n")

    assert second.read_artifact(spec_id, "results.csv") == b"id\n1\n"


def test_artifact_store_recovers_all_keys_after_process_restart(
    tmp_path: Path,
) -> None:
    """A fresh adapter instance can retrieve every durable artifact class."""
    spec_id = uuid.uuid4()
    run_id = uuid.uuid4()
    schema = TargetSchema(
        client_code="acme",
        name="default",
        tables=[],
    )
    before_restart = LocalArtifactStore(tmp_path)
    before_restart.put("acme/batch/raw.csv", b"source")
    before_restart.write_target_schema("acme", schema)
    before_restart.write_artifact(spec_id, "mapping.json", b"{}")
    before_restart.write_artifact(spec_id, "pipeline.py", b"print('ok')")
    before_restart.write_artifact(spec_id, "results.csv", b"id\n1\n")
    before_restart.write_log(run_id, b'{"status":"success"}')

    after_restart = LocalArtifactStore(tmp_path)

    assert after_restart.get("acme/batch/raw.csv") == b"source"
    assert after_restart.read_target_schema("acme") == schema
    assert after_restart.read_artifact(spec_id, "mapping.json") == b"{}"
    assert after_restart.read_artifact(spec_id, "pipeline.py") == b"print('ok')"
    assert after_restart.read_artifact(spec_id, "results.csv") == b"id\n1\n"
    assert after_restart.read_log(run_id) == b'{"status":"success"}'


def test_new_application_instance_retrieves_existing_artifact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Artifact HTTP retrieval survives application reconstruction."""
    from app import create_app
    from fastapi.testclient import TestClient
    from routers import api

    spec_id = uuid.uuid4()
    before_restart = LocalArtifactStore(tmp_path)
    before_restart.write_artifact(spec_id, "pipeline.py", b"print('durable')")
    monkeypatch.setattr(api, "get_artifact_store", lambda: before_restart)
    assert (
        TestClient(create_app()).get(f"/output-folders/{spec_id}/pipeline.py").text
        == "print('durable')"
    )

    after_restart = LocalArtifactStore(tmp_path)
    monkeypatch.setattr(api, "get_artifact_store", lambda: after_restart)
    response = TestClient(create_app()).get(f"/output-folders/{spec_id}/pipeline.py")

    assert response.status_code == 200
    assert response.text == "print('durable')"


def test_artifact_store_rejects_path_traversal(tmp_path: Path) -> None:
    store = LocalArtifactStore(tmp_path)

    with pytest.raises(ValueError, match="filename"):
        store.path(uuid.uuid4(), "../secret")


def test_mapping_spec_queries_have_one_production_owner() -> None:
    source_root = Path(__file__).parents[1] / "backend" / "src"
    owners: list[str] = []
    for path in source_root.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        if any(
            isinstance(node, ast.FunctionDef) and node.name == "load_mapping_spec"
            for node in tree.body
        ):
            owners.append(path.name)

    assert owners == ["mapping_specs.py"]


def test_ui_does_not_import_database_or_stage_internals() -> None:
    source = (Path(__file__).parents[1] / "backend" / "src" / "ui.py").read_text(
        encoding="utf-8"
    )
    tree = ast.parse(source)
    imported_modules = {
        node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
    }

    assert "db_ops" not in imported_modules
    assert "codegen" not in imported_modules
    assert "pipeline" not in imported_modules


def test_app_module_is_composition_only() -> None:
    """Application construction must not regress into endpoint ownership."""
    app_path = Path(__file__).parents[1] / "backend" / "src" / "app.py"
    tree = ast.parse(app_path.read_text(encoding="utf-8"))
    functions = {
        node.name
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    decorators = [
        decorator
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        for decorator in node.decorator_list
    ]

    assert functions == {"create_app", "lifespan"}
    assert len(decorators) == 1


def test_api_router_does_not_construct_fastapi_application() -> None:
    """Route translation and framework composition remain separate."""
    api_path = Path(__file__).parents[1] / "backend" / "src" / "routers" / "api.py"
    source = api_path.read_text(encoding="utf-8")

    assert "FastAPI(" not in source
    assert ".add_middleware(" not in source
    assert ".mount(" not in source


def test_client_persistence_has_one_implementation_owner() -> None:
    """Legacy db_ops may re-export commands but must not duplicate them."""
    source_root = Path(__file__).parents[1] / "backend" / "src"
    function_names = {
        "create_client",
        "create_ingestion_batch",
        "get_client_by_code",
        "get_client_by_id",
        "get_ingestion_batch",
    }
    owners: dict[str, list[str]] = {name: [] for name in function_names}

    for path in source_root.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in tree.body:
            if isinstance(node, ast.FunctionDef) and node.name in owners:
                owners[node.name].append(path.relative_to(source_root).as_posix())

    assert all(paths == ["repositories/clients.py"] for paths in owners.values())


def test_workflow_owns_all_human_gated_operations() -> None:
    """The canonical journey must remain explicit and discoverable."""
    workflow_path = Path(__file__).parents[1] / "backend" / "src" / "workflow.py"
    tree = ast.parse(workflow_path.read_text(encoding="utf-8"))
    functions = {
        node.name
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }

    assert {
        "ingest_and_propose",
        "approve_mapping_and_execute",
        "reject_mapping",
        "approve_result",
        "reject_result",
        "get_mapping_review",
        "get_result_review",
    } <= functions


def test_http_routes_do_not_import_stage_orchestration_internals() -> None:
    """API and HTMX routes must both delegate stage ownership to workflow."""
    source_root = Path(__file__).parents[1] / "backend" / "src"
    for relative_path in ("routers/api.py", "ui.py"):
        tree = ast.parse((source_root / relative_path).read_text(encoding="utf-8"))
        imported_modules = {
            node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
        }
        assert imported_modules.isdisjoint(
            {"codegen", "mapping", "mapping_specs", "parser", "pipeline"}
        )


def test_no_process_global_artifact_lookup_remains() -> None:
    """Artifact retrieval must use durable keys, never process memory."""
    source_root = Path(__file__).parents[1] / "backend" / "src"
    production_source = "\n".join(
        path.read_text(encoding="utf-8") for path in source_root.rglob("*.py")
    )

    assert "_output_folders" not in production_source
