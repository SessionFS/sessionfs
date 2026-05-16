"""Add wiki page revisions table + personas_active on session summaries.

Revision ID: 040
Revises: 039

v0.10.7 customer-ask provenance fields (tk_a1144426a013413c):
- New wiki_page_revisions table for per-revision authorship history
- New session_summaries.personas_active column (JSON list of personas
  observed across the session — session-level approximation of
  per-decision authorship; see Atlas's scope note in tc_3862682a48014ee4)

The wiki_page_revisions schema captures (revised_at, user_id,
persona_name, ticket_id, content_snapshot, revision_number) per page
edit. Full snapshots over diffs — KB pages are bounded in size and
snapshots are simpler to query. Composite index on (project_id,
page_slug, revised_at DESC, id DESC) mirrors the access pattern and
provides stable history ordering with id-tiebreak (same shape as
ContextCompilation in v0.10.5). Unique constraint on (project_id,
page_slug, revision_number) enables monotone per-page numbering.
"""

from alembic import op
import sqlalchemy as sa


revision = "040"
down_revision = "039"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "session_summaries",
        sa.Column(
            "personas_active",
            sa.Text(),
            nullable=False,
            server_default="[]",
        ),
    )
    # v0.10.7 R7 Codex catch — the UNIQUE constraint must be defined
    # inside op.create_table so SQLite can create it at table-creation
    # time. Calling op.create_unique_constraint as a separate ALTER
    # TABLE step (the original 040 shape) fails on SQLite because
    # SQLite ALTER TABLE doesn't support adding constraints. Since
    # this migration hasn't shipped, the right move is an in-place
    # fix rather than a follow-up repair migration — a linear
    # follow-up cannot heal a fresh SQLite chain that halts at 040's
    # failure before reaching the repair revision.
    op.create_table(
        "wiki_page_revisions",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "project_id",
            sa.String(64),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("page_slug", sa.String(100), nullable=False),
        sa.Column("revision_number", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(255), nullable=False),
        sa.Column("content_snapshot", sa.Text(), nullable=False, server_default=""),
        sa.Column("word_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "user_id",
            sa.String(36),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("persona_name", sa.String(50), nullable=True),
        sa.Column("ticket_id", sa.String(64), nullable=True),
        sa.Column(
            "revised_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "project_id",
            "page_slug",
            "revision_number",
            name="uq_wiki_revisions_number",
        ),
    )
    op.create_index(
        "idx_wiki_revisions_history",
        "wiki_page_revisions",
        ["project_id", "page_slug", "revised_at", "id"],
    )


def downgrade() -> None:
    # Dropping the table drops the inline UNIQUE constraint with it,
    # so no separate op.drop_constraint call is needed (and on SQLite
    # such a call would fail since ALTER TABLE can't drop constraints).
    op.drop_index("idx_wiki_revisions_history", table_name="wiki_page_revisions")
    op.drop_table("wiki_page_revisions")
    op.drop_column("session_summaries", "personas_active")
