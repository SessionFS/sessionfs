"""Meeting action items keep the model's owner text when no participant matches.

Revision ID: 121
Revises: 120

- ``meeting_action_items.owner_hint``: nullable ``VARCHAR(200)``. Extraction
  used to drop an item whose owner named no participant; it now keeps the item
  with no resolved owner and stores the (DLP-redacted) owner text here as a
  hint for the person who assigns it. Never used to resolve an assignee.

Additive and nullable, so existing rows need no backfill.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "121"
down_revision = "120"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "meeting_action_items",
        sa.Column("owner_hint", sa.String(200), nullable=True),
    )


def downgrade() -> None:
    with op.batch_alter_table("meeting_action_items") as batch:
        batch.drop_column("owner_hint")
