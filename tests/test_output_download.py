"""Tests for multi-table output downloads."""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
import routers.api as api
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient


def test_target_table_download_validates_schema_and_reads_artifact(monkeypatch) -> None:
    spec_id = uuid.uuid4()
    requested: list[str] = []

    class Store:
        def read_artifact(self, folder_id, filename):
            assert folder_id == spec_id
            requested.append(filename)
            return b"id\n1\n"

    monkeypatch.setattr(
        api,
        "get_mapping_review",
        lambda _: {"target_schema_json": {"tables": [{"name": "customers"}]}},
    )
    monkeypatch.setattr(api, "get_artifact_store", Store)

    response = api.get_generated_table_csv(spec_id, "customers", SimpleNamespace())
    assert response.body == b"id\n1\n"
    assert requested == ["customers.csv"]

    with pytest.raises(HTTPException) as exc:
        api.get_generated_table_csv(spec_id, "orders", SimpleNamespace())
    assert exc.value.status_code == 404
    assert requested == ["customers.csv"]


def test_target_table_download_route_is_registered(monkeypatch) -> None:
    spec_id = uuid.uuid4()
    app = FastAPI()
    app.include_router(api.app)
    app.dependency_overrides[api.require_auth] = lambda: SimpleNamespace()
    app.dependency_overrides[api.require_output_folder_access] = lambda: None
    monkeypatch.setattr(
        api,
        "get_mapping_review",
        lambda _: {"target_schema_json": {"tables": [{"name": "orders"}]}},
    )

    class Store:
        def read_artifact(self, folder_id, filename):
            assert folder_id == spec_id and filename == "orders.csv"
            return b"order_id\n1\n"

    monkeypatch.setattr(api, "get_artifact_store", Store)
    response = TestClient(app).get(f"/output-folders/{spec_id}/tables/orders.csv")
    assert response.status_code == 200
    assert response.content == b"order_id\n1\n"
