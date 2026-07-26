"""Execution-run persistence operations."""

from __future__ import annotations

import datetime
import uuid

from models import ExecutionRun, ExecutionStatus
from sqlmodel import Session


def approve_result(session: Session, run_id: uuid.UUID) -> ExecutionRun:
    """Apply the result-approval transition in the caller's transaction."""
    run = session.get(ExecutionRun, run_id)
    if run is None:
        raise ValueError("Execution run not found")
    run.status = ExecutionStatus.SUCCESS
    run.finished_at = datetime.datetime.now(datetime.UTC)
    session.add(run)
    session.flush()
    session.refresh(run)
    return run


def reject_result(
    session: Session,
    run_id: uuid.UUID,
    reason: str = "",
) -> ExecutionRun:
    """Apply the result-rejection transition in the caller's transaction."""
    run = session.get(ExecutionRun, run_id)
    if run is None:
        raise ValueError("Execution run not found")
    run.status = ExecutionStatus.FAILED
    run.finished_at = datetime.datetime.now(datetime.UTC)
    run.logs = {**(run.logs or {}), "rejection_reason": reason}
    session.add(run)
    session.flush()
    return run
