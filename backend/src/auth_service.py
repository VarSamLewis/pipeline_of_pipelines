"""WorkOS AuthKit-backed authentication and authorization.

This module provides:
- WorkOS client initialization.
- AuthKit login/callback helpers.
- Encrypted cookie session management.
- FastAPI dependencies for authentication and role checks.
- A local-dev bypass so scripts and tests can run without WorkOS.

Role storage:
    WorkOS user metadata holds the canonical role under the key ``role``.
    The local ``User`` table caches that role and is used for fast permission
    checks and audit lineage. Admins can change a user's role via
    ``update_user_role``, which writes back to WorkOS metadata and updates the
    local cache.
"""

from __future__ import annotations

import os
import secrets
import uuid
from typing import Any

from dotenv import load_dotenv
from fastapi import Depends, HTTPException, Request
from starlette.middleware.sessions import SessionMiddleware
from workos import WorkOSClient

load_dotenv()

WORKOS_CLIENT_ID = os.getenv("WORKOS_CLIENT_ID", "")
WORKOS_API_KEY = os.getenv("WORKOS_API_KEY", "")
WORKOS_REDIRECT_URI = os.getenv(
    "WORKOS_REDIRECT_URI", "http://localhost:8000/auth/callback"
)
WORKOS_AUTHKIT_DOMAIN = os.getenv("WORKOS_AUTHKIT_DOMAIN", "https://auth.workos.com")
SESSION_SECRET_KEY = os.getenv("SESSION_SECRET_KEY", secrets.token_urlsafe(32))
SESSION_MAX_AGE_SECONDS = int(os.getenv("SESSION_MAX_AGE", "86400"))
AUTH_BYPASS_LOCAL = os.getenv("AUTH_BYPASS_LOCAL", "false").lower() in {
    "1",
    "true",
    "yes",
}

# Delay model import until runtime to avoid import cycles with models.py.
_User: Any | None = None
_UserRole: Any | None = None


def _get_models() -> tuple[Any, Any]:
    """Import and cache SQLModel auth-related classes."""
    global _User, _UserRole
    if _User is None or _UserRole is None:
        from models import User, UserRole

        _User = User
        _UserRole = UserRole
    return _User, _UserRole


def get_workos_client() -> WorkOSClient:
    """Return a configured WorkOS client."""
    if not WORKOS_API_KEY:
        raise RuntimeError("WORKOS_API_KEY is not configured")
    return WorkOSClient(api_key=WORKOS_API_KEY, client_id=WORKOS_CLIENT_ID)


def get_session_middleware() -> SessionMiddleware:
    """Return Starlette session middleware for encrypted cookie sessions."""
    return SessionMiddleware(
        SESSION_SECRET_KEY,
        session_cookie="session",
        max_age=SESSION_MAX_AGE_SECONDS,
        same_site="lax",
        https_only=False,  # Set True in production/Azure behind HTTPS.
        path="/",
    )


def get_authkit_url(state: str) -> str:
    """Build the AuthKit authorization URL for a given state token."""
    client = get_workos_client()
    return client.user_management.get_authorization_url(
        redirect_uri=WORKOS_REDIRECT_URI,
        state=state,
    )


def _extract_role_from_workos_user(workos_user: Any) -> Any:
    """Read the role from WorkOS user metadata, defaulting to creator."""
    _User, UserRole = _get_models()  # noqa: N806
    metadata = workos_user.metadata or {}
    role_value = metadata.get("role", UserRole.CREATOR.value)
    try:
        return UserRole(role_value)
    except ValueError:
        return UserRole.CREATOR


def authenticate_with_workos(code: str) -> tuple[Any, bool]:
    """Exchange an AuthKit authorization code for a user.

    Returns:
        A tuple of (local_user_record, created).
    """
    from db_ops import get_session

    User, _UserRole = _get_models()  # noqa: N806
    client = get_workos_client()
    auth_response = client.user_management.authenticate_with_code(code=code)
    workos_user = auth_response.user

    with get_session() as session:
        from sqlmodel import select

        statement = select(User).where(User.workos_user_id == workos_user.id)
        user = session.exec(statement).first()
        role = _extract_role_from_workos_user(workos_user)

        now = __import__("datetime").datetime.utcnow()
        first_name = workos_user.first_name or ""
        last_name = workos_user.last_name or ""
        full_name = f"{first_name} {last_name}".strip() or None
        if user is None:
            user = User(
                workos_user_id=workos_user.id,
                email=workos_user.email,
                name=workos_user.name or full_name,
                role=role,
                last_login_at=now,
            )
            session.add(user)
            created = True
        else:
            user.email = workos_user.email
            user.name = workos_user.name or user.name
            user.role = role
            user.last_login_at = now
            session.add(user)
            created = False

        session.commit()
        session.refresh(user)
        return user, created


def update_user_role(user_id: uuid.UUID, role_value: str) -> Any:
    """Update a user's role in WorkOS metadata and sync the local cache.

    Args:
        user_id: Local UUID of the user to update.
        role_value: One of creator, reviewer, approver, admin.

    Returns:
        The updated local User record.

    Raises:
        ValueError: If the role is invalid or the user is not found.
        HTTPException: 502 if WorkOS update fails.
    """
    from db_ops import get_session

    User, UserRole = _get_models()  # noqa: N806
    try:
        role = UserRole(role_value)
    except ValueError as exc:
        raise ValueError(f"Invalid role: {role_value}") from exc

    with get_session() as session:
        user = session.get(User, user_id)
        if user is None:
            raise ValueError("User not found")

        client = get_workos_client()
        try:
            client.user_management.update_user(
                id=user.workos_user_id,
                metadata={"role": role.value},
            )
        except Exception as exc:
            raise HTTPException(
                status_code=502,
                detail=f"Failed to update WorkOS user metadata: {exc}",
            ) from exc

        user.role = role
        session.add(user)
        session.commit()
        session.refresh(user)
        return user


def _synthetic_local_user() -> Any:
    """Return an in-memory admin user for local development bypass."""
    User, UserRole = _get_models()  # noqa: N806
    return User(
        id=uuid.UUID("00000000-0000-0000-0000-000000000000"),
        workos_user_id="local-dev",
        email="local-dev@example.com",
        name="Local Developer",
        role=UserRole.ADMIN,
    )


def get_current_user(request: Request) -> Any | None:
    """Resolve the current user from the encrypted session cookie."""
    if AUTH_BYPASS_LOCAL:
        return _synthetic_local_user()

    user_id = request.session.get("user_id")
    if not user_id:
        return None

    from db_ops import get_session

    User, _UserRole = _get_models()  # noqa: N806
    with get_session() as session:
        return session.get(User, uuid.UUID(user_id))


def require_auth(request: Request) -> Any:
    """Dependency that returns the current user or raises 401/redirects.

    For HTMX requests the response includes ``HX-Redirect`` so the browser
    navigates to the login page. For regular browser requests it raises a 401
    that the frontend can intercept, or callers can redirect to ``/login``.
    """
    user = get_current_user(request)
    if user is None:
        is_htmx = request.headers.get("HX-Request") == "true"
        headers = {"HX-Redirect": "/login"} if is_htmx else {}
        raise HTTPException(
            status_code=401,
            detail="Authentication required",
            headers=headers,
        )
    return user


def require_role(*allowed_roles: str):
    """Dependency factory that restricts access to the given roles."""

    def checker(user: Any = Depends(require_auth)) -> Any:
        _, UserRole = _get_models()  # noqa: N806
        role_hierarchy = {
            UserRole.CREATOR.value: 1,
            UserRole.REVIEWER.value: 2,
            UserRole.APPROVER.value: 3,
            UserRole.ADMIN.value: 4,
        }
        user_level = role_hierarchy.get(user.role.value, 0)
        required_level = max(role_hierarchy.get(r, 0) for r in allowed_roles)

        if user_level < required_level:
            raise HTTPException(
                status_code=403,
                detail=f"Requires one of roles: {', '.join(allowed_roles)}",
            )
        return user

    return checker


def create_session(request: Request, user_id: uuid.UUID) -> None:
    """Persist the user id in the encrypted session cookie.

    The Starlette SessionMiddleware serialises ``request.session`` into the
    response, so callers set the value here and return the response unchanged.
    """
    request.session["user_id"] = str(user_id)


def clear_session(request: Request) -> None:
    """Clear the encrypted session cookie on logout."""
    request.session.clear()


def require_auth_dependency() -> Any:
    """Convenience alias for FastAPI ``Depends(require_auth)``.

    Returns:
        FastAPI Depends instance wrapping ``require_auth``.
    """
    return Depends(require_auth)
