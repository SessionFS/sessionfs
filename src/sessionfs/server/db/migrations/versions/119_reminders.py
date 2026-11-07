"""Reminders, and ticket notifications generalised to several subjects.

Revision ID: 119
Revises: 118

- ``reminders``: one row per reminder. ``owner_user_id`` is the person being
  reminded; ``org_id``/``project_id``/``group_id`` are derived from the source
  on write and are hints only (every read re-derives access from the live
  source), so they carry no FK. ``due_at`` is UTC; ``timezone`` is an IANA
  name kept for display only.
- ``reminder_notes``: append-only notes on a reminder (CASCADE).
- ``ticket_notifications`` gains ``subject_kind`` (ticket | reminder |
  approval, server default 'ticket' so every existing row backfills) and
  ``subject_id`` (backfilled from ``ticket_id``). ``ticket_id`` and
  ``project_id`` become nullable, with a CHECK that a ticket-subject row
  still names its ticket.

SQLite rebuilds ``ticket_notifications`` through batch mode; the rebuild keeps
``sqlite_autoincrement`` so a deleted max id is never reused (the agent delta
depends on monotonic ids). No earlier migration put a CHECK on
``ticket_notifications`` (093 created it with indexes only).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "119"
down_revision = "118"
branch_labels = None
depends_on = None


_SUBJECT_KIND_CHECK = "subject_kind IN ('ticket', 'reminder', 'approval')"
_TICKET_SUBJECT_CHECK = "subject_kind <> 'ticket' OR ticket_id IS NOT NULL"


def upgrade() -> None:
    op.create_table(
        "reminders",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column(
            "owner_user_id",
            sa.String(36),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "created_by_user_id",
            sa.String(36),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("created_via", sa.String(10), nullable=False, server_default="manual"),
        # Creation actor: a service key acts as its minter (created_by_user_id)
        # but is recorded as itself too.
        sa.Column("actor_type", sa.String(10), nullable=False, server_default="user"),
        sa.Column("service_key_id", sa.String(36), nullable=True),
        sa.Column("service_key_name", sa.String(100), nullable=True),
        sa.Column("closed_by_service_key_id", sa.String(36), nullable=True),
        # Last writer (create, rename, reschedule, snooze, close).
        sa.Column(
            "updated_by_user_id",
            sa.String(36),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "updated_actor_type", sa.String(10), nullable=False, server_default="user"
        ),
        sa.Column("updated_by_service_key_id", sa.String(36), nullable=True),
        sa.Column("org_id", sa.String(64), nullable=True),
        sa.Column("project_id", sa.String(64), nullable=True),
        sa.Column("group_id", sa.String(64), nullable=True),
        sa.Column("source_kind", sa.String(20), nullable=True),
        sa.Column("source_id", sa.String(64), nullable=True),
        sa.Column("source_lines", sa.String(32), nullable=True),
        sa.Column("linked_ticket_id", sa.String(64), nullable=True),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("original_due_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("timezone", sa.String(64), nullable=True),
        sa.Column("status", sa.String(12), nullable=False, server_default="open"),
        sa.Column("notified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("snooze_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "closed_by_user_id",
            sa.String(36),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint(
            "created_via IN ('manual', 'helper', 'meeting', 'api')",
            name="ck_reminders_created_via",
        ),
        sa.CheckConstraint(
            "status IN ('open', 'done', 'dismissed')",
            name="ck_reminders_status",
        ),
        sa.CheckConstraint(
            "actor_type IN ('user', 'service')", name="ck_reminders_actor_type"
        ),
        sa.CheckConstraint(
            "updated_actor_type IN ('user', 'service')",
            name="ck_reminders_updated_actor_type",
        ),
        sa.CheckConstraint(
            "source_kind IS NULL OR source_kind IN ('meeting_item', 'meeting', "
            "'ticket', 'session', 'knowledge_entry', 'chat_item')",
            name="ck_reminders_source_kind",
        ),
        sa.CheckConstraint(
            "(source_kind IS NULL AND source_id IS NULL) "
            "OR (source_kind IS NOT NULL AND source_id IS NOT NULL)",
            name="ck_reminders_source_pair",
        ),
        sa.CheckConstraint("length(title) <= 200", name="ck_reminders_title_len"),
        sa.CheckConstraint("snooze_count >= 0", name="ck_reminders_snooze_count"),
        sa.Index("idx_reminders_owner_status_due", "owner_user_id", "status", "due_at"),
        sa.Index("idx_reminders_creator", "created_by_user_id"),
        sa.Index("idx_reminders_source", "source_kind", "source_id"),
        # The due sweep scans open, un-notified rows by due_at.
        sa.Index("idx_reminders_due_sweep", "status", "notified_at", "due_at"),
    )
    op.create_table(
        "reminder_notes",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column(
            "reminder_id",
            sa.String(64),
            sa.ForeignKey("reminders.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "author_user_id",
            sa.String(36),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("actor_type", sa.String(10), nullable=False, server_default="user"),
        sa.Column("service_key_id", sa.String(36), nullable=True),
        sa.Column("service_key_name", sa.String(100), nullable=True),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint("length(body) <= 4000", name="ck_reminder_notes_body_len"),
        sa.CheckConstraint(
            "actor_type IN ('user', 'service')", name="ck_reminder_notes_actor_type"
        ),
        sa.Index("idx_reminder_notes_reminder", "reminder_id", "created_at"),
    )

    with op.batch_alter_table(
        "ticket_notifications", table_kwargs={"sqlite_autoincrement": True}
    ) as batch:
        batch.add_column(
            sa.Column(
                "subject_kind", sa.String(20), nullable=False, server_default="ticket"
            )
        )
        batch.add_column(sa.Column("subject_id", sa.String(64), nullable=True))
        batch.alter_column("ticket_id", existing_type=sa.String(64), nullable=True)
        batch.alter_column("project_id", existing_type=sa.String(64), nullable=True)
        batch.create_check_constraint(
            "ck_ticket_notifications_subject_kind", _SUBJECT_KIND_CHECK
        )
        batch.create_check_constraint(
            "ck_ticket_notifications_ticket_subject", _TICKET_SUBJECT_CHECK
        )
        batch.create_index(
            "idx_ticket_notifications_subject", ["subject_kind", "subject_id"]
        )
    op.execute(
        "UPDATE ticket_notifications SET subject_id = ticket_id "
        "WHERE subject_kind = 'ticket' AND subject_id IS NULL"
    )


def downgrade() -> None:
    # Rows about other subjects have no ticket and can't survive NOT NULL.
    op.execute("DELETE FROM ticket_notifications WHERE subject_kind <> 'ticket'")
    with op.batch_alter_table(
        "ticket_notifications", table_kwargs={"sqlite_autoincrement": True}
    ) as batch:
        batch.drop_index("idx_ticket_notifications_subject")
        batch.drop_constraint("ck_ticket_notifications_ticket_subject", type_="check")
        batch.drop_constraint("ck_ticket_notifications_subject_kind", type_="check")
        batch.alter_column("project_id", existing_type=sa.String(64), nullable=False)
        batch.alter_column("ticket_id", existing_type=sa.String(64), nullable=False)
        batch.drop_column("subject_id")
        batch.drop_column("subject_kind")
    op.drop_table("reminder_notes")
    op.drop_table("reminders")
