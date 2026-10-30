"""Record agent-run cost, model and loop fields (Review Loop R3, track A).

Revision ID: 106
Revises: 105

Additive only — every new column is nullable so existing rows are untouched.

- agent_runs.model / provider: the model the caller reports it ran, and its
  provider, so usage and findings can be sliced by model.
- agent_runs.input_tokens / output_tokens / cache_read_tokens /
  cache_write_tokens: usage counters reported by the caller (may be estimates).
- agent_runs.cost_usd + cost_basis: how much the run cost and whether that
  figure was reported by the runtime, estimated from tokens, planned ahead, or
  is unknown. cost_basis is a CHECK-enforced enum.
- agent_runs.role: which persona role this run is executing
  (implementer / ui-implementer / reviewer / design-reviewer /
  security-reviewer / release). CHECK-enforced.
- agent_runs.branch / base_sha / head_sha: the working tree the run operated
  on, so the review loop can correlate the run with the PR it produced.
- agent_runs.parent_run_id / continues_run_id / work_queue_id /
  work_queue_item_id: review-loop linkage. Plain strings (no FK), same as
  persona_name / ticket_id / session_id, so the audit row survives even if the
  linked row is later hard-deleted. Cross-project ownership is validated at the
  route layer (uniform 404).
- agent_runs.exit_reason: finished / out_of_turns / crashed. CHECK-enforced.
- agent_runs.occurred_at: wall-clock time the run is reported to have happened
  (distinct from created_at, the server-received time).
- idx_agent_run_project_parent (project_id, parent_run_id) for the loop query.

The three CHECK constraints are PostgreSQL-only, like 079/080's: SQLite cannot
ADD CONSTRAINT without a table rebuild, so the columns stay plain ADD COLUMN
there and the inline CheckConstraints in models.py cover create_all.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "106"
down_revision = "105"
branch_labels = None
depends_on = None

_TABLE = "agent_runs"

_CHECK_CONSTRAINTS = (
    (
        "ck_agent_run_cost_basis",
        "cost_basis IS NULL OR cost_basis IN "
        "('reported', 'estimated', 'plan', 'unknown')",
    ),
    (
        "ck_agent_run_role",
        "role IS NULL OR role IN ('implementer', 'ui-implementer', 'reviewer', "
        "'design-reviewer', 'security-reviewer', 'release')",
    ),
    (
        "ck_agent_run_exit_reason",
        "exit_reason IS NULL OR exit_reason IN ('finished', 'out_of_turns', 'crashed')",
    ),
)


def _columns() -> list[sa.Column]:
    return [
        sa.Column("model", sa.String(120), nullable=True),
        sa.Column("provider", sa.String(40), nullable=True),
        sa.Column("input_tokens", sa.BigInteger(), nullable=True),
        sa.Column("output_tokens", sa.BigInteger(), nullable=True),
        sa.Column("cache_read_tokens", sa.BigInteger(), nullable=True),
        sa.Column("cache_write_tokens", sa.BigInteger(), nullable=True),
        sa.Column("cost_usd", sa.Numeric(12, 6), nullable=True),
        sa.Column("cost_basis", sa.String(12), nullable=True),
        sa.Column("role", sa.String(30), nullable=True),
        sa.Column("branch", sa.String(200), nullable=True),
        sa.Column("base_sha", sa.String(64), nullable=True),
        sa.Column("head_sha", sa.String(64), nullable=True),
        sa.Column("parent_run_id", sa.String(64), nullable=True),
        sa.Column("continues_run_id", sa.String(64), nullable=True),
        sa.Column("work_queue_id", sa.String(64), nullable=True),
        sa.Column("work_queue_item_id", sa.String(64), nullable=True),
        sa.Column("exit_reason", sa.String(20), nullable=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=True),
    ]


def upgrade() -> None:
    sqlite = op.get_bind().dialect.name == "sqlite"
    for column in _columns():
        op.add_column(_TABLE, column)
    if not sqlite:
        for name, expression in _CHECK_CONSTRAINTS:
            op.create_check_constraint(name, _TABLE, expression)
    op.create_index("idx_agent_run_project_parent", _TABLE, ["project_id", "parent_run_id"])


def downgrade() -> None:
    op.drop_index("idx_agent_run_project_parent", table_name=_TABLE)
    if op.get_bind().dialect.name != "sqlite":
        for name, _expression in _CHECK_CONSTRAINTS:
            op.drop_constraint(name, _TABLE, type_="check")
    # Plain DROP COLUMN on both dialects (SQLite >= 3.35): no table rebuild.
    for column in reversed(_columns()):
        op.execute(sa.text(f"ALTER TABLE {_TABLE} DROP COLUMN {column.name}"))
