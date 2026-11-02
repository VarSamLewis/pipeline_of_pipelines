"""HTTP client for the explicit upload, review, and execution workflow."""

from __future__ import annotations

import hashlib
import json
import mimetypes
from pathlib import Path
from typing import Any, cast
from urllib.parse import quote
from uuid import UUID

import httpx

from pop_cli.auth import load_tokens
from pop_cli.config import load_config

EDITABLE_FIELDS = (
    "transformation_logic",
    "polars_expression",
    "source_columns",
    "tests",
)


def identifier(value: str) -> str:
    """Reject invalid identifiers before building API paths."""
    return str(UUID(value))


def document(path: Path) -> dict[str, Any]:
    """Read a JSON object from a local mapping or manifest."""
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"Expected a JSON object in {path}")
    return value


def write_document(path: Path, value: dict[str, Any]) -> None:
    """Write a local JSON document without leaving partial content."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def _contains_values(actual: Any, expected: Any) -> bool:
    """Compare user-supplied schema fields, ignoring server-filled defaults."""
    if isinstance(expected, dict):
        return isinstance(actual, dict) and all(
            key in actual and _contains_values(actual[key], value)
            for key, value in expected.items()
        )
    if isinstance(expected, list):
        return (
            isinstance(actual, list)
            and len(actual) == len(expected)
            and all(
                _contains_values(left, right)
                for left, right in zip(actual, expected, strict=True)
            )
        )
    return bool(actual == expected)


class Api:
    """Authenticated client for the deployed Pipeline of Pipelines API."""

    def __init__(self) -> None:
        config = load_config()
        token = load_tokens(config).access_token
        self.client = httpx.Client(
            base_url=config.api_url,
            headers={"Authorization": f"Bearer {token}"},
            timeout=300,
        )

    def __enter__(self) -> Api:
        return self

    def __exit__(self, *_args: object) -> None:
        self.client.close()

    def request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        """Send one request and report API errors without exposing credentials."""
        response = self.client.request(method, path, **kwargs)
        if response.is_error:
            try:
                detail = response.json().get("detail", response.reason_phrase)
            except (ValueError, AttributeError):
                detail = response.reason_phrase
            raise RuntimeError(f"API returned {response.status_code}: {detail}")
        return response

    def json(self, method: str, path: str, **kwargs: Any) -> Any:
        return self.request(method, path, **kwargs).json()


def upload(
    api: Api, client_id: str, paths: list[Path], manifest_path: Path
) -> dict[str, Any]:
    """Create a batch, upload local files, and preserve progress on partial failure."""
    client_id = identifier(client_id)
    if not paths:
        raise ValueError("At least one source file is required")
    if manifest_path.exists():
        raise ValueError(f"Manifest already exists: {manifest_path}")
    for path in paths:
        if not path.is_file():
            raise ValueError(f"Source file is not a readable regular file: {path}")
    batch = api.json(
        "POST", f"/clients/{client_id}/batches", json={"label": "CLI upload"}
    )
    manifest: dict[str, Any] = {
        "version": 1,
        "client_id": client_id,
        "batch_id": batch["id"],
        "files": [],
    }
    write_document(manifest_path, manifest)
    for path in paths:
        digest = hashlib.sha256()
        with path.open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
        with path.open("rb") as source:
            item = api.json(
                "POST",
                f"/clients/{client_id}/batches/{batch['id']}/files",
                files={"file": (path.name, source, mimetypes.guess_type(path.name)[0])},
            )
        manifest["files"].append(
            {
                "id": item["id"],
                "path": str(path),
                "sha256": digest.hexdigest(),
                "uploaded_at": item.get("uploaded_at"),
            }
        )
        write_document(manifest_path, manifest)
    return manifest


def propose(
    api: Api,
    client_id: str,
    schema_path: Path,
    manifest_path: Path,
    output: Path,
    *,
    spec_id: str | None = None,
) -> dict[str, Any]:
    """Create or resume a spec, parse sources, propose, and save for review."""
    client_id = identifier(client_id)
    manifest = document(manifest_path)
    if manifest.get("client_id") != client_id:
        raise ValueError("Upload manifest belongs to another client")
    raw_ids = [identifier(item["id"]) for item in manifest.get("files", [])]
    if not raw_ids:
        raise ValueError("Upload manifest contains no files")
    schema = document(schema_path)
    inferred_spec = False
    if output.exists():
        local_spec_id = identifier(document(output)["id"])
        if spec_id is not None and identifier(spec_id) != local_spec_id:
            raise ValueError("Existing mapping output belongs to a different spec")
        if spec_id is None:
            spec_id = local_spec_id
            inferred_spec = True
    if spec_id is not None:
        spec_id = identifier(spec_id)
        existing = api.json("GET", f"/mapping-specs/{spec_id}")
        if (
            existing["client_id"] != client_id
            or set(existing["source_raw_file_ids"]) != set(raw_ids)
            or not _contains_values(existing["target_schema_json"], schema)
        ):
            raise ValueError("Existing spec does not match client, manifest, or schema")
        if existing["status"] not in ("draft", "proposed"):
            if inferred_spec:
                # Keep the finalized server-side mapping intact; start a new review.
                spec_id = None
            else:
                raise ValueError("Only draft or proposed specs can be retried")
    if spec_id is None:
        spec = api.json(
            "POST",
            f"/clients/{client_id}/mapping-specs",
            json={"source_raw_file_ids": raw_ids, "target_schema": schema},
        )
        spec_id = identifier(spec["id"])
    try:
        for raw_id in raw_ids:
            if (
                spec_id
                and api.json("GET", f"/raw-files/{raw_id}")["status"] == "parsed"
            ):
                continue
            api.json("POST", f"/raw-files/{raw_id}/parse")
        api.json("POST", f"/mapping-specs/{spec_id}/propose")
        mapping = api.json("GET", f"/mapping-specs/{spec_id}")
    except (RuntimeError, httpx.HTTPError) as exc:
        raise RuntimeError(
            f"Proposal for spec {spec_id} failed: {exc}. "
            f"Retry with the same arguments and --spec {spec_id}"
        ) from exc
    write_document(output, mapping)
    return cast(dict[str, Any], mapping)


def push(api: Api, path: Path, *, dry_run: bool = False) -> list[str]:
    """Apply only supported changed fields after comparing with the server."""
    local = document(path)
    spec_id = identifier(local["id"])
    remote = api.json("GET", f"/mapping-specs/{spec_id}")
    if remote["status"] != "proposed":
        raise ValueError("Only proposed mappings may be edited")
    remote_columns = {identifier(col["id"]): col for col in remote["columns"]}
    local_columns = local.get("columns", [])
    if {identifier(col["id"]) for col in local_columns} != set(remote_columns):
        raise ValueError("Mapping columns differ from the server; fetch a new proposal")
    changes: list[tuple[str, dict[str, Any]]] = []
    for column in local_columns:
        column_id = identifier(column["id"])
        current = remote_columns[column_id]
        if (column["target_table"], column["target_column"]) != (
            current["target_table"],
            current["target_column"],
        ):
            raise ValueError("Mapping target fields may not be changed")
        patch = {
            field: column[field]
            for field in EDITABLE_FIELDS
            if field in column and column[field] != current[field]
        }
        if patch:
            changes.append((column_id, patch))
    if not dry_run:
        for column_id, patch in changes:
            api.json(
                "PATCH", f"/mapping-specs/{spec_id}/columns/{column_id}", json=patch
            )
    return [column_id for column_id, _ in changes]


def run(api: Api, path: Path, reviewer: str, output_dir: Path) -> dict[str, Any]:
    """Approve an explicitly reviewed mapping, execute it, and download artifacts."""
    local = document(path)
    spec_id = identifier(local["id"])
    remote = api.json("GET", f"/mapping-specs/{spec_id}")
    if remote["status"] != "proposed":
        raise ValueError("Mapping must be proposed and reviewed before running")
    if output_dir.exists() and any(output_dir.iterdir()):
        raise ValueError(f"Output directory is not empty: {output_dir}")
    if not reviewer.strip():
        raise ValueError("Reviewer is required")
    pending = push(api, path, dry_run=True)
    if pending:
        raise ValueError("Local edits have not been pushed; run 'pop push' first")
    api.json("POST", f"/mapping-specs/{spec_id}/approve", json={"reviewer": reviewer})
    result = api.json("POST", f"/mapping-specs/{spec_id}/execute", json={})
    csv_paths = result.get("csv_paths", {})
    if not csv_paths:
        raise RuntimeError("Execution produced no target-table CSVs")
    downloads = {
        name: f"/output-folders/{spec_id}/{name}"
        for name in ("pipeline.py", "mapping.json")
    }
    if len(csv_paths) == 1:
        downloads["results.csv"] = f"/output-folders/{spec_id}/results.csv"
    else:
        for table_name in csv_paths:
            if Path(table_name).name != table_name or table_name in {".", ".."}:
                raise RuntimeError("Execution returned an unsafe target-table name")
            filename = f"{table_name}.csv"
            downloads[filename] = (
                f"/output-folders/{spec_id}/tables/{quote(table_name, safe='')}.csv"
            )
    contents = {
        name: api.request("GET", url).content for name, url in downloads.items()
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    for filename, content in contents.items():
        (output_dir / filename).write_bytes(content)
    return cast(dict[str, Any], result)
