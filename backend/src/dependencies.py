"""Application dependency construction.

This module is the composition boundary for concrete local-development
adapters.  Azure-backed implementations can replace these factories without
changing workflow or route code.
"""

from __future__ import annotations

from functools import lru_cache

from artifact_store import ArtifactStore, LocalArtifactStore
from config import get_settings
from file_ops import ObjectStore


@lru_cache(maxsize=1)
def get_object_store() -> ObjectStore:
    """Return the raw-object view of the configured artifact store."""
    return get_artifact_store()


@lru_cache(maxsize=1)
def get_artifact_store() -> ArtifactStore:
    """Return the configured generated-artifact store."""
    return LocalArtifactStore(get_settings().data_dir)
