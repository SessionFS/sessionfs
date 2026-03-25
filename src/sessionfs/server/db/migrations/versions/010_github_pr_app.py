"""Add GitHub PR App tables and git metadata columns on sessions.

Revision ID: 010
Revises: 009
"""
from alembic import op
import sqlalchemy as sa

revision = "010"
down_revision = "009"


def upgrade():
    # Git metadata columns on sessions (for fast PR matching)
    op.add_column("sessions", sa.Column("git_remote_normalized", sa.String(255), nullable=True))
    op.add_column("sessions", sa.Column("git_branch", sa.String(255), nullable=True))
    op.add_column("sessions", sa.Column("git_commit", sa.String(40), nullable=True))
    op.create_index("idx_sessions_git_match", "sessions", ["git_remote_normalized", "git_branch"])

    # GitHub App installations
    op.create_table(
        "github_installations",
        sa.Column("id", sa.BigInteger(), primary_key=True),  # GitHub installation ID
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("account_login", sa.String(255), nullable=False),  # org or user
        sa.Column("account_type", sa.String(20), nullable=False),  # "Organization" or "User"
        sa.Column("auto_comment", sa.Boolean(), server_default=sa.text("true")),
        sa.Column("include_trust_score", sa.Boolean(), server_default=sa.text("true")),
        sa.Column("include_session_links", sa.Boolean(), server_default=sa.text("true")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )

    # PR comments tracking (to edit, not duplicate)
    op.create_table(
        "pr_comments",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("installation_id", sa.BigInteger(), nullable=False),
        sa.Column("repo_full_name", sa.String(255), nullable=False),
        sa.Column("pr_number", sa.Integer(), nullable=False),
        sa.Column("comment_id", sa.BigInteger(), nullable=False),  # GitHub comment ID
        sa.Column("session_ids", sa.Text(), nullable=False),  # JSON array
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("idx_pr_comments_repo_pr", "pr_comments", ["repo_full_name", "pr_number"], unique=True)


def downgrade():
    op.drop_table("pr_comments")
    op.drop_table("github_installations")
    op.drop_index("idx_sessions_git_match")
    op.drop_column("sessions", "git_commit")
    op.drop_column("sessions", "git_branch")
    op.drop_column("sessions", "git_remote_normalized")
