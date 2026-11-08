"""Indexes for merge-check lookups and the delivery-event retention sweep.

Revision ID: 127
Revises: 126

Additive only.
- ``idx_pull_requests_project_head`` on pull_requests (project_id, head_sha):
  merge checks and GitLab commit statuses look PRs up by head sha.
- ``idx_delivery_webhook_events_received`` on delivery_webhook_events
  (received_at): the GitHub webhook-delivery sweep now also prunes the
  delivery idempotency ledger past its retention window.
"""

from __future__ import annotations

from alembic import op

revision = "127"
down_revision = "126"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "idx_pull_requests_project_head", "pull_requests", ["project_id", "head_sha"]
    )
    op.create_index(
        "idx_delivery_webhook_events_received",
        "delivery_webhook_events",
        ["received_at"],
    )


def downgrade() -> None:
    op.drop_index("idx_delivery_webhook_events_received", table_name="delivery_webhook_events")
    op.drop_index("idx_pull_requests_project_head", table_name="pull_requests")
