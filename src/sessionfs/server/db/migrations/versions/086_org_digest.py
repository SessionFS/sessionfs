"""Weekly org activity digest (GitHub #97).

Revision ID: 086
Revises: 083

Adds the per-member opt-out flag and the idempotency ledger for the weekly
org activity digest email. ``org_digest_runs`` is keyed on
``(org_id, week_start)`` so re-running a sweep for an already-sent week is a
no-op; ``week_start`` is the Monday 00:00 UTC that opens the 7-day window.
``recipient_count`` is the intended recipient count recorded at reservation;
``sent_count`` is the actual delivered count recorded after sending.

Downgrade drops both.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "086"
down_revision = "085"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "org_members",
        sa.Column(
            "digest_opt_out",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    op.create_table(
        "org_digest_runs",
        sa.Column(
            "org_id",
            sa.String(64),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("week_start", sa.Date(), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("recipient_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("sent_count", sa.Integer(), nullable=False, server_default="0"),
        sa.PrimaryKeyConstraint("org_id", "week_start"),
    )


def downgrade() -> None:
    op.drop_table("org_digest_runs")
    op.drop_column("org_members", "digest_opt_out")
