"""Tests for central application configuration."""

from __future__ import annotations

from pathlib import Path

from config import DEFAULT_DATABASE_URL, Settings


def test_settings_derive_canonical_local_paths(tmp_path: Path) -> None:
    """All local paths should derive from one configured project root."""
    settings = Settings.from_env({}, project_root=tmp_path)

    assert settings.project_root == tmp_path.resolve()
    assert settings.object_store_dir == tmp_path / "data" / "object-store"
    assert settings.target_schema_path("acme") == (
        tmp_path / "data" / "target-schemas" / "acme" / "target_schema.json"
    )
    assert settings.output_folder("spec-1") == (
        tmp_path / "data" / "output-folders" / "spec-1"
    )
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
    assert settings.auth_bypass_local is True
