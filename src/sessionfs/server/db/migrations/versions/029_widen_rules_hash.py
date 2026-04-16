"""Widen sessions.rules_hash from String(64) to String(80).

The provenance capture uses sha256-prefixed hashes: 'sha256:' + 64 hex
chars = 71 chars. The original migration 028 set String(64), which
silently truncates on SQLite but raises on PostgreSQL — causing a 500
on sync push for any session with instruction provenance.

Revision ID: 029
Revises: 028
"""

from alembic import op
import sqlalchemy as sa

revision = "029"
down_revision = "028"


def upgrade() -> None:
    # PostgreSQL: ALTER COLUMN TYPE is a metadata-only change for
    # varchar widening (no table rewrite).
    with op.batch_alter_table("sessions") as batch_op:
        batch_op.alter_column(
            "rules_hash",
            existing_type=sa.String(64),
            type_=sa.String(80),
            existing_nullable=True,
        )


def downgrade() -> None:
    with op.batch_alter_table("sessions") as batch_op:
        batch_op.alter_column(
            "rules_hash",
            existing_type=sa.String(80),
            type_=sa.String(64),
            existing_nullable=True,
        )
