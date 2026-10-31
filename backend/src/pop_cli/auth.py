"""Clerk OAuth device authorization for the ``pop`` CLI."""

from __future__ import annotations

import json
import time
import webbrowser
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import httpx
import keyring
from keyring.errors import KeyringError

from pop_cli.config import CliConfig

KEYRING_SERVICE = "pipeline-of-pipelines"
DEVICE_CODE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"


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


@dataclass(frozen=True, slots=True)
class DeviceAuthorization:
    """The short-lived Clerk device-code authorization challenge."""

    device_code: str
    user_code: str
    verification_uri: str
    verification_uri_complete: str | None
    expires_at: float
    interval_seconds: int


def _device_authorization(config: CliConfig) -> DeviceAuthorization:
    """Start Clerk's device authorization grant without a redirect URI."""
    response = httpx.post(
        f"{config.clerk_issuer}/oauth/device_authorization",
        data={
            "client_id": config.clerk_oauth_client_id,
            "scope": "openid profile email offline_access",
        },
        timeout=30,
    )
    try:
        response.raise_for_status()
        payload = response.json()
        device_code = payload["device_code"]
        user_code = payload["user_code"]
        verification_uri = payload["verification_uri"]
        expires_in = payload["expires_in"]
    except (httpx.HTTPStatusError, ValueError, KeyError) as exc:
        raise RuntimeError("Could not start Clerk device login") from exc
    if not all(
        isinstance(value, str) for value in (device_code, user_code, verification_uri)
    ) or not isinstance(expires_in, int):
        raise RuntimeError("Clerk returned an invalid device login response")
    verification_uri_complete = payload.get("verification_uri_complete")
    return DeviceAuthorization(
        device_code=device_code,
        user_code=user_code,
        verification_uri=verification_uri,
        verification_uri_complete=(
            verification_uri_complete
            if isinstance(verification_uri_complete, str)
            else None
        ),
        expires_at=time.monotonic() + expires_in,
        interval_seconds=max(1, payload.get("interval", 5)),
    )


def _tokens_from_payload(payload: dict[str, Any]) -> TokenSet:
    access_token = payload.get("access_token")
    if not isinstance(access_token, str):
        raise RuntimeError("Clerk did not return a valid access token")
    expires_in = payload.get("expires_in")
    return TokenSet(
        access_token=access_token,
        refresh_token=(
            payload.get("refresh_token")
            if isinstance(payload.get("refresh_token"), str)
            else None
        ),
        expires_at=(
            int(time.time()) + expires_in if isinstance(expires_in, int) else None
        ),
    )


def _poll_for_tokens(config: CliConfig, device: DeviceAuthorization) -> TokenSet:
    """Poll Clerk until the device challenge is approved or expires."""
    interval = device.interval_seconds
    while time.monotonic() < device.expires_at:
        response = httpx.post(
            f"{config.clerk_issuer}/oauth/token",
            data={
                "grant_type": DEVICE_CODE_GRANT,
                "client_id": config.clerk_oauth_client_id,
                "device_code": device.device_code,
            },
            timeout=30,
        )
        if response.is_success:
            try:
                payload = response.json()
            except ValueError as exc:
                raise RuntimeError("Clerk returned an invalid token response") from exc
            if not isinstance(payload, dict):
                raise RuntimeError("Clerk returned an invalid token response")
            return _tokens_from_payload(payload)
        try:
            error = response.json().get("error")
        except ValueError as exc:
            raise RuntimeError("Could not complete Clerk device login") from exc
        if error == "authorization_pending":
            time.sleep(interval)
            continue
        if error == "slow_down":
            interval += 5
            time.sleep(interval)
            continue
        if error == "access_denied":
            raise RuntimeError("Clerk device login was denied")
        if error == "expired_token":
            break
        raise RuntimeError("Could not complete Clerk device login")
    raise RuntimeError("Clerk device login expired; run 'pop login' again")


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


def login(
    config: CliConfig,
    notify: Callable[[DeviceAuthorization], None],
) -> TokenSet:
    """Run Clerk's device authorization grant and store the resulting tokens."""
    device = _device_authorization(config)
    notify(device)
    webbrowser.open(device.verification_uri_complete or device.verification_uri)
    tokens = _poll_for_tokens(config, device)
    save_tokens(config, tokens)
    return tokens
