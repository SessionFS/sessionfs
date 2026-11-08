"""Keep a rotated-out resident key in a work item's implementer identity.

Revision ID: 125
Revises: 124

Additive only. ``work_queue_items.prior_implementer_service_key_ids`` (JSON
list as text, nullable) records every service key an in-flight item's
implementer identity was moved away from when its resident was rebound to a
new key (``POST /orgs/{org}/residents/{id}/rotate-key``). The self-review
check takes the union of the current and prior keys, so the people who
minted the old key never become independent reviewers of that work just
because the resident moved to an unrelated key.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "125"
down_revision = "124"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("work_queue_items") as batch:
        batch.add_column(
            sa.Column("prior_implementer_service_key_ids", sa.Text(), nullable=True)
        )


def downgrade() -> None:
    with op.batch_alter_table("work_queue_items") as batch:
        batch.drop_column("prior_implementer_service_key_ids")
