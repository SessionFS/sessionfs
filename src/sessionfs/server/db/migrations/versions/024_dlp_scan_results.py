"""Add dlp_scan_results column to sessions.

Revision ID: 024
Revises: 023
"""

from alembic import op
import sqlalchemy as sa

revision = "024"
down_revision = "023"


def upgrade() -> None:
    op.add_column(
        "sessions",
        sa.Column("dlp_scan_results", sa.Text, nullable=True),
    )


def downgrade() -> None:
    op.drop_column("sessions", "dlp_scan_results")
