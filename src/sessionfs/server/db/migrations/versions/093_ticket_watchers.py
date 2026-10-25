"""Ticket watchers, notifications, watch cursors and notification settings.

Revision ID: 093
Revises: 092

Four tables for the "ticket watchers" feature (notify people — and their
agents — when a ticket they follow changes hands):

- ``ticket_watchers`` — one row per (ticket, user). The composite PK is
  also the unique key. ``source`` is creator|assignee|commenter|reviewer|
  explicit; ``muted`` suppresses notifications without dropping the row.
- ``ticket_notifications`` — server-generated, per-user notifications.
  ``id`` is a monotonic BigInteger cursor. ``summary`` is server-written
  and never copies comment bodies. ``actor_type`` is user|service_key.
  ``project_id`` is denormalized so reads/delivery re-check isolation.
  Indexed on (user_id, id) for the delta cursor and (user_id, read_at)
  for the unread inbox.
- ``ticket_watch_cursors`` — (user_id, persona_name) → last-seen
  notification id. ``persona_name`` is the empty-string sentinel for the
  human's own cursor.
- ``user_notification_settings`` — per-user ``ticket_email`` preference
  (off | your_turn | all, default your_turn).

Downgrade drops the four tables in reverse dependency order.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "093"
down_revision = "092"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "ticket_watchers",
        sa.Column(
            "ticket_id",
            sa.String(64),
            sa.ForeignKey("tickets.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "user_id",
            sa.String(36),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("persona_name", sa.String(50), nullable=True),
        sa.Column("source", sa.String(20), nullable=False, server_default="explicit"),
        sa.Column("muted", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Index("idx_ticket_watchers_ticket", "ticket_id"),
        sa.Index("idx_ticket_watchers_user", "user_id"),
    )
    op.create_table(
        "ticket_notifications",
        sa.Column(
            "id",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            primary_key=True,
            autoincrement=True,
        ),
        sa.Column(
            "user_id",
            sa.String(36),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "ticket_id",
            sa.String(64),
            sa.ForeignKey("tickets.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "project_id",
            sa.String(64),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("actor_type", sa.String(20), nullable=False, server_default="user"),
        sa.Column("actor_user_id", sa.String(36), nullable=True),
        sa.Column("service_key_name", sa.String(100), nullable=True),
        sa.Column("event_count", sa.Integer(), nullable=False, server_default="1"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("read_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("emailed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Index("idx_ticket_notifications_user_id", "user_id", "id"),
        sa.Index("idx_ticket_notifications_user_read", "user_id", "read_at"),
        sa.Index("idx_ticket_notifications_ticket", "ticket_id"),
        # Sweep/retention work off (emailed_at, read_at, updated_at): the
        # delivery sweep selects unread+un-emailed rows by age, and retention
        # deletes read/emailed rows by age, so this index serves the sweep
        # selection without a full table scan.
        sa.Index(
            "idx_ticket_notifications_sweep",
            "emailed_at",
            "read_at",
            "updated_at",
        ),
        # The retention purge splits into two bounded deletes — read-and-old
        # and emailed-and-old — so each predicate gets a dedicated leading
        # (column, updated_at) index instead of one OR'd scan.
        sa.Index(
            "idx_ticket_notifications_read_updated",
            "read_at",
            "updated_at",
        ),
        sa.Index(
            "idx_ticket_notifications_emailed_updated",
            "emailed_at",
            "updated_at",
        ),
        # Never reuse a deleted id on SQLite (coalescing deletes and re-inserts).
        sqlite_autoincrement=True,
    )
    op.create_table(
        "ticket_watch_cursors",
        sa.Column(
            "user_id",
            sa.String(36),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("persona_name", sa.String(50), primary_key=True, server_default=""),
        sa.Column(
            "last_seen_notification_id",
            sa.BigInteger(),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_table(
        "user_notification_settings",
        sa.Column(
            "user_id",
            sa.String(36),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "ticket_email", sa.String(20), nullable=False, server_default="your_turn"
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )


def downgrade() -> None:
    op.drop_table("user_notification_settings")
    op.drop_table("ticket_watch_cursors")
    op.drop_table("ticket_notifications")
    op.drop_table("ticket_watchers")
