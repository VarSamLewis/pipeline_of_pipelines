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
from typing import Any, BinaryIO, cast

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


_MIME_TYPES: dict[str, str] = {
    "csv": "text/csv",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "pdf": "application/pdf",
    "eml": "message/rfc822",
    "txt": "text/plain",
    "md": "text/markdown",
    "docx": ("application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
}


def mime_type_for(file_type: str, default: str = "application/octet-stream") -> str:
    """Return the canonical MIME type for a detected file type."""
    return _MIME_TYPES.get(file_type, default)


def load_target_schema(path: str | Path) -> TargetSchema:
    """Load a supplied target schema from a JSON file."""
    path = Path(path)
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    return TargetSchema.model_validate(data)


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


class AzureBlobObjectStore(ObjectStore):
    """Object-store implementation backed by an Azure Blob Storage container.

    Uses ``DefaultAzureCredential`` (e.g. a Container App user-assigned managed
    identity) by default; pass a connection string for local development.
    """

    def __init__(
        self,
        account_url: str,
        container_name: str = "raw-files",
        credential: Any | None = None,
        connection_string: str | None = None,
    ) -> None:
        from azure.identity import DefaultAzureCredential
        from azure.storage.blob import BlobServiceClient

        if connection_string:
            self._service = BlobServiceClient.from_connection_string(connection_string)
        else:
            self._service = BlobServiceClient(
                account_url=account_url,
                credential=credential or DefaultAzureCredential(),
            )
        self.container_name = container_name
        self._container_client = self._service.get_container_client(container_name)
        if not self._container_client.exists():
            self._container_client.create_container()

    def _client(self, key: str) -> Any:
        return self._container_client.get_blob_client(key)

    def put(self, key: str, data: bytes) -> str:
        self._client(key).upload_blob(data, overwrite=True)
        return key

    def get(self, key: str) -> bytes:
        blob = self._client(key)
        if not blob.exists():
            raise FileNotFoundError(key)
        return cast(bytes, blob.download_blob().readall())

    def exists(self, key: str) -> bool:
        return cast(bool, self._client(key).exists())

    def delete(self, key: str) -> None:
        blob = self._client(key)
        if blob.exists():
            blob.delete_blob()

    def open(self, key: str) -> Any:
        blob = self._client(key)
        if not blob.exists():
            raise FileNotFoundError(key)
        return blob.download_blob()

    def list(self, prefix: str | None = None) -> list[str]:
        """List blob names under the given prefix (defaults to all)."""
        return [
            blob_obj.name
            for blob_obj in self._container_client.list_blobs(name_starts_with=prefix)
        ]


def build_storage_key(
    client_code: str,
    ingestion_batch_id: str,
    original_filename: str,
    sha256: str,
) -> str:
    """Build a deterministic storage key for a raw file."""
    safe_name = original_filename.replace("/", "_")
    return f"{client_code}/{ingestion_batch_id}/{sha256[:16]}_{safe_name}"
