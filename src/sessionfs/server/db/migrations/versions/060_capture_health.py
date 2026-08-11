"""Capture-health column on users — daemon reports per-watcher health.

Revision ID: 060
Revises: 059

Strictly additive: a single nullable Text column on users so the daemon
can upload per-watcher capture health (JSON-encoded list of watcher
status objects) and the dashboard can display it.  No backfill needed
because it's nullable — existing rows stay NULL.

Works on both SQLite and PostgreSQL (plain ALTER … ADD COLUMN).
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "060"
down_revision = "059"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("capture_health", sa.Text, nullable=True),
    )


def downgrade() -> None:
    op.drop_column("users", "capture_health")
