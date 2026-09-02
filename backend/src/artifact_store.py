"""Durable storage boundary for every workflow artifact.

Raw uploads, target schemas, generated mapping/code/results, and execution logs
are addressed by durable client/spec/run keys. Infrastructure adapters may
change the physical backend without changing workflow or HTTP code.
"""

from __future__ import annotations

import tempfile
import uuid
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

from file_ops import AzureBlobObjectStore, LocalObjectStore, ObjectStore
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


class AzureArtifactStore(AzureBlobObjectStore, ArtifactStore):
    """Artifact adapter backed by Azure Blob Storage.

    Artifacts are stored durably in Blob; ``folder()``/``path()`` expose a
    per-spec local working directory for the code-generation and subprocess
    harness. Local writes are mirrored up to Blob lazily on the next
    ``read_artifact``/``list_artifacts`` call, and Blob is pulled down when a
    spec folder is materialised for the first time.
    """

    def __init__(
        self,
        account_url: str,
        credential: Any | None = None,
        connection_string: str | None = None,
        *,
        raw_files: str = "raw-files",
        target_schemas: str = "target-schemas",
        output_folders: str = "output-folders",
        execution_logs: str = "execution-logs",
    ) -> None:
        super().__init__(
            account_url=account_url,
            container_name=raw_files,
            credential=credential,
            connection_string=connection_string,
        )
        self._target_schema_store = AzureBlobObjectStore(
            account_url=account_url,
            container_name=target_schemas,
            credential=credential,
            connection_string=connection_string,
        )
        self._output_folder_store = AzureBlobObjectStore(
            account_url=account_url,
            container_name=output_folders,
            credential=credential,
            connection_string=connection_string,
        )
        self._log_store = AzureBlobObjectStore(
            account_url=account_url,
            container_name=execution_logs,
            credential=credential,
            connection_string=connection_string,
        )
        self._workdirs: dict[uuid.UUID, Path] = {}
        self._materialized: set[uuid.UUID] = set()

    @staticmethod
    def _safe_segment(value: str) -> str:
        if not value or Path(value).name != value:
            raise ValueError("Storage key segment must not contain a path")
        return value

    def _workdir(self, spec_id: uuid.UUID) -> Path:
        workdir = self._workdirs.get(spec_id)
        if workdir is None or not workdir.exists():
            workdir = Path(tempfile.mkdtemp(prefix=f"pop-{spec_id}-"))
            self._workdirs[spec_id] = workdir
            self._materialized.discard(spec_id)
        return workdir

    def _pull(self, spec_id: uuid.UUID) -> None:
        workdir = self._workdir(spec_id)
        prefix = f"{spec_id}/"
        for blob_name in self._output_folder_store.list(prefix=prefix):
            leaf = Path(blob_name).name
            (workdir / leaf).write_bytes(self._output_folder_store.get(blob_name))

    def _sync_up(self, spec_id: uuid.UUID) -> None:
        workdir = self._workdir(spec_id)
        for path in workdir.iterdir():
            if path.is_file():
                self._output_folder_store.put(
                    f"{spec_id}/{path.name}",
                    path.read_bytes(),
                )

    def write_target_schema(
        self,
        client_code: str,
        schema: TargetSchema,
    ) -> None:
        key = f"{self._safe_segment(client_code)}/target_schema.json"
        self._target_schema_store.put(
            key,
            schema.model_dump_json(indent=2).encode("utf-8"),
        )

    def read_target_schema(self, client_code: str) -> TargetSchema:
        key = f"{self._safe_segment(client_code)}/target_schema.json"
        return TargetSchema.model_validate_json(self._target_schema_store.get(key))

    def folder(self, spec_id: uuid.UUID) -> Path:
        workdir = self._workdir(spec_id)
        if spec_id not in self._materialized:
            self._pull(spec_id)
            self._materialized.add(spec_id)
        return workdir

    def path(self, spec_id: uuid.UUID, filename: str) -> Path:
        if Path(filename).name != filename:
            raise ValueError("Artifact filename must not contain a path")
        return self._workdir(spec_id) / filename

    def read_artifact(self, spec_id: uuid.UUID, filename: str) -> bytes:
        if Path(filename).name != filename:
            raise ValueError("Artifact filename must not contain a path")
        self._sync_up(spec_id)
        return self._output_folder_store.get(f"{spec_id}/{filename}")

    def write_artifact(
        self,
        spec_id: uuid.UUID,
        filename: str,
        data: bytes,
    ) -> None:
        if Path(filename).name != filename:
            raise ValueError("Artifact filename must not contain a path")
        workdir = self._workdir(spec_id)
        workdir.mkdir(parents=True, exist_ok=True)
        (workdir / filename).write_bytes(data)
        self._output_folder_store.put(f"{spec_id}/{filename}", data)

    def list_artifacts(
        self,
        spec_id: uuid.UUID,
        suffix: str | None = None,
    ) -> list[str]:
        self._sync_up(spec_id)
        prefix = f"{spec_id}/"
        names = []
        for blob_name in self._output_folder_store.list(prefix=prefix):
            leaf = Path(blob_name).name
            if suffix is None or Path(leaf).suffix == suffix:
                names.append(leaf)
        return sorted(names)

    def write_log(self, run_id: uuid.UUID, data: bytes) -> None:
        self._log_store.put(f"{run_id}.json", data)

    def read_log(self, run_id: uuid.UUID) -> bytes:
        return self._log_store.get(f"{run_id}.json")
