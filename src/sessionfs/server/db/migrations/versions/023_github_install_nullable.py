"""Make GitHub installation user_id, account_login, account_type nullable.

Revision ID: 023
Revises: 022
"""

from alembic import op
import sqlalchemy as sa

revision = "023"
down_revision = "022"


def upgrade() -> None:
    op.alter_column("github_installations", "user_id", existing_type=sa.String(36), nullable=True)
    op.alter_column("github_installations", "account_login", existing_type=sa.String(255), nullable=True)
    op.alter_column("github_installations", "account_type", existing_type=sa.String(20), nullable=True)


def downgrade() -> None:
    op.alter_column("github_installations", "user_id", existing_type=sa.String(36), nullable=False)
    op.alter_column("github_installations", "account_login", existing_type=sa.String(255), nullable=False)
    op.alter_column("github_installations", "account_type", existing_type=sa.String(20), nullable=False)
