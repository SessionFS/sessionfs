"""Add session_summaries table.

Revision ID: 015
Revises: 014
"""

from alembic import op
import sqlalchemy as sa

revision = "015"
down_revision = "014"


def upgrade() -> None:
    op.create_table(
        "session_summaries",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("session_id", sa.String(64), sa.ForeignKey("sessions.id", ondelete="CASCADE"), nullable=False, unique=True),
        sa.Column("duration_minutes", sa.Integer, nullable=True),
        sa.Column("tool_call_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("files_modified", sa.Text, nullable=False, server_default="[]"),
        sa.Column("files_read", sa.Text, nullable=False, server_default="[]"),
        sa.Column("commands_executed", sa.Integer, nullable=False, server_default="0"),
        sa.Column("tests_run", sa.Integer, nullable=False, server_default="0"),
        sa.Column("tests_passed", sa.Integer, nullable=False, server_default="0"),
        sa.Column("tests_failed", sa.Integer, nullable=False, server_default="0"),
        sa.Column("packages_installed", sa.Text, nullable=False, server_default="[]"),
        sa.Column("errors_encountered", sa.Text, nullable=False, server_default="[]"),
        sa.Column("what_happened", sa.Text, nullable=True),
        sa.Column("key_decisions", sa.Text, nullable=True),
        sa.Column("outcome", sa.Text, nullable=True),
        sa.Column("open_issues", sa.Text, nullable=True),
        sa.Column("narrative_model", sa.String(100), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.add_column("users", sa.Column("summarize_trigger", sa.String(20), nullable=False, server_default="manual"))


def downgrade() -> None:
    op.drop_column("users", "summarize_trigger")
    op.drop_table("session_summaries")
