"""Storage-key layout regression tests."""

from datetime import UTC, datetime
from uuid import UUID

from file_ops import build_storage_key


def test_raw_upload_key_groups_files_by_organization_and_client() -> None:
    """Raw uploads receive readable, timestamped, collision-resistant keys."""
    key = build_storage_key(
        "Acme Ltd",
        "organization-id",
        "Orders / EMEA",
        "client-id",
        "March Orders.csv",
        datetime(2026, 10, 25, 14, 30, 12, 123456, tzinfo=UTC),
        UUID("00000000-0000-0000-0000-000000000001"),
    )

    assert key == (
        "organizations/acme-ltd--organization-id/"
        "clients/orders-emea--client-id/raw/"
        "march-orders_20261025T143012123456Z_00000000-0000-0000-0000-000000000001.csv"
    )


def test_repeated_uploads_get_distinct_keys_even_at_same_instant() -> None:
    instant = datetime(2026, 10, 25, 14, 30, 12, tzinfo=UTC)
    first = build_storage_key("Org", "org", "Client", "client", "data.csv", instant)
    second = build_storage_key("Org", "org", "Client", "client", "data.csv", instant)
    assert first != second
    assert "20261025T143012000000Z" in first
