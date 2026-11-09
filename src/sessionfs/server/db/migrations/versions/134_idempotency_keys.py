"""Idempotency-Key reservations and stored responses (tk_bd80efd511da4a2b).

Revision ID: 134
Revises: 133

``idempotency_keys`` holds one row per (credential, method, route template,
Idempotency-Key). The primary key ``scope_hash`` is a sha256 over those (the
raw credential is never stored). A row is ``in_progress`` while its request
runs (fenced by ``lease_token`` until ``locked_until``) and ``completed``
with the stored response afterwards. Rows are swept after 24 hours by
``POST /api/v1/admin/sweeps/idempotency-keys``, which scans ``created_at``.
New table only; nothing earlier creates these names.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "134"
down_revision = "133"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "idempotency_keys",
        sa.Column("scope_hash", sa.String(64), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("lease_token", sa.String(32), nullable=True),
        sa.Column("response_status", sa.Integer(), nullable=True),
        sa.Column("response_headers", sa.Text(), nullable=True),
        sa.Column("response_body", sa.LargeBinary(), nullable=True),
        sa.Column("replayable", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("locked_until", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("scope_hash", name="pk_idempotency_keys"),
        sa.CheckConstraint(
            "status IN ('in_progress', 'completed')", name="ck_idempotency_keys_status"
        ),
        # The 24-hour retention sweep scans by created_at.
        sa.Index("idx_idempotency_keys_created", "created_at"),
    )


def downgrade() -> None:
    op.drop_table("idempotency_keys")
