"""Local, non-secret configuration for the ``pop`` command-line client."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from pop_cli.defaults import (
    DEFAULT_CLERK_ISSUER,
    DEFAULT_CLERK_OAUTH_CLIENT_ID,
)


def config_path() -> Path:
    """Return the per-user CLI configuration path."""
    config_home = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return config_home / "pipeline-of-pipelines" / "config.toml"


@dataclass(frozen=True, slots=True)
class CliConfig:
    """Configuration required to connect a CLI profile to the API."""

    api_url: str
    clerk_oauth_client_id: str = DEFAULT_CLERK_OAUTH_CLIENT_ID
    clerk_issuer: str = DEFAULT_CLERK_ISSUER


def load_config() -> CliConfig:
    """Load CLI configuration or explain how to create it."""
    path = config_path()
    if not path.is_file():
        raise RuntimeError("CLI is not configured; run 'pop init' first")

    import tomllib

    values = tomllib.loads(path.read_text(encoding="utf-8"))
    profile = values.get("default")
    if not isinstance(profile, dict) or not isinstance(profile.get("api_url"), str):
        raise RuntimeError(f"Invalid CLI configuration in {path}")
    return CliConfig(
        api_url=profile["api_url"].rstrip("/"),
        clerk_oauth_client_id=str(
            profile.get("clerk_oauth_client_id", DEFAULT_CLERK_OAUTH_CLIENT_ID)
        ),
        clerk_issuer=str(profile.get("clerk_issuer", DEFAULT_CLERK_ISSUER)).rstrip(
            "/"
        ),
    )


def save_config(config: CliConfig) -> Path:
    """Persist non-secret CLI configuration with owner-only permissions."""
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "[default]\n"
        f"api_url = {json.dumps(config.api_url.rstrip('/'))}\n"
        f"clerk_oauth_client_id = {json.dumps(config.clerk_oauth_client_id)}\n"
        f"clerk_issuer = {json.dumps(config.clerk_issuer.rstrip('/'))}\n",
        encoding="utf-8",
    )
    path.chmod(0o600)
    return path
