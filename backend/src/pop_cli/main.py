"""Command-line entry point for Pipeline of Pipelines."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import typer

from pop_cli.auth import DeviceAuthorization, clear_tokens, load_tokens, login
from pop_cli.config import CliConfig, load_config, save_config
from pop_cli.workflow import Api
from pop_cli.workflow import propose as propose_mapping
from pop_cli.workflow import push as push_mapping
from pop_cli.workflow import run as run_mapping
from pop_cli.workflow import upload as upload_files

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
    """Sign in through Clerk using device authorization."""

    def show_device_instructions(device: DeviceAuthorization) -> None:
        typer.echo(f"Open {device.verification_uri}")
        typer.echo(f"Enter code: {device.user_code}")

    try:
        login(load_config(), show_device_instructions)
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


@app.command()
def upload(
    paths: list[Path] = typer.Argument(..., help="Local source files"),
    client: str = typer.Option(..., help="Client UUID"),
    manifest: Path = typer.Option(Path("upload.json"), help="Manifest output path"),
) -> None:
    """Upload files to a new ingestion batch and save their IDs."""
    try:
        with Api() as api:
            result = upload_files(api, client, paths, manifest)
    except (RuntimeError, ValueError, OSError, httpx.HTTPError) as exc:
        _fail(exc)
    typer.echo(f"Uploaded {len(result['files'])} files; manifest: {manifest}")


@app.command()
def propose(
    client: str = typer.Option(..., help="Client UUID"),
    schema: Path = typer.Option(..., help="Target schema JSON"),
    manifest: Path = typer.Option(Path("upload.json"), help="Upload manifest"),
    output: Path = typer.Option(Path("mapping.json"), help="Mapping output path"),
    spec: str | None = typer.Option(None, help="Resume an existing spec UUID"),
) -> None:
    """Parse uploaded sources and propose a mapping for review."""
    try:
        with Api() as api:
            mapping = propose_mapping(
                api, client, schema, manifest, output, spec_id=spec
            )
    except (RuntimeError, ValueError, OSError, KeyError, httpx.HTTPError) as exc:
        _fail(exc)
    typer.echo(f"Proposed mapping {mapping['id']}; review {output}")


@app.command()
def push(
    mapping: Path = typer.Argument(..., help="Reviewed mapping JSON"),
    dry_run: bool = typer.Option(False, help="Show changes without applying them"),
) -> None:
    """Apply edits from a reviewed mapping to the server."""
    try:
        with Api() as api:
            changes = push_mapping(api, mapping, dry_run=dry_run)
    except (RuntimeError, ValueError, OSError, KeyError, httpx.HTTPError) as exc:
        _fail(exc)
    typer.echo(f"{'Would update' if dry_run else 'Updated'} {len(changes)} columns")


@app.command()
def run(
    mapping: Path = typer.Argument(..., help="Reviewed and pushed mapping JSON"),
    reviewer: str = typer.Option(..., help="Reviewer name or email"),
    output_dir: Path = typer.Option(Path("deliverables"), help="Local deliverables"),
) -> None:
    """Approve, execute, and download the generated deliverables."""
    try:
        with Api() as api:
            result = run_mapping(api, mapping, reviewer, output_dir)
    except (RuntimeError, ValueError, OSError, KeyError, httpx.HTTPError) as exc:
        _fail(exc)
    typer.echo(f"Execution {result['execution_run_id']}; files: {output_dir}")


if __name__ == "__main__":
    app()
