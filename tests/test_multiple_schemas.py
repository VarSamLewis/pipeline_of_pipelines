"""Independent mapping specs for multiple target schemas."""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
import routers.api as api
from fastapi import HTTPException
from models import MappingSpecStatus


class FakeSession:
    def __enter__(self) -> FakeSession:
        return self

    def __exit__(self, *_: object) -> None:
        return None


def test_api_creates_two_specs_with_same_source_files(monkeypatch) -> None:
    client_id, file_id = uuid.uuid4(), uuid.uuid4()
    created: list[dict] = []
    monkeypatch.setattr(api, "get_session", FakeSession)
    monkeypatch.setattr(
        api, "get_client_by_id", lambda _session, _id: SimpleNamespace(id=client_id)
    )
    monkeypatch.setattr(
        api,
        "get_raw_file_by_id",
        lambda _session, _id: SimpleNamespace(id=file_id, client_id=client_id),
    )

    def create(_session, **kwargs):
        created.append(kwargs)
        return SimpleNamespace(
            id=uuid.uuid4(),
            client_id=client_id,
            status=MappingSpecStatus.DRAFT,
            target_schema_json=kwargs["target_schema"].model_dump(mode="json"),
            description=None,
        )

    monkeypatch.setattr(api, "create_mapping_spec", create)
    result = api.create_mapping_spec_endpoint(
        client_id,
        {
            "source_raw_file_ids": [str(file_id)],
            "target_schemas": [
                {"name": "customers", "tables": []},
                {"name": "orders", "tables": []},
            ],
        },
        user=SimpleNamespace(),
    )
    assert len(result["mapping_specs"]) == 2
    assert [item["target_schema"].name for item in created] == ["customers", "orders"]
    assert all(item["source_raw_file_ids"] == [file_id] for item in created)


def test_api_checks_all_schemas_before_creating_a_draft(monkeypatch) -> None:
    client_id = uuid.uuid4()
    monkeypatch.setattr(api, "get_session", FakeSession)
    monkeypatch.setattr(
        api, "get_client_by_id", lambda _session, _id: SimpleNamespace(id=client_id)
    )
    created: list[object] = []
    monkeypatch.setattr(api, "create_mapping_spec", lambda *a, **kw: created.append(kw))
    with pytest.raises(HTTPException) as exc:
        api.create_mapping_spec_endpoint(
            client_id,
            {"target_schemas": [{"name": "valid"}, {"tables": "not a list"}]},
            user=SimpleNamespace(),
        )
    assert created == []
    assert exc.value.status_code == 422


def test_api_rejects_other_clients_source_file(monkeypatch) -> None:
    client_id = uuid.uuid4()
    monkeypatch.setattr(api, "get_session", FakeSession)
    monkeypatch.setattr(
        api, "get_client_by_id", lambda _session, _id: SimpleNamespace(id=client_id)
    )
    monkeypatch.setattr(
        api,
        "get_raw_file_by_id",
        lambda *_: SimpleNamespace(client_id=uuid.uuid4()),
    )
    with pytest.raises(HTTPException) as exc:
        api.create_mapping_spec_endpoint(
            client_id,
            {"source_raw_file_ids": [str(uuid.uuid4())], "target_schema": {}},
            user=SimpleNamespace(),
        )
    assert exc.value.status_code == 422
