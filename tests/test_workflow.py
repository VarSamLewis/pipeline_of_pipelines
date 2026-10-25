"""Workflow regression tests that require the PostgreSQL test database."""

from __future__ import annotations

import uuid

from conftest import requires_postgres
from db_ops import create_mapping_spec, get_session
from models import (
    BetaInvitation,
    Organization,
    OrganizationMembership,
    OrganizationRole,
    PlatformRole,
    TargetSchema,
)
from sqlmodel import select
from workflow import (
    get_or_create_client,
    get_organization_key_settings,
    invite_organization_reviewer,
    propose_mapping,
    revoke_organization_reviewer_invitation,
)

pytestmark = requires_postgres


def _mapping_spec() -> tuple[uuid.UUID, uuid.UUID]:
    """Create an isolated organization and draft mapping spec."""
    with get_session() as session:
        organization = Organization(name=f"Test organization {uuid.uuid4()}")
        session.add(organization)
        session.flush()
        client = get_or_create_client(
            session, None, f"Test client {uuid.uuid4()}", organization.id
        )
        spec = create_mapping_spec(
            session,
            client_id=client.id,
            source_raw_file_ids=[],
            target_schema=TargetSchema(name="test", tables=[]),
            description="test",
        )
        return organization.id, spec.id


def test_propose_mapping_uses_organization_key(
    monkeypatch,
) -> None:
    """Re-proposals use the saved organization key for retrieval and LLM calls."""
    import workflow

    organization_id, spec_id = _mapping_spec()
    captured: dict[str, str | None] = {}

    monkeypatch.setattr(
        workflow,
        "get_organization_api_key",
        lambda session, client_id: "organization-key"
        if client_id == organization_id
        else None,
    )
    monkeypatch.setattr(
        workflow,
        "propose_mapping_spec",
        lambda *args, **kwargs: captured.update(api_key=kwargs["api_key"]),
    )
    monkeypatch.setattr(workflow, "get_mapping_review", lambda spec_id: {"id": spec_id})

    propose_mapping(spec_id)

    assert captured["api_key"] == "organization-key"


def test_invite_organization_reviewer_is_idempotent(monkeypatch) -> None:
    """Re-inviting an email refreshes the single pending reviewer invitation."""
    organization_id, _ = _mapping_spec()
    import workflow

    sent_emails: list[str] = []
    monkeypatch.setattr(
        workflow, "send_clerk_invitation", lambda email: sent_emails.append(email)
    )

    invite_organization_reviewer(organization_id, " Reviewer@Example.com ")
    invite_organization_reviewer(organization_id, "reviewer@example.com")

    organization = next(
        item
        for item in get_organization_key_settings()
        if item["id"] == organization_id
    )
    assert [item["email"] for item in organization["pending_invitations"]] == [
        "reviewer@example.com"
    ]
    assert sent_emails == ["reviewer@example.com"]


def test_reviewer_invitation_is_not_persisted_when_clerk_delivery_fails(
    monkeypatch,
) -> None:
    """A Clerk delivery failure leaves no unusable pending local invitation."""
    import pytest
    import workflow

    organization_id, _ = _mapping_spec()
    monkeypatch.setattr(
        workflow,
        "send_clerk_invitation",
        lambda email: (_ for _ in ()).throw(RuntimeError("Clerk unavailable")),
    )

    with pytest.raises(RuntimeError, match="Clerk unavailable"):
        invite_organization_reviewer(organization_id, "reviewer@example.com")

    organization = next(
        item
        for item in get_organization_key_settings()
        if item["id"] == organization_id
    )
    assert organization["pending_invitations"] == []


def test_revoke_organization_reviewer_invitation(monkeypatch) -> None:
    """Revoked reviewer invitations are not shown as pending."""
    organization_id, _ = _mapping_spec()
    import workflow

    monkeypatch.setattr(workflow, "send_clerk_invitation", lambda email: None)
    invite_organization_reviewer(organization_id, "reviewer@example.com")
    organization = next(
        item
        for item in get_organization_key_settings()
        if item["id"] == organization_id
    )

    revoke_organization_reviewer_invitation(
        organization_id,
        organization["pending_invitations"][0]["id"],
    )

    organization = next(
        item
        for item in get_organization_key_settings()
        if item["id"] == organization_id
    )
    assert organization["pending_invitations"] == []


def test_clerk_authentication_accepts_reviewer_invitation(monkeypatch) -> None:
    """An invited colleague receives only the reviewer organization membership."""
    import auth_service

    organization_id, _ = _mapping_spec()
    import workflow

    monkeypatch.setattr(workflow, "send_clerk_invitation", lambda email: None)
    invite_organization_reviewer(organization_id, "reviewer@example.com")
    monkeypatch.setattr(
        auth_service,
        "_verified_claims",
        lambda token: {
            "sub": f"clerk-{uuid.uuid4()}",
            "email": "reviewer@example.com",
            "email_verified": True,
        },
    )

    user = auth_service.authenticate_clerk_session("test-token")

    assert user.platform_role == PlatformRole.USER
    with get_session() as session:
        invitation = session.exec(
            select(BetaInvitation).where(
                BetaInvitation.organization_id == organization_id,
                BetaInvitation.email == "reviewer@example.com",
            )
        ).one()
        membership = session.exec(
            select(OrganizationMembership).where(
                OrganizationMembership.user_id == user.id,
                OrganizationMembership.organization_id == organization_id,
            )
        ).one()
    assert invitation.status == "accepted"
    assert membership.role == OrganizationRole.MEMBER
