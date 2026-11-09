"""Ordering state for merge check posts (GitHub check runs, GitLab statuses).

Revision ID: 137
Revises: 136

``merge_check_posts`` holds one row per (project, provider, repo, head sha).
``eval_seq`` is a monotonic evaluation sequence taken right before readiness
is computed; ``posted_seq`` is advanced by a guarded UPDATE right before a
post is sent, so an evaluation that read older state can never post over a
newer one. ``posted_state``/``posted_summary`` are what the newest post
sends, so a request overtaken in flight can be re-sent. No existing table
fits: a commit may have no pull request row, or several. New table only;
nothing earlier creates these names.

"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "137"
down_revision = "136"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "merge_check_posts",
        sa.Column(
            "project_id",
            sa.String(64),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("provider", sa.String(20), nullable=False),
        sa.Column("repo", sa.String(200), nullable=False),
        sa.Column("head_sha", sa.String(64), nullable=False),
        sa.Column("eval_seq", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("posted_seq", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("posted_state", sa.String(32), nullable=True),
        sa.Column("posted_summary", sa.Text(), nullable=True),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.PrimaryKeyConstraint(
            "project_id", "provider", "repo", "head_sha", name="pk_merge_check_posts"
        ),
        sa.CheckConstraint(
            "provider IN ('github', 'gitlab')", name="ck_merge_check_posts_provider"
        ),
    )


def downgrade() -> None:
    op.drop_table("merge_check_posts")
