"""Safe structured request-audit logging for Cloud Run."""

from __future__ import annotations

import logging
import uuid

from starlette.requests import Request


def _create_logger() -> logging.Logger:
    """Create a logger whose JSON lines are parsed by Cloud Logging."""
    logger = logging.getLogger("pipeline.request_audit")
    if logger.handlers:
        return logger
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    return logger


REQUEST_AUDIT_LOGGER = _create_logger()


def request_id_for(request: Request) -> str:
    """Use a valid caller request ID or generate one safe to echo and log."""
    supplied = request.headers.get("X-Request-ID")
    if supplied:
        try:
            return str(uuid.UUID(supplied))
        except ValueError:
            pass
    return str(uuid.uuid4())
