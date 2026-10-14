"""Structured agent-feedback / outcome records.

Revision ID: 064
Revises: 063

Adds an append-only `agent_feedback` table for recording structured
outcome/feedback on agent work — a record points at at least one target
(an agent run, a ticket, or a ticket comment) within a single project,
classifies it by `feedback_type` × `verdict` × optional `label`, and
carries the standard service-key audit triple.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "064"
down_revision = "063"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "agent_feedback",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column(
            "project_id",
            sa.String(length=64),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "agent_run_id",
            sa.String(length=64),
            sa.ForeignKey("agent_runs.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "ticket_id",
            sa.String(length=64),
            sa.ForeignKey("tickets.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "comment_id",
            sa.String(length=64),
            sa.ForeignKey("ticket_comments.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("feedback_type", sa.String(length=20), nullable=False),
        sa.Column("verdict", sa.String(length=10), nullable=False),
        sa.Column("label", sa.String(length=50), nullable=True),
        sa.Column("final_owner", sa.String(length=100), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column(
            "source", sa.String(length=10), nullable=False, server_default="human"
        ),
        sa.Column(
            "created_by_user_id",
            sa.String(length=36),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "actor_type", sa.String(length=20), nullable=False, server_default="user"
        ),
        sa.Column("service_key_id", sa.String(length=36), nullable=True),
        sa.Column("service_key_name", sa.String(length=100), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint(
            "feedback_type IN ('route','summary','actionability',"
            "'duplicate','resolution')",
            name="ck_agent_feedback_type",
        ),
        sa.CheckConstraint(
            "verdict IN ('positive','negative','neutral')",
            name="ck_agent_feedback_verdict",
        ),
        sa.CheckConstraint(
            "source IN ('human','auto')", name="ck_agent_feedback_source"
        ),
        # No "at least one target" CHECK: targets are ON DELETE SET NULL so a
        # record outlives the run/ticket/comment it judged, which a CHECK
        # would turn into a failed delete. The API requires a target on create.
    )
    op.create_index(
        "idx_agent_feedback_project_created",
        "agent_feedback",
        ["project_id", "created_at"],
    )
    op.create_index("idx_agent_feedback_run", "agent_feedback", ["agent_run_id"])
    op.create_index("idx_agent_feedback_ticket", "agent_feedback", ["ticket_id"])
    op.create_index("idx_agent_feedback_comment", "agent_feedback", ["comment_id"])
    # At most one automatic wrong-route record per (ticket, new owner), even
    # when two reassignments race.
    op.create_index(
        "uq_agent_feedback_auto_route",
        "agent_feedback",
        ["ticket_id", "final_owner"],
        unique=True,
        sqlite_where=sa.text("source = 'auto' AND label = 'wrong_route'"),
        postgresql_where=sa.text("source = 'auto' AND label = 'wrong_route'"),
    )


def downgrade() -> None:
    op.drop_index("uq_agent_feedback_auto_route", table_name="agent_feedback")
    op.drop_index("idx_agent_feedback_comment", table_name="agent_feedback")
    op.drop_index("idx_agent_feedback_ticket", table_name="agent_feedback")
    op.drop_index("idx_agent_feedback_run", table_name="agent_feedback")
    op.drop_index("idx_agent_feedback_project_created", table_name="agent_feedback")
    op.drop_table("agent_feedback")
