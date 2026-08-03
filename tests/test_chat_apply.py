"""Regression tests for the LLM chat apply flow.

The Apply buttons previously built a dynamic ``<form>`` in JavaScript and fired
it with ``htmx.trigger(form, 'submit')`` without calling ``htmx.process(form)``.
HTMX never attached a submit listener to that form, so the browser fell back to
a native POST to the *current* page URL:

- ``/mapping/{spec_id}`` absorbed the stray POST via a now-removed
  ``changes_json`` fallback (the old "hacky fix"), and
- ``/results/{run_id}`` had no POST route at all, producing
  ``405 Method Not Allowed``.

These tests pin the dedicated ``/chat/apply`` endpoints and the rendered
template markup so the flow cannot silently regress to the native-POST
fallback again.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import pytest
import workflow
from fastapi.testclient import TestClient


@pytest.fixture
def client() -> TestClient:
    """Return a TestClient that does not follow redirects."""
    from app import app

    return TestClient(app, follow_redirects=False)


@pytest.fixture
def apply_targets(monkeypatch: pytest.MonkeyPatch) -> dict[str, list]:
    """Record calls into the workflow apply functions."""
    calls: dict[str, list] = {
        "apply_refinements": [],
        "apply_refinements_and_reexecute": [],
    }

    def fake_apply(
        spec_id: uuid.UUID,
        changes: list[dict],
        user: Any | None = None,
    ) -> None:
        calls["apply_refinements"].append((spec_id, changes, user))

    def fake_apply_reexecute(
        run_id: uuid.UUID,
        changes: list[dict],
        user: Any | None = None,
    ) -> None:
        calls["apply_refinements_and_reexecute"].append((run_id, changes, user))

    monkeypatch.setattr(workflow, "apply_refinements", fake_apply)
    monkeypatch.setattr(
        workflow, "apply_refinements_and_reexecute", fake_apply_reexecute
    )
    return calls


def _changes() -> list[dict]:
    return [
        {
            "column_id": str(uuid.uuid4()),
            "field": "polars_expression",
            "new_value": "pl.col('date').dt.month()",
        }
    ]


def test_mapping_chat_apply_posts_to_dedicated_endpoint(
    client: TestClient,
    apply_targets: dict[str, list],
) -> None:
    """The mapping apply must hit /chat/apply, not the bare page URL."""
    spec_id = uuid.uuid4()
    changes = _changes()
    response = client.post(
        f"/mapping/{spec_id}/chat/apply",
        data={"changes_json": json.dumps(changes)},
    )

    assert response.status_code in (200, 303), response.text
    assert len(apply_targets["apply_refinements"]) == 1
    call_spec_id, call_changes, _user = apply_targets["apply_refinements"][0]
    assert call_spec_id == spec_id
    assert call_changes == changes


def test_results_chat_apply_posts_to_dedicated_endpoint(
    client: TestClient,
    apply_targets: dict[str, list],
) -> None:
    """The results (CSV) apply must hit /chat/apply, not the bare page URL."""
    run_id = uuid.uuid4()
    changes = _changes()
    response = client.post(
        f"/results/{run_id}/chat/apply",
        data={"changes_json": json.dumps(changes)},
    )

    assert response.status_code in (200, 303), response.text
    assert len(apply_targets["apply_refinements_and_reexecute"]) == 1
    call = apply_targets["apply_refinements_and_reexecute"][0]
    call_run_id, call_changes, _user = call
    assert call_run_id == run_id
    assert call_changes == changes


def test_bare_post_to_results_page_has_no_fallback(
    client: TestClient,
) -> None:
    """A native POST to /results/{run_id} is still a 405.

    This was the original failure mode. The Apply form must target the
    /chat/apply endpoint instead of relying on a page-level fallback.
    """
    response = client.post(f"/results/{uuid.uuid4()}", data={"changes_json": "[]"})
    assert response.status_code == 405


def test_bare_post_to_mapping_page_no_longer_applies_changes(
    client: TestClient,
    apply_targets: dict[str, list],
) -> None:
    """The old mapping fallback no longer applies refinements."""
    response = client.post(
        f"/mapping/{uuid.uuid4()}",
        data={"changes_json": json.dumps(_changes())},
    )

    assert response.status_code in (200, 303), response.text
    assert apply_targets["apply_refinements"] == []


def test_chat_diff_renders_htmx_apply_form() -> None:
    """The Apply control must be a real HTMX form, not a JS-built one."""
    import ui

    template = ui.templates.env.get_template("partials/chat_diff.html")
    proposals = [
        {
            "column_id": str(uuid.uuid4()),
            "target_table": "records",
            "target_column": "reporting_period",
            "field": "polars_expression",
            "old_value": "pl.col('date')",
            "new_value": "pl.col('date').dt.month()",
            "reason": "Derive the period from the date",
        }
    ]

    for page, apply_url in [
        ("mapping", "/mapping/spec-1/chat/apply"),
        ("results", "/results/run-1/chat/apply"),
    ]:
        html = template.render(
            proposals=proposals,
            feedback="Fix the period",
            page=page,
            spec_id="spec-1" if page == "mapping" else None,
            run_id="run-1" if page == "results" else None,
            sidebar_id=page,
        )

        assert f'hx-post="{apply_url}"' in html
        assert f'action="{apply_url}"' in html
        assert 'name="changes_json"' in html
        assert "document.createElement('form')" not in html
        assert "htmx.trigger" not in html
