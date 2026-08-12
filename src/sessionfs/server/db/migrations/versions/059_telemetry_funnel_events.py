"""Telemetry funnel events — client-emitter vocabulary.

Revision ID: 059
Revises: 058

Strictly additive: three nullable columns on telemetry_events so the
new client-side emitter can send event/event_ts/tool without a schema
change.  No backfill needed because they're nullable — existing rows
stay NULL.

Works on both SQLite and PostgreSQL (plain ALTER … ADD COLUMN).
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "059"
down_revision = "058"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "telemetry_events",
        sa.Column("event", sa.String(length=40), nullable=True),
    )
    op.add_column(
        "telemetry_events",
        sa.Column("event_ts", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "telemetry_events",
        sa.Column("tool", sa.String(length=50), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("telemetry_events", "tool")
    op.drop_column("telemetry_events", "event_ts")
    op.drop_column("telemetry_events", "event")
