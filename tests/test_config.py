"""Configuration keeps the candidate worker optional in production."""

from __future__ import annotations

import pytest
from config import Settings  # noqa: E402


def _production_env(**overrides: str) -> dict[str, str]:
    """A minimal valid production environment."""
    env = {
        "APP_ENVIRONMENT": "production",
        "SESSION_SECRET_KEY": "production-session-secret",
        "SESSION_COOKIE_SECURE": "true",
        "BOOTSTRAP_ADMIN_EMAILS": "admin@example.com",
        "BYOK_ENCRYPTION_KEY": "production-byok-key",
        "CLERK_SECRET_KEY": "sk_test_example",
    }
    env.update(overrides)
    return env


def test_production_allows_unconfigured_candidate_worker(tmp_path) -> None:
    """The worker is opt-in: production boots without it wired."""
    settings = Settings.from_env(_production_env(), project_root=tmp_path)
    assert settings.candidate_worker_url == ""


def test_production_rejects_non_https_candidate_worker(tmp_path) -> None:
    with pytest.raises(ValueError, match="HTTPS"):
        Settings.from_env(
            _production_env(CANDIDATE_WORKER_URL="http://worker.internal/execute"),
            project_root=tmp_path,
        )


def test_production_accepts_https_candidate_worker(tmp_path) -> None:
    settings = Settings.from_env(
        _production_env(CANDIDATE_WORKER_URL="https://worker.example.run.app"),
        project_root=tmp_path,
    )
    assert settings.candidate_worker_url == "https://worker.example.run.app"
