"""Durable markers for work that used to run only after the response.

Revision ID: 138
Revises: 137

``deferred_jobs`` holds one row per (kind, subject_id) for jobs that the API
used to run on a detached task after the response was sent (merge check
rechecks, org-wide merge rechecks, knowledge extraction after a push, the
knowledge backfill of a new project). With CPU billed only during requests
those tasks may never run, so the request now writes this row and an existing
scheduled sweep drains it (tk_e3eecad8c3874d92). New table only; nothing
earlier creates this name.

"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "138"
down_revision = "137"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "deferred_jobs",
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("subject_id", sa.String(64), nullable=False),
        sa.Column("generation", sa.BigInteger(), nullable=False, server_default="1"),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("claim_token", sa.String(32), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_error", sa.String(200), nullable=True),
        sa.Column("payload", sa.Text(), nullable=True),
        sa.Column("cursor", sa.String(64), nullable=True),
        sa.PrimaryKeyConstraint("kind", "subject_id", name="pk_deferred_jobs"),
    )
    op.create_index(
        "idx_deferred_jobs_kind_requested", "deferred_jobs", ["kind", "requested_at"]
    )


def downgrade() -> None:
    op.drop_index("idx_deferred_jobs_kind_requested", table_name="deferred_jobs")
    op.drop_table("deferred_jobs")
