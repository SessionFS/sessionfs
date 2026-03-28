"""Add autosync settings and watchlist.

Revision ID: 013
Revises: 012
"""

from alembic import op
import sqlalchemy as sa

revision = "013"
down_revision = "012"


def upgrade() -> None:
    op.add_column("users", sa.Column("sync_mode", sa.String(20), nullable=False, server_default="off"))
    op.add_column("users", sa.Column("sync_debounce", sa.Integer, nullable=False, server_default="30"))

    op.create_table(
        "sync_watchlist",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.String(64), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("session_id", sa.String(64), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
        sa.Column("last_synced_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_message", sa.Text, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("user_id", "session_id", name="uq_sync_watchlist_user_session"),
    )
    op.create_index("idx_sync_watchlist_user", "sync_watchlist", ["user_id"])


def downgrade() -> None:
    op.drop_index("idx_sync_watchlist_user")
    op.drop_table("sync_watchlist")
    op.drop_column("users", "sync_debounce")
    op.drop_column("users", "sync_mode")
