"""Browser-level upload and review journey against the real app and database."""

from __future__ import annotations

import json
import socket
import threading
import time
import uuid
from collections.abc import Iterator

import pytest
from byok import save_organization_api_key
from db_ops import get_session
from fastapi.testclient import TestClient
from models import Organization, OrganizationMembership, OrganizationRole, User
from uvicorn import Config, Server

pytestmark = pytest.mark.browser


@pytest.fixture
def live_app() -> Iterator[str]:
    from app import app

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    server = Server(Config(app=app, host="127.0.0.1", port=port, log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 15
    try:
        while not server.started and time.monotonic() < deadline:
            time.sleep(0.05)
        assert server.started, "The browser test server did not start"
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=10)


def test_browser_uploads_two_schemas_and_reviews_each_mapping(
    live_app: str, mock_openai: None
) -> None:
    from app import app
    from playwright.sync_api import expect, sync_playwright

    local_id = uuid.UUID(int=0)
    with get_session() as session:
        if session.get(User, local_id) is None:
            session.add(
                User(
                    id=local_id,
                    external_user_id="local-dev",
                    email="local-dev@example.com",
                )
            )
        organization = Organization(name=f"Browser test {uuid.uuid4()}")
        session.add(organization)
        session.flush()
        session.add(
            OrganizationMembership(
                organization_id=organization.id,
                user_id=local_id,
                role=OrganizationRole.ORG_ADMIN,
            )
        )
        session.commit()
        organization_id = str(organization.id)
        save_organization_api_key(
            session, organization.id, "test-byok-key", "https://api.openai.com/v1"
        )

    response = TestClient(app).post(
        "/clients",
        json={"name": "Browser client", "organization_id": organization_id},
    )
    assert response.status_code == 200, response.text
    client_id = response.json()["id"]
    schema = {
        "name": "first",
        "tables": [
            {
                "name": "records",
                "columns": [
                    {"name": "record_id", "dtype": "Int64", "required": True},
                    {"name": "full_name", "dtype": "String", "required": True},
                    {"name": "score", "dtype": "Float64", "required": False},
                ],
            }
        ],
    }

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(args=["--no-sandbox"])
        try:
            page = browser.new_page()
            page.goto(f"{live_app}/upload")
            page.select_option("#organization_select", organization_id)
            page.select_option("#client_select", client_id)
            page.locator("#source_files").set_input_files(
                {
                    "name": "data.csv",
                    "mimeType": "text/csv",
                    "buffer": b"id,name,score\n1,Alice,88.5\n",
                }
            )
            page.locator("#target_schema").set_input_files(
                [
                    {
                        "name": "first.json",
                        "mimeType": "application/json",
                        "buffer": json.dumps(schema).encode(),
                    },
                    {
                        "name": "second.json",
                        "mimeType": "application/json",
                        "buffer": json.dumps({**schema, "name": "second"}).encode(),
                    },
                ]
            )
            page.get_by_role("button", name="Propose mappings").click()
            links = page.locator("#upload-result a[href^='/mapping/']")
            expect(links).to_have_count(2, timeout=30000)
            assert "first.json" in page.locator("#upload-result").inner_text()
            assert "second.json" in page.locator("#upload-result").inner_text()
            reviews = links.evaluate_all(
                "elements => elements.map(element => element.getAttribute('href'))"
            )
            for review in reviews:
                page.goto(f"{live_app}{review}")
                expect(
                    page.get_by_role("heading", name="Mapping editor")
                ).to_be_visible()
            page.goto(f"{live_app}/clients/{client_id}/files")
            assert "data.csv" in page.locator("body").inner_text()
            assert "Uploaded" in page.locator("body").inner_text()
        finally:
            browser.close()
