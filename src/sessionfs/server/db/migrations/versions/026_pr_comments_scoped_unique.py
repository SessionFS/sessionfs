"""Scope pr_comments unique index to (installation_id, repo_full_name, pr_number).

The original index in migration 010 was unique on (repo_full_name, pr_number),
which causes insert-time collisions when the same repo name appears across
installations or providers (GitHub installation vs GitLab scope 0, or two
GitHub installations covering the same repo). Runtime queries already scope
by installation_id, so widen the uniqueness constraint to match.

Revision ID: 026
Revises: 025
"""
from alembic import op

revision = "026"
down_revision = "025"


def upgrade():
    op.drop_index("idx_pr_comments_repo_pr", table_name="pr_comments")
    op.create_index(
        "idx_pr_comments_installation_repo_pr",
        "pr_comments",
        ["installation_id", "repo_full_name", "pr_number"],
        unique=True,
    )


def downgrade():
    op.drop_index("idx_pr_comments_installation_repo_pr", table_name="pr_comments")
    op.create_index(
        "idx_pr_comments_repo_pr",
        "pr_comments",
        ["repo_full_name", "pr_number"],
        unique=True,
    )
