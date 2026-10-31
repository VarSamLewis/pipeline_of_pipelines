"""Clerk OAuth PKCE authentication for the ``pop`` CLI."""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import time
import webbrowser
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

import httpx
import keyring
from keyring.errors import KeyringError

from pop_cli.config import CliConfig

CALLBACK_URL = "http://127.0.0.1:8765/callback"
KEYRING_SERVICE = "pipeline-of-pipelines"


def _pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()
    ).rstrip(b"=").decode()
    return verifier, challenge


def authorization_url(config: CliConfig, state: str, challenge: str) -> str:
    """Build the Clerk OAuth authorization URL for a PKCE login."""
    query = urlencode(
        {
            "response_type": "code",
            "client_id": config.clerk_oauth_client_id,
            "redirect_uri": CALLBACK_URL,
            "scope": "openid profile email offline_access",
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
    )
    return f"{config.clerk_issuer}/oauth/authorize?{query}"


class _CallbackHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        query = parse_qs(urlparse(self.path).query)
        self.server.oauth_query = query  # type: ignore[attr-defined]
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"<p>Login complete. You can close this window.</p>")

    def log_message(self, _format: str, *_args: object) -> None:
        """Suppress callback request logs in normal CLI use."""


def _receive_callback() -> dict[str, list[str]]:
    with HTTPServer(("127.0.0.1", 8765), _CallbackHandler) as server:
        server.timeout = 180
        server.handle_request()
        query: Any = getattr(server, "oauth_query", None)
    if not isinstance(query, dict):
        raise RuntimeError("Timed out waiting for Clerk login callback")
    return query


@dataclass(frozen=True, slots=True)
class TokenSet:
    access_token: str
    refresh_token: str | None
    expires_at: int | None

    def to_json(self) -> str:
        return json.dumps(
            {
                "access_token": self.access_token,
                "refresh_token": self.refresh_token,
                "expires_at": self.expires_at,
            }
        )


def _token_key(config: CliConfig) -> str:
    return config.clerk_issuer


def save_tokens(config: CliConfig, tokens: TokenSet) -> None:
    """Store OAuth tokens in the operating system credential store."""
    try:
        keyring.set_password(KEYRING_SERVICE, _token_key(config), tokens.to_json())
    except KeyringError as exc:
        raise RuntimeError(
            "Could not store CLI credentials in the OS keychain"
        ) from exc


def clear_tokens(config: CliConfig) -> None:
    """Remove locally stored OAuth tokens."""
    try:
        keyring.delete_password(KEYRING_SERVICE, _token_key(config))
    except keyring.errors.PasswordDeleteError:
        return
    except KeyringError as exc:
        raise RuntimeError(
            "Could not remove CLI credentials from the OS keychain"
        ) from exc


def load_tokens(config: CliConfig) -> TokenSet:
    """Load an access token previously stored by ``pop login``."""
    try:
        stored = keyring.get_password(KEYRING_SERVICE, _token_key(config))
    except KeyringError as exc:
        raise RuntimeError(
            "Could not read CLI credentials from the OS keychain"
        ) from exc
    if stored is None:
        raise RuntimeError("Not logged in; run 'pop login' first")
    try:
        values = json.loads(stored)
        access_token = values["access_token"]
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise RuntimeError(
            "Stored CLI credentials are invalid; run 'pop login'"
        ) from exc
    if not isinstance(access_token, str):
        raise RuntimeError("Stored CLI credentials are invalid; run 'pop login'")
    refresh_token = values.get("refresh_token")
    expires_at = values.get("expires_at")
    return TokenSet(
        access_token=access_token,
        refresh_token=refresh_token if isinstance(refresh_token, str) else None,
        expires_at=expires_at if isinstance(expires_at, int) else None,
    )


def login(config: CliConfig) -> TokenSet:
    """Run browser-based Clerk OAuth authorization-code login with PKCE."""
    verifier, challenge = _pkce_pair()
    state = secrets.token_urlsafe(32)
    url = authorization_url(config, state, challenge)
    webbrowser.open(url)
    query = _receive_callback()
    if query.get("state", [None])[0] != state:
        raise RuntimeError("Clerk login callback state did not match")
    if error := query.get("error", [None])[0]:
        raise RuntimeError(f"Clerk login failed: {error}")
    code = query.get("code", [None])[0]
    if not isinstance(code, str):
        raise RuntimeError("Clerk login callback did not include an authorization code")

    response = httpx.post(
        f"{config.clerk_issuer}/oauth/token",
        data={
            "grant_type": "authorization_code",
            "client_id": config.clerk_oauth_client_id,
            "code": code,
            "redirect_uri": CALLBACK_URL,
            "code_verifier": verifier,
        },
        timeout=30,
    )
    try:
        response.raise_for_status()
        payload = response.json()
        access_token = payload["access_token"]
    except (httpx.HTTPStatusError, ValueError, KeyError) as exc:
        raise RuntimeError("Could not exchange Clerk authorization code") from exc
    if not isinstance(access_token, str):
        raise RuntimeError("Clerk did not return a valid access token")
    expires_in = payload.get("expires_in")
    tokens = TokenSet(
        access_token=access_token,
        refresh_token=(
            payload.get("refresh_token")
            if isinstance(payload.get("refresh_token"), str)
            else None
        ),
        expires_at=(
            int(time.time()) + expires_in
            if isinstance(expires_in, int)
            else None
        ),
    )
    save_tokens(config, tokens)
    return tokens
