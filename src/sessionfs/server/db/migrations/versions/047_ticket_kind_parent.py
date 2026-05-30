"""Ticket kind enum + parent_ticket_id for Issue/Task rollup.

Revision ID: 047
Revises: 046

Closes tk_dbccde26ed604b3c (Option A from tk_23f523c1bdd94fc5).

The 2026-05-26 CORS incident exposed the Issue-vs-Ticket abstraction
gap: a single reported problem spawned multiple executor workstreams
with no parent linking and no PM-level rollup. Compass chose Option A
(extend the existing Ticket table) over Option B (new Issue entity)
in KB entry #604 — Tasks remain the existing executor unit, Issues are
PM-triaged containers that roll up one or more child Tasks.

Two new columns:

- `kind` String(10), NOT NULL, server_default='task'. Values: 'issue'
  or 'task'. Default 'task' so existing rows keep their current
  behavior with no data migration required.

- `parent_ticket_id` String(64), nullable, FK to tickets.id with
  ON DELETE SET NULL. When a Task is filed under an Issue, the link
  goes here. Issues themselves have parent_ticket_id NULL (single-
  level nesting only in v1 — no Issue-under-Issue, enforced at the
  route layer, not via CHECK constraint to keep SQLite local-mode
  cross-DB compatible).

Plus a composite index on (project_id, parent_ticket_id) to make the
rollup query ("all children of this Issue") fast.

NOT a reuse of TicketDependency. That table is a DAG ordering
constraint ("A blocks B"); parent_ticket_id is a container
relationship ("A rolls up B"). Two distinct relationships — conflating
them would break dependency-query semantics everywhere.

Strictly additive — no backfill, no existing-row mutations.
"""

from alembic import op
import sqlalchemy as sa


revision = "047"
down_revision = "046"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("tickets") as batch:
        batch.add_column(
            sa.Column(
                "kind",
                sa.String(length=10),
                nullable=False,
                server_default="task",
            )
        )
        batch.add_column(
            sa.Column(
                "parent_ticket_id",
                sa.String(length=64),
                sa.ForeignKey("tickets.id", ondelete="SET NULL"),
                nullable=True,
            )
        )
    op.create_index(
        "idx_ticket_project_parent",
        "tickets",
        ["project_id", "parent_ticket_id"],
    )


def downgrade() -> None:
    op.drop_index("idx_ticket_project_parent", table_name="tickets")
    with op.batch_alter_table("tickets") as batch:
        batch.drop_column("parent_ticket_id")
        batch.drop_column("kind")
