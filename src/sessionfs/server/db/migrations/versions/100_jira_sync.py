"""Jira two-way sync + completion evidence (GitHub #98, part B).

Revision ID: 100
Revises: 099

Adds the schema for two-way Jira sync:

- ``tracker_sync_events``: the append-only ledger of inbound/outbound sync
  actions. One row per applied (or failed) status transition, field edit,
  comment push, completion-evidence push, or error. ``jira_event_id`` is
  nullable and unique *per link* (idempotency for inbound webhooks); outbound
  events leave it NULL. ``payload`` is a JSON string bounded by the route
  writer, not by the schema (SQLite + PostgreSQL share ``Text``).
- ``org_tracker_connections`` gains the webhook secret (Fernet-encrypted, so
  the inbound HMAC can be verified; ``webhook_secret_hash`` + ``last4`` are
  the write-only display/integrity hints) and the sync toggles: ``sync_status``
  (two-way status sync), ``sync_comments`` (``off|inbound|outbound|both``),
  and ``status_map`` (JSON: sessionfs status → Jira transition name, and Jira
  status category → sessionfs action).
- ``tracker_sync_events`` doubles as the durable outbound outbox: ``state``
  (``pending`` | ``done`` | ``exhausted`` | ``skipped``), ``retry_count`` and
  ``next_attempt_at`` drive the drain/sweep with exponential backoff (max 5
  attempts); ``dedupe_key`` (unique per link) claims one completion-evidence
  row per completion so concurrent jobs can never double-post.
- ``org_tracker_connections.jira_account_id`` persists the connection's own
  Atlassian accountId so inbound webhooks can drop echoes of our own writes;
  ``webhook_auth_failure_count`` / ``_window_start`` / ``last_..._at`` count
  rejected (bad-signature) deliveries in a rolling 24 h window so health can
  report them without one audit row per unauthenticated request.
- ``ticket_tracker_links`` gains ``last_synced_at`` / ``last_inbound_at`` for
  the health surface.
- ``ticket_comments`` gains ``source`` (``sessionfs`` default; ``jira`` for
  inbound comment_created events).

Purely additive apart from the ``source`` backfill default. Downgrade drops
the new columns and the new table.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "100"
down_revision = "099"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "tracker_sync_events",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("org_id", sa.String(length=64), nullable=False),
        sa.Column("link_id", sa.String(length=64), nullable=False),
        sa.Column("ticket_id", sa.String(length=64), nullable=False),
        sa.Column("direction", sa.String(length=10), nullable=False),
        sa.Column("kind", sa.String(length=20), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("jira_event_id", sa.String(length=120), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("applied", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("state", sa.String(length=12), nullable=False, server_default="done"),
        sa.Column("retry_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("dedupe_key", sa.String(length=160), nullable=True),
        sa.ForeignKeyConstraint(["org_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["link_id"], ["ticket_tracker_links.ticket_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("link_id", "jira_event_id", name="uq_tracker_sync_event_jira_id"),
        sa.UniqueConstraint("link_id", "dedupe_key", name="uq_tracker_sync_event_dedupe"),
    )
    op.create_index("idx_tracker_sync_events_org_created", "tracker_sync_events", ["org_id", "created_at"])
    op.create_index("idx_tracker_sync_events_link", "tracker_sync_events", ["link_id"])
    op.create_index(
        "idx_tracker_sync_events_due", "tracker_sync_events", ["state", "next_attempt_at"]
    )

    op.add_column(
        "org_tracker_connections", sa.Column("encrypted_webhook_secret", sa.Text(), nullable=True)
    )
    op.add_column(
        "org_tracker_connections", sa.Column("webhook_secret_hash", sa.String(length=64), nullable=True)
    )
    op.add_column(
        "org_tracker_connections", sa.Column("webhook_secret_last4", sa.String(length=4), nullable=True)
    )
    op.add_column(
        "org_tracker_connections",
        sa.Column("sync_status", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )
    op.add_column(
        "org_tracker_connections",
        sa.Column("sync_comments", sa.String(length=20), nullable=False, server_default="off"),
    )
    op.add_column(
        "org_tracker_connections", sa.Column("status_map", sa.Text(), nullable=True)
    )
    op.add_column(
        "org_tracker_connections", sa.Column("jira_account_id", sa.String(length=128), nullable=True)
    )
    op.add_column(
        "org_tracker_connections",
        sa.Column("webhook_auth_failure_count", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "org_tracker_connections",
        sa.Column("webhook_auth_failure_window_start", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "org_tracker_connections",
        sa.Column("last_webhook_auth_failure_at", sa.DateTime(timezone=True), nullable=True),
    )

    op.add_column(
        "ticket_tracker_links", sa.Column("last_synced_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "ticket_tracker_links", sa.Column("last_inbound_at", sa.DateTime(timezone=True), nullable=True)
    )

    op.add_column(
        "ticket_comments",
        sa.Column("source", sa.String(length=20), nullable=False, server_default="sessionfs"),
    )


def downgrade() -> None:
    op.drop_column("ticket_comments", "source")
    op.drop_column("ticket_tracker_links", "last_inbound_at")
    op.drop_column("ticket_tracker_links", "last_synced_at")
    op.drop_column("org_tracker_connections", "last_webhook_auth_failure_at")
    op.drop_column("org_tracker_connections", "webhook_auth_failure_window_start")
    op.drop_column("org_tracker_connections", "webhook_auth_failure_count")
    op.drop_column("org_tracker_connections", "jira_account_id")
    op.drop_column("org_tracker_connections", "status_map")
    op.drop_column("org_tracker_connections", "sync_comments")
    op.drop_column("org_tracker_connections", "sync_status")
    op.drop_column("org_tracker_connections", "webhook_secret_last4")
    op.drop_column("org_tracker_connections", "webhook_secret_hash")
    op.drop_column("org_tracker_connections", "encrypted_webhook_secret")
    op.drop_index("idx_tracker_sync_events_due", table_name="tracker_sync_events")
    op.drop_index("idx_tracker_sync_events_link", table_name="tracker_sync_events")
    op.drop_index("idx_tracker_sync_events_org_created", table_name="tracker_sync_events")
    op.drop_table("tracker_sync_events")
