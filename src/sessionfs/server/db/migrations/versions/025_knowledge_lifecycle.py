"""Add knowledge lifecycle fields to knowledge_entries and projects.

Revision ID: 025
Revises: 024
"""

from alembic import op
import sqlalchemy as sa

revision = "025"
down_revision = "024"


def upgrade() -> None:
    op.add_column(
        "knowledge_entries",
        sa.Column("last_relevant_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "knowledge_entries",
        sa.Column("reference_count", sa.Integer, nullable=False, server_default="0"),
    )
    op.add_column(
        "knowledge_entries",
        sa.Column("superseded_by", sa.Integer, nullable=True),
    )
    op.add_column(
        "projects",
        sa.Column("kb_retention_days", sa.Integer, nullable=False, server_default="180"),
    )
    op.add_column(
        "projects",
        sa.Column("kb_max_context_words", sa.Integer, nullable=False, server_default="8000"),
    )
    op.add_column(
        "projects",
        sa.Column("kb_section_page_limit", sa.Integer, nullable=False, server_default="30"),
    )


def downgrade() -> None:
    op.drop_column("projects", "kb_section_page_limit")
    op.drop_column("projects", "kb_max_context_words")
    op.drop_column("projects", "kb_retention_days")
    op.drop_column("knowledge_entries", "superseded_by")
    op.drop_column("knowledge_entries", "reference_count")
    op.drop_column("knowledge_entries", "last_relevant_at")
