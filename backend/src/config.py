"""Central application configuration.

Environment variables are read once through :func:`get_settings`.  Callers that
need deterministic configuration in tests can construct :class:`Settings`
directly with :meth:`Settings.from_env`.
"""

from __future__ import annotations

import os
import secrets
from collections.abc import Mapping
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

DEFAULT_DATABASE_URL = "postgresql+psycopg://postgres:postgres@localhost:5432/pipeline"


def _as_bool(value: str | None, *, default: bool = False) -> bool:
    """Parse a conventional environment-variable boolean."""
    if value is None:
        return default
    return value.lower() in {"1", "true", "yes"}


@dataclass(frozen=True, slots=True)
class Settings:
    """Runtime configuration and canonical local-development paths."""

    project_root: Path
    app_environment: str
    data_dir: Path
    object_store_dir: Path
    target_schemas_dir: Path
    output_folders_dir: Path
    work_dir: Path
    static_dir: Path
    templates_dir: Path
    database_url: str
    openai_api_key: str | None
    openai_base_url: str | None
    mapping_model: str
    embedding_model: str
    clerk_publishable_key: str
    clerk_issuer: str
    clerk_audience: str
    clerk_jwks_url: str
    clerk_sign_in_url: str
    clerk_jwt_template: str
    session_secret_key: str
    session_max_age_seconds: int
    session_cookie_secure: bool
    auth_bypass_local: bool
    pipeline_execution_timeout_seconds: int
    max_upload_size_bytes: int
    bootstrap_admin_emails: frozenset[str]
    gcs_bucket: str

    @classmethod
    def from_env(
        cls,
        env: Mapping[str, str] | None = None,
        *,
        project_root: Path | None = None,
    ) -> Settings:
        """Build settings from an environment mapping."""
        values = os.environ if env is None else env
        default_root = Path(__file__).resolve().parents[2]
        configured_root: str | Path = (
            project_root
            if project_root is not None
            else values.get("PIPELINE_PROJECT_ROOT", str(default_root))
        )
        root = Path(configured_root).resolve()
        data_dir = Path(values.get("PIPELINE_DATA_DIR", root / "data")).resolve()
        app_environment = values.get("APP_ENVIRONMENT", "local").lower()
        session_secret_key = values.get("SESSION_SECRET_KEY")
        auth_bypass_local = _as_bool(values.get("AUTH_BYPASS_LOCAL"))
        session_cookie_secure = _as_bool(values.get("SESSION_COOKIE_SECURE"))
        bootstrap_admin_emails = frozenset(
            email.strip().lower()
            for email in values.get("BOOTSTRAP_ADMIN_EMAILS", "").split(",")
            if email.strip()
        )
        if app_environment == "production":
            if not session_secret_key:
                raise ValueError("SESSION_SECRET_KEY is required in production")
            if auth_bypass_local:
                raise ValueError("AUTH_BYPASS_LOCAL must be false in production")
            if not session_cookie_secure:
                raise ValueError("SESSION_COOKIE_SECURE must be true in production")
            if not bootstrap_admin_emails:
                raise ValueError("BOOTSTRAP_ADMIN_EMAILS is required in production")

        return cls(
            project_root=root,
            app_environment=app_environment,
            data_dir=data_dir,
            object_store_dir=Path(
                values.get("OBJECT_STORE_DIR", data_dir / "object-store")
            ).resolve(),
            target_schemas_dir=Path(
                values.get("TARGET_SCHEMAS_DIR", data_dir / "target-schemas")
            ).resolve(),
            output_folders_dir=Path(
                values.get("OUTPUT_FOLDERS_DIR", data_dir / "output-folders")
            ).resolve(),
            work_dir=Path(values.get("WORK_DIR", "/tmp/pipeline-work")).resolve(),
            static_dir=Path(
                values.get("STATIC_DIR", root / "backend" / "static")
            ).resolve(),
            templates_dir=Path(
                values.get("TEMPLATES_DIR", root / "backend" / "templates")
            ).resolve(),
            database_url=values.get("DATABASE_URL", DEFAULT_DATABASE_URL),
            openai_api_key=values.get("OPENAI_API_KEY"),
            openai_base_url=values.get("OPENAI_BASE_URL"),
            mapping_model=values.get("MAPPING_MODEL", "gpt-4o-mini"),
            embedding_model=values.get("EMBEDDING_MODEL", "text-embedding-3-small"),
            clerk_publishable_key=values.get("CLERK_PUBLISHABLE_KEY", ""),
            clerk_issuer=values.get("CLERK_ISSUER", ""),
            clerk_audience=values.get("CLERK_AUDIENCE", ""),
            clerk_jwks_url=values.get("CLERK_JWKS_URL", ""),
            clerk_sign_in_url=values.get("CLERK_SIGN_IN_URL", ""),
            clerk_jwt_template=values.get("CLERK_JWT_TEMPLATE", "pipeline-beta"),
            session_secret_key=session_secret_key or secrets.token_urlsafe(32),
            session_max_age_seconds=int(values.get("SESSION_MAX_AGE", "86400")),
            session_cookie_secure=session_cookie_secure,
            auth_bypass_local=auth_bypass_local,
            pipeline_execution_timeout_seconds=int(
                values.get("PIPELINE_EXECUTION_TIMEOUT_SECONDS", "120")
            ),
            max_upload_size_bytes=int(values.get("MAX_UPLOAD_SIZE_BYTES", "52428800")),
            bootstrap_admin_emails=bootstrap_admin_emails,
            gcs_bucket=values.get("GCS_BUCKET", ""),
        )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings instance."""
    return Settings.from_env()
