"""Add base_url column to user_judge_settings.

Revision ID: 011
Revises: 010
"""

from alembic import op
import sqlalchemy as sa

revision = "011"
down_revision = "010"


def upgrade() -> None:
    op.add_column(
        "user_judge_settings",
        sa.Column("base_url", sa.String(500), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("user_judge_settings", "base_url")
