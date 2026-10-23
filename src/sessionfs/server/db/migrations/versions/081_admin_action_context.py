"""Operator context on admin_actions (GitHub #84).

Revision ID: 081
Revises: 080

Additive only. admin_actions (migration 007) records platform-admin
actions; self-hosted operators also need it to say WHY an override was
made and WHAT changed, including overrides they performed by hand.

- reason: nullable Text. Human-written justification.
- before_state / after_state: nullable Text holding JSON (the same
  convention as admin_actions.details and org_audit_events.before/after,
  dialect-neutral).
- source: VARCHAR(20) NOT NULL, server default 'api'. 'api' = the server
  performed the action and wrote the row; 'operator_record' = an operator
  recorded an action they performed outside the API (for example raw SQL),
  which the server did not verify. Every pre-081 row was server-written,
  so the default backfills them correctly.
- idx_admin_actions_created_at (created_at): the audit-log listing orders
  by it.
- idx_admin_actions_target (target_type, target_id): "what happened to
  this project / user / org" lookups.

admin_actions had no indexes before this revision. Downgrade drops the
indexes and the columns.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "081"
down_revision = "080"
branch_labels = None
depends_on = None

_TABLE = "admin_actions"
_COLUMNS = ("reason", "before_state", "after_state", "source")


def upgrade() -> None:
    op.add_column(_TABLE, sa.Column("reason", sa.Text(), nullable=True))
    op.add_column(_TABLE, sa.Column("before_state", sa.Text(), nullable=True))
    op.add_column(_TABLE, sa.Column("after_state", sa.Text(), nullable=True))
    op.add_column(
        _TABLE,
        sa.Column("source", sa.String(20), nullable=False, server_default="api"),
    )
    op.create_index("idx_admin_actions_created_at", _TABLE, ["created_at"])
    op.create_index("idx_admin_actions_target", _TABLE, ["target_type", "target_id"])


def downgrade() -> None:
    op.drop_index("idx_admin_actions_target", table_name=_TABLE)
    op.drop_index("idx_admin_actions_created_at", table_name=_TABLE)
    # Plain DROP COLUMN on both dialects (SQLite >= 3.35): no table rebuild.
    for column in reversed(_COLUMNS):
        op.execute(sa.text(f"ALTER TABLE {_TABLE} DROP COLUMN {column}"))
