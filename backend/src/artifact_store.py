"""Durable storage boundary for every workflow artifact.

Raw uploads, target schemas, generated mapping/code/results, and execution logs
are addressed by durable client/spec/run keys. Infrastructure adapters may
change the physical backend without changing workflow or HTTP code.
"""

from __future__ import annotations

import uuid
from abc import ABC, abstractmethod
from io import BytesIO
from pathlib import Path
from typing import BinaryIO

from file_ops import LocalObjectStore, ObjectStore
from models import TargetSchema


class ArtifactStore(ObjectStore, ABC):
    """Store all workflow artifacts behind durable, deterministic keys."""

    @abstractmethod
    def write_target_schema(
        self,
        client_id: str,
        schema: TargetSchema,
    ) -> None:
        """Persist a client's target schema."""

    @abstractmethod
    def read_target_schema(self, client_id: str) -> TargetSchema:
        """Retrieve a client's target schema."""

    @abstractmethod
    def folder(self, spec_id: uuid.UUID) -> Path:
        """Return the working directory used to generate a spec's artifacts."""

    @abstractmethod
    def path(self, spec_id: uuid.UUID, filename: str) -> Path:
        """Return a validated path for one artifact."""

    @abstractmethod
    def read_artifact(self, spec_id: uuid.UUID, filename: str) -> bytes:
        """Read a generated mapping, pipeline, or result artifact."""

    @abstractmethod
    def write_artifact(
        self,
        spec_id: uuid.UUID,
        filename: str,
        data: bytes,
    ) -> None:
        """Write a generated mapping, pipeline, or result artifact."""

    @abstractmethod
    def list_artifacts(
        self,
        spec_id: uuid.UUID,
        suffix: str | None = None,
    ) -> list[str]:
        """List artifact filenames for a mapping specification."""

    @abstractmethod
    def write_log(self, run_id: uuid.UUID, data: bytes) -> None:
        """Persist an execution log by durable run id."""

    @abstractmethod
    def read_log(self, run_id: uuid.UUID) -> bytes:
        """Retrieve an execution log by durable run id."""

    @abstractmethod
    def sync_folder(self, spec_id: uuid.UUID) -> None:
        """Persist generated files from the local working directory."""


class LocalArtifactStore(LocalObjectStore, ArtifactStore):
    """Development artifact adapter backed by a deterministic local directory."""

    def __init__(self, data_dir: str | Path) -> None:
        self.data_dir = Path(data_dir).resolve()
        self.output_folders_dir = self.data_dir / "output-folders"
        self.target_schemas_dir = self.data_dir / "target-schemas"
        self.logs_dir = self.data_dir / "execution-logs"
        super().__init__(self.data_dir / "object-store")

    @staticmethod
    def _safe_segment(value: str) -> str:
        if not value or Path(value).name != value:
            raise ValueError("Storage key segment must not contain a path")
        return value

    def write_target_schema(
        self,
        client_id: str,
        schema: TargetSchema,
    ) -> None:
        path = (
            self.target_schemas_dir
            / self._safe_segment(client_id)
            / "target_schema.json"
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(schema.model_dump_json(indent=2), encoding="utf-8")

    def read_target_schema(self, client_id: str) -> TargetSchema:
        path = (
            self.target_schemas_dir
            / self._safe_segment(client_id)
            / "target_schema.json"
        )
        return TargetSchema.model_validate_json(path.read_text(encoding="utf-8"))

    def folder(self, spec_id: uuid.UUID) -> Path:
        return self.output_folders_dir / str(spec_id)

    def path(self, spec_id: uuid.UUID, filename: str) -> Path:
        if Path(filename).name != filename:
            raise ValueError("Artifact filename must not contain a path")
        return self.folder(spec_id) / filename

    def read_artifact(self, spec_id: uuid.UUID, filename: str) -> bytes:
        return self.path(spec_id, filename).read_bytes()

    def write_artifact(
        self,
        spec_id: uuid.UUID,
        filename: str,
        data: bytes,
    ) -> None:
        path = self.path(spec_id, filename)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    def list_artifacts(
        self,
        spec_id: uuid.UUID,
        suffix: str | None = None,
    ) -> list[str]:
        folder = self.folder(spec_id)
        if not folder.exists():
            return []
        return sorted(
            path.name
            for path in folder.iterdir()
            if path.is_file() and (suffix is None or path.suffix == suffix)
        )

    def write_log(self, run_id: uuid.UUID, data: bytes) -> None:
        path = self.logs_dir / f"{run_id}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    def read_log(self, run_id: uuid.UUID) -> bytes:
        return (self.logs_dir / f"{run_id}.json").read_bytes()

    def sync_folder(self, spec_id: uuid.UUID) -> None:
        """Local artifacts are already durable."""


class GCSArtifactStore(ArtifactStore):
    """GCS-backed durable artifacts with an ephemeral local work directory."""

    def __init__(self, bucket_name: str, work_dir: str | Path) -> None:
        from google.cloud import storage  # type: ignore[import-untyped]

        self.bucket = storage.Client().bucket(bucket_name)
        self.work_dir = Path(work_dir).resolve()

    @staticmethod
    def _safe_segment(value: str) -> str:
        if not value or Path(value).name != value:
            raise ValueError("Storage key segment must not contain a path")
        return value

    @staticmethod
    def _artifact_key(spec_id: uuid.UUID, filename: str) -> str:
        if Path(filename).name != filename:
            raise ValueError("Artifact filename must not contain a path")
        return f"mappings/{spec_id}/artifacts/{filename}"

    @staticmethod
    def _legacy_artifact_key(spec_id: uuid.UUID, filename: str) -> str:
        return f"output-folders/{spec_id}/{filename}"

    def put(self, key: str, data: bytes) -> str:
        self.bucket.blob(key).upload_from_string(data)
        return key

    def get(self, key: str) -> bytes:
        return bytes(self.bucket.blob(key).download_as_bytes())

    def exists(self, key: str) -> bool:
        return bool(self.bucket.blob(key).exists())

    def delete(self, key: str) -> None:
        self.bucket.blob(key).delete(if_generation_match=None)

    def open(self, key: str) -> BinaryIO:
        return BytesIO(self.get(key))

    def write_target_schema(self, client_id: str, schema: TargetSchema) -> None:
        key = f"clients/{self._safe_segment(client_id)}/schemas/current/schema.json"
        self.put(key, schema.model_dump_json(indent=2).encode())

    def read_target_schema(self, client_id: str) -> TargetSchema:
        key = f"clients/{self._safe_segment(client_id)}/schemas/current/schema.json"
        if not self.exists(key):
            key = f"target-schemas/{self._safe_segment(client_id)}/target_schema.json"
        return TargetSchema.model_validate_json(self.get(key))

    def folder(self, spec_id: uuid.UUID) -> Path:
        folder = self.work_dir / "mappings" / str(spec_id)
        folder.mkdir(parents=True, exist_ok=True)
        prefix = f"mappings/{spec_id}/artifacts/"
        if not list(self.bucket.list_blobs(prefix=prefix)):
            prefix = f"output-folders/{spec_id}/"
        for blob in self.bucket.list_blobs(prefix=prefix):
            name = blob.name.removeprefix(prefix)
            if Path(name).name != name:
                continue
            (folder / name).write_bytes(bytes(blob.download_as_bytes()))
        return folder

    def path(self, spec_id: uuid.UUID, filename: str) -> Path:
        self._artifact_key(spec_id, filename)
        return self.folder(spec_id) / filename

    def read_artifact(self, spec_id: uuid.UUID, filename: str) -> bytes:
        key = self._artifact_key(spec_id, filename)
        if not self.exists(key):
            key = self._legacy_artifact_key(spec_id, filename)
        return self.get(key)

    def write_artifact(self, spec_id: uuid.UUID, filename: str, data: bytes) -> None:
        self.put(self._artifact_key(spec_id, filename), data)

    def list_artifacts(
        self,
        spec_id: uuid.UUID,
        suffix: str | None = None,
    ) -> list[str]:
        prefix = f"mappings/{spec_id}/artifacts/"
        if not list(self.bucket.list_blobs(prefix=prefix)):
            prefix = f"output-folders/{spec_id}/"
        return sorted(
            name
            for blob in self.bucket.list_blobs(prefix=prefix)
            if (name := blob.name.removeprefix(prefix))
            and Path(name).name == name
            and (suffix is None or name.endswith(suffix))
        )

    def write_log(self, run_id: uuid.UUID, data: bytes) -> None:
        self.put(f"runs/{run_id}/logs/execution.json", data)

    def read_log(self, run_id: uuid.UUID) -> bytes:
        key = f"runs/{run_id}/logs/execution.json"
        if not self.exists(key):
            key = f"execution-logs/{run_id}.json"
        return self.get(key)

    def sync_folder(self, spec_id: uuid.UUID) -> None:
        folder = self.work_dir / "mappings" / str(spec_id)
        if not folder.exists():
            return
        for path in folder.iterdir():
            if path.is_file():
                self.write_artifact(spec_id, path.name, path.read_bytes())
