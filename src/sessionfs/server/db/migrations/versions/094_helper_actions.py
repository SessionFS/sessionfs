"""Session-helper write actions (confirm-first proposals).

Revision ID: 094
Revises: 093

One table: ``helper_actions``. The session helper is read-only; its first
write actions (set a project's auto-narrative, move sessions to Trash) are
PROPOSALS only — a row starts ``pending`` and nothing changes until the
caller confirms it in the dashboard. ``kind`` is set_auto_narrative or
trash_sessions; ``params`` holds the exact targets + desired value;
``preview`` holds the per-target before/after + display names computed at
proposal time; ``result`` holds the per-target ok/error after confirm.
``expires_at`` is proposal time + 15 minutes. JSON-as-Text for cross-DB
compatibility. Indexed on (user_id, status) for the pending-count cap and
the dashboard's pending listing, and on conversation_id for the per-thread
lookups (history recovery, conversation delete). ``message_id`` is the id of
the helper_messages tool row that carried the proposal, so a restored thread
can map its tool row back to the action.

Downgrade drops the table.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "094"
down_revision = "093"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "helper_actions",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column(
            "user_id",
            sa.String(36),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "conversation_id",
            sa.String(64),
            sa.ForeignKey("helper_conversations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("message_id", sa.String(64), nullable=True),
        sa.Column("kind", sa.String(40), nullable=False),
        sa.Column("params", sa.Text(), nullable=True),
        sa.Column("preview", sa.Text(), nullable=True),
        sa.Column(
            "status", sa.String(20), nullable=False, server_default="pending"
        ),
        sa.Column("result", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Index("idx_helper_actions_user_status", "user_id", "status"),
        sa.Index("idx_helper_actions_conversation", "conversation_id"),
    )


def downgrade() -> None:
    op.drop_table("helper_actions")
