"""Agent work queues: work_queues + work_queue_items + work_queue_runs.

Revision ID: 054
Revises: 053

tk_529a64620db846f5 (WQ-P1) — foundation for MCP-first agent work queues
(design tk_c2ed6093acde4d55, docs/design/agent-work-queues.md §3/§5/§7/§14).
This phase is DATA MODEL + the atomic-claim primitive ONLY — no MCP tools,
no routes, no step engine (those are WQ-P2/P3).

A WorkQueue is a durable, project-scoped plan for an agent to repeatedly
service a set of tickets without a human dispatcher: it owns selection (a
filter or explicit ticket-id list), a mode, a per-ticket cursor, a stop
condition + budget, and an append-only wake audit. The three tables map to:
  - work_queues       — the durable loop definition.
  - work_queue_items  — the per-ticket cursor (the durable resumable state).
                        Carries the seen-vs-acked comment cursor split and the
                        directive lease (open_directive_id/open_directive_run_id)
                        so a crash between directive and writeback REPLAYS the
                        same directive rather than losing or double-counting it.
  - work_queue_runs   — one row per wake (poll/no-op wakes included). Kept a
                        SEPARATE table from agent_runs (Atlas R2): a wake may be
                        a no-op that produces no AgentRun; when it does produce
                        one, the row links it via the nullable agent_run_id.

Strictly additive (down_revision='053'):
  - No edits to migrations 001–053; already-migrated DBs unaffected; single head.
  - Inline CheckConstraint declared INSIDE op.create_table (SQLite-safe — the
    same pattern as migrations 050–053; NOT a follow-up op.create_check_constraint).
  - No lastrowid — all three tables use app-assigned String(64) PKs
    (wq_/wqi_/wqr_ + token_hex). Never rely on integer autoincrement.
  - Claim index idx_wqi_claim (work_queue_id, item_status, next_eligible_at)
    covers the atomic-claim predicate (services/work_queues.py).

Downgrade: drop the three tables in reverse dependency order
(runs → items → queues).
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "054"
down_revision = "053"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── work_queues — the durable loop definition ─────────────────
    op.create_table(
        "work_queues",
        sa.Column("id", sa.String(length=64), primary_key=True),  # wq_<hex>
        sa.Column(
            "project_id",
            sa.String(length=64),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("mode", sa.String(length=30), nullable=False),
        sa.Column("assigned_persona", sa.String(length=50), nullable=True),
        # JSON-as-Text (not native JSONB) for cross-DB SQLite/PG compatibility.
        sa.Column(
            "selector", sa.Text(), nullable=False, server_default="{}"
        ),
        sa.Column(
            "auto_adopt",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column(
            "max_adopt_per_wake",
            sa.Integer(),
            nullable=False,
            server_default="5",
        ),
        sa.Column(
            "stop_condition",
            sa.String(length=30),
            nullable=False,
            server_default="queue_empty",
        ),
        sa.Column(
            "cadence_seconds",
            sa.Integer(),
            nullable=False,
            server_default="300",
        ),
        sa.Column(
            "max_tickets_per_run",
            sa.Integer(),
            nullable=False,
            server_default="1",
        ),
        sa.Column(
            "max_attempts_per_item",
            sa.Integer(),
            nullable=False,
            server_default="3",
        ),
        sa.Column(
            "status",
            sa.String(length=20),
            nullable=False,
            server_default="active",
        ),
        sa.Column(
            "lease_epoch",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        # Provenance triple (v0.10.10 convention).
        sa.Column("created_by_user_id", sa.String(length=64), nullable=False),
        sa.Column("created_by_session_id", sa.String(length=64), nullable=True),
        sa.Column("created_by_persona", sa.String(length=50), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "project_id", "name", name="uq_work_queue_project_name"
        ),
        # Inline CHECKs (SQLite-safe — declared at create_table, same as 050–053).
        sa.CheckConstraint(
            "mode IN ('review_until_clean', 'implement_until_done', 'triage')",
            name="ck_work_queue_mode",
        ),
        sa.CheckConstraint(
            "status IN ('active', 'paused', 'completed', 'cancelled')",
            name="ck_work_queue_status",
        ),
    )
    op.create_index(
        "idx_work_queue_project_status",
        "work_queues",
        ["project_id", "status"],
    )

    # ── work_queue_items — the per-ticket cursor (durable state) ───
    op.create_table(
        "work_queue_items",
        sa.Column("id", sa.String(length=64), primary_key=True),  # wqi_<hex>
        sa.Column(
            "work_queue_id",
            sa.String(length=64),
            sa.ForeignKey("work_queues.id", ondelete="CASCADE"),
            nullable=False,
        ),
        # Plain string — the ticket may be hard-deleted; the cursor row
        # survives (same rationale as agent_runs.ticket_id).
        sa.Column("ticket_id", sa.String(length=64), nullable=False),
        sa.Column(
            "item_status",
            sa.String(length=20),
            nullable=False,
            server_default="pending",
        ),
        # SEEN cursor — newest comment the server has SHOWN in a directive
        # (the `since` floor for the next delta). Advanced when a directive
        # is emitted. NOT proof the agent acted.
        sa.Column(
            "last_seen_comment_at", sa.DateTime(timezone=True), nullable=True
        ),
        sa.Column("last_seen_comment_id", sa.String(length=64), nullable=True),
        # ACKED cursor — newest comment DURABLY reviewed. Advanced ONLY by
        # complete_work_queue_step after the writeback is validated/committed.
        # The stop oracle + reviewer-turn check read the ACKED cursor.
        sa.Column(
            "last_acked_comment_at", sa.DateTime(timezone=True), nullable=True
        ),
        sa.Column("last_acked_comment_id", sa.String(length=64), nullable=True),
        # Directive lease — the outstanding directive for this item (null when
        # none open). While set, the same directive re-emits idempotently.
        sa.Column("open_directive_id", sa.String(length=64), nullable=True),
        sa.Column("open_directive_run_id", sa.String(length=64), nullable=True),
        sa.Column("last_agent_run_id", sa.String(length=64), nullable=True),
        sa.Column("last_verdict", sa.String(length=20), nullable=True),
        # Count of EMITTED directives / action attempts (runaway-loop guard).
        # Passive waits do NOT increment it.
        sa.Column(
            "attempts", sa.Integer(), nullable=False, server_default="0"
        ),
        # Earliest the item should be re-picked (backoff). Part of the atomic
        # claim predicate.
        sa.Column(
            "next_eligible_at", sa.DateTime(timezone=True), nullable=True
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "work_queue_id", "ticket_id", name="uq_work_queue_item"
        ),
        # item_status is the queue's view of the loop, DISTINCT from
        # Ticket.status.
        sa.CheckConstraint(
            "item_status IN "
            "('pending', 'active', 'waiting', 'done', 'failed')",
            name="ck_work_queue_item_status",
        ),
    )
    # Claim index — covers the atomic-claim predicate
    # (work_queue_id, item_status, next_eligible_at).
    op.create_index(
        "idx_wqi_claim",
        "work_queue_items",
        ["work_queue_id", "item_status", "next_eligible_at"],
    )
    op.create_index(
        "idx_wqi_ticket", "work_queue_items", ["ticket_id"]
    )

    # ── work_queue_runs — append-only wake audit ──────────────────
    op.create_table(
        "work_queue_runs",
        sa.Column("id", sa.String(length=64), primary_key=True),  # wqr_<hex>
        sa.Column(
            "work_queue_id",
            sa.String(length=64),
            sa.ForeignKey("work_queues.id", ondelete="CASCADE"),
            nullable=False,
        ),
        # Nullable — a wake can be poll-only / no-op (no item serviced).
        sa.Column(
            "work_queue_item_id",
            sa.String(length=64),
            sa.ForeignKey("work_queue_items.id", ondelete="SET NULL"),
            nullable=True,
        ),
        # Nullable — link to AgentRun only when the wake produced one.
        sa.Column(
            "agent_run_id",
            sa.String(length=64),
            sa.ForeignKey("agent_runs.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("directive_id", sa.String(length=64), nullable=True),
        sa.Column("outcome", sa.String(length=30), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index(
        "idx_wqr_queue_created",
        "work_queue_runs",
        ["work_queue_id", "created_at"],
    )


def downgrade() -> None:
    # Reverse dependency order: runs → items → queues.
    op.drop_index("idx_wqr_queue_created", table_name="work_queue_runs")
    op.drop_table("work_queue_runs")
    op.drop_index("idx_wqi_ticket", table_name="work_queue_items")
    op.drop_index("idx_wqi_claim", table_name="work_queue_items")
    op.drop_table("work_queue_items")
    op.drop_index(
        "idx_work_queue_project_status", table_name="work_queues"
    )
    op.drop_table("work_queues")
