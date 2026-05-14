"""Add agent_runs table.

v0.10.2 — AgentRun layer for ephemeral agent execution tracking.

AgentRun is the execution record: one traceable run of one persona,
optionally against one ticket, with trigger metadata, severity,
findings, policy evaluation, and CI-friendly exit behavior.

This is a tracking/enforcement feature, NOT a model orchestrator.
`POST /agent-runs/{id}/start` returns compiled persona+ticket context
the caller should feed into its own model; the server does not spawn
Codex/Claude/Bedrock. Auto-spawning is deferred.

Status FSM (enforced at the routes layer):
    queued → running → passed | failed | errored | cancelled
    queued → cancelled (direct cancel before start)
    running → cancelled (mid-run cancel)

`persona_name`, `ticket_id`, and `session_id` are plain Strings, NOT
foreign keys — the same pattern v0.10.1 tickets and sessions use.
Personas can be soft-deleted; tickets and sessions can be hard-deleted
independently. The AgentRun audit row should survive either way (same
rationale as the rules_hash / rules_version pattern on sessions).

JSON-as-text columns (`findings`, `triggered_by_session_id`) follow the
v0.10.1 ticket pattern: `NOT NULL DEFAULT '[]'` so raw SQL writes can't
slip a NULL past the ORM's `Mapped[str]` typing.

Indexes:
- `idx_agent_run_project_status` for the dashboard recent-runs view.
- `idx_agent_run_ticket` for "what runs touched this ticket?" queries.
- `idx_agent_run_project_persona` for per-persona dashboards.
- `idx_agent_run_project_trigger` for CI dedup (project + source + ref).
- `idx_agent_run_project_created` for chronological list pagination.

Revision ID: 038
Revises: 037
"""

from alembic import op
import sqlalchemy as sa


revision = "038"
down_revision = "037"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "agent_runs",
        # Identity.
        sa.Column("id", sa.String(64), primary_key=True),  # run_<hex>
        sa.Column(
            "project_id",
            sa.String(64),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        # Required execution metadata.
        sa.Column("persona_name", sa.String(50), nullable=False),
        sa.Column("tool", sa.String(50), nullable=False, server_default="generic"),
        sa.Column(
            "trigger_source",
            sa.String(30),
            nullable=False,
            server_default="manual",
        ),
        sa.Column(
            "status",
            sa.String(20),
            nullable=False,
            server_default="queued",
        ),
        # Optional ticket linkage. Plain String — ticket may be deleted.
        sa.Column("ticket_id", sa.String(64), nullable=True),
        # CI / trigger context.
        sa.Column("trigger_ref", sa.String(200), nullable=True),
        sa.Column("ci_provider", sa.String(30), nullable=True),
        sa.Column("ci_run_url", sa.Text(), nullable=True),
        # Result fields (set on complete).
        sa.Column("result_summary", sa.Text(), nullable=True),
        sa.Column("severity", sa.String(20), nullable=True),
        sa.Column("findings_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("findings", sa.Text(), nullable=False, server_default="[]"),
        # Policy fields.
        sa.Column("fail_on", sa.String(20), nullable=True),
        sa.Column("policy_result", sa.String(10), nullable=True),
        sa.Column("exit_code", sa.Integer(), nullable=True),
        # Session linkage (the .sfs session that captured this run, if any).
        sa.Column("session_id", sa.String(64), nullable=True),
        # Triggerer provenance.
        sa.Column(
            "triggered_by_user_id",
            sa.String(64),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("triggered_by_persona", sa.String(50), nullable=True),
        # Timestamps.
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("duration_seconds", sa.Integer(), nullable=True),
    )

    # Recent-runs view (dashboard, list endpoint default sort).
    op.create_index(
        "idx_agent_run_project_status",
        "agent_runs",
        ["project_id", "status"],
    )
    # Per-ticket reverse lookup ("what runs touched this ticket?").
    op.create_index(
        "idx_agent_run_ticket",
        "agent_runs",
        ["ticket_id"],
    )
    # Per-persona dashboards.
    op.create_index(
        "idx_agent_run_project_persona",
        "agent_runs",
        ["project_id", "persona_name"],
    )
    # CI dedup — `was this commit/PR already reviewed by atlas?`
    op.create_index(
        "idx_agent_run_project_trigger",
        "agent_runs",
        ["project_id", "trigger_source", "trigger_ref"],
    )
    # Chronological list pagination.
    op.create_index(
        "idx_agent_run_project_created",
        "agent_runs",
        ["project_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("idx_agent_run_project_created", table_name="agent_runs")
    op.drop_index("idx_agent_run_project_trigger", table_name="agent_runs")
    op.drop_index("idx_agent_run_project_persona", table_name="agent_runs")
    op.drop_index("idx_agent_run_ticket", table_name="agent_runs")
    op.drop_index("idx_agent_run_project_status", table_name="agent_runs")
    op.drop_table("agent_runs")
