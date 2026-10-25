"""Replay an agent run with an edited prompt/ticket/context (GitHub #95).

Revision ID: 085
Revises: 083

Additive only.

- agent_runs.replayed_from: nullable, same width as agent_runs.id. A plain
  string, not a FK, so the audit row keeps its provenance even if the parent
  run is later hard-deleted (matches persona_name / ticket_id / session_id).
- agent_runs.prompt: nullable edited prompt.
- agent_runs.context_refs: nullable, JSON-encoded list[str] as Text (matches
  findings).
- idx_agent_run_replayed_from (replayed_from).

Downgrade drops the index and the three columns.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "085"
down_revision = "084"
branch_labels = None
depends_on = None

_TABLE = "agent_runs"


def upgrade() -> None:
    op.add_column(_TABLE, sa.Column("replayed_from", sa.String(64), nullable=True))
    op.add_column(_TABLE, sa.Column("prompt", sa.Text(), nullable=True))
    op.add_column(_TABLE, sa.Column("context_refs", sa.Text(), nullable=True))
    op.create_index("idx_agent_run_replayed_from", _TABLE, ["replayed_from"])


def downgrade() -> None:
    op.drop_index("idx_agent_run_replayed_from", table_name=_TABLE)
    op.execute(sa.text(f"ALTER TABLE {_TABLE} DROP COLUMN replayed_from"))
    op.execute(sa.text(f"ALTER TABLE {_TABLE} DROP COLUMN prompt"))
    op.execute(sa.text(f"ALTER TABLE {_TABLE} DROP COLUMN context_refs"))
