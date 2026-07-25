"""Artifact storage boundary for generated pipelines and execution results.

Application and HTTP code address artifacts by mapping-spec identifier.  The
local adapter deterministically maps that durable identifier to a directory;
future Blob Storage adapters can retain the same interface without leaking
filesystem paths into workflow code.
"""

from __future__ import annotations

import uuid
from abc import ABC, abstractmethod
from pathlib import Path


class ArtifactStore(ABC):
    """Store and retrieve generated artifacts by durable mapping-spec id."""

    @abstractmethod
    def folder(self, spec_id: uuid.UUID) -> Path:
        """Return the working directory used to generate a spec's artifacts."""

    @abstractmethod
    def path(self, spec_id: uuid.UUID, filename: str) -> Path:
        """Return a validated path for one artifact."""


class LocalArtifactStore(ArtifactStore):
    """Development artifact adapter backed by a deterministic local directory."""

    def __init__(self, base_path: str | Path) -> None:
        self.base_path = Path(base_path).resolve()

    def folder(self, spec_id: uuid.UUID) -> Path:
        return self.base_path / str(spec_id)

    def path(self, spec_id: uuid.UUID, filename: str) -> Path:
        if Path(filename).name != filename:
            raise ValueError("Artifact filename must not contain a path")
        return self.folder(spec_id) / filename
