"""Ticket due dates, estimates and the append-only event log (GitHub #113).

Revision ID: 102
Revises: 101

Additive only.

- tickets.due_date: nullable calendar date (no time component).
- tickets.estimated_effort: nullable Jira-duration string ("2d 4h", "30m"),
  bounded to 32 chars, validated in the API layer.
- ticket_events: the append-only ledger of who created/edited/moved/linked a
  ticket. ``action`` is a fixed verb (created, status_changed, assigned,
  field_edited, due_date_set, estimate_set, jira_linked, ...); ``field`` is
  only set for field-scoped actions; ``before_value`` / ``after_value`` are
  JSON strings (a bound-limited value or a ``{"changed": true, "length": N}``
  stub for free-text fields, so comment/description bodies never land here).
  ``project_id`` is denormalised for cross-ticket reads without a join;
  ``actor_type`` is one of user|service|system|jira. The actor columns are
  plain provenance strings with no FK (a deleted user still shows up as
  "a former user"). ``seq`` is a monotonic surrogate key (BigInteger on
  PostgreSQL, INTEGER on SQLite) that orders the timeline; ``id`` is the
  public string id the API exposes and is unique.
- idx_ticket_events_ticket_seq (ticket_id, seq).

Downgrade drops the index, the table and both ticket columns. No backfill.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "102"
down_revision = "101"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("tickets", sa.Column("due_date", sa.Date(), nullable=True))
    op.add_column("tickets", sa.Column("estimated_effort", sa.String(length=32), nullable=True))

    op.create_table(
        "ticket_events",
        sa.Column(
            "seq",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            nullable=False,
            autoincrement=True,
        ),
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("ticket_id", sa.String(length=64), nullable=False),
        sa.Column("project_id", sa.String(length=64), nullable=False),
        sa.Column("action", sa.String(length=40), nullable=False),
        sa.Column("field", sa.String(length=40), nullable=True),
        sa.Column("before_value", sa.Text(), nullable=True),
        sa.Column("after_value", sa.Text(), nullable=True),
        sa.Column("actor_type", sa.String(length=20), nullable=False),
        sa.Column("actor_user_id", sa.String(length=64), nullable=True),
        sa.Column("actor_persona", sa.String(length=100), nullable=True),
        sa.Column("actor_label", sa.String(length=200), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["ticket_id"], ["tickets.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("seq"),
        sa.UniqueConstraint("id"),
        sqlite_autoincrement=True,
    )
    op.create_index(
        "idx_ticket_events_ticket_seq", "ticket_events", ["ticket_id", "seq"]
    )


def downgrade() -> None:
    op.drop_index("idx_ticket_events_ticket_seq", table_name="ticket_events")
    op.drop_table("ticket_events")
    op.execute(sa.text("ALTER TABLE tickets DROP COLUMN estimated_effort"))
    op.execute(sa.text("ALTER TABLE tickets DROP COLUMN due_date"))
