"""Unit tests for mapping proposal normalization."""

from __future__ import annotations

from mapping import resolve_composite_keys
from models import ProposedMapping, SourceColumnRef


def test_unavailable_lookup_becomes_unmapped_field() -> None:
    """A hallucinated lookup must not reject the complete mapping proposal."""
    mapping = ProposedMapping(
        target_table="output",
        target_column="region",
        source_columns=[
            SourceColumnRef(
                source_table="Packaging Data.xlsx::Sheet1",
                source_column="site_ref",
            )
        ],
        transformation_type="lookup",
        lookup_source_table="site_locations.csv",
        lookup_key="site_ref",
        lookup_value="region",
        polars_expression="col('site_ref')",
        confidence=0.95,
    )
    source_catalogs = [
        {
            "tables": [
                {
                    "original_filename": "Packaging Data.xlsx",
                    "display_name": "Sheet1",
                    "source_table_id": "packaging-sheet",
                    "columns": [
                        {
                            "source_column_id": "site-ref",
                            "normalized_name": "site_ref",
                        }
                    ],
                }
            ]
        }
    ]

    assert resolve_composite_keys([mapping], source_catalogs) == []
    assert mapping.source_columns == []
    assert mapping.lookup_source_table is None
    assert mapping.polars_expression is None
    assert mapping.confidence == 0.0
    assert mapping.transformation_logic.startswith("Unmapped:")
