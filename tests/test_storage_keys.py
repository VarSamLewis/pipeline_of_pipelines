"""Storage-key layout regression tests."""

from datetime import UTC, datetime

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
    )

    assert key == (
        "organizations/acme-ltd--organization-id/"
        "clients/orders-emea--client-id/raw/march-orders_20261025T143012123456Z.csv"
    )
