"""Add audit_reports table, audit_trigger on users, gitlab_settings.

Revision ID: 014
Revises: 013
"""

from alembic import op
import sqlalchemy as sa

revision = "014"
down_revision = "013"


def upgrade() -> None:
    # Audit reports table
    op.create_table(
        "audit_reports",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("session_id", sa.String(64), sa.ForeignKey("sessions.id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id", sa.String(64), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("judge_model", sa.String(100), nullable=False),
        sa.Column("judge_provider", sa.String(50), nullable=False),
        sa.Column("judge_base_url", sa.String(500), nullable=True),
        sa.Column("trust_score", sa.Integer, nullable=False),
        sa.Column("total_claims", sa.Integer, nullable=False, server_default="0"),
        sa.Column("verified_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("unverified_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("contradiction_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("findings", sa.Text, nullable=False, server_default="[]"),
        sa.Column("execution_time_ms", sa.Integer, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("idx_audit_reports_session", "audit_reports", ["session_id"])
    op.create_index("idx_audit_reports_user", "audit_reports", ["user_id"])

    # Auto-audit trigger on users
    op.add_column("users", sa.Column("audit_trigger", sa.String(20), nullable=False, server_default="manual"))

    # GitLab settings per user
    op.create_table(
        "gitlab_settings",
        sa.Column("user_id", sa.String(64), sa.ForeignKey("users.id"), primary_key=True),
        sa.Column("instance_url", sa.String(500), nullable=False, server_default="https://gitlab.com"),
        sa.Column("encrypted_access_token", sa.Text, nullable=False),
        sa.Column("webhook_secret", sa.String(64), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )


def downgrade() -> None:
    op.drop_table("gitlab_settings")
    op.drop_column("users", "audit_trigger")
    op.drop_index("idx_audit_reports_user")
    op.drop_index("idx_audit_reports_session")
    op.drop_table("audit_reports")
