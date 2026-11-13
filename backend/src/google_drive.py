"""Read-only Google OAuth and explicit Drive import service.

Google credentials are deliberately separate from Clerk authentication.  This
module uses the REST APIs directly so tokens stay server-side and calls are
straightforward to stub in integration tests.
"""

from __future__ import annotations

import io
import secrets
import uuid
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlencode

import httpx
from byok import _fernet
from config import get_settings
from db_ops import create_raw_file, get_session
from dependencies import get_object_store
from file_ops import compute_sha256, detect_file_type, store_raw_upload
from models import (
    Client,
    GoogleConnection,
    GoogleConnectionStatus,
    GoogleDriveImport,
    GoogleImportStatus,
    Organization,
    OrganizationMembership,
)
from openpyxl import Workbook
from repositories.clients import create_ingestion_batch
from sqlmodel import Session, select

GOOGLE_SHEETS_MIME = "application/vnd.google-apps.spreadsheet"
XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
ALLOWED_DRIVE_MIMES = {
    "text/csv",
    "application/vnd.ms-excel",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    GOOGLE_SHEETS_MIME,
}
SCOPES = "openid email https://www.googleapis.com/auth/drive.file https://www.googleapis.com/auth/spreadsheets.readonly"
GoogleQueryValue = str | int | float | bool | None
GoogleQueryParams = (
    dict[str, GoogleQueryValue | list[GoogleQueryValue]]
    | list[tuple[str, GoogleQueryValue]]
)


class GoogleDriveError(ValueError):
    """A safe error suitable for presentation to an authenticated user."""


def authorization_url(state: str) -> str:
    settings = get_settings()
    if not (settings.google_oauth_client_id and settings.google_oauth_redirect_uri):
        raise GoogleDriveError("Google Drive import is not configured")
    return "https://accounts.google.com/o/oauth2/v2/auth?" + urlencode(
        {
            "client_id": settings.google_oauth_client_id,
            "redirect_uri": settings.google_oauth_redirect_uri,
            "response_type": "code",
            "scope": SCOPES,
            "access_type": "offline",
            "prompt": "consent",
            "state": state,
        }
    )


def new_state() -> str:
    """Create an unguessable state nonce; its user binding lives in the session."""
    return secrets.token_urlsafe(32)


def _token_request(data: dict[str, str]) -> dict[str, Any]:
    try:
        response = httpx.post(
            "https://oauth2.googleapis.com/token", data=data, timeout=20
        )
        response.raise_for_status()
        payload = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise GoogleDriveError("Google authorization could not be completed") from exc
    if not isinstance(payload, dict):
        raise GoogleDriveError("Google returned an invalid authorization response")
    return payload


def connect_callback(user_id: uuid.UUID, code: str) -> None:
    """Exchange a callback code and upsert the connection for its app user."""
    settings = get_settings()
    if not settings.google_oauth_client_secret:
        raise GoogleDriveError("Google Drive import is not configured")
    token = _token_request(
        {
            "code": code,
            "client_id": settings.google_oauth_client_id,
            "client_secret": settings.google_oauth_client_secret,
            "redirect_uri": settings.google_oauth_redirect_uri,
            "grant_type": "authorization_code",
        }
    )
    refresh_token = token.get("refresh_token")
    access_token = token.get("access_token")
    if not isinstance(refresh_token, str) or not isinstance(access_token, str):
        raise GoogleDriveError(
            "Google did not grant offline Drive access; reconnect and approve access"
        )
    try:
        profile_response = httpx.get(
            "https://openidconnect.googleapis.com/v1/userinfo",
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=20,
        )
        profile_response.raise_for_status()
        profile = profile_response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise GoogleDriveError(
            "Could not confirm the connected Google account"
        ) from exc
    subject = profile.get("sub") if isinstance(profile, dict) else None
    if not isinstance(subject, str):
        raise GoogleDriveError("Google did not return an account identity")
    with get_session() as session:
        connection = session.exec(
            select(GoogleConnection).where(GoogleConnection.user_id == user_id)
        ).first()
        if connection is None:
            connection = GoogleConnection(
                user_id=user_id, google_account_id=subject, encrypted_refresh_token=""
            )
        connection.google_account_id = subject
        connection.google_email = (
            profile.get("email") if isinstance(profile.get("email"), str) else None
        )
        connection.granted_scopes = str(token.get("scope", SCOPES)).split()
        connection.encrypted_refresh_token = (
            _fernet().encrypt(refresh_token.encode()).decode()
        )
        connection.status = GoogleConnectionStatus.ACTIVE
        connection.revoked_at = None
        connection.updated_at = datetime.now(UTC).replace(tzinfo=None)
        session.add(connection)
        session.commit()


def connection_for_user(
    session: Session, user_id: uuid.UUID
) -> GoogleConnection | None:
    return session.exec(
        select(GoogleConnection).where(GoogleConnection.user_id == user_id)
    ).first()


def picker_access_token(user_id: uuid.UUID) -> str:
    """Mint a short-lived token only for Google's Picker browser component."""
    with get_session() as session:
        connection = connection_for_user(session, user_id)
        if connection is None:
            raise GoogleDriveError("Connect your Google account first")
        return _access_token(session, connection)


def disconnect(user_id: uuid.UUID) -> None:
    with get_session() as session:
        connection = connection_for_user(session, user_id)
        if connection is None:
            return
        connection.status = GoogleConnectionStatus.DISCONNECTED
        connection.encrypted_refresh_token = ""
        connection.revoked_at = datetime.now(UTC).replace(tzinfo=None)
        connection.updated_at = connection.revoked_at
        session.add(connection)
        session.commit()


def _access_token(session: Session, connection: GoogleConnection) -> str:
    if (
        connection.status != GoogleConnectionStatus.ACTIVE
        or not connection.encrypted_refresh_token
    ):
        raise GoogleDriveError("Reconnect your Google account to import files")
    try:
        refresh_token = (
            _fernet().decrypt(connection.encrypted_refresh_token.encode()).decode()
        )
    except Exception as exc:
        raise GoogleDriveError("Reconnect your Google account to import files") from exc
    settings = get_settings()
    token = _token_request(
        {
            "client_id": settings.google_oauth_client_id,
            "client_secret": settings.google_oauth_client_secret or "",
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        }
    )
    access_token = token.get("access_token")
    if not isinstance(access_token, str):
        connection.status = GoogleConnectionStatus.REVOKED
        connection.revoked_at = datetime.now(UTC).replace(tzinfo=None)
        session.add(connection)
        session.commit()
        raise GoogleDriveError("Google access was revoked; reconnect your account")
    return access_token


def _google_get(
    url: str,
    token: str,
    *,
    params: GoogleQueryParams | None = None,
) -> httpx.Response:
    response = httpx.get(
        url, headers={"Authorization": f"Bearer {token}"}, params=params, timeout=60
    )
    if response.status_code in {401, 403}:
        raise GoogleDriveError(
            "Google denied access to this file; select it again or reconnect"
        )
    if response.status_code == 404:
        raise GoogleDriveError("The selected Drive file no longer exists")
    try:
        response.raise_for_status()
    except httpx.HTTPError as exc:
        raise GoogleDriveError("Could not read the selected Drive file") from exc
    return response


def _native_sheet_xlsx(file_id: str, token: str) -> bytes:
    metadata = _google_get(
        f"https://sheets.googleapis.com/v4/spreadsheets/{file_id}",
        token,
        params={"fields": "sheets.properties"},
    ).json()
    sheets = metadata.get("sheets", []) if isinstance(metadata, dict) else []
    ranges = [sheet.get("properties", {}).get("title") for sheet in sheets]
    ranges = [title for title in ranges if isinstance(title, str)]
    range_params: list[tuple[str, GoogleQueryValue]] = [
        ("ranges", title) for title in ranges
    ]
    values = _google_get(
        f"https://sheets.googleapis.com/v4/spreadsheets/{file_id}/values:batchGet",
        token,
        params=range_params,
    ).json()
    workbook = Workbook()
    active_sheet = workbook.active
    if active_sheet is not None:
        workbook.remove(active_sheet)
    total_cells = 0
    for title, value_range in zip(ranges, values.get("valueRanges", []), strict=False):
        worksheet = workbook.create_sheet(title=title[:31] or "Sheet")
        rows = value_range.get("values", []) if isinstance(value_range, dict) else []
        for row in rows:
            total_cells += len(row)
            if total_cells > 2_000_000:
                raise GoogleDriveError(
                    "Selected Google Sheet exceeds the 2,000,000-cell import limit"
                )
            worksheet.append(row)
    contents = io.BytesIO()
    workbook.save(contents)
    return contents.getvalue()


def import_drive_file(
    *, user_id: uuid.UUID, client_id: uuid.UUID, drive_file_id: str
) -> GoogleDriveImport:
    """Create a new immutable raw-file snapshot from one Picker-selected file."""
    with get_session() as session:
        connection = connection_for_user(session, user_id)
        client = session.get(Client, client_id)
        if connection is None or client is None:
            raise GoogleDriveError("Google connection or client not found")
        membership = session.exec(
            select(OrganizationMembership).where(
                OrganizationMembership.user_id == user_id,
                OrganizationMembership.organization_id == client.organization_id,
                OrganizationMembership.revoked_at.is_(None),  # type: ignore[union-attr]
            )
        ).first()
        if membership is None:
            raise GoogleDriveError("Client membership required")
        token = _access_token(session, connection)
        metadata = _google_get(
            f"https://www.googleapis.com/drive/v3/files/{drive_file_id}",
            token,
            params={"fields": "id,name,mimeType,modifiedTime,size"},
        ).json()
        mime_type = metadata.get("mimeType") if isinstance(metadata, dict) else None
        name = metadata.get("name") if isinstance(metadata, dict) else None
        if (
            not isinstance(mime_type, str)
            or mime_type not in ALLOWED_DRIVE_MIMES
            or not isinstance(name, str)
        ):
            raise GoogleDriveError("Select a CSV, Excel workbook, or Google Sheet")
        audit = GoogleDriveImport(
            user_id=user_id,
            connection_id=connection.id,
            client_id=client_id,
            drive_file_id=drive_file_id,
            drive_mime_type=mime_type,
        )
        session.add(audit)
        session.flush()
        try:
            contents = (
                _native_sheet_xlsx(drive_file_id, token)
                if mime_type == GOOGLE_SHEETS_MIME
                else _google_get(
                    f"https://www.googleapis.com/drive/v3/files/{drive_file_id}",
                    token,
                    params={"alt": "media"},
                ).content
            )
            if len(contents) > get_settings().max_upload_size_bytes:
                raise GoogleDriveError(
                    "Selected Drive file exceeds the individual upload limit"
                )
            filename = f"{name}.xlsx" if mime_type == GOOGLE_SHEETS_MIME else name
            file_type = detect_file_type(filename, contents)
            if file_type not in {"csv", "xlsx"}:
                raise GoogleDriveError(
                    "Selected Drive file is not a supported spreadsheet"
                )
            organization = session.get(Organization, client.organization_id)
            if organization is None:
                raise GoogleDriveError("Client organization not found")
            batch = create_ingestion_batch(
                session,
                client.id,
                "Google Drive import",
                {"source": "google_drive", "drive_file_id": drive_file_id},
            )
            storage_key = store_raw_upload(
                get_object_store(),
                organization.name,
                str(organization.id),
                client.name,
                str(client.id),
                filename,
                contents,
            )
            raw_file = create_raw_file(
                session,
                client.id,
                batch.id,
                filename,
                storage_key,
                compute_sha256(contents),
                len(contents),
                "text/csv" if file_type == "csv" else XLSX_MIME,
                {
                    "source": "google_drive",
                    "drive_file_id": drive_file_id,
                    "drive_modified_time": metadata.get("modifiedTime"),
                },
            )
            audit.raw_file_id = raw_file.id
            audit.imported_checksum = raw_file.sha256
            audit.status = GoogleImportStatus.SUCCEEDED
            modified = metadata.get("modifiedTime")
            if isinstance(modified, str):
                audit.drive_modified_time = datetime.fromisoformat(
                    modified.replace("Z", "+00:00")
                ).replace(tzinfo=None)
            audit.completed_at = datetime.now(UTC).replace(tzinfo=None)
            session.add(audit)
            session.commit()
            session.refresh(audit)
            return audit
        except Exception as exc:
            audit.status = GoogleImportStatus.FAILED
            audit.error_code = "import_failed"
            audit.completed_at = datetime.now(UTC).replace(tzinfo=None)
            session.add(audit)
            session.commit()
            if isinstance(exc, GoogleDriveError):
                raise
            raise GoogleDriveError("Could not import the selected Drive file") from exc
