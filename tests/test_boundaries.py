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
