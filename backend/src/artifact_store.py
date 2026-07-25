"""Durable storage boundary for every workflow artifact.

Raw uploads, target schemas, generated mapping/code/results, and execution logs
are addressed by durable client/spec/run keys. Infrastructure adapters may
change the physical backend without changing workflow or HTTP code.
"""

from __future__ import annotations

import uuid
from abc import ABC, abstractmethod
from pathlib import Path

from file_ops import LocalObjectStore, ObjectStore
from models import TargetSchema


class ArtifactStore(ObjectStore, ABC):
    """Store all workflow artifacts behind durable, deterministic keys."""

    @abstractmethod
    def write_target_schema(
        self,
        client_code: str,
        schema: TargetSchema,
    ) -> None:
        """Persist a client's target schema."""

    @abstractmethod
    def read_target_schema(self, client_code: str) -> TargetSchema:
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
        client_code: str,
        schema: TargetSchema,
    ) -> None:
        path = (
            self.target_schemas_dir
            / self._safe_segment(client_code)
            / "target_schema.json"
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(schema.model_dump_json(indent=2), encoding="utf-8")

    def read_target_schema(self, client_code: str) -> TargetSchema:
        path = (
            self.target_schemas_dir
            / self._safe_segment(client_code)
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
