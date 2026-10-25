"""Scheduled KB compile settings and compile trigger (GitHub #93).

Revision ID: 089
Revises: 083

Additive only. Gives operators a way to keep long-lived project knowledge
bases fresh on a cadence instead of relying on a user clicking Compile.

- projects.compile_schedule: VARCHAR(16) NOT NULL, server default 'off'.
  One of 'off', 'nightly', 'weekly'. The server default backfills 'off'
  onto every existing project so the sweep never starts compiling rows the
  operator has not opted in; NEW projects are created with an explicit
  'nightly' (see routes/projects.py), so the blast radius of the sweep is
  limited to projects created after this release.
- projects.compile_after_claims: nullable Integer. When set, the sweep also
  compiles a project once it has this many pending claims, regardless of
  schedule.
- projects.last_scheduled_compile_at: nullable timestamptz. When the sweep
  last compiled (or last attempted) this project. Set only for scheduled
  compiles, never manual.
- projects.last_decay_at: nullable timestamptz. When confidence decay last
  ran for this project. Decay is time-based (0.8 ** floor(elapsed / 30d)),
  not per-compile, so nightly compiles no longer compound decay.
- context_compilations.trigger: VARCHAR(16) NOT NULL, server default
  'manual'. 'manual' = a user clicked Compile; 'scheduled' = the sweep ran
  it. Every pre-089 row was manual, so the default backfills them correctly.

Downgrade drops the columns.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "089"
down_revision = "088"
branch_labels = None
depends_on = None

_SCHEDULE_COLUMNS = (
    "compile_schedule",
    "compile_after_claims",
    "last_scheduled_compile_at",
    "last_decay_at",
)


def upgrade() -> None:
    op.add_column(
        "projects",
        sa.Column("compile_schedule", sa.String(16), nullable=False, server_default="off"),
    )
    op.add_column(
        "projects",
        sa.Column("compile_after_claims", sa.Integer(), nullable=True),
    )
    op.add_column(
        "projects",
        sa.Column("last_scheduled_compile_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "projects",
        sa.Column("last_decay_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "context_compilations",
        sa.Column("trigger", sa.String(16), nullable=False, server_default="manual"),
    )


def downgrade() -> None:
    # Plain DROP COLUMN on both dialects (SQLite >= 3.35): no table rebuild.
    op.execute(sa.text("ALTER TABLE context_compilations DROP COLUMN trigger"))
    for column in reversed(_SCHEDULE_COLUMNS):
        op.execute(sa.text(f"ALTER TABLE projects DROP COLUMN {column}"))
