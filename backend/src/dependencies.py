"""Application dependency construction.

This module is the composition boundary for concrete local-development
adapters.  Azure-backed implementations can replace these factories without
changing workflow or route code.
"""

from __future__ import annotations

from functools import lru_cache

from artifact_store import ArtifactStore, LocalArtifactStore
from config import get_settings
from file_ops import LocalObjectStore, ObjectStore


@lru_cache(maxsize=1)
def get_object_store() -> ObjectStore:
    """Return the configured process-wide object store."""
    settings = get_settings()
    return LocalObjectStore(str(settings.object_store_dir))


@lru_cache(maxsize=1)
def get_artifact_store() -> ArtifactStore:
    """Return the configured generated-artifact store."""
    return LocalArtifactStore(get_settings().output_folders_dir)
