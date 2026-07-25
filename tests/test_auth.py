"""Unit tests for the WorkOS-backed auth layer.

These tests exercise role hierarchy, local bypass mode, and the WorkOS user
metadata mapping without making network calls to WorkOS.
"""

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

from auth_service import _extract_role_from_workos_user, get_current_user, require_role
from fastapi import HTTPException
from models import UserRole


class FakeWorkOSUser:
    """Minimal stand-in for a WorkOS user object."""

    def __init__(self, metadata: dict[str, str] | None = None) -> None:
        self.metadata = metadata


class FakeRequest:
    """Minimal stand-in for a Starlette Request with a session dict."""

    def __init__(self, session: dict[str, str] | None = None) -> None:
        self.session = session or {}
        self.headers: dict[str, str] = {}


def test_extract_role_defaults_to_creator() -> None:
    """Missing or invalid metadata role defaults to creator."""
    user = FakeWorkOSUser(metadata={})
    assert _extract_role_from_workos_user(user) == UserRole.CREATOR

    user_invalid = FakeWorkOSUser(metadata={"role": "superuser"})
    assert _extract_role_from_workos_user(user_invalid) == UserRole.CREATOR


def test_extract_role_reads_metadata() -> None:
    """A valid role in WorkOS metadata is parsed correctly."""
    user = FakeWorkOSUser(metadata={"role": "admin"})
    assert _extract_role_from_workos_user(user) == UserRole.ADMIN


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
        workos_user_id="admin-1",
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
        workos_user_id="creator-1",
        email="creator@example.com",
        role=UserRole.CREATOR,
    )
    checker = require_role("approver")
    with pytest.raises(HTTPException) as exc_info:
        checker(creator_user)
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
