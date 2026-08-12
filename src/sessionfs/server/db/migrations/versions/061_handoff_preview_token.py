"""Handoff preview token — unauthenticated recipient preview before signup.

Revision ID: 061
Revises: 059

Strictly additive: one nullable column `preview_token_hash` on `handoffs`
for the P1 pre-signup landing page. Raw token goes only into the recipient
email link (never stored); sha256 at rest. Nullable — existing rows stay
NULL and constant-404 on the preview endpoint.

Works on both SQLite and PostgreSQL (plain ALTER … ADD COLUMN).
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "061"
down_revision = "060"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "handoffs",
        sa.Column("preview_token_hash", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "handoffs",
        sa.Column("preview_snapshot", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("handoffs", "preview_snapshot")
    op.drop_column("handoffs", "preview_token_hash")
