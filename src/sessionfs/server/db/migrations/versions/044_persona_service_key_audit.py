"""Agent persona service-key audit columns.

Revision ID: 044
Revises: 043

Phase 3.6 service-key opt-in adds persona CRUD routes to
require_scope("personas:read") / require_scope("personas:write").
AgentPersona rows need the same service-key provenance shape used by
the v0.10.10 audit-row tables and v0.10.19 Ticket rows so persona
writes do not silently look like direct human writes.

Strictly additive: nullable columns only, no defaults, no constraints,
no indexes.
"""

from alembic import op
import sqlalchemy as sa


revision = "044"
down_revision = "043"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("agent_personas") as batch:
        batch.add_column(sa.Column("actor_type", sa.String(20), nullable=True))
        batch.add_column(sa.Column("service_key_id", sa.String(36), nullable=True))
        batch.add_column(sa.Column("service_key_name", sa.String(100), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("agent_personas") as batch:
        batch.drop_column("service_key_name")
        batch.drop_column("service_key_id")
        batch.drop_column("actor_type")
