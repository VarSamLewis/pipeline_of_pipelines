"""Add user-owned Google Drive read connections and import audit trail.

Revision ID: 0004_google_drive_read_import
Revises: 0002_context_chunks
"""

import sqlalchemy as sa
from alembic import op

revision = "0004_google_drive_read_import"
down_revision = "0002_context_chunks"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # The baseline bootstraps existing installations from current ORM metadata,
    # which may already include these tables.
    if sa.inspect(op.get_bind()).has_table("googleconnection", schema="pipeline"):
        return
    op.create_table(
        "googleconnection",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "user_id", sa.Uuid(), sa.ForeignKey("user.id"), nullable=False, unique=True
        ),
        sa.Column("google_account_id", sa.String(), nullable=False),
        sa.Column("google_email", sa.String(), nullable=True),
        sa.Column("granted_scopes", sa.JSON(), nullable=False),
        sa.Column("encrypted_refresh_token", sa.Text(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("revoked_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_googleconnection_user_id", "googleconnection", ["user_id"])
    op.create_index(
        "ix_googleconnection_google_account_id",
        "googleconnection",
        ["google_account_id"],
    )
    op.create_table(
        "googledriveimport",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("user_id", sa.Uuid(), sa.ForeignKey("user.id"), nullable=False),
        sa.Column(
            "connection_id",
            sa.Uuid(),
            sa.ForeignKey("googleconnection.id"),
            nullable=False,
        ),
        sa.Column("client_id", sa.Uuid(), sa.ForeignKey("client.id"), nullable=False),
        sa.Column("raw_file_id", sa.Uuid(), sa.ForeignKey("rawfile.id"), nullable=True),
        sa.Column("drive_file_id", sa.String(), nullable=False),
        sa.Column("drive_mime_type", sa.String(), nullable=False),
        sa.Column("drive_modified_time", sa.DateTime(), nullable=True),
        sa.Column("imported_checksum", sa.String(), nullable=True),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("error_code", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
    )
    for column in ("user_id", "connection_id", "client_id", "drive_file_id"):
        op.create_index(f"ix_googledriveimport_{column}", "googledriveimport", [column])


def downgrade() -> None:
    op.drop_table("googledriveimport")
    op.drop_table("googleconnection")
