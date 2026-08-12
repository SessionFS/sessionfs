"""Add dlp_checked + dlp_checked_at to share_links for public view DLP gate.

Revision ID: 062
Revises: 061

Strictly additive — two nullable columns.  dlp_checked is NULL until
first access (scan-on-first-access + cache pattern); once stamped it is
either true (passed) or false (blocked — secrets found).  dlp_checked_at
records the timestamp of the check.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "062"
down_revision = "061"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "share_links",
        sa.Column("dlp_checked", sa.Boolean(), nullable=True),
    )
    op.add_column(
        "share_links",
        sa.Column("dlp_checked_etag", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "share_links",
        sa.Column("dlp_checked_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("share_links", "dlp_checked_at")
    op.drop_column("share_links", "dlp_checked_etag")
    op.drop_column("share_links", "dlp_checked")
