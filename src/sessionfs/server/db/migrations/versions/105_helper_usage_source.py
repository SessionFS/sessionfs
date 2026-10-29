"""Tag helper usage ledger rows with their source (GitHub #116, M1b).

Revision ID: 105
Revises: 104

Additive only.

- helper_usage.source: what wrote the ledger row. ``helper`` is the existing
  session-helper turn path (and the backfill default for every row written
  before this migration); ``meeting_extract`` is the meeting action-item
  extraction call, so org monthly budgets and usage summaries can tell the
  two apart without changing either budget query (both still charge the org
  key exactly as before). A CHECK (ck_helper_usage_source) limits the column
  to those two values.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "105"
down_revision = "104"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("helper_usage") as batch:
        batch.add_column(
            sa.Column(
                "source",
                sa.String(32),
                nullable=False,
                server_default=sa.text("'helper'"),
            )
        )
        batch.create_check_constraint(
            "ck_helper_usage_source", "source IN ('helper', 'meeting_extract')"
        )


def downgrade() -> None:
    with op.batch_alter_table("helper_usage") as batch:
        batch.drop_constraint("ck_helper_usage_source", type_="check")
        batch.drop_column("source")
