"""CLI argument handling for multi-schema proposals."""

from __future__ import annotations

from pathlib import Path

import pop_cli.main as cli
from typer.testing import CliRunner


class FakeApi:
    def __enter__(self) -> FakeApi:
        return self

    def __exit__(self, *args: object) -> None:
        return None


def test_propose_multiple_schemas_creates_two_reviewable_outputs(
    tmp_path: Path, monkeypatch
) -> None:
    schema_a = tmp_path / "customers.json"
    schema_b = tmp_path / "orders.json"
    schema_a.write_text("{}")
    schema_b.write_text("{}")
    outputs: list[Path] = []

    def propose_mapping(*args, **kwargs):
        outputs.append(args[4])
        return {"id": f"spec-{len(outputs)}"}

    monkeypatch.setattr(cli, "Api", FakeApi)
    monkeypatch.setattr(cli, "propose_mapping", propose_mapping)
    output_dir = tmp_path / "mappings"
    result = CliRunner().invoke(
        cli.app,
        [
            "propose",
            "--client",
            "client",
            "--schema",
            str(schema_a),
            "--schema",
            str(schema_b),
            "--output-dir",
            str(output_dir),
        ],
    )

    assert result.exit_code == 0, result.output
    assert outputs == [
        output_dir / "customers-1-mapping.json",
        output_dir / "orders-2-mapping.json",
    ]
    assert "spec-1" in result.output and "spec-2" in result.output


def test_propose_multiple_schemas_requires_output_dir(tmp_path: Path) -> None:
    a, b = tmp_path / "a.json", tmp_path / "b.json"
    a.write_text("{}")
    b.write_text("{}")
    result = CliRunner().invoke(
        cli.app,
        ["propose", "--client", "client", "--schema", str(a), "--schema", str(b)],
    )
    assert result.exit_code != 0
    assert "--output-dir" in result.output
