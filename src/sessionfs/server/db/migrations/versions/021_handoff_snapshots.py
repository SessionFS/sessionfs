"""Add snapshot fields to handoffs for metadata immutability.

Revision ID: 021
Revises: 020
"""

from alembic import op
import sqlalchemy as sa

revision = "021"
down_revision = "020"


def upgrade() -> None:
    op.add_column("handoffs", sa.Column("snapshot_title", sa.String(500), nullable=True))
    op.add_column("handoffs", sa.Column("snapshot_tool", sa.String(100), nullable=True))
    op.add_column("handoffs", sa.Column("snapshot_model_id", sa.String(200), nullable=True))
    op.add_column("handoffs", sa.Column("snapshot_message_count", sa.Integer, nullable=True))
    op.add_column("handoffs", sa.Column("snapshot_total_tokens", sa.BigInteger, nullable=True))

    # Backfill existing rows from their linked session (SQLite + PostgreSQL compatible)
    op.execute(sa.text("""
        UPDATE handoffs SET
            snapshot_title = (SELECT title FROM sessions WHERE sessions.id = handoffs.session_id),
            snapshot_tool = (SELECT source_tool FROM sessions WHERE sessions.id = handoffs.session_id),
            snapshot_model_id = (SELECT model_id FROM sessions WHERE sessions.id = handoffs.session_id),
            snapshot_message_count = (SELECT message_count FROM sessions WHERE sessions.id = handoffs.session_id),
            snapshot_total_tokens = (
                SELECT COALESCE(total_input_tokens, 0) + COALESCE(total_output_tokens, 0)
                FROM sessions WHERE sessions.id = handoffs.session_id
            )
        WHERE snapshot_title IS NULL
          AND EXISTS (SELECT 1 FROM sessions WHERE sessions.id = handoffs.session_id)
    """))


def downgrade() -> None:
    op.drop_column("handoffs", "snapshot_total_tokens")
    op.drop_column("handoffs", "snapshot_message_count")
    op.drop_column("handoffs", "snapshot_model_id")
    op.drop_column("handoffs", "snapshot_tool")
    op.drop_column("handoffs", "snapshot_title")
