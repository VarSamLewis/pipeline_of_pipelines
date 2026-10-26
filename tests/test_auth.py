"""Unit tests for Clerk-backed authorization dependencies."""

from __future__ import annotations

import os
import uuid
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

# Ensure local auth bypass is active for these unit tests before importing the
# auth module, which evaluates AUTH_BYPASS_LOCAL at import time.
os.environ["AUTH_BYPASS_LOCAL"] = "true"

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend" / "src"))

from auth_service import get_current_user, require_client_membership, require_role
from fastapi import HTTPException
from models import PlatformRole


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

    def __init__(self, membership: object | None, organization_id: uuid.UUID) -> None:
        self.membership = membership
        self.organization_id = organization_id

    def get(self, _: object, __: object) -> SimpleNamespace:
        return SimpleNamespace(organization_id=self.organization_id)

    def exec(self, _: object) -> FakeMembershipResult:
        return FakeMembershipResult(self.membership)


def test_local_bypass_returns_admin_user() -> None:
    """In bypass mode the current user is always the synthetic admin."""
    request = FakeRequest()
    user = get_current_user(request)
    assert user is not None
    assert user.platform_role == PlatformRole.PLATFORM_ADMIN
    assert user.email == "local-dev@example.com"


def test_logout_redirects_post_to_get_login() -> None:
    """Logout uses 303 so the browser follows the redirect with GET."""
    from app import app

    response = TestClient(app).post("/auth/logout", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_session_version_invalidates_existing_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A role or membership change invalidates existing signed sessions."""
    import auth_service
    import db_ops
    from models import PlatformRole, User

    class FakeSession:
        def __enter__(self) -> FakeSession:
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def get(self, _: object, __: object) -> User:
            return User(
                external_user_id="member-1",
                email="member@example.com",
                platform_role=PlatformRole.USER,
                session_version=2,
            )

    monkeypatch.setattr(auth_service, "AUTH_BYPASS_LOCAL", False)
    monkeypatch.setattr(db_ops, "get_session", lambda: FakeSession())
    request = FakeRequest(
        {"user_id": str(uuid.uuid4()), "session_version": 1}
    )

    assert auth_service.get_current_user(request) is None
    assert request.session == {}


def test_send_clerk_invitation_uses_backend_api(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reviewer invitations are delegated to Clerk's server-side API."""
    import auth_service

    captured: dict[str, object] = {}

    class FakeResponse:
        def __enter__(self) -> FakeResponse:
            return self

        def __exit__(self, *args: object) -> None:
            return None

    def fake_urlopen(request: object, timeout: int) -> FakeResponse:
        captured["request"] = request
        captured["timeout"] = timeout
        return FakeResponse()

    monkeypatch.setattr(
        auth_service,
        "get_settings",
        lambda: SimpleNamespace(clerk_secret_key="sk_test_secret"),
    )
    monkeypatch.setattr(auth_service, "urlopen", fake_urlopen)

    auth_service.send_clerk_invitation("reviewer@example.com")

    request = captured["request"]
    assert request.full_url == "https://api.clerk.com/v1/invitations"
    assert request.get_header("Authorization") == "Bearer sk_test_secret"
    assert request.data == b'{"email_address": "reviewer@example.com"}'
    assert captured["timeout"] == 10


def test_require_role_allows_platform_admin() -> None:
    """Platform administrators satisfy platform-admin dependencies."""
    from models import User

    admin_user = User(
        id=uuid.uuid4(),
        external_user_id="admin-1",
        email="admin@example.com",
        platform_role=PlatformRole.PLATFORM_ADMIN,
    )
    checker = require_role("admin")
    assert checker(admin_user) == admin_user


def test_require_role_rejects_standard_user_for_platform_admin() -> None:
    """Standard users cannot satisfy platform-admin dependencies."""
    from models import User

    creator_user = User(
        id=uuid.uuid4(),
        external_user_id="creator-1",
        email="creator@example.com",
        platform_role=PlatformRole.USER,
    )
    checker = require_role("admin")
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
        platform_role=PlatformRole.USER,
    )
    require_client_membership(
        FakeMembershipSession(object(), uuid.uuid4()), user, uuid.uuid4()
    )


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
        platform_role=PlatformRole.USER,
    )
    with pytest.raises(HTTPException) as exc_info:
        require_client_membership(
            FakeMembershipSession(None, uuid.uuid4()), user, uuid.uuid4()
        )
    assert exc_info.value.status_code == 403


def test_client_membership_rejects_admin_without_membership(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Admins remain constrained to their organization memberships."""
    import auth_service
    from models import User

    monkeypatch.setattr(auth_service, "AUTH_BYPASS_LOCAL", False)
    user = User(
        external_user_id="admin-1",
        email="admin@example.com",
        platform_role=PlatformRole.PLATFORM_ADMIN,
    )
    with pytest.raises(HTTPException) as exc_info:
        require_client_membership(
            FakeMembershipSession(None, uuid.uuid4()), user, uuid.uuid4()
        )
    assert exc_info.value.status_code == 403


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
