"""FastAPI application composition.

This module owns framework construction, middleware, static assets, and router
registration.  Business orchestration belongs to workflow services; endpoint
translation belongs to router modules.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from auth_service import SESSION_MAX_AGE_SECONDS, SESSION_SECRET_KEY
from config import get_settings
from db_ops import create_tables, get_engine
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from routers.api import app as api_router
from starlette.middleware.sessions import SessionMiddleware
from ui import router as ui_router


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
            "Auditable data-transformation platform for heterogeneous client "
            "files."
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
        https_only=False,
        path="/",
    )
    application.mount(
        "/static",
        StaticFiles(directory=str(settings.static_dir)),
        name="static",
    )
    application.include_router(ui_router)
    application.include_router(api_router)
    return application


app = create_app()
