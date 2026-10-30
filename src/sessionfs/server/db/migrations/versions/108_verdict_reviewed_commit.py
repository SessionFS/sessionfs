"""Bind trusted review verdicts to the exact commit they reviewed (Review Loop B2).

Revision ID: 108
Revises: 107

Additive only.

- ticket_comments.reviewed_commit: nullable hex SHA of the head commit a
  trusted review verdict reviewed. A VERIFIED-CLEAN verdict only counts for
  merge readiness when it is bound to the current head, so a verdict on a
  moved SHA no longer marks a branch ready. Only review verdict rows populate
  it; every other comment leaves it NULL.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "108"
down_revision = "107"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("ticket_comments") as batch:
        batch.add_column(
            sa.Column("reviewed_commit", sa.String(64), nullable=True)
        )


def downgrade() -> None:
    with op.batch_alter_table("ticket_comments") as batch:
        batch.drop_column("reviewed_commit")
