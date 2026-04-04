"""Add knowledge entries and context compilations tables.

Revision ID: 018
Revises: 017
"""

from alembic import op
import sqlalchemy as sa

revision = "018"
down_revision = "017"


def upgrade() -> None:
    # --- knowledge_entries table ---
    op.create_table(
        "knowledge_entries",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("project_id", sa.String(64), nullable=False),
        sa.Column("session_id", sa.String(64), nullable=False),
        sa.Column("user_id", sa.String(64), nullable=False),
        sa.Column("entry_type", sa.String(20), nullable=False),
        sa.Column("content", sa.Text, nullable=False),
        sa.Column("confidence", sa.Float, server_default="1.0"),
        sa.Column("source_context", sa.Text, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("compiled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("dismissed", sa.Boolean, server_default="false"),
    )
    op.create_index("idx_ke_project", "knowledge_entries", ["project_id"])
    op.create_index("idx_ke_session", "knowledge_entries", ["session_id"])
    op.create_index("idx_ke_type", "knowledge_entries", ["project_id", "entry_type"])

    # --- context_compilations table ---
    op.create_table(
        "context_compilations",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("project_id", sa.String(64), nullable=False),
        sa.Column("user_id", sa.String(64), nullable=False),
        sa.Column("entries_compiled", sa.Integer, nullable=False),
        sa.Column("context_before", sa.Text, nullable=True),
        sa.Column("context_after", sa.Text, nullable=True),
        sa.Column("compiled_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )


def downgrade() -> None:
    op.drop_table("context_compilations")
    op.drop_index("idx_ke_type", table_name="knowledge_entries")
    op.drop_index("idx_ke_session", table_name="knowledge_entries")
    op.drop_index("idx_ke_project", table_name="knowledge_entries")
    op.drop_table("knowledge_entries")
