from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import Any

from config import get_settings
from db_ops import (
    create_extracted_evidence,
    get_embedding,
    get_mapping_spec,
    get_session,
)
from mapping import call_mapping_llm
from mapping_specs import load_mapping_spec, load_target_schema_from_spec
from models import (
    TargetSchema,
)


def _build_refinement_prompt(
    mapping_spec: dict[str, Any],
    target_schema: TargetSchema,
    feedback: str,
    validation_context: dict[str, Any] | None = None,
) -> list[dict[str, str]]:
    columns_text = json.dumps(
        [
            {
                "target_table": c["target_table"],
                "target_column": c["target_column"],
                "transformation_type": c.get("transformation_type"),
                "polars_expression": c.get("polars_expression"),
                "filter_expression": c.get("filter_expression"),
                "lookup_source_table": c.get("lookup_source_table"),
                "lookup_value": c.get("lookup_value"),
                "aggregation_source_table": c.get("aggregation_source_table"),
                "aggregation_expression": c.get("aggregation_expression"),
            }
            for c in mapping_spec["columns"]
        ],
        indent=2,
    )

    validation_section = ""
    if validation_context:
        validation_section = (
            f"\nValidation results from the last execution:\n"
            f"{json.dumps(validation_context, indent=2)}\n\n"
            "The user is responding to these results. Your proposals should fix "
            "the validation failures shown above."
        )

    prompt = {
        "role": "system",
        "content": (
            "You are a data-mapping refinement assistant. Given the current "
            "mapping specification and user feedback, propose specific column-level "
            "changes to improve the mappings. "
            "Return valid JSON with a 'proposals' array. Each proposal has: "
            "target_column (string), field (string: the field to change, e.g. "
            "'polars_expression', 'filter_expression', 'lookup_value'), "
            "old_value (string or null), new_value (string), "
            "reason (string explaining the change). "
            "Only propose changes that directly address the user's feedback. "
            "Do not modify unrelated columns. "
            "Use the same Polars expression conventions as the existing mappings. "
            "Available globals: pl, col, when, coalesce, lit, concat_str, concat, "
            "null, Int64, Float64, String, Date, Datetime, Boolean."
        ),
    }
    user_content = (
        f"Current mapping specification:\n{columns_text}\n\n"
        f"Target schema:\n{target_schema.model_dump_json(indent=2)}\n\n"
        f"{validation_section}"
        f"User feedback:\n{feedback}\n\n"
        "Return JSON: {\"proposals\": [{\"target_column\": ..., "
        "\"field\": ..., \"old_value\": ..., \"new_value\": ..., "
        "\"reason\": ...}]}"
    )
    return [prompt, {"role": "user", "content": user_content}]


def propose_refinements(
    spec_id: uuid.UUID,
    feedback: str,
    validation_context: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    settings = get_settings()
    mapping_spec = load_mapping_spec(spec_id)
    target_schema = load_target_schema_from_spec(mapping_spec)

    messages = _build_refinement_prompt(
        mapping_spec, target_schema, feedback, validation_context
    )
    response = call_mapping_llm(
        messages,
        model=settings.mapping_model,
        api_key=settings.openai_api_key,
        base_url=settings.openai_base_url,
        temperature=0.1,
    )

    raw_proposals = response.get("proposals", [])
    proposals = []
    columns = mapping_spec.get("columns", [])
    col_by_target = {c["target_column"]: c for c in columns}

    for p in raw_proposals:
        target_column = p.get("target_column", "")
        field = p.get("field", "polars_expression")
        old_value = p.get("old_value")
        new_value = p.get("new_value")
        reason = p.get("reason", "")

        existing = col_by_target.get(target_column)
        if existing is None:
            continue

        proposals.append({
            "column_id": existing.get("id"),
            "target_table": existing.get("target_table"),
            "target_column": target_column,
            "field": field,
            "old_value": old_value or existing.get(field),
            "new_value": new_value,
            "reason": reason,
        })

    return proposals


def store_feedback(
    feedback_text: str,
    spec_id: uuid.UUID,
    run_id: uuid.UUID | None = None,
) -> None:
    with get_session() as session:
        spec = get_mapping_spec(session, spec_id)
        if spec is None:
            raise ValueError(f"Mapping spec not found: {spec_id}")
        client_id = spec.client_id

        raw_file_ids = [uuid.UUID(str(x)) for x in spec.source_raw_file_ids]
        raw_file_id = raw_file_ids[0] if raw_file_ids else uuid.uuid4()

        feedback_record = {
            "spec_id": str(spec_id),
            "run_id": str(run_id) if run_id else None,
            "feedback": feedback_text,
            "timestamp": datetime.now(UTC).isoformat(),
        }
        from dependencies import get_artifact_store
        store = get_artifact_store()
        feedback_dir = store.folder(spec_id) / "feedback"
        feedback_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(UTC).strftime("%Y%m%d_%H%M%S_%f")
        (feedback_dir / f"{ts}.json").write_text(
            json.dumps(feedback_record, indent=2)
        )

        embedding = get_embedding(feedback_text)
        create_extracted_evidence(
            session=session,
            client_id=client_id,
            raw_file_id=raw_file_id,
            evidence_type="user_feedback",
            content=feedback_text,
            embedding=embedding,
            page_ref=f"spec://{spec_id}",
            chunk_index=None,
            metadata={
                "spec_id": str(spec_id),
                "run_id": str(run_id) if run_id else None,
                "source": "chat_feedback",
            },
        )
