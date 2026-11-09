"""Per-org concurrency slots for expensive routes.

Revision ID: 136
Revises: 135

``org_concurrency_slots`` holds one row per held slot: (scope_key,
route_class, slot_no) is the primary key, so two requests can never hold the
same numbered slot. ``scope_key`` is ``org:<id>`` or ``user:<id>`` (personal
projects). Rows exist only while a request runs; ``expires_at`` lets a later
claim take over a slot whose holder died. New table only; nothing earlier
creates this name.

"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "136"
down_revision = "135"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "org_concurrency_slots",
        sa.Column("scope_key", sa.String(80), nullable=False),
        sa.Column("route_class", sa.String(16), nullable=False),
        sa.Column("slot_no", sa.Integer(), nullable=False),
        sa.Column("holder", sa.String(32), nullable=False),
        sa.Column("acquired_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint(
            "scope_key", "route_class", "slot_no", name="pk_org_concurrency_slots"
        ),
    )


def downgrade() -> None:
    op.drop_table("org_concurrency_slots")
