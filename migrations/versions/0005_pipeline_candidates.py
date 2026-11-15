"""Track bounded candidate previews and human approval.

Revision ID: 0005_pipeline_candidates
Revises: 0004_google_drive_read_import
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0005_pipeline_candidates"
down_revision = "0004_google_drive_read_import"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # The baseline creates all current SQLModel tables on fresh installations.
    if sa.inspect(op.get_bind()).has_table("pipelinecandidate", schema="pipeline"):
        return
    op.create_table(
        "pipelinecandidate",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("mapping_spec_id", sa.Uuid(), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("mode", sa.String(), nullable=False),
        sa.Column("plan", sa.JSON(), nullable=False),
        sa.Column("script", sa.Text(), nullable=False),
        sa.Column("source_hashes", sa.JSON(), nullable=False),
        sa.Column("result", sa.JSON(), nullable=False),
        sa.Column("approved_by", sa.String()),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("approved_at", sa.DateTime()),
        sa.ForeignKeyConstraint(["mapping_spec_id"], ["mappingspec.id"]),
        sa.UniqueConstraint(
            "mapping_spec_id", "attempt", name="uq_pipelinecandidate_spec_attempt"
        ),
        schema="pipeline",
    )
    op.create_index(
        "ix_pipelinecandidate_mapping_spec_id",
        "pipelinecandidate",
        ["mapping_spec_id"],
        schema="pipeline",
    )


def downgrade() -> None:
    raise RuntimeError("Candidate review history must not be discarded")
