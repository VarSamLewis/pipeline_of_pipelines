"""LLM-assisted source-to-target mapping specification.

This module prepares evidence, spreadsheet profiles, and the supplied target
schema as prompts for an OpenAI-compatible chat completion model, parses the
model's proposed mappings into the durable mapping schema, and surfaces
citations back to evidence and business rules.

A client folder may contain many heterogeneous files (Excel data, PDF context,
email instructions). The LLM is expected to act dynamically across all of them
when proposing how to shape the data into the supplied target schema.
"""

from __future__ import annotations

import json
import uuid
from typing import Any, cast

from config import get_settings
from models import (
    BusinessRule,
    ExtractedEvidence,
    MappingSpec,
    MappingSpecStatus,
    ProposedMapping,
    SourceColumnRef,
    TargetSchema,
)


def _catalog_tables(source_catalogs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Flatten canonical catalogs while tolerating legacy profile shapes."""
    tables: list[dict[str, Any]] = []
    for catalog in source_catalogs:
        if "tables" in catalog:
            tables.extend(catalog.get("tables", []))
        elif "sheets" in catalog:
            tables.extend(catalog.get("sheets", []))
        else:
            tables.append(catalog)
    return tables


_KEY_DELIM = "::"


def _build_catalog_index(
    source_catalogs: list[dict[str, Any]],
) -> dict[str, tuple[dict[str, Any], dict[str, Any]]]:
    """Build a mapping from ``filename::sheet::column`` to (table, column)."""
    index: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {}
    for table in _catalog_tables(source_catalogs):
        filename = table.get("original_filename") or "unknown"
        sheet = (
            table.get("display_name")
            or table.get("location", {}).get("sheet_name")
            or ""
        )
        for col in table.get("columns", []):
            col_name = (
                col.get("normalized_name")
                or col.get("original_name")
                or col.get("header")
                or col.get("column")
                or ""
            )
            key = f"{filename}{_KEY_DELIM}{sheet}{_KEY_DELIM}{col_name}"
            index[key] = (table, col)
    return index


def _build_table_index(
    source_catalogs: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Build a mapping from ``filename::sheet`` to the catalog table."""
    index: dict[str, dict[str, Any]] = {}
    for table in _catalog_tables(source_catalogs):
        filename = table.get("original_filename") or "unknown"
        sheet = (
            table.get("display_name")
            or table.get("location", {}).get("sheet_name")
            or ""
        )
        key = f"{filename}{_KEY_DELIM}{sheet}"
        index[key] = table
    return index


def _resolve_table_key(
    table_key: str,
    tbl_index: dict[str, dict[str, Any]],
) -> dict[str, Any] | None:
    """Resolve a table key by exact composite match or display-name fallback."""
    entry = tbl_index.get(table_key)
    if entry is not None:
        return entry
    for candidate in tbl_index.values():
        if candidate.get("display_name", "").lower() == table_key.lower():
            return candidate
    return None


def resolve_composite_keys(
    mappings: list[ProposedMapping],
    source_catalogs: list[dict[str, Any]],
) -> list[str]:
    """Resolve ``filename::sheet::column`` keys to catalog entries in place.

    Populates ``source_table_id``, ``source_column_id``, ``raw_file_id``,
    ``source_table``, and ``source_column`` on each ``SourceColumnRef``.
    Also resolves ``lookup_source_table`` and ``aggregation_source_table``.

    Returns a list of error strings (empty on success).
    """
    col_index = _build_catalog_index(source_catalogs)
    tbl_index = _build_table_index(source_catalogs)
    errors: list[str] = []

    for mapping in mappings:
        for ref in mapping.source_columns:
            table_key = ref.source_table
            col_name = ref.source_column

            # 1. Exact composite key: filename::sheet::column
            col_entry = col_index.get(f"{table_key}{_KEY_DELIM}{col_name}")

            # 2. Table composite key + case-insensitive column name.
            if col_entry is None:
                table_entry = tbl_index.get(table_key)
                if table_entry is not None:
                    for col in table_entry.get("columns", []):
                        candidate = (
                            col.get("normalized_name")
                            or col.get("original_name")
                            or col.get("header")
                            or col.get("column")
                            or ""
                        )
                        if candidate.lower() == col_name.lower():
                            col_entry = (table_entry, col)
                            break

            # 3. Display-name table fallback + case-insensitive column.
            if col_entry is None:
                for _tbl_key, table_entry in tbl_index.items():
                    if table_entry.get("display_name", "").lower() == table_key.lower():
                        for col in table_entry.get("columns", []):
                            candidate = (
                                col.get("normalized_name")
                                or col.get("original_name")
                                or col.get("header")
                                or col.get("column")
                                or ""
                            )
                            if candidate.lower() == col_name.lower():
                                col_entry = (table_entry, col)
                                break
                    if col_entry is not None:
                        break

            if col_entry is None:
                errors.append(
                    f"source column {col_name!r} not found for key {table_key!r}"
                )
                continue

            table, col = col_entry
            ref.source_table_id = table.get("source_table_id", "")
            ref.source_column_id = col.get("source_column_id", "")
            raw_id = table.get("raw_file_id")
            if raw_id:
                ref.raw_file_id = uuid.UUID(str(raw_id))
            ref.source_table = str(
                table.get("display_name") or table.get("source_table_id")
            )
            ref.source_column = str(
                col.get("normalized_name")
                or col.get("original_name")
                or col.get("source_column_id")
            )

        for field_name in ("lookup_source_table", "aggregation_source_table"):
            table_key = getattr(mapping, field_name)
            if not table_key:
                continue
            table_entry = _resolve_table_key(table_key, tbl_index)
            if table_entry is None:
                errors.append(
                    f"{field_name} {table_key!r} is not in the catalog"
                )
                continue
            setattr(mapping, field_name, table_entry.get("source_table_id", ""))

    return errors


def _gather_targeted_evidence(
    session: Any,
    client_id: uuid.UUID,
    target_schema: TargetSchema,
    source_catalogs: list[dict[str, Any]],
    search_evidence_by_text: Any,
    top_k_per_query: int = 5,
    max_total: int = 40,
) -> list[ExtractedEvidence]:
    """Query the vector evidence store for each target and source column pair.

    The goal is to retrieve only the most relevant PDF, email, and text chunks
    for the proposed mapping, rather than dumping a random sample of evidence.
    """
    queries: set[str] = set()

    # Target-driven queries
    for table in target_schema.tables:
        queries.add(f"target table {table.name}")
        for col in table.columns:
            queries.add(f"map {col.name}")
            queries.add(f"{table.name} {col.name}")

    # Source-driven queries from the canonical catalog.
    for source_table_info in _catalog_tables(source_catalogs):
        table_name = (
            source_table_info.get("display_name")
            or source_table_info.get("sheet_name")
            or source_table_info.get("source_table", "")
        )
        for col in source_table_info.get("columns", []):
            header = (
                col.get("original_name")
                or col.get("normalized_name")
                or col.get("header")
                or col.get("column")
            )
            if header:
                queries.add(f"{table_name} {header}")
                queries.add(f"column {header}")

    # Cross-reference queries for the trickiest columns
    queries.add("region code mapping")
    queries.add("revenue calculation")
    queries.add("total revenue")
    queries.add("line total")
    queries.add("customer name misspelling")
    queries.add("test orders exclude")

    seen_ids: set[uuid.UUID] = set()
    evidence_items: list[ExtractedEvidence] = []
    for query in queries:
        if len(evidence_items) >= max_total:
            break
        results = search_evidence_by_text(
            session, query, client_id, top_k=top_k_per_query
        )
        for item in results:
            if item.id in seen_ids:
                continue
            seen_ids.add(item.id)
            evidence_items.append(item)
            if len(evidence_items) >= max_total:
                break
    return evidence_items


def build_mapping_prompt(
    target_schema: TargetSchema,
    source_catalogs: list[dict[str, Any]],
    evidence_items: list[ExtractedEvidence],
    business_rules: list[BusinessRule],
    raw_file_summary: list[dict[str, Any]],
) -> list[dict[str, str]]:
    """Build a composite-key-grounded mapping prompt."""
    evidence_entries = [
        f"Evidence ID {e.id}:\n{e.content[:800]}" for e in evidence_items
    ]
    evidence_text = "\n---\n".join(evidence_entries)
    rules_text = "\n".join(f"- {r.rule_text}" for r in business_rules)

    col_index = _build_catalog_index(source_catalogs)
    tbl_index = _build_table_index(source_catalogs)
    source_index_text = "\n".join(f"- {key}" for key in sorted(col_index))
    table_index_text = "\n".join(f"- {key}" for key in sorted(tbl_index))

    prompt = {
        "role": "system",
        "content": (
            "You are a data-mapping assistant. Given a target schema, source "
            "catalog, and evidence retrieved from a vector database, "
            "propose source-to-target column mappings. Each mapping may use one or "
            "more source columns. Reference sources using composite keys in the "
            "format filename::sheet::column (e.g. data.csv::Orders::Revenue). "
            "Never invent a composite key. Use evidence IDs to cite support for "
            "each mapping. Return valid JSON with a 'mappings' array."
        ),
    }
    user_content = (
        f"Target schema:\n{target_schema.model_dump_json(indent=2)}\n\n"
        f"Source files:\n{json.dumps(raw_file_summary, indent=2)}\n\n"
        f"Available source columns (use these composite keys):\n"
        f"{source_index_text}\n\n"
        f"Available source tables (for lookup_source_table / "
        f"aggregation_source_table):\n{table_index_text}\n\n"
        f"Evidence retrieved from the vector database for this mapping:\n"
        f"{evidence_text}\n\n"
        f"Business rules:\n{rules_text}\n\n"
        'Return JSON with this shape: {"mappings": [{"target_table": "...", '
        '"target_column": "...", "source_columns": [{"source_table": '
        '"filename::sheet", "source_column": "column_name"}], '
        '"transformation_logic": '
        '"...", "transformation_type": "expression", '
        '"polars_expression": "col(\'source_col\').cast(pl.Int64)", '
        '"tests": ["not_null", "unique"], '
        '"evidence_ids": ["..."], "business_rule_ids": ["..."], '
        '"confidence": 0.95}]}. '
        "tests must be an array of plain strings, not objects. "
        "For per-row expressions use transformation_type=expression with valid "
        "Polars syntax. Available globals: pl, col, when, concat, coalesce, null, "
        "Int64, Float64, String, Date, Datetime, Boolean. "
        "Use `when(condition).then(value).otherwise(value)` for conditional logic; "
        "`when` is a global alias for pl.when. Wrap string literals in .then() and "
        ".otherwise() with pl.lit(). "
        "For string concatenation use `concat(col('a'), '-', col('b'))` or "
        "`col('a') + '-' + col('b')`. "
        "For dates use col('dt').str.to_date('%Y-%m-%d', strict=False). For multiple "
        "date formats use coalesce: "
        "`coalesce(col('dt').str.to_date('%d/%m/%Y', strict=False), "
        "col('dt').str.to_date('%Y-%m-%d', strict=False), "
        "col('dt').str.to_date('%d-%b-%Y', strict=False))`. "
        "For title case use col('x').str.to_titlecase(). "
        "For code lookup/replace use col('x').str.strip_chars().replace("
        "{'A': 'Alpha', 'B': 'Beta'}). "
        "str.contains is case sensitive; use inline regex flag (?i) for "
        "case-insensitive matching, e.g. col('unit').str.contains(r'(?i)kg'). "
        "For filters use transformation_type=filter and provide filter_expression, "
        'e.g. "filter_expression": '
        "\"~col('order_id').cast(str).str.starts_with('9999')\". "
        "For aggregations use transformation_type=aggregation with "
        "aggregation_source_table, aggregation_group_key, and "
        "aggregation_expression. aggregation_source_table must be a table "
        'composite key, e.g. "aggregation_source_table": "data.csv::Orders", '
        '"aggregation_group_key": "cust_id", '
        "\"aggregation_expression\": \"(col('qty') * col('unit_price')).sum()\". "
        "For cross-table lookups use transformation_type=lookup with "
        "lookup_source_table, lookup_key, and lookup_value. lookup_source_table "
        "must be a table composite key, e.g. "
        '"lookup_source_table": "data.csv::LookupTable", "lookup_key": "prod_sku", '
        '"lookup_value": "prod_name". '
        "For lookups the source_columns should reference the column in the "
        "base/source table that contains the join key, not the lookup table column. "
        "Normalise categorical values to the exact allowed_values in the target "
        "schema (match case). Use `null` for missing values in expressions. "
        "Do not invent undefined functions; do not use str.strip() or bare when()."
    )
    return [prompt, {"role": "user", "content": user_content}]


def call_mapping_llm(
    messages: list[dict[str, str]],
    model: str,
    api_key: str | None,
    base_url: str | None,
    temperature: float = 0.2,
) -> dict[str, Any]:
    """Call an OpenAI-compatible chat completion endpoint to propose mappings."""
    settings = get_settings()
    api_key = api_key or settings.openai_api_key
    base_url = base_url or settings.openai_base_url
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY is not set and no api_key was provided")

    from openai import OpenAI

    client = OpenAI(api_key=api_key, base_url=base_url)
    response = client.chat.completions.create(
        model=model,
        messages=cast(Any, messages),
        temperature=temperature,
        response_format={"type": "json_object"},
    )
    content = response.choices[0].message.content
    if content is None:
        raise RuntimeError("LLM returned empty content")
    return cast(dict[str, Any], json.loads(content))


def _normalize_polars_expression(expression: str | None) -> str | None:
    """Fix common Polars mistakes made by the LLM.

    Keeps the expression unchanged if it is None.
    """
    if expression is None:
        return None
    import re

    # LLMs often use pandas/string-style title casing
    expression = expression.replace(".str.title()", ".str.to_titlecase()")
    # LLMs often call Series.map() which does not exist in Polars
    if ".map({" in expression:
        expression = expression.replace(".map({", ".replace({")
    # LLMs often use pandas str.strip() instead of Polars str.strip_chars()
    expression = expression.replace(".str.strip()", ".str.strip_chars()")
    # str.contains does not accept a case parameter; use (?i) inline flag instead.
    expression = expression.replace(", case=False)", ")")
    expression = expression.replace("str.contains('kg'", "str.contains(r'(?i)kg'")
    expression = expression.replace('str.contains("kg"', 'str.contains(r"(?i)kg"')
    # LLMs often pass bare string literals to .then()/.otherwise(); wrap in pl.lit().
    expression = re.sub(
        r"\.then\((['\"])([^'\"]*)\1\)",
        r".then(pl.lit(\1\2\1))",
        expression,
    )
    expression = re.sub(
        r"\.otherwise\((['\"])([^'\"]*)\1\)",
        r".otherwise(pl.lit(\1\2\1))",
        expression,
    )
    # LLMs often pass a global regex flag to str.replace; Polars replaces
    # all occurrences by default.
    expression = re.sub(
        r"\.str\.replace\((.+?),\s*(['\"])([^'\"]*)\2,\s*(['\"])g\4\)",
        r".str.replace(\1, \2\3\2)",
        expression,
    )
    # Replace bare 'null' with Python None (defined in generated scripts).
    expression = re.sub(r"\bnull\b", "None", expression)
    # LLMs sometimes try to index .str.extract() with [0]; Polars already
    # returns the first capture group, so the index is both invalid and
    # unnecessary.
    expression = re.sub(
        r"\.str\.extract\(((?:[^()]*|\([^()]*\))*)\)\[0\]",
        r".str.extract(\1)",
        expression,
    )
    return expression


def parse_llm_mapping_response(
    response: dict[str, Any],
    mapping_spec_id: uuid.UUID,
    target_schema: TargetSchema,
) -> list[ProposedMapping]:
    """Convert an LLM response into a list of ProposedMapping objects."""
    mappings: list[ProposedMapping] = []
    for raw in response.get("mappings", []):
        source_columns = [
            SourceColumnRef.model_validate(ref) for ref in raw.get("source_columns", [])
        ]
        tests: list[str] = []
        for t in raw.get("tests", []):
            if isinstance(t, str):
                tests.append(t)
            else:
                tests.append(json.dumps(t))
        mappings.append(
            ProposedMapping(
                target_table=raw["target_table"],
                target_column=raw["target_column"],
                source_columns=source_columns,
                transformation_logic=raw.get("transformation_logic", ""),
                polars_expression=_normalize_polars_expression(
                    raw.get("polars_expression")
                ),
                transformation_type=raw.get("transformation_type", "expression"),
                aggregation_source_table=raw.get("aggregation_source_table"),
                aggregation_expression=raw.get("aggregation_expression"),
                aggregation_group_key=raw.get("aggregation_group_key"),
                lookup_source_table=raw.get("lookup_source_table"),
                lookup_key=raw.get("lookup_key"),
                lookup_value=raw.get("lookup_value"),
                filter_expression=raw.get("filter_expression"),
                tests=tests,
                evidence_ids=[uuid.UUID(x) for x in raw.get("evidence_ids", [])],
                business_rule_ids=[
                    uuid.UUID(x) for x in raw.get("business_rule_ids", [])
                ],
                confidence=raw.get("confidence"),
            )
        )
    return mappings


def propose_mapping_spec(
    session: Any,
    mapping_spec_id: uuid.UUID,
    target_schema: TargetSchema,
    model: str = "gpt-4o-mini",
    api_key: str | None = None,
    base_url: str | None = None,
    top_k_evidence: int = 10,
) -> MappingSpec:
    """Generate a proposed mapping specification from evidence and profiles."""
    from db_ops import (
        delete_mapping_columns,
        get_mapping_spec,
        get_spreadsheet_profile,
        search_evidence_by_text,
        update_mapping_spec_status,
    )

    spec = get_mapping_spec(session, mapping_spec_id)
    if spec is None:
        raise ValueError(f"Mapping spec not found: {mapping_spec_id}")

    raw_files = [
        session.get(__import__("models").RawFile, rid)
        for rid in spec.source_raw_file_ids
    ]
    raw_files = [rf for rf in raw_files if rf is not None]

    source_catalogs: list[dict[str, Any]] = []
    for raw_file in raw_files:
        profile = get_spreadsheet_profile(session, raw_file.id)
        if profile:
            source_catalogs.append(profile.profile_json)

    raw_file_summary = [
        {
            "filename": rf.original_filename,
            "mime_type": rf.mime_type,
            "raw_file_id": str(rf.id),
        }
        for rf in raw_files
    ]

    evidence_items = _gather_targeted_evidence(
        session,
        spec.client_id,
        target_schema,
        source_catalogs,
        search_evidence_by_text,
        top_k_per_query=max(1, top_k_evidence // 2),
        max_total=top_k_evidence * 4,
    )
    business_rules = session.exec(
        __import__("sqlmodel")
        .select(BusinessRule)
        .where(
            BusinessRule.client_id == spec.client_id,
            BusinessRule.status == "approved",
        )
    ).all()

    settings = get_settings()
    api_key = api_key or settings.openai_api_key
    base_url = base_url or settings.openai_base_url
    if not api_key:
        raise RuntimeError(
            "OPENAI_API_KEY is not set. Mapping proposals require an LLM."
        )

    messages = build_mapping_prompt(
        target_schema,
        source_catalogs,
        evidence_items,
        business_rules,
        raw_file_summary,
    )
    response = call_mapping_llm(messages, model, api_key, base_url)
    proposed = parse_llm_mapping_response(response, mapping_spec_id, target_schema)
    resolution_errors = resolve_composite_keys(proposed, source_catalogs)
    if resolution_errors:
        raise ValueError(
            "LLM mapping failed source resolution: "
            + "; ".join(resolution_errors)
        )
    validation = validate_mapping_columns(proposed, target_schema, source_catalogs)
    validation_errors = [
        error
        for result in validation
        for error in result["validation_errors"]
    ]
    if validation_errors:
        raise ValueError(
            "LLM mapping failed validation: "
            + "; ".join(validation_errors)
        )

    delete_mapping_columns(session, mapping_spec_id)
    columns = [
        {
            "target_table": m.target_table,
            "target_column": m.target_column,
            "source_columns": [ref.model_dump(mode="json") for ref in m.source_columns],
            "transformation_logic": m.transformation_logic,
            "polars_expression": m.polars_expression,
            "transformation_type": m.transformation_type,
            "aggregation_source_table": m.aggregation_source_table,
            "aggregation_expression": m.aggregation_expression,
            "aggregation_group_key": m.aggregation_group_key,
            "lookup_source_table": m.lookup_source_table,
            "lookup_key": m.lookup_key,
            "lookup_value": m.lookup_value,
            "filter_expression": m.filter_expression,
            "tests": m.tests,
            "evidence_ids": [str(eid) for eid in m.evidence_ids],
            "business_rule_ids": [str(rid) for rid in m.business_rule_ids],
            "confidence": m.confidence,
        }
        for m in proposed
    ]
    from db_ops import create_mapping_columns

    create_mapping_columns(session, mapping_spec_id, columns)
    update_mapping_spec_status(
        session,
        mapping_spec_id,
        MappingSpecStatus.PROPOSED,
    )

    refreshed = get_mapping_spec(session, mapping_spec_id)
    if refreshed is None:
        raise RuntimeError("Mapping spec disappeared after update")
    return refreshed


def validate_mapping_columns(
    mappings: list[ProposedMapping],
    target_schema: TargetSchema,
    spreadsheet_profiles: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Validate proposed mappings against the target schema and source columns."""
    catalog_tables = _catalog_tables(spreadsheet_profiles)
    catalog_by_table_id = {
        table["source_table_id"]: table
        for table in catalog_tables
        if table.get("source_table_id")
    }
    catalog_columns_by_id = {
        column["source_column_id"]: (table, column)
        for table in catalog_tables
        for column in table.get("columns", [])
        if column.get("source_column_id")
    }
    target_columns = {(t.name, c.name) for t in target_schema.tables for c in t.columns}

    results = []
    for mapping in mappings:
        errors: list[str] = []
        if (mapping.target_table, mapping.target_column) not in target_columns:
            errors.append("target column not in target schema")
        for ref in mapping.source_columns:
            if not ref.source_table_id:
                errors.append(
                    f"source {ref.source_table!r}.{ref.source_column!r} "
                    "is missing source_table_id"
                )
                continue
            table = catalog_by_table_id.get(ref.source_table_id)
            if table is None:
                errors.append(
                    f"source_table_id {ref.source_table_id!r} is not in the catalog"
                )
                continue
            if not ref.source_column_id:
                errors.append(
                    f"source {ref.source_table!r}.{ref.source_column!r} "
                    "is missing source_column_id"
                )
                continue
            resolved = catalog_columns_by_id.get(ref.source_column_id)
            if resolved is None or resolved[0] is not table:
                errors.append(
                    f"source_column_id {ref.source_column_id!r} does not belong "
                    f"to source_table_id {ref.source_table_id!r}"
                )
                continue
            expected_raw_file_id = table.get("raw_file_id")
            if expected_raw_file_id and str(ref.raw_file_id or "") != str(
                expected_raw_file_id
            ):
                errors.append(
                    f"raw_file_id for source_column_id "
                    f"{ref.source_column_id!r} does not match the catalog"
                )
        if mapping.transformation_type == "lookup" and mapping.lookup_source_table:
            lookup_tbl = catalog_by_table_id.get(
                mapping.lookup_source_table
            )
            if lookup_tbl is None:
                errors.append(
                    f"lookup_source_table "
                    f"{mapping.lookup_source_table!r} "
                    "is not in the catalog"
                )
            else:
                tbl_col_names = {
                    c.get("normalized_name") or c.get("original_name")
                    for c in lookup_tbl.get("columns", [])
                }
                if (
                    mapping.lookup_key
                    and mapping.lookup_key not in tbl_col_names
                ):
                    errors.append(
                        f"lookup_key {mapping.lookup_key!r} not "
                        "found in lookup table "
                        f"{mapping.lookup_source_table!r}"
                    )
                if (
                    mapping.lookup_value
                    and mapping.lookup_value not in tbl_col_names
                ):
                    errors.append(
                        f"lookup_value {mapping.lookup_value!r} not "
                        "found in lookup table "
                        f"{mapping.lookup_source_table!r}"
                    )
        if (
            mapping.transformation_type == "aggregation"
            and mapping.aggregation_source_table
        ):
            agg_tbl = catalog_by_table_id.get(
                mapping.aggregation_source_table
            )
            if agg_tbl is None:
                errors.append(
                    f"aggregation_source_table "
                    f"{mapping.aggregation_source_table!r} "
                    "is not in the catalog"
                )
            else:
                tbl_col_names = {
                    c.get("normalized_name") or c.get("original_name")
                    for c in agg_tbl.get("columns", [])
                }
                if (
                    mapping.aggregation_group_key
                    and mapping.aggregation_group_key
                    not in tbl_col_names
                ):
                    errors.append(
                        f"aggregation_group_key "
                        f"{mapping.aggregation_group_key!r} not "
                        "found in aggregation table "
                        f"{mapping.aggregation_source_table!r}"
                    )
        results.append(
            {
                "target_table": mapping.target_table,
                "target_column": mapping.target_column,
                "validation_errors": errors,
            }
        )
    return results


def check_target_schema_coverage(
    mappings: list[ProposedMapping],
    target_schema: TargetSchema,
) -> dict[str, Any]:
    """Report which target schema columns are covered or missing."""
    covered = {(m.target_table, m.target_column) for m in mappings}
    missing_required: list[tuple[str, str]] = []
    missing_optional: list[tuple[str, str]] = []
    covered_required: list[tuple[str, str]] = []
    covered_optional: list[tuple[str, str]] = []

    for table in target_schema.tables:
        for col in table.columns:
            key = (table.name, col.name)
            if key in covered:
                (covered_required if col.required else covered_optional).append(key)
            else:
                (missing_required if col.required else missing_optional).append(key)

    return {
        "covered_required": covered_required,
        "covered_optional": covered_optional,
        "missing_required": missing_required,
        "missing_optional": missing_optional,
    }
