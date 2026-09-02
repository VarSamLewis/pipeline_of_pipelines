"""Tests for prose-only mapping codegen: prompts, normalization, and repair."""

from __future__ import annotations

import ast
import uuid

import pytest
from codegen import (
    _extract_focus_column,
    _normalize_generated_pipeline,
    _projection_for_codegen,
    generate_polars_script,
)
from feedback import _build_refinement_prompt
from mapping import (
    build_codegen_prompt,
    build_mapping_prompt,
    parse_llm_mapping_response,
)
from models import TargetSchema, TargetSchemaColumn, TargetSchemaTable


@pytest.fixture
def target_schema() -> TargetSchema:
    return TargetSchema(
        client_code="tst",
        tables=[
            TargetSchemaTable(
                name="products",
                columns=[
                    TargetSchemaColumn(name="sku", dtype="String"),
                    TargetSchemaColumn(name="product_line", dtype="String"),
                ],
            )
        ],
    )


def _expression_column() -> dict:
    return {
        "target_table": "products",
        "target_column": "sku",
        "source_columns": [
            {"source_table": "items.csv::Sheet1", "source_column": "sku"}
        ],
        "transformation_logic": "Uppercase the sku after trimming spaces.",
        "transformation_type": "expression",
    }


def _filter_column() -> dict:
    return {
        "target_table": "products",
        "target_column": "sku",
        "source_columns": [],
        "transformation_logic": "Exclude rows whose order_id starts with 9999.",
        "transformation_type": "filter",
    }


def _lookup_column() -> dict:
    return {
        "target_table": "products",
        "target_column": "product_line",
        "source_columns": [
            {"source_table": "items.csv::Sheet1", "source_column": "line_code"}
        ],
        "transformation_logic": "Look up the line name for each line code.",
        "transformation_type": "lookup",
        "lookup_source_table": "lines.csv::Lines",
        "lookup_key": "line_code",
        "lookup_value": "line_name",
    }


# ---------------------------------------------------------------------------
# Deterministic draft: prose placeholders
# ---------------------------------------------------------------------------


def test_draft_emits_prose_placeholders(target_schema: TargetSchema) -> None:
    spec = {"columns": [_expression_column(), _filter_column()]}
    script = generate_polars_script(spec, target_schema)
    ast.parse(script)  # draft must stay syntactically valid
    assert "# TARGET COLUMN: sku" in script
    assert "# Transformation: Uppercase the sku after trimming spaces." in script
    assert "PLACEHOLDER" in script


def test_draft_lookup_remains_deterministic(target_schema: TargetSchema) -> None:
    spec = {"columns": [_lookup_column()]}
    script = generate_polars_script(spec, target_schema)
    ast.parse(script)
    assert "_lut" in script
    assert 'how="left"' in script
    assert "PLACEHOLDER" not in script


# ---------------------------------------------------------------------------
# Post-generation normalization
# ---------------------------------------------------------------------------


def test_normalize_wraps_bare_string_literals() -> None:
    code = "when(col('x').is_null()).then('Other').otherwise('N/A')"
    fixed = _normalize_generated_pipeline(code)
    assert ".then(pl.lit('Other'))" in fixed
    assert ".otherwise(pl.lit('N/A'))" in fixed


def test_normalize_handles_double_quotes() -> None:
    fixed = _normalize_generated_pipeline('.then("Active")')
    assert '.then(pl.lit("Active"))' in fixed


def test_normalize_skips_wrapped_literals_and_column_refs() -> None:
    code = ".then(pl.lit('A')).otherwise(col('x'))"
    assert _normalize_generated_pipeline(code) == code


def test_normalize_fixes_string_method_names() -> None:
    fixed = _normalize_generated_pipeline("df.str.title().str.strip()")
    assert fixed == "df.str.to_titlecase().str.strip_chars()"


# ---------------------------------------------------------------------------
# Codegen projection (prose-first context)
# ---------------------------------------------------------------------------


def test_projection_strips_expression_fields() -> None:
    col = {
        **_expression_column(),
        "polars_expression": "col('sku').str.to_uppercase()",
        "filter_expression": "~col('x')",
        "aggregation_expression": "col('y').sum()",
    }
    projected = _projection_for_codegen(col)
    for field in (
        "polars_expression",
        "filter_expression",
        "aggregation_expression",
    ):
        assert field not in projected
    assert projected["transformation_logic"] == col["transformation_logic"]


def test_projection_legacy_fallback_only_without_prose() -> None:
    legacy = {
        **_expression_column(),
        "transformation_logic": "",
        "polars_expression": "col('sku').str.to_uppercase()",
    }
    projected = _projection_for_codegen(legacy)
    assert "legacy_polars_reference" in projected

    with_prose = {**legacy, "transformation_logic": "Uppercase it."}
    projected = _projection_for_codegen(with_prose)
    assert "legacy_polars_reference" not in projected


# ---------------------------------------------------------------------------
# Focused repair: target-column extraction
# ---------------------------------------------------------------------------


def _traceback(line: int) -> str:
    return (
        "Traceback (most recent call last):\n"
        f'  File "/tmp/pipeline.py", line {line}, in build_target_tables\n'
        "    boom\n"
    )


def test_extract_focus_column_from_target_comment() -> None:
    lines = ["df = 1"] * 30 + [
        "# TARGET COLUMN: weight_kg",
        "# Transformation: divide by 1000 when grams",
        "df = df.with_columns(pl.col('w'))",
    ]
    focus = _extract_focus_column("\n".join(lines), _traceback(33))
    assert focus == "weight_kg"


def test_extract_focus_column_alias_fallback() -> None:
    code = "x = 1\ny = 2\ndf = df.with_columns(pl.col('a').alias('status'))"
    focus = _extract_focus_column(code, _traceback(3))
    assert focus == "status"


def test_extract_focus_column_returns_none_without_traceback() -> None:
    assert _extract_focus_column("x = 1", "no file info") is None


# ---------------------------------------------------------------------------
# Prompts are prose-only
# ---------------------------------------------------------------------------


def test_mapping_prompt_is_prose_only(target_schema: TargetSchema) -> None:
    messages = build_mapping_prompt(target_schema, [], [], [], [])
    user_content = messages[1]["content"]
    assert "polars_expression" not in user_content
    assert "plain English" in user_content
    assert "Do NOT write" in user_content


def test_codegen_prompt_contains_polars_rules(target_schema: TargetSchema) -> None:
    messages = build_codegen_prompt(target_schema, [], [], [], [], "{}", "draft")
    system_content = messages[0]["content"]
    assert "pl.lit()" in system_content
    assert "PLACEHOLDER" in system_content
    assert "str.to_uppercase()" in system_content


def test_parse_llm_mapping_response_ignores_expression_fields(
    target_schema: TargetSchema,
) -> None:
    response = {
        "mappings": [
            {
                "target_table": "products",
                "target_column": "sku",
                "source_columns": [],
                "transformation_logic": "Uppercase it.",
                "polars_expression": "col('x')",
                "filter_expression": "~col('y')",
                "aggregation_expression": "col('z').sum()",
            }
        ]
    }
    mappings = parse_llm_mapping_response(response, uuid.uuid4(), target_schema)
    column = mappings[0]
    assert column.polars_expression is None
    assert column.filter_expression is None
    assert column.aggregation_expression is None
    assert column.transformation_logic == "Uppercase it."


def test_refinement_prompt_targets_transformation_logic(
    target_schema: TargetSchema,
) -> None:
    spec = {
        "columns": [
            {
                "target_table": "products",
                "target_column": "sku",
                "transformation_logic": "Uppercase it.",
                "transformation_type": "expression",
            }
        ]
    }
    messages = _build_refinement_prompt(spec, target_schema, "make sku uppercase")
    system_content = messages[0]["content"]
    assert "'transformation_logic'" in system_content
    assert "Polars expression conventions" not in system_content
    assert "polars_expression" not in messages[1]["content"]


# ---------------------------------------------------------------------------
# Automatic repair loop
# ---------------------------------------------------------------------------

_FAILING_ERROR = (
    "Pipeline execution failed (exit 1):\n"
    '  File "/tmp/pipeline.py", line 5, in build_target_tables\n'
    "    boom\n"
)


def test_execute_with_repair_regenerates_and_succeeds(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import codegen

    pipeline_path = tmp_path / "pipeline.py"
    pipeline_path.write_text("broken")
    calls = {"exec": 0, "repair": 0}

    def fake_execute(*args: object, **kwargs: object) -> dict:
        calls["exec"] += 1
        if calls["exec"] < 3:
            raise RuntimeError(_FAILING_ERROR)
        return {"records.csv": tmp_path / "records.csv"}

    def fake_repair(
        spec_id: uuid.UUID,
        base_code: str,
        error_message: str | None = None,
        focus_column: str | None = None,
    ) -> str:
        calls["repair"] += 1
        if calls["repair"] == 1:
            assert base_code == "broken"
        else:
            assert base_code == "fixed"
        assert _FAILING_ERROR in (error_message or "")
        return "fixed"

    monkeypatch.setattr(codegen, "execute_generated_pipeline", fake_execute)
    monkeypatch.setattr(codegen, "_codegen_with_context", fake_repair)

    result = codegen._execute_with_repair(
        pipeline_path, tmp_path, object_store=object(), spec_id=uuid.uuid4()
    )
    assert result == {"records.csv": tmp_path / "records.csv"}
    assert calls["exec"] == 3
    assert calls["repair"] == 2
    assert pipeline_path.read_text() == "fixed"


def test_execute_with_repair_raises_after_exhaustion(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import codegen

    pipeline_path = tmp_path / "pipeline.py"
    pipeline_path.write_text("broken")
    calls = {"exec": 0}

    def fake_execute(*args: object, **kwargs: object) -> dict:
        calls["exec"] += 1
        raise RuntimeError(_FAILING_ERROR)

    def fake_repair(*args: object, **kwargs: object) -> str:
        return "still broken"

    monkeypatch.setattr(codegen, "execute_generated_pipeline", fake_execute)
    monkeypatch.setattr(codegen, "_codegen_with_context", fake_repair)

    with pytest.raises(RuntimeError, match="exit 1"):
        codegen._execute_with_repair(
            pipeline_path, tmp_path, object_store=object(), spec_id=uuid.uuid4()
        )
    assert calls["exec"] == 3
