"""Add ticket leases, context manifests, and retrieval audit tables.

Revision ID: 039
Revises: 038
"""

from alembic import op
import sqlalchemy as sa


revision = "039"
down_revision = "038"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "tickets",
        sa.Column("lease_epoch", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "sessions",
        sa.Column("retrieval_audit_id", sa.String(64), nullable=True),
    )
    op.add_column(
        "context_compilations",
        sa.Column("source_manifest", sa.Text(), nullable=False, server_default="{}"),
    )
    op.create_index(
        "ix_sessions_retrieval_audit_id",
        "sessions",
        ["retrieval_audit_id"],
    )
    op.create_table(
        "retrieval_audit_contexts",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column(
            "project_id",
            sa.String(64),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("ticket_id", sa.String(64), nullable=True),
        sa.Column("persona_name", sa.String(50), nullable=True),
        sa.Column("lease_epoch", sa.Integer(), nullable=True),
        sa.Column(
            "created_by_user_id",
            sa.String(64),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "idx_retrieval_ctx_project",
        "retrieval_audit_contexts",
        ["project_id"],
    )
    op.create_index(
        "idx_retrieval_ctx_ticket",
        "retrieval_audit_contexts",
        ["ticket_id"],
    )
    op.create_table(
        "retrieval_audit_events",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "context_id",
            sa.String(64),
            sa.ForeignKey("retrieval_audit_contexts.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "project_id",
            sa.String(64),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("session_id", sa.String(64), nullable=True),
        sa.Column("tool_name", sa.String(100), nullable=False),
        sa.Column("arguments", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("returned_refs", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("source", sa.String(20), nullable=False, server_default="mcp"),
        sa.Column(
            "caller_user_id",
            sa.String(64),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index(
        "idx_retrieval_event_context",
        "retrieval_audit_events",
        ["context_id", "created_at"],
    )
    op.create_index(
        "idx_retrieval_event_session",
        "retrieval_audit_events",
        ["session_id"],
    )


def downgrade() -> None:
    op.drop_index("idx_retrieval_event_session", table_name="retrieval_audit_events")
    op.drop_index("idx_retrieval_event_context", table_name="retrieval_audit_events")
    op.drop_table("retrieval_audit_events")
    op.drop_index("idx_retrieval_ctx_ticket", table_name="retrieval_audit_contexts")
    op.drop_index("idx_retrieval_ctx_project", table_name="retrieval_audit_contexts")
    op.drop_table("retrieval_audit_contexts")
    op.drop_index("ix_sessions_retrieval_audit_id", table_name="sessions")
    op.drop_column("context_compilations", "source_manifest")
    op.drop_column("sessions", "retrieval_audit_id")
    op.drop_column("tickets", "lease_epoch")
