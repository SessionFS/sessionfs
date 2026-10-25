"""Make retrieval audit project_id nullable for helper turns (piece 3).

Revision ID: 090
Revises: 083

A session-helper turn can be scoped to a set of sessions that span projects
(or have no project), so the helper's per-call retrieval-audit event can no
longer always name a project.  Both ``retrieval_audit_contexts.project_id``
and ``retrieval_audit_events.project_id`` become nullable; the API routes keep
filtering by project, so a context/event created through the API still always
carries one.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "090"
down_revision = "089"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # batch_alter_table is required so SQLite (used in tests) can change the
    # nullability — plain ALTER COLUMN ... DROP NOT NULL is unsupported there.
    with op.batch_alter_table("retrieval_audit_contexts") as batch:
        batch.alter_column(
            "project_id",
            existing_type=sa.String(64),
            nullable=True,
        )
    with op.batch_alter_table("retrieval_audit_events") as batch:
        batch.alter_column(
            "project_id",
            existing_type=sa.String(64),
            nullable=True,
        )


def downgrade() -> None:
    # Re-assert NOT NULL.  Only helper rows can carry a NULL project_id
    # (API-created contexts/events always set one), so drop exactly those
    # before the batch table recreation re-applies NOT NULL: helper events
    # explicitly, then any NULL-project contexts (whose events cascade).
    op.execute(
        "DELETE FROM retrieval_audit_events "
        "WHERE source = 'helper' AND project_id IS NULL"
    )
    op.execute("DELETE FROM retrieval_audit_contexts WHERE project_id IS NULL")
    with op.batch_alter_table("retrieval_audit_contexts") as batch:
        batch.alter_column(
            "project_id",
            existing_type=sa.String(64),
            nullable=False,
        )
    with op.batch_alter_table("retrieval_audit_events") as batch:
        batch.alter_column(
            "project_id",
            existing_type=sa.String(64),
            nullable=False,
        )
