"""Ticket field-edit audit table for the update_ticket verb.

Revision ID: 048
Revises: 047

Closes tk_835a876529de4551. The new PATCH-shaped update verb (exposed
as the existing PUT route /api/v1/projects/{pid}/tickets/{tid}) now
writes a TicketEdit audit row per mutated field on every successful
update, so the historical record of what each field looked like
before/after each edit is preserved server-side.

This table is the durable audit trail companion to the auto-posted
diff comment on TicketComment. The comment is human-readable summary;
this table is the structured per-field history that future SoD /
regulatory queries can rely on.

Columns:
- id String(64) PK — unique edit row identifier.
- ticket_id String(64) FK tickets.id ON DELETE CASCADE — the audit
  trail dies with its ticket; consistent with how completion_notes,
  TicketComment, and TicketDependency are scoped.
- edited_by_user_id String(64) — authenticated user who made the
  edit. Plain String (not FK to users.id) for audit-row survival
  policy: if the user account is later deleted, the audit trail
  must NOT cascade away. Matches AdminAction / KnowledgeEntry
  audit-triple convention.
- edited_by_persona String(50) nullable — persona attribution if the
  editing session was running under one. NOT an authorization grant;
  see the route layer for actual authz.
- field_name String(50) — which Ticket field was mutated.
- old_value Text — JSON-encoded prior value (string, list, etc.).
- new_value Text — JSON-encoded new value.
- edited_at DateTime(tz=True) — when the mutation committed.
- lease_epoch Integer — the ticket's lease_epoch at edit time, so
  audit queries can reconstruct concurrent-edit ordering.

Index on (ticket_id, edited_at DESC) for history queries.

JSONB vs Text: keeping old_value/new_value as JSON-encoded Text
strings (not native JSONB) for SQLite compatibility under Helm /
local-mode deployments. PG queries can still json_extract these via
the JSON_EXTRACT_PATH operator if cross-DB indexing is needed later.
"""

from alembic import op
import sqlalchemy as sa


revision = "048"
down_revision = "047"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "ticket_edits",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column(
            "ticket_id",
            sa.String(length=64),
            sa.ForeignKey("tickets.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("edited_by_user_id", sa.String(length=64), nullable=False),
        sa.Column("edited_by_persona", sa.String(length=50), nullable=True),
        sa.Column("field_name", sa.String(length=50), nullable=False),
        sa.Column("old_value", sa.Text(), nullable=True),
        sa.Column("new_value", sa.Text(), nullable=True),
        sa.Column(
            "edited_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("lease_epoch", sa.Integer(), nullable=False, server_default="0"),
    )
    op.create_index(
        "idx_ticket_edits_ticket_at",
        "ticket_edits",
        ["ticket_id", "edited_at"],
    )


def downgrade() -> None:
    op.drop_index("idx_ticket_edits_ticket_at", table_name="ticket_edits")
    op.drop_table("ticket_edits")
