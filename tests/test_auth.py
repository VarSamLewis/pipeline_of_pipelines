"""Unit tests for Clerk-backed authorization dependencies."""

from __future__ import annotations

import os
import uuid

import pytest

# Ensure local auth bypass is active for these unit tests before importing the
# auth module, which evaluates AUTH_BYPASS_LOCAL at import time.
os.environ["AUTH_BYPASS_LOCAL"] = "true"

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend" / "src"))

from auth_service import get_current_user, require_client_membership, require_role
from fastapi import HTTPException
from models import UserRole


class FakeRequest:
    """Minimal stand-in for a Starlette Request with a session dict."""

    def __init__(self, session: dict[str, str] | None = None) -> None:
        self.session = session or {}
        self.headers: dict[str, str] = {}


class FakeMembershipResult:
    """Minimal SQLModel result used by membership authorization tests."""

    def __init__(self, membership: object | None) -> None:
        self.membership = membership

    def first(self) -> object | None:
        return self.membership


class FakeMembershipSession:
    """Minimal session that returns the configured active membership result."""

    def __init__(self, membership: object | None) -> None:
        self.membership = membership

    def exec(self, _: object) -> FakeMembershipResult:
        return FakeMembershipResult(self.membership)


def test_local_bypass_returns_admin_user() -> None:
    """In bypass mode the current user is always the synthetic admin."""
    request = FakeRequest()
    user = get_current_user(request)
    assert user is not None
    assert user.role == UserRole.ADMIN
    assert user.email == "local-dev@example.com"


def test_require_role_allows_equal_or_higher_role() -> None:
    """require_role permits users whose role meets or exceeds the requirement."""
    from models import User

    admin_user = User(
        id=uuid.uuid4(),
        external_user_id="admin-1",
        email="admin@example.com",
        role=UserRole.ADMIN,
    )
    checker = require_role("reviewer")
    assert checker(admin_user) == admin_user


def test_require_role_rejects_insufficient_role() -> None:
    """require_role rejects users below the required role level."""
    from models import User

    creator_user = User(
        id=uuid.uuid4(),
        external_user_id="creator-1",
        email="creator@example.com",
        role=UserRole.CREATOR,
    )
    checker = require_role("approver")
    with pytest.raises(HTTPException) as exc_info:
        checker(creator_user)
    assert exc_info.value.status_code == 403


def test_client_membership_allows_active_member(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An active membership permits access to its client."""
    import auth_service
    from models import User

    monkeypatch.setattr(auth_service, "AUTH_BYPASS_LOCAL", False)
    user = User(
        external_user_id="member-1",
        email="member@example.com",
        role=UserRole.REVIEWER,
    )
    require_client_membership(FakeMembershipSession(object()), user, uuid.uuid4())


def test_client_membership_rejects_missing_or_revoked_member(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Missing and revoked memberships are both denied by the active query."""
    import auth_service
    from models import User

    monkeypatch.setattr(auth_service, "AUTH_BYPASS_LOCAL", False)
    user = User(
        external_user_id="former-member-1",
        email="former@example.com",
        role=UserRole.REVIEWER,
    )
    with pytest.raises(HTTPException) as exc_info:
        require_client_membership(FakeMembershipSession(None), user, uuid.uuid4())
    assert exc_info.value.status_code == 403


def test_client_membership_allows_admin_without_membership(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Admins can manage every tenant without explicit memberships."""
    import auth_service
    from models import User

    monkeypatch.setattr(auth_service, "AUTH_BYPASS_LOCAL", False)
    user = User(
        external_user_id="admin-1",
        email="admin@example.com",
        role=UserRole.ADMIN,
    )
    require_client_membership(FakeMembershipSession(None), user, uuid.uuid4())


def test_require_auth_rejects_anonymous_request() -> None:
    """require_auth raises 401 when no user is in the session.

    This only applies when AUTH_BYPASS_LOCAL is disabled; the test file forces
    bypass mode so it is skipped here.
    """
    from auth_service import AUTH_BYPASS_LOCAL, require_auth

    if AUTH_BYPASS_LOCAL:
        pytest.skip("AUTH_BYPASS_LOCAL is enabled")

    request = FakeRequest()
    with pytest.raises(HTTPException) as exc_info:
        require_auth(request)
    assert exc_info.value.status_code == 401


def test_require_auth_adds_htmx_redirect_header() -> None:
    """An HTMX request receives HX-Redirect to /login."""
    from auth_service import AUTH_BYPASS_LOCAL, require_auth

    if AUTH_BYPASS_LOCAL:
        pytest.skip("AUTH_BYPASS_LOCAL is enabled")

    request = FakeRequest()
    request.headers["HX-Request"] = "true"
    with pytest.raises(HTTPException) as exc_info:
        require_auth(request)
    assert exc_info.value.headers.get("HX-Redirect") == "/login"
