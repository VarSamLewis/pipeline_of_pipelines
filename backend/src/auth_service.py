"""Microsoft Entra ID (OIDC) backed authentication and authorization.

This module provides:
- An MSAL confidential-client (authorization code + PKCE) login flow.
- ID-token validation (signature, issuer, audience, and nonce handled by MSAL).
- Encrypted cookie session management (unchanged from the now-removed WorkOS
  backend).
- FastAPI dependencies for authentication and role checks.
- A local-dev bypass so scripts and tests can run without Entra.

Role storage:
    Entra ID application roles (creator < reviewer < approver < admin) are the
    source of truth. The ``roles`` claim of the validated ID token carries the
    assigned roles; the local ``User`` table caches the highest one for fast
    permission checks and audit lineage. Admins assign/revoke roles in Entra.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Mapping
from typing import Any, cast

from config import get_settings
from dotenv import load_dotenv
from fastapi import Depends, HTTPException, Request
from msal import ConfidentialClientApplication  # type: ignore[import-untyped]

load_dotenv()

_settings = get_settings()
ENTRA_TENANT_ID = _settings.entra_tenant_id
ENTRA_CLIENT_ID = _settings.entra_client_id
ENTRA_CLIENT_SECRET = _settings.entra_client_secret
ENTRA_REDIRECT_URI = _settings.entra_redirect_uri
SESSION_SECRET_KEY = _settings.session_secret_key
SESSION_MAX_AGE_SECONDS = _settings.session_max_age_seconds
SESSION_COOKIE_SECURE = _settings.session_cookie_secure
AUTH_BYPASS_LOCAL = _settings.auth_bypass_local

# Delay model import until runtime to avoid import cycles with models.py.
_User: Any | None = None
_UserRole: Any | None = None

_msal_app: ConfidentialClientApplication | None = None

_ROLE_LEVELS: dict[str, int] = {
    "creator": 1,
    "reviewer": 2,
    "approver": 3,
    "admin": 4,
}


def _get_models() -> tuple[Any, Any]:
    """Import and cache SQLModel auth-related classes."""
    global _User, _UserRole
    if _User is None or _UserRole is None:
        from models import User, UserRole

        _User = User
        _UserRole = UserRole
    return _User, _UserRole


def get_msal_app() -> ConfidentialClientApplication:
    """Return the configured MSAL confidential client application."""
    global _msal_app
    if not all([ENTRA_TENANT_ID, ENTRA_CLIENT_ID, ENTRA_CLIENT_SECRET]):
        raise RuntimeError(
            "ENTRA_TENANT_ID/ENTRA_CLIENT_ID/ENTRA_CLIENT_SECRET are not configured"
        )
    if _msal_app is None:
        _msal_app = ConfidentialClientApplication(
            client_id=ENTRA_CLIENT_ID,
            client_credential=ENTRA_CLIENT_SECRET,
            authority=f"https://login.microsoftonline.com/{ENTRA_TENANT_ID}",
        )
    return _msal_app


def get_entra_authorize_url(request: Request) -> str:
    """Start an authorization-code + PKCE flow and return the Entra URL.

    The MSAL flow (including its CSRF ``state`` and nonce) is stored in the
    session for the callback step.
    """
    flow = get_msal_app().initiate_auth_code_flow(
        scopes=["openid", "profile", "email"],
        redirect_uri=ENTRA_REDIRECT_URI,
    )
    request.session["auth_flow"] = flow
    return cast(str, flow["auth_uri"])


def _extract_role_from_claims(claims: Mapping[str, Any]) -> Any:
    """Pick the highest-priority Entra app role present in the ID-token claims."""
    _User, UserRole = _get_models()  # noqa: N806
    roles = claims.get("roles") or []
    best = UserRole.CREATOR
    best_level = _ROLE_LEVELS["creator"]
    for role_value in roles:
        level = _ROLE_LEVELS.get(str(role_value), 0)
        if level > best_level:
            best_level = level
            best = UserRole(role_value)
    return best


def authenticate_with_entra(
    request: Request,
    query_params: Mapping[str, str],
) -> tuple[Any, bool]:
    """Exchange the Entra authorization-code response for a local user.

    Returns:
        A tuple of (local_user_record, created).

    Raises:
        HTTPException: 400 for an expired/missing login flow, 401 when the
            token exchange or validation fails.
    """
    from db_ops import get_session

    flow = request.session.pop("auth_flow", None)
    if not flow:
        raise HTTPException(
            status_code=400,
            detail="Missing or expired login state; please sign in again",
        )

    result = get_msal_app().acquire_token_by_auth_code_flow(
        flow,
        dict(query_params),
    )
    if "error" in result:
        raise HTTPException(
            status_code=401,
            detail=f"Authentication failed: {result.get('error_description')}",
        )

    User, UserRole = _get_models()  # noqa: N806
    claims = result["id_token_claims"]
    object_id = str(claims.get("oid") or claims.get("sub"))
    email = (
        claims.get("email")
        or claims.get("preferred_username")
        or claims.get("upn")
        or f"{object_id}@unknown"
    )
    name = claims.get("name")
    role = _extract_role_from_claims(claims)

    now = __import__("datetime").datetime.utcnow()
    with get_session() as session:
        from sqlmodel import select

        statement = select(User).where(User.external_user_id == object_id)
        user = session.exec(statement).first()
        if user is None:
            user = User(
                external_user_id=object_id,
                email=email,
                name=name,
                role=role,
                last_login_at=now,
            )
            session.add(user)
            created = True
        else:
            user.email = email
            user.name = name or user.name
            user.role = role
            user.last_login_at = now
            session.add(user)
            created = False

        session.commit()
        session.refresh(user)
        return user, created


def _synthetic_local_user() -> Any:
    """Return an in-memory admin user for local development bypass."""
    User, UserRole = _get_models()  # noqa: N806
    return User(
        id=uuid.UUID("00000000-0000-0000-0000-000000000000"),
        external_user_id="local-dev",
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


def require_role(*allowed_roles: str) -> Callable[[Any], Any]:
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
