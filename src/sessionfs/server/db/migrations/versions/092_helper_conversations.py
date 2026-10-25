"""Session helper conversations and messages.

Revision ID: 092
Revises: 090

docs/design/session-helper.md §"Migration 089" (renumbered 092 by the
release branch). Two tables for the Enterprise session helper:

- ``helper_conversations`` — one row per helper thread. ``org_id`` is the
  org that owns the conversation's scope and is NULL for personal scope
  (SET NULL so deleting the org demotes the thread rather than destroying
  it). ``deleted_at`` is reserved for a future soft-delete; the v1 DELETE
  and the retention sweep hard-delete rows.
- ``helper_messages`` — one row per user/assistant/tool message, in order.
  Tool messages carry the sanitized tool identity (``tool_name``,
  ``tool_call_id``) and sanitized ``tool_args``; assistant messages carry
  the answer text plus a JSON ``citations`` list. ``content`` is the
  model-visible text after DLP redaction. ``stop_reason`` records how the
  turn ended (``stop``, ``max_tokens``, ``max_steps``, ``deadline``, …) and
  ``client_disconnected`` when the caller dropped the stream mid-answer.

Downgrade drops both tables.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "092"
down_revision = "091"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "helper_conversations",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column(
            "user_id",
            sa.String(36),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "org_id",
            sa.String(64),
            sa.ForeignKey("organizations.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("title", sa.String(500), nullable=True),
        sa.Column("scope", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Index("idx_helper_conversations_user_updated", "user_id", "updated_at"),
    )
    op.create_table(
        "helper_messages",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column(
            "conversation_id",
            sa.String(64),
            sa.ForeignKey("helper_conversations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("role", sa.String(20), nullable=False),
        sa.Column("content", sa.Text(), nullable=False, server_default=""),
        sa.Column("tool_name", sa.String(100), nullable=True),
        sa.Column("tool_call_id", sa.String(100), nullable=True),
        sa.Column("tool_args", sa.Text(), nullable=True),
        sa.Column("citations", sa.Text(), nullable=True),
        sa.Column("key_source", sa.String(20), nullable=True),
        sa.Column("input_tokens", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("output_tokens", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("stop_reason", sa.String(40), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Index("idx_helper_messages_conversation", "conversation_id", "created_at"),
    )


def downgrade() -> None:
    op.drop_table("helper_messages")
    op.drop_table("helper_conversations")
