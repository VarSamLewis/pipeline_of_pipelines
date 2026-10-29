"""Tests for database-operation batching behaviour."""

from __future__ import annotations

import uuid
from typing import Any

import db_ops
from models import RawFile


class _Session:
    def __init__(self) -> None:
        self.added: list[Any] = []
        self.commit_count = 0
        self.refreshed: list[Any] = []

    def add(self, record: Any) -> None:
        self.added.append(record)

    def commit(self) -> None:
        self.commit_count += 1

    def refresh(self, record: Any) -> None:
        self.refreshed.append(record)


def test_parse_raw_file_batches_evidence_commits(monkeypatch) -> None:
    """A file with many evidence chunks requires one database commit."""
    session = _Session()
    raw_file = RawFile(
        id=uuid.uuid4(),
        client_id=uuid.uuid4(),
        ingestion_batch_id=uuid.uuid4(),
        original_filename="notes.txt",
        storage_key="raw/notes.txt",
        sha256="hash",
        mime_type="text/plain",
    )
    monkeypatch.setattr(
        db_ops,
        "extract_evidence_chunks",
        lambda parsed, raw_file_id: [
            {"content": "first", "evidence_type": "text"},
            {"content": "second", "evidence_type": "text"},
        ],
    )
    monkeypatch.setattr(db_ops, "get_embedding", lambda *args, **kwargs: None)

    db_ops._parse_raw_file(session, raw_file, b"notes", "txt")

    assert len(session.added) == 2
    assert session.commit_count == 1
