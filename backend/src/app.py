"""FastAPI application composition.

This module owns framework construction, middleware, static assets, and router
registration.  Business orchestration belongs to workflow services; endpoint
translation belongs to router modules.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

from auth_service import (
    SESSION_COOKIE_SECURE,
    SESSION_MAX_AGE_SECONDS,
    SESSION_SECRET_KEY,
)
from config import get_settings
from db_ops import create_tables, get_engine
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from routers.api import app as api_router
from starlette.middleware.sessions import SessionMiddleware
from starlette.requests import Request
from starlette.responses import Response
from ui import router as ui_router


def _request_audit_logger() -> logging.Logger:
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


REQUEST_AUDIT_LOGGER = _request_audit_logger()


def _request_id(request: Request) -> str:
    """Use a valid caller request ID or generate one safe to echo and log."""
    supplied = request.headers.get("X-Request-ID")
    if supplied:
        try:
            return str(uuid.UUID(supplied))
        except ValueError:
            pass
    return str(uuid.uuid4())


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    """Initialize durable infrastructure for the application lifetime."""
    create_tables(get_engine())
    yield


def create_app() -> FastAPI:
    """Compose and return the configured HTTP application."""
    settings = get_settings()
    application = FastAPI(
        title="Pipeline of Pipelines",
        description=(
            "Auditable data-transformation platform for heterogeneous client files."
        ),
        version="0.1.0",
        lifespan=lifespan,
    )
    application.add_middleware(
        SessionMiddleware,
        secret_key=SESSION_SECRET_KEY,
        session_cookie="session",
        max_age=SESSION_MAX_AGE_SECONDS,
        same_site="lax",
        https_only=SESSION_COOKIE_SECURE,
        path="/",
    )

    @application.middleware("http")
    async def add_security_headers(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        """Apply browser protections and emit safe authenticated-request audit logs."""
        request_id = _request_id(request)
        request.state.request_id = request_id
        started_at = time.perf_counter()
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Permissions-Policy"] = (
            "camera=(), microphone=(), geolocation=()"
        )
        if settings.app_environment == "production":
            response.headers["Strict-Transport-Security"] = (
                "max-age=31536000; includeSubDomains"
            )
        response.headers["X-Request-ID"] = request_id

        actor_user_id = getattr(request.state, "actor_user_id", None)
        if actor_user_id is not None:
            REQUEST_AUDIT_LOGGER.info(
                json.dumps(
                    {
                        "severity": "INFO",
                        "message": "authenticated_request",
                        "request_id": request_id,
                        "actor_user_id": actor_user_id,
                        "method": request.method,
                        "path": request.url.path,
                        "status_code": response.status_code,
                        "duration_ms": round((time.perf_counter() - started_at) * 1000),
                    }
                )
            )
        return response

    application.mount(
        "/static",
        StaticFiles(directory=str(settings.static_dir)),
        name="static",
    )
    application.include_router(ui_router)
    application.include_router(api_router)
    return application


app = create_app()
