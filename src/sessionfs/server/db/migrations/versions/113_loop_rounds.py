"""The review-round number on agent runs (Review Loop R3, track D3).

Revision ID: 113
Revises: 112

Additive only — ``agent_runs.review_round`` is nullable so existing rows are
untouched. A reviewer run created by ``record_review_round`` stamps the loop
round it answered; ``(work_queue_item_id, review_round)`` is the idempotency
key for that endpoint, so a retried round reuses the same run instead of
creating a duplicate verdict.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "113"
down_revision = "112"
branch_labels = None
depends_on = None

_TABLE = "agent_runs"


def upgrade() -> None:
    op.add_column(_TABLE, sa.Column("review_round", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.execute(sa.text("ALTER TABLE agent_runs DROP COLUMN review_round"))
