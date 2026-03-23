"""Widen token and size columns to bigint.

Revision ID: 004
Revises: 003
"""
from alembic import op
import sqlalchemy as sa

revision = "004"
down_revision = "003"


def upgrade():
    op.alter_column("sessions", "total_input_tokens", type_=sa.BigInteger())
    op.alter_column("sessions", "total_output_tokens", type_=sa.BigInteger())
    op.alter_column("sessions", "duration_ms", type_=sa.BigInteger())
    op.alter_column("sessions", "blob_size_bytes", type_=sa.BigInteger())


def downgrade():
    op.alter_column("sessions", "total_input_tokens", type_=sa.Integer())
    op.alter_column("sessions", "total_output_tokens", type_=sa.Integer())
    op.alter_column("sessions", "duration_ms", type_=sa.Integer())
    op.alter_column("sessions", "blob_size_bytes", type_=sa.Integer())
