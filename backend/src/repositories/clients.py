"""Client and ingestion-batch persistence operations."""

from __future__ import annotations

import uuid
from typing import Any

from models import Client, IngestionBatch
from sqlmodel import Session, select


def create_client(
    session: Session,
    name: str,
    code: str,
    metadata: dict[str, Any] | None = None,
) -> Client:
    """Register a client in the caller-owned transaction."""
    client = Client(name=name, code=code, meta=metadata or {})
    session.add(client)
    session.flush()
    session.refresh(client)
    return client


def get_client_by_code(session: Session, code: str) -> Client | None:
    """Fetch a client by its short code."""
    return session.exec(select(Client).where(Client.code == code)).first()


def get_client_by_id(
    session: Session,
    client_id: uuid.UUID,
) -> Client | None:
    """Fetch a client by UUID."""
    return session.get(Client, client_id)


def create_ingestion_batch(
    session: Session,
    client_id: uuid.UUID,
    label: str | None,
    metadata: dict[str, Any] | None = None,
) -> IngestionBatch:
    """Create an ingestion batch in the caller-owned transaction."""
    batch = IngestionBatch(
        client_id=client_id,
        label=label,
        meta=metadata or {},
    )
    session.add(batch)
    session.flush()
    session.refresh(batch)
    return batch


def get_ingestion_batch(
    session: Session,
    batch_id: uuid.UUID,
) -> IngestionBatch | None:
    """Fetch an ingestion batch by UUID."""
    return session.get(IngestionBatch, batch_id)
