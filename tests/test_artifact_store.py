"""Tests for the GCS artifact adapter without external cloud credentials."""

from __future__ import annotations

import uuid

from artifact_store import GCSArtifactStore


class FakeBlob:
    """In-memory stand-in for one Cloud Storage blob."""

    def __init__(self, bucket: FakeBucket, name: str) -> None:
        self.bucket = bucket
        self.name = name

    def upload_from_string(self, data: bytes) -> None:
        self.bucket.objects[self.name] = data

    def download_as_bytes(self) -> bytes:
        return self.bucket.objects[self.name]

    def exists(self) -> bool:
        return self.name in self.bucket.objects

    def delete(self, if_generation_match: None = None) -> None:
        del if_generation_match
        self.bucket.objects.pop(self.name, None)


class FakeBucket:
    """In-memory stand-in for a Cloud Storage bucket."""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    def blob(self, name: str) -> FakeBlob:
        return FakeBlob(self, name)

    def list_blobs(self, prefix: str) -> list[FakeBlob]:
        return [
            FakeBlob(self, name) for name in self.objects if name.startswith(prefix)
        ]


class FakeStorageClient:
    """Minimal storage client returning one in-memory bucket."""

    def __init__(self, bucket: FakeBucket) -> None:
        self._bucket = bucket

    def bucket(self, _: str) -> FakeBucket:
        return self._bucket


def test_gcs_store_materializes_and_syncs_artifacts(
    tmp_path,
    monkeypatch,
) -> None:
    """Generated files move between GCS and the ephemeral work directory."""
    from google.cloud import storage

    bucket = FakeBucket()
    monkeypatch.setattr(storage, "Client", lambda: FakeStorageClient(bucket))
    store = GCSArtifactStore("test-bucket", tmp_path)
    spec_id = uuid.uuid4()

    store.put("raw-files/source.csv", b"id\n1\n")
    assert store.get("raw-files/source.csv") == b"id\n1\n"

    store.write_artifact(spec_id, "pipeline.py", b"print('first')")
    folder = store.folder(spec_id)
    assert (folder / "pipeline.py").read_bytes() == b"print('first')"

    (folder / "results.csv").write_bytes(b"id\n1\n")
    store.sync_folder(spec_id)
    assert store.read_artifact(spec_id, "results.csv") == b"id\n1\n"
    assert store.list_artifacts(spec_id) == ["pipeline.py", "results.csv"]
