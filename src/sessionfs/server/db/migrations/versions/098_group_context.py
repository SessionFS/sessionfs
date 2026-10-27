"""Project-group knowledge context (GitHub #101, phase 2).

Revision ID: 098
Revises: 097

Adds the group-compile columns to ``project_groups`` so a group can hold a
deterministic overview built from its member projects' compiled context and
top active claims:

- ``compile_enabled`` — opt-in flag (server default false). Only groups with
  this set are (re)compiled by the KB compile sweep; existing groups opt out
  until an admin turns it on, so no pre-existing group is swept without a
  deliberate choice.
- ``context_document`` — the deterministic overview text the compile writes.
- ``context_compiled_at`` — when the overview was last compiled (NULL = never).
- ``compiled_by_user_id`` — the user who last compiled (the group admin that
  ran the compile, or the group creator for a scheduled recompile). A plain
  string pointer like ``knowledge_entries.user_id``, not a foreign key, so the
  column can be added in-place across dialects.
- ``context_sources`` — JSON array of the contributing project ids (the
  member projects the compiler could read at compile time). The context view
  returns the stored document only to a caller who can still read every
  contributing project.
- ``compile_attempted_at`` — when the scheduled sweep last claimed the group
  (whether the recompile then succeeded, was skipped or failed). The sweep
  orders due groups by it, so a group that keeps being skipped or failing
  rotates behind healthy ones, and claims a group with a conditional UPDATE
  on it, so two overlapping sweeps never compile the same group.

Purely additive; downgrade drops the columns.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "098"
down_revision = "097"
branch_labels = None
depends_on = None

_COLUMNS = (
    "compile_enabled",
    "context_document",
    "context_compiled_at",
    "compiled_by_user_id",
    "context_sources",
    "compile_attempted_at",
)


def upgrade() -> None:
    op.add_column(
        "project_groups",
        sa.Column("compile_enabled", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )
    op.add_column(
        "project_groups",
        sa.Column("context_document", sa.Text(), nullable=False, server_default=""),
    )
    op.add_column(
        "project_groups",
        sa.Column("context_compiled_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "project_groups",
        sa.Column("compiled_by_user_id", sa.String(length=36), nullable=True),
    )
    op.add_column(
        "project_groups",
        sa.Column("context_sources", sa.JSON(), nullable=True),
    )
    op.add_column(
        "project_groups",
        sa.Column("compile_attempted_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    # Plain DROP COLUMN on both dialects (SQLite >= 3.35): no table rebuild.
    for column in reversed(_COLUMNS):
        op.execute(sa.text(f"ALTER TABLE project_groups DROP COLUMN {column}"))
