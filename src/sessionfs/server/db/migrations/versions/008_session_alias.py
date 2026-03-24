"""Add alias column to sessions for user-chosen short names.

Revision ID: 008
Revises: 007
"""
from alembic import op
import sqlalchemy as sa

revision = "008"
down_revision = "007"


def upgrade():
    op.add_column("sessions", sa.Column("alias", sa.String(100), nullable=True))
    op.create_index("idx_sessions_alias", "sessions", ["user_id", "alias"], unique=True)


def downgrade():
    op.drop_index("idx_sessions_alias")
    op.drop_column("sessions", "alias")
