"""Knowledge Base v2: claim classification, entity refs, freshness, usage counters.

Adds claim_class, entity_ref, entity_type, freshness_class, supersession_reason,
promoted_at, promoted_by, and three usage counters to knowledge_entries.
Backfills non-dismissed entries as 'claim', dismissed as 'note'.

Revision ID: 027
Revises: 026
"""

from alembic import op
import sqlalchemy as sa

revision = "027"
down_revision = "026"


def upgrade() -> None:
    op.add_column(
        "knowledge_entries",
        sa.Column("claim_class", sa.String(20), nullable=False, server_default="claim"),
    )
    op.add_column(
        "knowledge_entries",
        sa.Column("entity_ref", sa.String(200), nullable=True),
    )
    op.add_column(
        "knowledge_entries",
        sa.Column("entity_type", sa.String(50), nullable=True),
    )
    op.add_column(
        "knowledge_entries",
        sa.Column("freshness_class", sa.String(20), nullable=False, server_default="current"),
    )
    op.add_column(
        "knowledge_entries",
        sa.Column("supersession_reason", sa.Text, nullable=True),
    )
    op.add_column(
        "knowledge_entries",
        sa.Column("promoted_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "knowledge_entries",
        sa.Column("promoted_by", sa.String(50), nullable=True),
    )
    op.add_column(
        "knowledge_entries",
        sa.Column("retrieved_count", sa.Integer, nullable=False, server_default="0"),
    )
    op.add_column(
        "knowledge_entries",
        sa.Column("used_in_answer_count", sa.Integer, nullable=False, server_default="0"),
    )
    op.add_column(
        "knowledge_entries",
        sa.Column("compiled_count", sa.Integer, nullable=False, server_default="0"),
    )

    # Composite index for active-claim queries (compile, get_project_context, etc.)
    op.create_index(
        "idx_ke_active_claims",
        "knowledge_entries",
        ["project_id", "claim_class", "freshness_class", "dismissed"],
    )
    # Index for entity-scoped queries and auto-supersession
    op.create_index(
        "idx_ke_entity",
        "knowledge_entries",
        ["project_id", "entity_ref"],
    )

    # Backfill: non-dismissed existing entries become 'claim', dismissed become 'note'
    op.execute(
        "UPDATE knowledge_entries SET claim_class = 'claim' WHERE dismissed = false"
    )
    op.execute(
        "UPDATE knowledge_entries SET claim_class = 'note' WHERE dismissed = true"
    )

    # Backfill: existing projects still carry kb_max_context_words = 8000
    # from migration 025. The v2 default is 2000 (active-truth-per-token
    # principle — smaller budgets produce sharper context docs). Update
    # projects that still have the old default so upgraded instances get
    # the new behavior without manual per-project reconfiguration.
    op.execute(
        "UPDATE projects SET kb_max_context_words = 2000 "
        "WHERE kb_max_context_words = 8000 OR kb_max_context_words IS NULL"
    )


def downgrade() -> None:
    op.drop_index("idx_ke_entity", table_name="knowledge_entries")
    op.drop_index("idx_ke_active_claims", table_name="knowledge_entries")
    op.drop_column("knowledge_entries", "compiled_count")
    op.drop_column("knowledge_entries", "used_in_answer_count")
    op.drop_column("knowledge_entries", "retrieved_count")
    op.drop_column("knowledge_entries", "promoted_by")
    op.drop_column("knowledge_entries", "promoted_at")
    op.drop_column("knowledge_entries", "supersession_reason")
    op.drop_column("knowledge_entries", "freshness_class")
    op.drop_column("knowledge_entries", "entity_type")
    op.drop_column("knowledge_entries", "entity_ref")
    op.drop_column("knowledge_entries", "claim_class")
