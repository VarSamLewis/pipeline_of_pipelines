"""Tests for source diagnostics on the mapping review page."""

from __future__ import annotations

import uuid

from fastapi.testclient import TestClient


def test_mapping_review_surfaces_catalog_warnings(
    monkeypatch,
) -> None:
    """Users should see parse warnings before the approval control."""
    import ui
    from app import app

    monkeypatch.setattr(
        ui,
        "get_mapping_review",
        lambda spec_id: {
            "id": str(spec_id),
            "columns": [],
            "source_catalogs": [
                {
                    "original_filename": "clients.csv",
                    "warnings": [
                        {
                            "code": "encoding_fallback",
                            "message": "Decoded using Windows-1252.",
                            "severity": "warning",
                        }
                    ],
                    "tables": [
                        {
                            "source_table_id": "table-1",
                            "display_name": "clients.csv",
                            "original_filename": "clients.csv",
                            "row_count": 2,
                            "confidence": 0.8,
                            "location": {
                                "sheet_name": None,
                                "cell_range": None,
                                "header_row": 2,
                            },
                            "warnings": [
                                {
                                    "code": "preamble_detected",
                                    "message": "Detected one title row.",
                                    "severity": "warning",
                                }
                            ],
                        }
                    ],
                }
            ],
        },
    )

    response = TestClient(app).get(f"/mapping/{uuid.uuid4()}")

    assert response.status_code == 200
    assert "Discovered source tables" in response.text
    assert "encoding_fallback" in response.text
    assert "preamble_detected" in response.text
    assert response.text.index("Parsing warnings") < response.text.index(
        "Confirm &amp; run pipeline"
        if "Confirm &amp; run pipeline" in response.text
        else "Confirm & run pipeline"
    )
