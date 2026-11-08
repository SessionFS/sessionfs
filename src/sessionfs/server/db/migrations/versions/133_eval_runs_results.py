"""Evaluation runs, per-case results and the grader agent-run link (GitHub #121).

Revision ID: 133
Revises: 132

- ``eval_runs``: one grading pass of a pinned suite version against one
  subject (an artifact version, a commit or a free-text label). ``verdict``
  and ``trusted`` are computed by the server at finish. A partial index on
  (suite_lineage_id, artifact_id, finished_at) WHERE state = 'finished'
  serves promotion-gate and baseline lookups.
- ``eval_case_results``: one row per (run, case), unique, append-only.
  ``body_sha256`` makes a replayed batch idempotent; ``output`` / ``notes``
  are cleared by the retention sweep (``output_cleared_at``) while the
  verdict, score and ``output_sha256`` stay.
- ``agent_runs.eval_run_id`` (nullable) and ``'grader'`` added to
  ``ck_agent_run_role``. Migration 106 created that CHECK on PostgreSQL only
  (SQLite never had it), so the constraint swap is PostgreSQL-only too:
  DROP CONSTRAINT then ADD CONSTRAINT, never DROP INDEX (the 056 lesson).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "133"
down_revision = "132"
branch_labels = None
depends_on = None

_ROLE_OLD = (
    "role IS NULL OR role IN ('implementer', 'ui-implementer', 'reviewer', "
    "'design-reviewer', 'security-reviewer', 'release')"
)
_ROLE_NEW = (
    "role IS NULL OR role IN ('implementer', 'ui-implementer', 'reviewer', "
    "'design-reviewer', 'security-reviewer', 'release', 'grader')"
)


def upgrade() -> None:
    op.create_table(
        "eval_runs",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column(
            "project_id",
            sa.String(64),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "suite_id",
            sa.String(64),
            sa.ForeignKey("eval_suites.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("suite_lineage_id", sa.String(64), nullable=False),
        sa.Column(
            "artifact_id",
            sa.String(64),
            sa.ForeignKey("artifacts.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("subject_label", sa.String(200), nullable=True),
        sa.Column("commit_sha", sa.String(64), nullable=True),
        sa.Column("ticket_id", sa.String(64), nullable=True),
        sa.Column("trigger_source", sa.String(20), nullable=False, server_default="manual"),
        sa.Column("trigger_ref", sa.String(200), nullable=True),
        sa.Column("agent_run_id", sa.String(64), nullable=True),
        sa.Column("grader_persona", sa.String(50), nullable=False),
        sa.Column("grader_user_id", sa.String(36), nullable=True),
        sa.Column("grader_service_key_id", sa.String(36), nullable=True),
        sa.Column(
            "created_by_user_id",
            sa.String(36),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("actor_type", sa.String(20), nullable=False, server_default="user"),
        sa.Column("service_key_id", sa.String(36), nullable=True),
        sa.Column("service_key_name", sa.String(100), nullable=True),
        sa.Column("state", sa.String(20), nullable=False, server_default="running"),
        sa.Column("verdict", sa.String(20), nullable=True),
        sa.Column("trusted", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("untrusted_reason", sa.String(200), nullable=True),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("baseline_run_id", sa.String(64), nullable=True),
        sa.Column("voided_reason", sa.String(500), nullable=True),
        sa.Column("voided_by_user_id", sa.String(36), nullable=True),
        sa.Column("voided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "trigger_source IN ('manual', 'ci', 'workflow', 'promotion')",
            name="ck_eval_runs_trigger_source",
        ),
        sa.CheckConstraint(
            "state IN ('running', 'finished', 'voided', 'expired')", name="ck_eval_runs_state"
        ),
        sa.CheckConstraint(
            "verdict IS NULL OR verdict IN ('passed', 'failed', 'incomplete')",
            name="ck_eval_runs_verdict",
        ),
        sa.CheckConstraint(
            "actor_type IN ('user', 'service_key')", name="ck_eval_runs_actor_type"
        ),
    )
    op.create_index(
        "idx_eval_runs_project_started", "eval_runs", ["project_id", "started_at"]
    )
    op.create_index("idx_eval_runs_suite_lineage", "eval_runs", ["suite_lineage_id"])
    op.create_index(
        "idx_eval_runs_gate",
        "eval_runs",
        ["suite_lineage_id", "artifact_id", "finished_at"],
        postgresql_where=sa.text("state = 'finished'"),
        sqlite_where=sa.text("state = 'finished'"),
    )

    op.create_table(
        "eval_case_results",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column(
            "run_id",
            sa.String(64),
            sa.ForeignKey("eval_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "case_id",
            sa.String(64),
            sa.ForeignKey("eval_cases.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("case_key", sa.String(64), nullable=False),
        sa.Column("verdict", sa.String(10), nullable=False),
        sa.Column("score", sa.Numeric(8, 4), nullable=True),
        sa.Column("grader_kind", sa.String(20), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("output", sa.Text(), nullable=True),
        sa.Column("output_sha256", sa.String(64), nullable=True),
        sa.Column("body_sha256", sa.String(64), nullable=False),
        sa.Column("output_cleared_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "verdict IN ('pass', 'fail', 'error', 'skip')",
            name="ck_eval_case_results_verdict",
        ),
        sa.CheckConstraint(
            "score IS NULL OR (score >= 0 AND score <= 1)",
            name="ck_eval_case_results_score",
        ),
        sa.UniqueConstraint("run_id", "case_id", name="uq_eval_case_results_run_case"),
    )
    op.create_index(
        "idx_eval_case_results_created", "eval_case_results", ["created_at"]
    )

    op.add_column("agent_runs", sa.Column("eval_run_id", sa.String(64), nullable=True))
    if op.get_bind().dialect.name != "sqlite":
        op.drop_constraint("ck_agent_run_role", "agent_runs", type_="check")
        op.create_check_constraint("ck_agent_run_role", "agent_runs", _ROLE_NEW)


def downgrade() -> None:
    if op.get_bind().dialect.name != "sqlite":
        # Grader runs would violate the old CHECK; keep the rows, drop the role.
        op.execute(sa.text("UPDATE agent_runs SET role = NULL WHERE role = 'grader'"))
        op.drop_constraint("ck_agent_run_role", "agent_runs", type_="check")
        op.create_check_constraint("ck_agent_run_role", "agent_runs", _ROLE_OLD)
    # Plain DROP COLUMN on both dialects (SQLite >= 3.35): no table rebuild.
    op.execute(sa.text("ALTER TABLE agent_runs DROP COLUMN eval_run_id"))
    op.drop_index("idx_eval_case_results_created", table_name="eval_case_results")
    op.drop_table("eval_case_results")
    op.drop_index("idx_eval_runs_gate", table_name="eval_runs")
    op.drop_index("idx_eval_runs_suite_lineage", table_name="eval_runs")
    op.drop_index("idx_eval_runs_project_started", table_name="eval_runs")
    op.drop_table("eval_runs")
