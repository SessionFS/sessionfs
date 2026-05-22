"""Knowledge-entry agent author attribution.

Revision ID: 045
Revises: 044

Phase 4a makes agent-authored knowledge entries first-class rows by
adding:
- persona_name: nullable attribution to a project persona.
- author_class: non-null human/agent marker, defaulting to "human" so
  PostgreSQL backfills existing rows and SQLite 3.20+ can add the
  column in place with NOT NULL DEFAULT.
- idx_knowledge_persona_recent: (project_id, persona_name, created_at
  DESC) for Scout v4 per-persona recent retrieval.

Persona validation policy: the Phase 4a proposal called for free-text
persona_name, but the shipped v0.10.7 wiki PageWriteRequest path
validates persona_name against agent_personas in the project. KB entry
writes mirror that existing provenance policy instead of introducing a
second attribution rule.

Strictly additive: two columns + one composite index.
"""

from alembic import op
import sqlalchemy as sa


revision = "045"
down_revision = "044"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("knowledge_entries") as batch:
        batch.add_column(sa.Column("persona_name", sa.String(64), nullable=True))
        batch.add_column(
            sa.Column(
                "author_class",
                sa.String(16),
                nullable=False,
                server_default="human",
            )
        )
        # NB: index columns intentionally use plain string `"created_at"`
        # rather than `sa.text("created_at DESC")`. Both PostgreSQL and
        # SQLite can walk an ascending index in reverse for
        # ORDER BY created_at DESC queries, so an ASC composite index
        # serves the Scout v4 per-persona recent-retrieval path equally
        # well. The TextClause form was also flagged by CI mypy (list-item
        # 2 has incompatible type "TextClause"; expected "str") so the
        # plain-string form is both portable and type-clean.
        batch.create_index(
            "idx_knowledge_persona_recent",
            ["project_id", "persona_name", "created_at"],
        )


def downgrade() -> None:
    with op.batch_alter_table("knowledge_entries") as batch:
        batch.drop_index("idx_knowledge_persona_recent")
        batch.drop_column("author_class")
        batch.drop_column("persona_name")
