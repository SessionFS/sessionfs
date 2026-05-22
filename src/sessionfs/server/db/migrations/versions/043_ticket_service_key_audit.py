"""Ticket service-key audit columns.

Revision ID: 043
Revises: 042

Phase 3.5 service-key opt-in adds POST /projects/{pid}/tickets to
require_scope("tickets:write"). Ticket rows need the same service-key
provenance shape used by the v0.10.10 audit-row tables so a created
ticket does not silently look like a direct human write.

Strictly additive: nullable columns only, no defaults, no constraints,
no indexes.
"""

from alembic import op
import sqlalchemy as sa


revision = "043"
down_revision = "042"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("tickets") as batch:
        batch.add_column(sa.Column("actor_type", sa.String(20), nullable=True))
        batch.add_column(sa.Column("service_key_id", sa.String(36), nullable=True))
        batch.add_column(sa.Column("service_key_name", sa.String(100), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("tickets") as batch:
        batch.drop_column("service_key_name")
        batch.drop_column("service_key_id")
        batch.drop_column("actor_type")
