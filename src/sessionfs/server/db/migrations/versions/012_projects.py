"""Add projects table for shared project context.

Revision ID: 012
Revises: 011
"""

from alembic import op
import sqlalchemy as sa

revision = "012"
down_revision = "011"


def upgrade() -> None:
    op.create_table(
        "projects",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("git_remote_normalized", sa.String(255), nullable=False, unique=True),
        sa.Column("context_document", sa.Text, nullable=False, server_default=""),
        sa.Column("owner_id", sa.String(64), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("idx_projects_remote", "projects", ["git_remote_normalized"])


def downgrade() -> None:
    op.drop_index("idx_projects_remote")
    op.drop_table("projects")
