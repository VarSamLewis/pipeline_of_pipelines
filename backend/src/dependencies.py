"""Application dependency construction.

This module is the composition boundary for concrete local-development
adapters.  Azure-backed implementations can replace these factories without
changing workflow or route code.
"""

from __future__ import annotations

from functools import lru_cache

from artifact_store import ArtifactStore, AzureArtifactStore, LocalArtifactStore
from config import get_settings
from file_ops import ObjectStore


def _build_artifact_store() -> ArtifactStore:
    """Choose the artifact store implementation from configuration.

    Local filesystem is the default (development/tests). When Azure Blob
    Storage is configured the store is backed by Blob using the default
    credential chain (managed identity in Container Apps), or a connection
    string for local development.
    """
    settings = get_settings()
    if (
        not settings.azure_storage_account_url
        and not settings.azure_storage_connection_string
    ):
        return LocalArtifactStore(settings.data_dir)

    kwargs: dict[str, str] = {}
    if settings.azure_storage_account_url:
        kwargs["account_url"] = settings.azure_storage_account_url
    if settings.azure_storage_connection_string:
        kwargs["connection_string"] = settings.azure_storage_connection_string
    return AzureArtifactStore(**kwargs)


@lru_cache(maxsize=1)
def get_object_store() -> ObjectStore:
    """Return the raw-object view of the configured artifact store."""
    return get_artifact_store()


@lru_cache(maxsize=1)
def get_artifact_store() -> ArtifactStore:
    """Return the configured generated-artifact store."""
    return _build_artifact_store()
