"""Bounded candidate generation, isolated previews, and human-gated publishing."""

from __future__ import annotations

import base64
import copy
import hashlib
import io
import json
import re
import uuid
from datetime import UTC, datetime, timedelta
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
# A prepare request runs at most one LLM call plus preview per attempt, and the
# API caps requests at 300s. The lease must comfortably exceed that ceiling so
# an in-flight request is never reclaimed by a concurrent one.
CANDIDATE_LEASE_SECONDS = 600


class CandidateSupersededError(RuntimeError):
    """The candidate row changed state elsewhere; this writer must stop."""


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
        # A prepare request that timed out or crashed never reported back and
        # would otherwise block this mapping version forever. Reclaim stale
        # running rows as failed attempts; they still count toward the budget.
        cutoff = datetime.now(UTC).replace(tzinfo=None) - timedelta(
            seconds=CANDIDATE_LEASE_SECONDS
        )
        for item in previous:
            if item.status == "running" and item.created_at < cutoff:
                item.status = "failed"
                item.result = {"error": "Preview lease expired; no result was reported"}
                session.add(item)
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


def _save_candidate(candidate: PipelineCandidate, expected_status: str) -> None:
    """Update candidate fields only while the row still holds expected_status.

    The conditional update prevents a request whose row was reclaimed by the
    lease expiry from resurrecting it after a concurrent request took over.
    """
    with get_session() as session:
        row = session.exec(
            select(PipelineCandidate)
            .where(
                PipelineCandidate.id == candidate.id,
                PipelineCandidate.status == expected_status,
            )
            .with_for_update()
        ).first()
        if row is None:
            raise CandidateSupersededError(
                "Candidate was reclaimed or finalized elsewhere; no update applied"
            )
        row.status = candidate.status
        row.mode = candidate.mode
        row.plan = candidate.plan
        row.script = candidate.script
        row.source_hashes = candidate.source_hashes
        row.result = candidate.result
        row.approved_by = candidate.approved_by
        row.approved_at = candidate.approved_at
        session.add(row)
        session.commit()
        session.refresh(row)


def _reload_candidate(candidate: PipelineCandidate) -> PipelineCandidate:
    """Replace in-memory state with the row's authoritative database state."""
    with get_session() as session:
        fresh = session.get(PipelineCandidate, candidate.id)
        if fresh is not None:
            candidate.status = fresh.status
            candidate.mode = fresh.mode
            candidate.plan = fresh.plan
            candidate.script = fresh.script
            candidate.source_hashes = fresh.source_hashes
            candidate.result = fresh.result
            candidate.approved_by = fresh.approved_by
            candidate.approved_at = fresh.approved_at
    return candidate


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
            validation = run_validation_tests(frames, plan["columns"], target_schema)
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
            _save_candidate(candidate, expected_status="running")
            return candidate
        except CandidateSupersededError:
            # Another actor reclaimed or finalized this attempt; its state is
            # already recorded. Stop without touching the attempt budget.
            return _reload_candidate(candidate)
        except Exception as exc:
            try:
                candidate.status = "failed"
                candidate.result = {"error": str(exc)[:4000]}
                _save_candidate(candidate, expected_status="running")
            except CandidateSupersededError:
                return _reload_candidate(candidate)
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


PUBLISH_STAGE = "publish_stage"


def _lock_candidate(
    session: Any, candidate_id: uuid.UUID, status: str
) -> PipelineCandidate | None:
    """Lock the candidate row for update only while it holds `status`."""
    return cast(
        "PipelineCandidate | None",
        session.exec(
            select(PipelineCandidate)
            .where(
                PipelineCandidate.id == candidate_id,
                PipelineCandidate.status == status,
            )
            .with_for_update()
        ).first(),
    )


def _publish_candidate(candidate_id: uuid.UUID, reviewer: str) -> uuid.UUID:
    """Publish a candidate whose row is in the `publishing` state.

    Every stage checkpoints its progress on the candidate row, so an
    interrupted publication can be reconciled without duplicating execution
    runs or validation records. Verification happens before any side effect;
    artifact writes are idempotent overwrites; the append-only record stage is
    guarded by its checkpoint.
    """
    store = get_artifact_store()
    run_id: uuid.UUID | None = None
    try:
        # Stage 1: verify every retained hash before touching anything shared.
        with get_session() as session:
            row = _lock_candidate(session, candidate_id, status="publishing")
            if row is None:
                raise CandidateSupersededError("Candidate is not awaiting publication")
            spec_id = row.mapping_spec_id
            if row.result.get("mapping_hash") != _mapping_hash(
                load_mapping_spec(spec_id)
            ):
                raise ValueError(
                    "Mapping changed since preview; create a new mapping version"
                )
            if (
                row.result.get("script_sha256")
                != hashlib.sha256(row.script.encode()).hexdigest()
            ):
                raise ValueError("Candidate code changed since preview")
            for name in row.result["source_snapshots"]:
                raw_file_id = name.split("_", 1)[0]
                data = store.read_artifact(candidate_id, f"source_{name}")
                if hashlib.sha256(data).hexdigest() != row.source_hashes[raw_file_id]:
                    raise ValueError("Candidate source snapshot changed since review")
            verified_outputs: dict[str, bytes] = {}
            for name in row.result["outputs"]:
                data = store.read_artifact(candidate_id, name)
                if (
                    hashlib.sha256(data).hexdigest()
                    != row.result["output_hashes"][name]
                ):
                    raise ValueError("Candidate preview changed since review")
                verified_outputs[name] = data
            plan = row.plan
            script = row.script
            output_names = list(row.result["outputs"])
            spec = get_mapping_spec(session, spec_id)
            client = session.get(Client, spec.client_id) if spec else None
            if spec is None or client is None or client.organization_id is None:
                raise ValueError("Mapping organization not found")
            client_id = client.id
            organization_id = client.organization_id

        # Stage 2: the execution run, checkpointed so a retry reuses it
        # instead of creating a duplicate.
        with get_session() as session:
            row = _lock_candidate(session, candidate_id, status="publishing")
            if row is None:
                raise CandidateSupersededError("Candidate is not awaiting publication")
            run_id_value = row.result.get("publishing_run_id")
            if run_id_value:
                run_id = uuid.UUID(run_id_value)
            else:
                run_id = record_execution_run(
                    client_id, spec_id, organization_id, "local"
                )
                row.result = {
                    **row.result,
                    "publishing_run_id": str(run_id),
                    PUBLISH_STAGE: "run_created",
                }
                session.add(row)
                session.commit()

        # Stage 3: write the published artifacts (idempotent overwrites).
        schema = load_target_schema_from_spec(plan)
        filenames = generated_artifact_filenames(
            schema,
            plan.get("client_name", "client"),
            plan.get("created_at"),
        )
        store.write_artifact(spec_id, filenames["pipeline"], script.encode())
        store.write_artifact(spec_id, filenames["mapping"], json.dumps(plan).encode())
        frames: dict[str, pl.DataFrame] = {}
        for name, data in verified_outputs.items():
            store.write_artifact(spec_id, name, data)
            frames[Path(name).stem] = pl.read_csv(io.BytesIO(data))
        if len(output_names) == 1:
            store.write_artifact(
                spec_id, filenames["results"], verified_outputs[output_names[0]]
            )

        # Stage 4: append-only execution records, written exactly once under
        # the row lock so a concurrent reconciler cannot double-record.
        with get_session() as session:
            row = _lock_candidate(session, candidate_id, status="publishing")
            if row is None:
                raise CandidateSupersededError(
                    "Candidate publication was finalized elsewhere"
                )
            if row.result.get(PUBLISH_STAGE) != "records_written":
                record_validation_results(run_id, row.result["validation"])
                record_staging_metadata(run_id, frames, plan["columns"], schema)
                store.write_log(
                    run_id,
                    json.dumps(
                        {"candidate_id": str(candidate_id), "attempt": row.attempt}
                    ).encode(),
                )
                row.result = {**row.result, PUBLISH_STAGE: "records_written"}
                session.add(row)
                session.commit()

        # Stage 5: finalize the run and the candidate (both idempotent).
        finish_execution_run(run_id, ExecutionStatus.SUCCESS)
        with get_session() as session:
            row = _lock_candidate(session, candidate_id, status="publishing")
            if row is None:
                # Another actor finalized the candidate; the run already
                # finished successfully, so nothing is left to do.
                return run_id
            row.result = {**row.result, PUBLISH_STAGE: "finished"}
            row.status = "approved"
            row.approved_by = reviewer
            row.approved_at = datetime.now(UTC).replace(tzinfo=None)
            session.add(row)
            session.commit()
        return run_id
    except Exception as exc:
        # Only unwind when this publication still owns the row. If another
        # actor finalized it, the run may already be a successful record.
        if _rollback_publication(candidate_id) and run_id is not None:
            finish_execution_run(run_id, ExecutionStatus.FAILED, str(exc)[:2000])
        raise


def _rollback_publication(candidate_id: uuid.UUID) -> bool:
    """Return a stuck publishing candidate to review, clearing checkpoints.

    A candidate whose publication reached the final checkpoint keeps its run
    id so reconciliation can finish it without creating a duplicate run.
    Returns True when the row was actually rolled back.
    """
    with get_session() as session:
        row = _lock_candidate(session, candidate_id, status="publishing")
        if row is None or row.result.get(PUBLISH_STAGE) == "finished":
            return False
        row.status = "passed"
        row.result = {
            key: value
            for key, value in row.result.items()
            if key not in {"publishing_run_id", PUBLISH_STAGE}
        }
        session.add(row)
        session.commit()
        return True


def approve_candidate(
    spec_id: uuid.UUID, candidate_id: uuid.UUID, reviewer: str
) -> uuid.UUID:
    """Publish only the exact reviewed code and source snapshot results."""
    with get_session() as session:
        row = session.exec(
            select(PipelineCandidate)
            .where(
                PipelineCandidate.id == candidate_id,
                PipelineCandidate.mapping_spec_id == spec_id,
            )
            .with_for_update()
        ).first()
        if row is None or row.status != "passed":
            raise ValueError("Candidate must pass preview and await review")
        row.status = "publishing"
        session.add(row)
        session.commit()
    return _publish_candidate(candidate_id, reviewer)


def reconcile_candidate(
    spec_id: uuid.UUID, candidate_id: uuid.UUID, reviewer: str
) -> uuid.UUID:
    """Finish or roll back a publication interrupted by a crash or timeout.

    All retained hashes are re-verified before any shared artifact is touched.
    """
    with get_session() as session:
        row = session.exec(
            select(PipelineCandidate)
            .where(
                PipelineCandidate.id == candidate_id,
                PipelineCandidate.mapping_spec_id == spec_id,
            )
            .with_for_update()
        ).first()
        if row is None or row.status != "publishing":
            raise ValueError("Candidate is not awaiting publication")
    return _publish_candidate(candidate_id, reviewer)
