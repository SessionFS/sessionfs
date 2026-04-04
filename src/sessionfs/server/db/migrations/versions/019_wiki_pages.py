"""Add wiki pages, knowledge links, and auto_narrative.

Revision ID: 019
Revises: 018
"""

from alembic import op
import sqlalchemy as sa

revision = "019"
down_revision = "018"


def upgrade() -> None:
    # --- knowledge_pages table ---
    op.create_table(
        "knowledge_pages",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("project_id", sa.String(64), nullable=False),
        sa.Column("slug", sa.String(100), nullable=False),
        sa.Column("title", sa.String(255), nullable=False),
        sa.Column("page_type", sa.String(20), nullable=False),
        sa.Column("content", sa.Text, nullable=False, server_default=""),
        sa.Column("word_count", sa.Integer, server_default="0"),
        sa.Column("entry_count", sa.Integer, server_default="0"),
        sa.Column("parent_slug", sa.String(100), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("auto_generated", sa.Boolean, server_default="false"),
        sa.UniqueConstraint("project_id", "slug", name="uq_kp_project_slug"),
    )
    op.create_index("idx_kp_project", "knowledge_pages", ["project_id"])

    # --- knowledge_links table ---
    op.create_table(
        "knowledge_links",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("project_id", sa.String(64), nullable=False),
        sa.Column("source_type", sa.String(20), nullable=False),
        sa.Column("source_id", sa.String(64), nullable=False),
        sa.Column("target_type", sa.String(20), nullable=False),
        sa.Column("target_id", sa.String(64), nullable=False),
        sa.Column("link_type", sa.String(20), nullable=False, server_default="related"),
        sa.Column("confidence", sa.Float, server_default="1.0"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint(
            "project_id", "source_type", "source_id", "target_type", "target_id",
            name="uq_kl_link",
        ),
    )
    op.create_index("idx_kl_source", "knowledge_links", ["project_id", "source_type", "source_id"])
    op.create_index("idx_kl_target", "knowledge_links", ["project_id", "target_type", "target_id"])

    # --- auto_narrative on projects ---
    op.add_column("projects", sa.Column("auto_narrative", sa.Boolean, server_default="false"))


def downgrade() -> None:
    op.drop_column("projects", "auto_narrative")
    op.drop_index("idx_kl_target", table_name="knowledge_links")
    op.drop_index("idx_kl_source", table_name="knowledge_links")
    op.drop_table("knowledge_links")
    op.drop_index("idx_kp_project", table_name="knowledge_pages")
    op.drop_table("knowledge_pages")
