"""Tests for central application configuration."""

from __future__ import annotations

from pathlib import Path

import pytest
from config import DEFAULT_DATABASE_URL, Settings


def test_settings_derive_canonical_local_paths(tmp_path: Path) -> None:
    """All local paths should derive from one configured project root."""
    settings = Settings.from_env({}, project_root=tmp_path)

    assert settings.project_root == tmp_path.resolve()
    assert settings.object_store_dir == tmp_path / "data" / "object-store"
    assert settings.target_schemas_dir == tmp_path / "data" / "target-schemas"
    assert settings.output_folders_dir == tmp_path / "data" / "output-folders"
    assert settings.static_dir == tmp_path / "backend" / "static"
    assert settings.templates_dir == tmp_path / "backend" / "templates"
    assert settings.database_url == DEFAULT_DATABASE_URL


def test_settings_honour_environment_overrides(tmp_path: Path) -> None:
    """Explicit environment values should override local defaults."""
    custom_data = tmp_path / "custom-data"
    settings = Settings.from_env(
        {
            "PIPELINE_DATA_DIR": str(custom_data),
            "DATABASE_URL": "postgresql+psycopg://example/test",
            "OPENAI_API_KEY": "test-key",
            "MAPPING_MODEL": "test-model",
            "SESSION_SECRET_KEY": "test-secret",
            "SESSION_MAX_AGE": "120",
            "PIPELINE_EXECUTION_TIMEOUT_SECONDS": "45",
            "MAX_UPLOAD_SIZE_BYTES": "1024",
            "BOOTSTRAP_ADMIN_EMAILS": "Admin@example.com, second@example.com",
            "AUTH_BYPASS_LOCAL": "yes",
        },
        project_root=tmp_path,
    )

    assert settings.data_dir == custom_data
    assert settings.object_store_dir == custom_data / "object-store"
    assert settings.database_url == "postgresql+psycopg://example/test"
    assert settings.openai_api_key == "test-key"
    assert settings.mapping_model == "test-model"
    assert settings.session_secret_key == "test-secret"
    assert settings.session_max_age_seconds == 120
    assert settings.pipeline_execution_timeout_seconds == 45
    assert settings.max_upload_size_bytes == 1024
    assert settings.bootstrap_admin_emails == {
        "admin@example.com",
        "second@example.com",
    }
    assert settings.auth_bypass_local is True


@pytest.mark.parametrize(
    "env",
    [
        {"APP_ENVIRONMENT": "production"},
        {
            "APP_ENVIRONMENT": "production",
            "SESSION_SECRET_KEY": "test-secret",
            "AUTH_BYPASS_LOCAL": "true",
            "SESSION_COOKIE_SECURE": "true",
            "BOOTSTRAP_ADMIN_EMAILS": "admin@example.com",
        },
        {
            "APP_ENVIRONMENT": "production",
            "SESSION_SECRET_KEY": "test-secret",
            "SESSION_COOKIE_SECURE": "false",
            "BOOTSTRAP_ADMIN_EMAILS": "admin@example.com",
        },
    ],
)
def test_production_settings_reject_unsafe_auth_configuration(
    tmp_path: Path,
    env: dict[str, str],
) -> None:
    """Production startup fails closed for session and auth settings."""
    with pytest.raises(ValueError):
        Settings.from_env(env, project_root=tmp_path)


def test_production_settings_require_clerk_secret_key(tmp_path: Path) -> None:
    """Production cannot advertise invitations without Clerk delivery credentials."""
    env = {
        "APP_ENVIRONMENT": "production",
        "SESSION_SECRET_KEY": "test-secret",
        "SESSION_COOKIE_SECURE": "true",
        "BOOTSTRAP_ADMIN_EMAILS": "admin@example.com",
        "BYOK_ENCRYPTION_KEY": "test-key",
    }

    with pytest.raises(ValueError, match="CLERK_SECRET_KEY"):
        Settings.from_env(env, project_root=tmp_path)
