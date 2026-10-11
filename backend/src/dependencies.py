"""Application dependency construction.

This module is the composition boundary for concrete local-development
adapters. GCP-backed implementations can replace these factories without
changing workflow or route code.
"""

from __future__ import annotations

from functools import lru_cache

from artifact_store import ArtifactStore, GCSArtifactStore, LocalArtifactStore
from config import get_settings
from file_ops import ObjectStore


@lru_cache(maxsize=1)
def get_object_store() -> ObjectStore:
    """Return the raw-object view of the configured artifact store."""
    return get_artifact_store()


@lru_cache(maxsize=1)
def get_artifact_store() -> ArtifactStore:
    """Return the configured generated-artifact store."""
    settings = get_settings()
    if settings.gcs_bucket:
        return GCSArtifactStore(settings.gcs_bucket, settings.work_dir)
    return LocalArtifactStore(settings.data_dir)
