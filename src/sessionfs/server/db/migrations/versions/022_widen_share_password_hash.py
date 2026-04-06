"""Widen share_links.password_hash for PBKDF2 salt$hash format.

Revision ID: 022
Revises: 021
"""

from alembic import op
import sqlalchemy as sa

revision = "022"
down_revision = "021"


def upgrade() -> None:
    op.alter_column(
        "share_links",
        "password_hash",
        type_=sa.String(200),
        existing_type=sa.String(64),
        existing_nullable=True,
    )


def downgrade() -> None:
    op.alter_column(
        "share_links",
        "password_hash",
        type_=sa.String(64),
        existing_type=sa.String(200),
        existing_nullable=True,
    )
