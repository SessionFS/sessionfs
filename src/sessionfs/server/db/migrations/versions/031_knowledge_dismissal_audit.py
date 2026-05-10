"""Add dismissal audit columns to knowledge_entries.

Adds dismissed_at, dismissed_by, dismissed_reason so dismissals carry
provenance and can be reviewed. Existing rows where dismissed=true keep
NULL audit fields — there's no source of truth for who dismissed them
historically. New dismissals via the dismiss endpoint / MCP tool will
populate all three.

Revision ID: 031
Revises: 030
"""

from alembic import op
import sqlalchemy as sa

revision = "031"
down_revision = "030"


def upgrade() -> None:
    with op.batch_alter_table("knowledge_entries") as batch_op:
        batch_op.add_column(
            sa.Column("dismissed_at", sa.DateTime(timezone=True), nullable=True)
        )
        batch_op.add_column(
            sa.Column("dismissed_by", sa.String(64), nullable=True)
        )
        batch_op.add_column(
            sa.Column("dismissed_reason", sa.Text, nullable=True)
        )


def downgrade() -> None:
    with op.batch_alter_table("knowledge_entries") as batch_op:
        batch_op.drop_column("dismissed_reason")
        batch_op.drop_column("dismissed_by")
        batch_op.drop_column("dismissed_at")
