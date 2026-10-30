"""Store review-loop config on the parent issue ticket (Review Loop D1).

Revision ID: 110
Revises: 109

Additive only.

- tickets.loop_config: nullable JSON-as-Text holding the runtime config of a
  review loop, populated ONLY on the loop's parent issue ticket:

      {
        "queue_id": "wq_...",
        "reviewer_persona": "reviewer",
        "token_budget": 200000,        # optional
        "cost_budget_usd": 12.5,       # optional (admin-only)
        "max_review_rounds": 3,        # optional (no default)
        "checklist_version": 7         # optional
      }

  Every other ticket leaves it NULL. No new table — a loop IS a parent
  issue ticket (docs/design/review-loop-r3.md §4).

- work_queues actor provenance (review checklist #3): ``actor_type``,
  ``service_key_id``, ``service_key_name`` so a review-loop queue stood up
  by a service key records the key's identity (same shape as ``tickets``).
  ``created_by_user_id`` stays the key's backing user.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "110"
down_revision = "109"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("tickets") as batch:
        batch.add_column(sa.Column("loop_config", sa.Text(), nullable=True))
    with op.batch_alter_table("work_queues") as batch:
        batch.add_column(
            sa.Column(
                "actor_type",
                sa.String(length=20),
                nullable=False,
                server_default="user",
            )
        )
        batch.add_column(
            sa.Column("service_key_id", sa.String(length=36), nullable=True)
        )
        batch.add_column(
            sa.Column("service_key_name", sa.String(length=100), nullable=True)
        )


def downgrade() -> None:
    with op.batch_alter_table("work_queues") as batch:
        batch.drop_column("service_key_name")
        batch.drop_column("service_key_id")
        batch.drop_column("actor_type")
    with op.batch_alter_table("tickets") as batch:
        batch.drop_column("loop_config")
