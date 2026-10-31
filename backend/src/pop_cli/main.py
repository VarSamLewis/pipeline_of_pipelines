"""Command-line entry point for Pipeline of Pipelines."""

from __future__ import annotations

import json

import httpx
import typer

from pop_cli.auth import clear_tokens, load_tokens, login
from pop_cli.config import CliConfig, load_config, save_config

app = typer.Typer(help="Pipeline of Pipelines command-line client.")


def _fail(exc: Exception) -> None:
    typer.echo(f"Error: {exc}", err=True)
    raise typer.Exit(1) from exc


@app.command()
def init(
    api_url: str = typer.Option("http://127.0.0.1:8000", help="Pipeline API URL"),
) -> None:
    """Configure the CLI using its built-in public Clerk settings."""
    path = save_config(CliConfig(api_url=api_url))
    typer.echo(f"Configuration saved to {path}")


@app.command("login")
def login_command() -> None:
    """Sign in through Clerk in the default browser."""
    try:
        login(load_config())
    except RuntimeError as exc:
        _fail(exc)
    typer.echo("Logged in successfully")


@app.command()
def logout() -> None:
    """Remove locally stored CLI credentials."""
    try:
        clear_tokens(load_config())
    except RuntimeError as exc:
        _fail(exc)
    typer.echo("Logged out")


@app.command()
def whoami() -> None:
    """Show the user associated with the stored access token."""
    try:
        config = load_config()
        tokens = load_tokens(config)
        response = httpx.get(
            f"{config.api_url}/auth/me",
            headers={"Authorization": f"Bearer {tokens.access_token}"},
            timeout=15,
        )
        response.raise_for_status()
    except (RuntimeError, httpx.HTTPError) as exc:
        _fail(exc)
    typer.echo(json.dumps(response.json(), indent=2, sort_keys=True))


if __name__ == "__main__":
    app()
