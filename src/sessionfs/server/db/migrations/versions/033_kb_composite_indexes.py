"""Add composite indexes on knowledge_entries for the Tier A list paths.

list_entries filters/sorts on dismissed, claim_class, freshness_class,
compiled_at, created_at — but the table only had indexes on project_id,
session_id, and (project_id, entry_type). Three composites cover the
hot paths:

- idx_ke_listing: default list filter combo (dismissed + class)
- idx_ke_pending: pending-compile path (compiled_at IS NULL)
- idx_ke_cursor:  keyset cursor scan order (project_id, created_at)

All creates are IF NOT EXISTS so the migration is re-runnable on a
deploy that already has matching indexes from a hand-applied fix.

Revision ID: 033
Revises: 032
"""

from alembic import op

revision = "033"
down_revision = "032"


def upgrade() -> None:
    op.create_index(
        "idx_ke_listing",
        "knowledge_entries",
        ["project_id", "dismissed", "claim_class", "freshness_class"],
        if_not_exists=True,
    )
    op.create_index(
        "idx_ke_pending",
        "knowledge_entries",
        ["project_id", "compiled_at", "dismissed"],
        if_not_exists=True,
    )
    op.create_index(
        "idx_ke_cursor",
        "knowledge_entries",
        ["project_id", "created_at"],
        if_not_exists=True,
    )


def downgrade() -> None:
    op.drop_index("idx_ke_cursor", table_name="knowledge_entries", if_exists=True)
    op.drop_index("idx_ke_pending", table_name="knowledge_entries", if_exists=True)
    op.drop_index("idx_ke_listing", table_name="knowledge_entries", if_exists=True)
