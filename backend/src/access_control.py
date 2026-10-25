"""Shared UI/API resource authorization dependencies."""

from __future__ import annotations

import uuid
from typing import Any

from auth_service import AUTH_BYPASS_LOCAL, require_auth, require_client_membership
from db_ops import get_mapping_spec, get_session
from fastapi import Depends, HTTPException


def require_mapping_spec_access(
    spec_id: uuid.UUID, user: Any = Depends(require_auth)
) -> None:
    """Authorize through the client that owns a mapping specification."""
    if AUTH_BYPASS_LOCAL:
        return
    with get_session() as session:
        spec = get_mapping_spec(session, spec_id)
        if spec is None:
            raise HTTPException(404, "Mapping spec not found")
        require_client_membership(session, user, spec.client_id)


def require_execution_run_access(
    run_id: uuid.UUID, user: Any = Depends(require_auth)
) -> None:
    """Authorize through the client that owns an execution run."""
    if AUTH_BYPASS_LOCAL:
        return
    from models import ExecutionRun

    with get_session() as session:
        run = session.get(ExecutionRun, run_id)
        if run is None:
            raise HTTPException(404, "Execution run not found")
        require_client_membership(session, user, run.client_id)
