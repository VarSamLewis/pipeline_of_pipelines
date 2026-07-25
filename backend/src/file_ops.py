"""File operations for immutable raw-file ingestion.

This module provides:
- File type detection from filenames and magic bytes.
- An abstract object-store interface plus a local-filesystem implementation.
- Helpers to read raw file bytes and persist parsed artifacts locally.
- Target-schema loading from a JSON file.
- Folder ingestion so a client can drop a mixed bag of files at once.

All writes are additive; no raw file is ever mutated in place.
"""

from __future__ import annotations

import hashlib
import json
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, BinaryIO

from models import TargetSchema


def compute_sha256(file_bytes: bytes) -> str:
    """Return the full SHA-256 hex digest of the given bytes."""
    return hashlib.sha256(file_bytes).hexdigest()


def detect_file_type(filename: str, file_bytes: bytes | None = None) -> str:
    """Detect the file type from extension and optional magic bytes."""
    name = filename.lower()
    if name.endswith(".csv"):
        return "csv"
    if name.endswith((".xlsx", ".xls")):
        return "xlsx"
    if name.endswith(".pdf"):
        return "pdf"
    if name.endswith((".eml", ".msg")):
        return "eml"
    if name.endswith(".txt"):
        return "txt"
    if name.endswith(".md"):
        return "md"
    if name.endswith(".docx"):
        return "docx"
    if file_bytes is not None:
        if file_bytes.startswith(b"PK"):
            return "xlsx"
        if file_bytes.startswith(b"%PDF"):
            return "pdf"
    return "unknown"


def load_target_schema(path: str | Path) -> TargetSchema:
    """Load a supplied target schema from a JSON file."""
    path = Path(path)
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    return TargetSchema.model_validate(data)


def save_target_schema(schema: TargetSchema, path: str | Path) -> Path:
    """Persist a target schema to a JSON file."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(schema.model_dump(mode="json"), f, indent=2)
    return path


def discover_client_files(folder_path: str | Path) -> list[Path]:
    """Discover all ingestible files in a folder, excluding target schema JSON."""
    folder = Path(folder_path)
    files = [p for p in folder.iterdir() if p.is_file() and p.suffix.lower() != ".json"]
    return sorted(files)


def find_target_schema_file(folder_path: str | Path) -> Path | None:
    """Locate a target-schema JSON file inside a client folder."""
    folder = Path(folder_path)
    for name in ("target_schema.json", "target-schema.json", "schema.json"):
        candidate = folder / name
        if candidate.exists():
            return candidate
    json_files = sorted(folder.glob("*.json"))
    return json_files[0] if json_files else None


def list_files(folder_path: str | Path) -> list[str]:
    """Return a list of file paths under the given folder."""
    return sorted(str(p) for p in Path(folder_path).rglob("*") if p.is_file())


def read_file_bytes(file_path: str | Path) -> bytes:
    """Read an entire file into memory as bytes."""
    return Path(file_path).read_bytes()


def read_files(folder_path: str | Path) -> list[tuple[str, bytes]]:
    """Read every file in a folder and return filename/byte pairs."""
    result: list[tuple[str, bytes]] = []
    for path in sorted(Path(folder_path).iterdir()):
        if path.is_file():
            result.append((path.name, path.read_bytes()))
    return result


def write_parsed_dict(
    parsed_content: dict[str, Any],
    folder_path: str | Path,
    filename: str,
) -> Path:
    """Write a parsed dictionary to the object store as JSON."""
    folder = Path(folder_path)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / filename
    with path.open("w", encoding="utf-8") as f:
        json.dump(parsed_content, f, indent=2)
    return path


class ObjectStore(ABC):
    """Abstract object store for raw and parsed file storage."""

    @abstractmethod
    def put(self, key: str, data: bytes) -> str:
        """Store an object under the given key."""

    @abstractmethod
    def get(self, key: str) -> bytes:
        """Retrieve an object by key."""

    @abstractmethod
    def exists(self, key: str) -> bool:
        """Return True if the object exists, otherwise False."""

    @abstractmethod
    def delete(self, key: str) -> None:
        """Remove an object from the store."""

    @abstractmethod
    def open(self, key: str) -> BinaryIO:
        """Open an object as a binary stream."""


class LocalObjectStore(ObjectStore):
    """Object-store implementation backed by the local filesystem."""

    def __init__(self, base_path: str | Path) -> None:
        self.base_path = Path(base_path).resolve()
        self.base_path.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        return self.base_path / key

    def put(self, key: str, data: bytes) -> str:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return key

    def get(self, key: str) -> bytes:
        return self._path(key).read_bytes()

    def exists(self, key: str) -> bool:
        return self._path(key).exists()

    def delete(self, key: str) -> None:
        path = self._path(key)
        if path.exists():
            path.unlink()

    def open(self, key: str) -> BinaryIO:
        return self._path(key).open("rb")


def build_storage_key(
    client_code: str,
    ingestion_batch_id: str,
    original_filename: str,
    sha256: str,
) -> str:
    """Build a deterministic storage key for a raw file."""
    safe_name = original_filename.replace("/", "_")
    return f"{client_code}/{ingestion_batch_id}/{sha256[:16]}_{safe_name}"


def write_audit_log(
    event_type: str,
    entity_type: str,
    entity_id: str,
    actor: str | None,
    payload: dict[str, Any],
) -> None:
    """Write a structured audit event to durable storage.

    Currently logs to stdout; production should persist to the audit log table.
    """
    print(
        {
            "event_type": event_type,
            "entity_type": entity_type,
            "entity_id": entity_id,
            "actor": actor,
            "payload": payload,
        }
    )
