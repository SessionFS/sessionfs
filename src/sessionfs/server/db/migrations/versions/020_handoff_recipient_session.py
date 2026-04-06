"""Add recipient_session_id to handoffs.

Revision ID: 020
Revises: 019
"""

from alembic import op
import sqlalchemy as sa

revision = "020"
down_revision = "019"


def upgrade() -> None:
    op.add_column(
        "handoffs",
        sa.Column("recipient_session_id", sa.String(64), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("handoffs", "recipient_session_id")
