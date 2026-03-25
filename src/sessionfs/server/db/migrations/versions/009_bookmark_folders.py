"""Add bookmark folders and bookmarks tables.

Revision ID: 009
Revises: 008
"""
from alembic import op
import sqlalchemy as sa

revision = "009"
down_revision = "008"


def upgrade():
    op.create_table(
        "bookmark_folders",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("color", sa.String(7), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("idx_folders_user", "bookmark_folders", ["user_id"])
    op.create_table(
        "bookmarks",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "folder_id",
            sa.String(36),
            sa.ForeignKey("bookmark_folders.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("session_id", sa.String(64), sa.ForeignKey("sessions.id"), nullable=False),
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("idx_bookmarks_folder", "bookmarks", ["folder_id"])
    op.create_index("idx_bookmarks_user_session", "bookmarks", ["user_id", "session_id"])


def downgrade():
    op.drop_table("bookmarks")
    op.drop_table("bookmark_folders")
