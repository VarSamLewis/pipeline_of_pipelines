"""Tests for stage-owned application and infrastructure boundaries."""

from __future__ import annotations

import ast
import uuid
from pathlib import Path

import pytest
from artifact_store import LocalArtifactStore


def test_local_artifact_store_is_deterministic_across_instances(
    tmp_path: Path,
) -> None:
    spec_id = uuid.uuid4()
    first = LocalArtifactStore(tmp_path)
    second = LocalArtifactStore(tmp_path)

    artifact = first.path(spec_id, "results.csv")
    artifact.parent.mkdir(parents=True)
    artifact.write_text("id\n1\n", encoding="utf-8")

    assert second.path(spec_id, "results.csv").read_text(encoding="utf-8") == (
        "id\n1\n"
    )


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
            isinstance(node, ast.FunctionDef)
            and node.name == "load_mapping_spec"
            for node in tree.body
        ):
            owners.append(path.name)

    assert owners == ["mapping_specs.py"]


def test_ui_does_not_import_database_or_stage_internals() -> None:
    source = (
        Path(__file__).parents[1] / "backend" / "src" / "ui.py"
    ).read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported_modules = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
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

    assert functions == {"create_app"}
    assert decorators == []


def test_api_router_does_not_construct_fastapi_application() -> None:
    """Route translation and framework composition remain separate."""
    api_path = (
        Path(__file__).parents[1]
        / "backend"
        / "src"
        / "routers"
        / "api.py"
    )
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

    assert all(
        paths == ["repositories/clients.py"] for paths in owners.values()
    )
