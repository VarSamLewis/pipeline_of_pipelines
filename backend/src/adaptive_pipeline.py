"""Bounded candidate generation, isolated previews, and human-gated publishing."""

from __future__ import annotations

import base64
import copy
import hashlib
import io
import json
import re
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import httpx
import polars as pl
from byok import get_organization_api_key, get_organization_provider_base_url
from codegen import generate_polars_script, generated_artifact_filenames
from config import get_settings
from db_ops import get_mapping_spec, get_raw_file_by_id, get_session
from dependencies import get_artifact_store, get_object_store
from mapping import call_mapping_llm, validate_mapping_expression_dtypes
from mapping_specs import load_mapping_spec, load_target_schema_from_spec
from models import (
    Client,
    ExecutionStatus,
    MappingSpec,
    MappingSpecStatus,
    PipelineCandidate,
)
from pipeline import (
    finish_execution_run,
    record_execution_run,
    record_staging_metadata,
    record_validation_results,
    run_validation_tests,
)
from sandbox_worker import (
    MAX_SCRIPT_BYTES,
    MAX_SOURCE_BYTES,
    MAX_TOTAL_SOURCE_BYTES,
    CandidateRequest,
    CandidateResult,
)
from sqlmodel import select

MAX_CANDIDATE_RUNS = 3  # Per mapping version, including failed previews.


class PreviewExecutionError(ValueError):
    """Keep worker details for human review but send only the type to the model."""

    def __init__(self, message: str, failure_type: str | None) -> None:
        super().__init__(message)
        self.failure_type = failure_type or "ProcessError"


_ALLOWED_OVERRIDES = {
    "transformation_type",
    "polars_expression",
    "filter_expression",
    "aggregation_source_table",
    "aggregation_expression",
    "aggregation_group_key",
    "lookup_source_table",
    "lookup_key",
    "lookup_value",
}


def _compile_candidate(
    proposal: dict[str, Any], mapping: dict[str, Any]
) -> tuple[str, dict[str, Any], str]:
    """Compile a plan grounded in approved columns, or accept a script draft."""
    mode = proposal.get("mode")
    if mode == "script":
        script = proposal.get("script")
        if not isinstance(script, str) or not script.strip():
            raise ValueError("Script proposal has no Python code")
        if len(script.encode()) > MAX_SCRIPT_BYTES:
            raise ValueError("Candidate script is too large")
        compile(script, "pipeline.py", "exec")
        return script, mapping, mode
    if mode != "plan":
        raise ValueError("Proposal must choose plan or script mode")
    overrides = proposal.get("overrides", [])
    if not isinstance(overrides, list):
        raise ValueError("Plan overrides must be a list")
    updated = copy.deepcopy(mapping)
    columns = {
        (col["target_table"], col["target_column"]): col for col in updated["columns"]
    }
    seen: set[tuple[str, str]] = set()
    for override in overrides:
        if not isinstance(override, dict):
            raise ValueError("Invalid plan override")
        target = (override.get("target_table"), override.get("target_column"))
        if target not in columns or target in seen:
            raise ValueError("Plan references an unknown or repeated target field")
        if set(override) - _ALLOWED_OVERRIDES - {"target_table", "target_column"}:
            raise ValueError("Plan may not alter source or target identities")
        seen.add(target)
        columns[cast(tuple[str, str], target)].update(
            {key: value for key, value in override.items() if key in _ALLOWED_OVERRIDES}
        )
    schema = load_target_schema_from_spec(updated)
    errors = validate_mapping_expression_dtypes(updated["columns"], schema)
    if errors:
        raise ValueError("; ".join(errors))
    script = generate_polars_script(updated, schema, "mapping.json")
    if len(script.encode()) > MAX_SCRIPT_BYTES:
        raise ValueError("Generated candidate exceeds script limit")
    compile(script, "pipeline.py", "exec")
    return script, updated, mode


def _source_snapshots(mapping: dict[str, Any]) -> tuple[dict[str, str], dict[str, str]]:
    """Read bounded immutable inputs; never forward runtime secrets to worker."""
    with get_session() as session:
        files = [
            get_raw_file_by_id(session, uuid.UUID(value))
            for value in mapping["source_raw_file_ids"]
        ]
        if any(file is None for file in files):
            raise ValueError("A source snapshot is no longer available")
        sources: dict[str, str] = {}
        hashes: dict[str, str] = {}
        total = 0
        for file in files:
            assert file is not None
            if file.client_id != uuid.UUID(mapping["client_id"]):
                raise ValueError("Source snapshot belongs to another client")
            if file.size_bytes > MAX_SOURCE_BYTES:
                raise ValueError(
                    "Source exceeds 2 MiB preview limit; no script was run"
                )
            name = f"{file.id}_{file.original_filename}"
            if "/" in name or "\\" in name or len(name) > 217:
                raise ValueError("Invalid source snapshot filename")
            data = get_object_store().get(file.storage_key)
            total += len(data)
            if len(data) > MAX_SOURCE_BYTES or total > MAX_TOTAL_SOURCE_BYTES:
                raise ValueError(
                    "Sources exceed 4 MiB preview limit; no script was run"
                )
            digest = hashlib.sha256(data).hexdigest()
            if digest != file.sha256:
                raise ValueError("Source snapshot hash mismatch")
            sources[name] = base64.b64encode(data).decode()
            hashes[str(file.id)] = digest
        return sources, hashes


def _mapping_hash(mapping: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(mapping, sort_keys=True, default=str).encode()
    ).hexdigest()


def _request_preview(
    script: str, mapping: dict[str, Any], sources: dict[str, str]
) -> CandidateResult:
    url = get_settings().candidate_worker_url.rstrip("/")
    if not url:
        raise RuntimeError("Isolated candidate worker is not configured")
    headers: dict[str, str] = {}
    if url.startswith("https://"):
        from google.auth.transport.requests import Request
        from google.oauth2.id_token import fetch_id_token

        token = cast(Any, fetch_id_token)(Request(), url)
        headers["Authorization"] = f"Bearer {token}"
    elif not (get_settings().auth_bypass_local and url.startswith("http://127.0.0.1:")):
        raise RuntimeError("Candidate worker must use authenticated HTTPS")
    response = httpx.post(
        f"{url}/execute",
        json=CandidateRequest(
            script=script, mapping=mapping, sources=sources
        ).model_dump(),
        headers=headers,
        timeout=48,
    )
    response.raise_for_status()
    return CandidateResult.model_validate(response.json())


def _reserve_attempt(spec_id: uuid.UUID) -> PipelineCandidate:
    with get_session() as session:
        spec = session.exec(
            select(MappingSpec).where(MappingSpec.id == spec_id).with_for_update()
        ).first()
        if spec is None or spec.status != MappingSpecStatus.APPROVED:
            raise ValueError("Approve the mapping before preparing candidate code")
        previous = list(
            session.exec(
                select(PipelineCandidate)
                .where(PipelineCandidate.mapping_spec_id == spec_id)
                .order_by(cast(Any, PipelineCandidate.attempt))
            ).all()
        )
        if any(item.status == "running" for item in previous):
            raise ValueError("A candidate preview is already running")
        if any(
            item.status in {"passed", "publishing", "approved"} for item in previous
        ):
            raise ValueError("A candidate is already awaiting review or approved")
        if len(previous) >= MAX_CANDIDATE_RUNS:
            raise ValueError(
                "Three candidate executions exhausted for this mapping version"
            )
        candidate = PipelineCandidate(
            mapping_spec_id=spec_id,
            attempt=len(previous) + 1,
            status="running",
            mode="pending",
            script="",
        )
        session.add(candidate)
        session.commit()
        session.refresh(candidate)
        return candidate


def _save_candidate(candidate: PipelineCandidate) -> None:
    with get_session() as session:
        session.add(candidate)
        session.commit()
        session.refresh(candidate)


def prepare_candidate(spec_id: uuid.UUID) -> PipelineCandidate:
    """Try at most three revisions, stopping for review at the first passing one."""
    if not get_settings().candidate_worker_url:
        raise RuntimeError("Isolated candidate worker is not configured")
    mapping = load_mapping_spec(spec_id)
    sources, hashes = _source_snapshots(mapping)
    with get_session() as session:
        spec = get_mapping_spec(session, spec_id)
        client = session.get(Client, spec.client_id) if spec else None
        if client is None or client.organization_id is None:
            raise ValueError("Client organization not found")
        key = get_organization_api_key(session, client.organization_id)
        provider = get_organization_provider_base_url(session, client.organization_id)
    if not key or not provider:
        raise ValueError("Configure an organization model provider before preview")
    feedback = "None"
    while True:
        candidate = _reserve_attempt(spec_id)
        try:
            proposal = call_mapping_llm(
                [
                    {
                        "role": "system",
                        "content": (
                            "Propose a complete Polars transformation plan for the "
                            "approved mapping, returning JSON. Prefer "
                            '{"mode":"plan","overrides":[{"target_table":...,'
                            '"target_column":...,"polars_expression":...}]} '
                            "using only approved source and target fields. For cases "
                            "the mapping generator cannot express, return "
                            '{"mode":"script","script":"complete Python..."}. '
                            "A script must accept --source-folder and --output-folder, "
                            "load mapping.json from its own directory, and write one "
                            "CSV per target table. Do not call the network "
                            "or use secrets."
                        ),
                    },
                    {
                        "role": "user",
                        "content": (
                            "Approved mapping and schema: "
                            f"{json.dumps(mapping, default=str)}\n"
                            f"Previous attempt failure category: {feedback}. "
                            "No raw source data is included."
                        ),
                    },
                ],
                model=get_settings().mapping_model,
                api_key=key,
                base_url=provider,
                timeout_seconds=30,
            )
            script, plan, mode = _compile_candidate(proposal, mapping)
            candidate.script = script
            candidate.plan = plan
            candidate.mode = mode
            candidate.source_hashes = hashes
            preview = _request_preview(script, plan, sources)
            if not preview.passed:
                raise PreviewExecutionError(
                    preview.error or "Candidate failed in worker", preview.failure_type
                )
            frames = {
                Path(name).stem: pl.read_csv(io.BytesIO(base64.b64decode(content)))
                for name, content in preview.outputs.items()
                if Path(name).name == name and name.endswith(".csv")
            }
            if not frames:
                raise ValueError("Candidate produced no valid target tables")
            target_schema = load_target_schema_from_spec(plan)
            if set(frames) != {table.name for table in target_schema.tables}:
                raise ValueError(
                    "Candidate output tables do not match the target schema"
                )
            for table in target_schema.tables:
                if {column.name for column in table.columns} != set(
                    frames[table.name].columns
                ):
                    raise ValueError(
                        "Candidate output columns do not match the target schema"
                    )
            validation = run_validation_tests(
                frames, plan["columns"], target_schema
            )
            failures = [test["test_name"] for test in validation if not test["passed"]]
            if failures:
                raise ValueError("Validation checks failed: " + ", ".join(failures))
            store = get_artifact_store()
            output_hashes: dict[str, str] = {}
            for name, encoded in preview.outputs.items():
                data = base64.b64decode(encoded)
                store.write_artifact(
                    candidate.id,
                    name,
                    data,
                )
                output_hashes[name] = hashlib.sha256(data).hexdigest()
            source_names = list(sources)
            for name, encoded in sources.items():
                store.write_artifact(
                    candidate.id, f"source_{name}", base64.b64decode(encoded)
                )
            candidate.result = {
                "outputs": list(preview.outputs),
                "output_hashes": output_hashes,
                "source_snapshots": source_names,
                "validation": validation,
                "mapping_hash": _mapping_hash(mapping),
                "script_sha256": hashlib.sha256(script.encode()).hexdigest(),
            }
            candidate.status = "passed"
            _save_candidate(candidate)
            return candidate
        except Exception as exc:
            candidate.status = "failed"
            candidate.result = {"error": str(exc)[:4000]}
            _save_candidate(candidate)
            # Only the exception category and failed check names may reach the LLM,
            # never a traceback, original rows, or worker stdout/stderr.
            checks = (
                re.findall(r"[\w.-]+\.(?:not_null|unique|positive)", str(exc))
                if str(exc).startswith("Validation checks failed: ")
                else []
            )
            failure_type = getattr(exc, "failure_type", type(exc).__name__)
            if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,79}", failure_type):
                failure_type = "ProcessError"
            feedback = f"{failure_type}; checks: {', '.join(checks[:10])}"
            if candidate.attempt == MAX_CANDIDATE_RUNS:
                return candidate


def get_candidates(spec_id: uuid.UUID) -> list[PipelineCandidate]:
    with get_session() as session:
        return list(
            session.exec(
                select(PipelineCandidate)
                .where(PipelineCandidate.mapping_spec_id == spec_id)
                .order_by(cast(Any, PipelineCandidate.attempt))
            ).all()
        )


def approve_candidate(
    spec_id: uuid.UUID, candidate_id: uuid.UUID, reviewer: str
) -> uuid.UUID:
    """Publish only the exact reviewed code and source snapshot results."""
    with get_session() as session:
        candidate = session.exec(
            select(PipelineCandidate)
            .where(
                PipelineCandidate.id == candidate_id,
                PipelineCandidate.mapping_spec_id == spec_id,
            )
            .with_for_update()
        ).first()
        if candidate is None or candidate.status != "passed":
            raise ValueError("Candidate must pass preview and await review")
        if candidate.result.get("mapping_hash") != _mapping_hash(
            load_mapping_spec(spec_id)
        ):
            raise ValueError(
                "Mapping changed since preview; create a new mapping version"
            )
        if candidate.result.get("script_sha256") != hashlib.sha256(
            candidate.script.encode()
        ).hexdigest():
            raise ValueError("Candidate code changed since preview")
        spec = get_mapping_spec(session, spec_id)
        client = session.get(Client, spec.client_id) if spec else None
        if spec is None or client is None or client.organization_id is None:
            raise ValueError("Mapping organization not found")
        client_id = client.id
        organization_id = client.organization_id
        candidate.status = "publishing"
        session.add(candidate)
        session.commit()
        session.refresh(candidate)

    run_id: uuid.UUID | None = None
    published = False
    try:
        run_id = record_execution_run(client_id, spec_id, organization_id, "local")
        store = get_artifact_store()
        schema = load_target_schema_from_spec(candidate.plan)
        filenames = generated_artifact_filenames(
            schema,
            candidate.plan.get("client_name", "client"),
            candidate.plan.get("created_at"),
        )
        outputs = candidate.result["outputs"]
        for name in candidate.result["source_snapshots"]:
            raw_file_id = name.split("_", 1)[0]
            data = store.read_artifact(candidate.id, f"source_{name}")
            if hashlib.sha256(data).hexdigest() != candidate.source_hashes[raw_file_id]:
                raise ValueError("Candidate source snapshot changed since review")
        verified_outputs: dict[str, bytes] = {}
        for name in outputs:
            data = store.read_artifact(candidate.id, name)
            if (
                hashlib.sha256(data).hexdigest()
                != candidate.result["output_hashes"][name]
            ):
                raise ValueError("Candidate preview changed since review")
            verified_outputs[name] = data
        store.write_artifact(spec_id, filenames["pipeline"], candidate.script.encode())
        store.write_artifact(
            spec_id, filenames["mapping"], json.dumps(candidate.plan).encode()
        )
        frames: dict[str, pl.DataFrame] = {}
        for name, data in verified_outputs.items():
            store.write_artifact(
                spec_id,
                name,
                data,
            )
            frames[Path(name).stem] = pl.read_csv(io.BytesIO(data))
        if len(outputs) == 1:
            store.write_artifact(
                spec_id, filenames["results"], store.read_artifact(spec_id, outputs[0])
            )
        record_validation_results(run_id, candidate.result["validation"])
        record_staging_metadata(run_id, frames, candidate.plan["columns"], schema)
        store.write_log(
            run_id,
            json.dumps(
                {"candidate_id": str(candidate.id), "attempt": candidate.attempt}
            ).encode(),
        )
        finish_execution_run(run_id, ExecutionStatus.SUCCESS)
        published = True
        candidate.status = "approved"
        candidate.approved_by = reviewer
        candidate.approved_at = datetime.now(UTC).replace(tzinfo=None)
        _save_candidate(candidate)
        return run_id
    except Exception as exc:
        if run_id is not None and not published:
            finish_execution_run(run_id, ExecutionStatus.FAILED, str(exc))
        if not published:
            candidate.status = "passed"
            _save_candidate(candidate)
        raise
