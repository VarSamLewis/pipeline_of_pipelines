"""Encryption and persistence helpers for organization BYOK credentials."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from config import get_settings
from cryptography.fernet import Fernet, InvalidToken
from models import OrganizationApiKey
from sqlmodel import Session, select


def _fernet() -> Fernet:
    key = get_settings().byok_encryption_key
    if not key:
        raise RuntimeError("BYOK_ENCRYPTION_KEY must be configured to save API keys")
    try:
        return Fernet(key.encode("utf-8"))
    except ValueError as exc:
        raise RuntimeError("BYOK_ENCRYPTION_KEY is not a valid Fernet key") from exc


def save_organization_api_key(
    session: Session,
    client_id: uuid.UUID,
    api_key: str,
) -> None:
    """Encrypt and upsert an organization API key without retaining plaintext."""
    api_key = api_key.strip()
    if not api_key:
        raise ValueError("API key is required")
    record = session.exec(
        select(OrganizationApiKey).where(OrganizationApiKey.client_id == client_id)
    ).first()
    if record is None:
        record = OrganizationApiKey(client_id=client_id, encrypted_key="")
    record.encrypted_key = _fernet().encrypt(api_key.encode("utf-8")).decode("utf-8")
    record.key_suffix = api_key[-4:]
    record.updated_at = datetime.now(UTC).replace(tzinfo=None)
    session.add(record)
    session.commit()


def get_organization_api_key(session: Session, client_id: uuid.UUID) -> str | None:
    """Decrypt an organization's key only for the outbound provider request."""
    record = session.exec(
        select(OrganizationApiKey).where(OrganizationApiKey.client_id == client_id)
    ).first()
    if record is None:
        return None
    try:
        return _fernet().decrypt(record.encrypted_key.encode("utf-8")).decode("utf-8")
    except InvalidToken as exc:
        raise RuntimeError("Saved organization API key cannot be decrypted") from exc


def get_organization_api_key_suffix(
    session: Session,
    client_id: uuid.UUID,
) -> str | None:
    """Return only the non-sensitive key suffix for settings UI display."""
    record = session.exec(
        select(OrganizationApiKey).where(OrganizationApiKey.client_id == client_id)
    ).first()
    return record.key_suffix if record else None
