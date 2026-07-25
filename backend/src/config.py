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

DEFAULT_DATABASE_URL = (
    "postgresql+psycopg://postgres:postgres@localhost:5432/pipeline"
)


def _as_bool(value: str | None, *, default: bool = False) -> bool:
    """Parse a conventional environment-variable boolean."""
    if value is None:
        return default
    return value.lower() in {"1", "true", "yes"}


@dataclass(frozen=True, slots=True)
class Settings:
    """Runtime configuration and canonical local-development paths."""

    project_root: Path
    data_dir: Path
    object_store_dir: Path
    target_schemas_dir: Path
    output_folders_dir: Path
    static_dir: Path
    templates_dir: Path
    database_url: str
    openai_api_key: str | None
    openai_base_url: str | None
    mapping_model: str
    embedding_model: str
    workos_client_id: str
    workos_api_key: str
    workos_redirect_uri: str
    workos_authkit_domain: str
    session_secret_key: str
    session_max_age_seconds: int
    auth_bypass_local: bool

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

        return cls(
            project_root=root,
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
            embedding_model=values.get(
                "EMBEDDING_MODEL", "text-embedding-3-small"
            ),
            workos_client_id=values.get("WORKOS_CLIENT_ID", ""),
            workos_api_key=values.get("WORKOS_API_KEY", ""),
            workos_redirect_uri=values.get(
                "WORKOS_REDIRECT_URI",
                "http://localhost:8000/auth/callback",
            ),
            workos_authkit_domain=values.get(
                "WORKOS_AUTHKIT_DOMAIN",
                "https://auth.workos.com",
            ),
            session_secret_key=values.get(
                "SESSION_SECRET_KEY",
                secrets.token_urlsafe(32),
            ),
            session_max_age_seconds=int(values.get("SESSION_MAX_AGE", "86400")),
            auth_bypass_local=_as_bool(values.get("AUTH_BYPASS_LOCAL")),
        )

    def target_schema_path(self, client_code: str) -> Path:
        """Return the canonical local target-schema path for a client."""
        return self.target_schemas_dir / client_code / "target_schema.json"

    def output_folder(self, identifier: object) -> Path:
        """Return the canonical local artifact folder for a spec or run."""
        return self.output_folders_dir / str(identifier)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings instance."""
    return Settings.from_env()
