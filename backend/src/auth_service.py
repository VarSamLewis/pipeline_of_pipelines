"""Clerk JWT authentication, local sessions, and authorization dependencies."""

from __future__ import annotations

import uuid
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any, cast

import jwt
from config import get_settings
from dotenv import load_dotenv
from fastapi import Depends, HTTPException, Request
from jwt import PyJWKClient
from sqlmodel import Session, select

load_dotenv()

_settings = get_settings()
CLERK_PUBLISHABLE_KEY = _settings.clerk_publishable_key
CLERK_AUDIENCE = _settings.clerk_audience
CLERK_ISSUER = _settings.clerk_issuer
CLERK_JWKS_URL = _settings.clerk_jwks_url
CLERK_SIGN_IN_URL = _settings.clerk_sign_in_url
CLERK_JWT_TEMPLATE = _settings.clerk_jwt_template
SESSION_SECRET_KEY = _settings.session_secret_key
SESSION_MAX_AGE_SECONDS = _settings.session_max_age_seconds
SESSION_COOKIE_SECURE = _settings.session_cookie_secure
AUTH_BYPASS_LOCAL = _settings.auth_bypass_local
BOOTSTRAP_ADMIN_EMAILS = _settings.bootstrap_admin_emails
_jwks_client: PyJWKClient | None = None


def _get_jwks_client() -> PyJWKClient:
    if not CLERK_JWKS_URL:
        raise RuntimeError("CLERK_JWKS_URL is not configured")
    global _jwks_client
    if _jwks_client is None:
        _jwks_client = PyJWKClient(CLERK_JWKS_URL, cache_keys=True)
    return _jwks_client


def _verified_claims(token: str) -> Mapping[str, Any]:
    if not CLERK_ISSUER or not CLERK_AUDIENCE:
        raise RuntimeError("Clerk JWT verification is not configured")
    try:
        key = _get_jwks_client().get_signing_key_from_jwt(token)
        return jwt.decode(
            token,
            key.key,
            algorithms=["RS256"],
            issuer=CLERK_ISSUER,
            audience=CLERK_AUDIENCE,
            options={"require": ["sub", "exp", "iat", "nbf"]},
        )
    except jwt.PyJWTError as exc:
        raise HTTPException(401, "Invalid Clerk session token") from exc


def authenticate_clerk_session(token: str) -> Any:
    """Verify a Clerk JWT and provision only identities with pending invitations."""
    from db_ops import get_session
    from models import BetaInvitation, ClientMembership, User, UserRole

    claims = _verified_claims(token)
    external_id = claims["sub"]
    email = claims.get("email") or claims.get("email_address")
    if not isinstance(external_id, str) or not isinstance(email, str) or not email:
        raise HTTPException(401, "Clerk token must contain subject and email")
    if claims.get("email_verified") is False:
        raise HTTPException(401, "Clerk email is not verified")
    email = email.strip().lower()
    name = claims.get("name") if isinstance(claims.get("name"), str) else None
    now = datetime.now(UTC).replace(tzinfo=None)

    with get_session() as session:
        user = session.exec(
            select(User).where(User.external_user_id == external_id)
        ).first()
        if user is not None:
            user.email = email
            user.name = name or user.name
            user.last_login_at = now
            session.add(user)
            session.commit()
            session.refresh(user)
            return user

        invitations = session.exec(
            select(BetaInvitation).where(
                BetaInvitation.email == email, BetaInvitation.status == "pending"
            )
        ).all()
        is_bootstrap_admin = email in BOOTSTRAP_ADMIN_EMAILS
        if not invitations and not is_bootstrap_admin:
            raise HTTPException(403, "No pending beta invitation")
        user = User(
            external_user_id=external_id,
            email=email,
            name=name,
            role=UserRole.ADMIN if is_bootstrap_admin else UserRole.CREATOR,
            last_login_at=now,
        )
        session.add(user)
        session.flush()
        for invitation in invitations:
            session.add(
                ClientMembership(
                    user_id=user.id,
                    client_id=invitation.client_id,
                    role=invitation.role,
                )
            )
            invitation.status = "accepted"
            invitation.updated_at = now
            session.add(invitation)
        session.commit()
        session.refresh(user)
        return user


def _synthetic_local_user() -> Any:
    from models import User, UserRole

    return User(
        id=uuid.UUID("00000000-0000-0000-0000-000000000000"),
        external_user_id="local-dev",
        email="local-dev@example.com",
        name="Local Developer",
        role=UserRole.ADMIN,
    )


def get_current_user(request: Request) -> Any | None:
    if AUTH_BYPASS_LOCAL:
        return _synthetic_local_user()
    user_id = request.session.get("user_id")
    if not user_id:
        return None
    try:
        parsed_id = uuid.UUID(user_id)
    except ValueError:
        return None
    from db_ops import get_session
    from models import User

    with get_session() as session:
        return session.get(User, parsed_id)


def require_auth(request: Request) -> Any:
    user = get_current_user(request)
    if user is None:
        headers = (
            {"HX-Redirect": "/login"}
            if request.headers.get("HX-Request") == "true"
            else {}
        )
        raise HTTPException(401, "Authentication required", headers=headers)
    return user


def require_role(*allowed_roles: str) -> Callable[[Any], Any]:
    def checker(user: Any = Depends(require_auth)) -> Any:
        from models import UserRole

        levels = {
            UserRole.CREATOR: 1,
            UserRole.REVIEWER: 2,
            UserRole.APPROVER: 3,
            UserRole.ADMIN: 4,
        }
        if levels[user.role] < max(levels[UserRole(role)] for role in allowed_roles):
            raise HTTPException(
                403, f"Requires one of roles: {', '.join(allowed_roles)}"
            )
        return user

    return checker


def require_client_membership(
    session: Session,
    user: Any,
    client_id: uuid.UUID,
) -> None:
    """Raise 403 unless the user has an active membership for the client."""
    from models import ClientMembership, UserRole

    if AUTH_BYPASS_LOCAL or user.role == UserRole.ADMIN:
        return
    membership = session.exec(
        select(ClientMembership).where(
            ClientMembership.user_id == user.id,
            ClientMembership.client_id == client_id,
            cast(Any, ClientMembership.revoked_at).is_(None),
        )
    ).first()
    if membership is None:
        raise HTTPException(403, "Client membership required")


def create_session(request: Request, user_id: uuid.UUID) -> None:
    request.session["user_id"] = str(user_id)


def clear_session(request: Request) -> None:
    request.session.clear()
