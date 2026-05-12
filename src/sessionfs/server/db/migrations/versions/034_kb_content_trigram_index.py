"""Add pg_trgm GIN index on knowledge_entries.content for ILIKE search.

routes/knowledge.py:list_entries does a substring search with
`content.ilike('%query%')`. Pre-v0.9.9.10, this was a sequential scan
on PostgreSQL — fine while KB volume is small but a hotspot at scale.

The pg_trgm extension's gin_trgm_ops opclass lets the planner use a
GIN index for arbitrary ILIKE patterns (including leading-wildcard
queries), so the existing route code gets sped up with zero
application change.

SQLite has no trigram extension; this migration is a no-op there.
KB content search on SQLite stays on the linear scan, which is fine
because SQLite is dev/test only and KB volumes are bounded in those
environments.

Revision ID: 034
Revises: 033
"""

from alembic import op

revision = "034"
down_revision = "033"


def _is_postgres() -> bool:
    bind = op.get_bind()
    return bind.dialect.name == "postgresql"


def upgrade() -> None:
    if not _is_postgres():
        # SQLite / other: no-op. The route falls back to a linear ILIKE
        # which is acceptable at dev/test volumes.
        return

    # Make sure the extension is available. Idempotent.
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")

    # GIN index keyed on content via the trigram opclass. The planner
    # uses this for `ILIKE '%pattern%'` queries with three or more
    # consecutive non-wildcard chars — which covers every real search
    # the dashboard or MCP `search_project_knowledge` tool generates.
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_ke_content_trgm "
        "ON knowledge_entries USING gin (content gin_trgm_ops)"
    )


def downgrade() -> None:
    if not _is_postgres():
        return
    op.execute("DROP INDEX IF EXISTS idx_ke_content_trgm")
    # Leave the pg_trgm extension installed — other tables (or future
    # migrations) may depend on it, and a CREATE EXTENSION IF NOT EXISTS
    # in a later upgrade() is cheap.
