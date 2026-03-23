"""Add full-text search support.

Revision ID: 003
Revises: 002
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "003"
down_revision: Union[str, None] = "002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "sessions",
        sa.Column("messages_text", sa.Text(), server_default="", nullable=False),
    )
    # PostgreSQL-specific: tsvector column + GIN index
    op.execute("""
        ALTER TABLE sessions ADD COLUMN search_vector tsvector
        GENERATED ALWAYS AS (
            to_tsvector('english', coalesce(title, '') || ' ' || coalesce(messages_text, ''))
        ) STORED
    """)
    op.execute("CREATE INDEX idx_sessions_search ON sessions USING GIN(search_vector)")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_sessions_search")
    op.execute("ALTER TABLE sessions DROP COLUMN IF EXISTS search_vector")
    op.drop_column("sessions", "messages_text")
