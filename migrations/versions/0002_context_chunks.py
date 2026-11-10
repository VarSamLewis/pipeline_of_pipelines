"""Version client evidence chunks and pin context used in mappings.

Revision ID: 0002_context_chunks
Revises: 0001_legacy_schema
"""

from __future__ import annotations

from alembic import op

revision = "0002_context_chunks"
down_revision = "0001_legacy_schema"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # IF NOT EXISTS also supports new installations bootstrapped from current models.
    op.execute(
        "ALTER TABLE pipeline.extractedevidence "
        "ADD COLUMN IF NOT EXISTS active BOOLEAN NOT NULL DEFAULT TRUE"
    )
    op.execute(
        "ALTER TABLE pipeline.extractedevidence "
        "ADD COLUMN IF NOT EXISTS superseded_by_id UUID"
    )
    op.execute(
        "ALTER TABLE pipeline.extractedevidence "
        "ADD COLUMN IF NOT EXISTS superseded_at TIMESTAMP"
    )
    op.execute(
        "ALTER TABLE pipeline.extractedevidence "
        "ADD COLUMN IF NOT EXISTS embedding_model VARCHAR"
    )
    op.execute(
        "ALTER TABLE pipeline.mappingspec "
        "ADD COLUMN IF NOT EXISTS context_evidence_ids JSON NOT NULL DEFAULT '[]'"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_extractedevidence_active "
        "ON pipeline.extractedevidence (active)"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint "
        "WHERE conname = 'extractedevidence_superseded_by_id_fkey') THEN "
        "ALTER TABLE pipeline.extractedevidence "
        "ADD CONSTRAINT extractedevidence_superseded_by_id_fkey "
        "FOREIGN KEY (superseded_by_id) "
        "REFERENCES pipeline.extractedevidence(id); "
        "END IF; END $$"
    )


def downgrade() -> None:
    raise RuntimeError("Chunk history is append-only; downgrade is not supported")
