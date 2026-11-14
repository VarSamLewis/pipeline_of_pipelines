"""Baseline existing installations and bootstrap new databases.

Revision ID: 0001_legacy_schema
Revises:
"""

from __future__ import annotations

from alembic import op
from db_ops import create_tables

revision = "0001_legacy_schema"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # The pre-migration bootstrap handles legacy enums, columns, and data backfills.
    # Keep it frozen until all pre-Alembic installations have been upgraded.
    create_tables(op.get_bind().engine)


def downgrade() -> None:
    raise RuntimeError("Downgrading the baseline would destroy existing client data")
