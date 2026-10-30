"""Wiki page revision service-key audit columns.

Revision ID: 107
Revises: 106

G0 opens wiki writes to service keys with ``knowledge:write``. Revision
rows need the same service-key provenance shape as tickets (migration 043)
so a wiki edit made by a resident service key does not silently look like
a direct human write.

Strictly additive: nullable columns only, no defaults, no constraints,
no indexes.
"""

from alembic import op
import sqlalchemy as sa


revision = "107"
down_revision = "106"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("wiki_page_revisions") as batch:
        batch.add_column(sa.Column("actor_type", sa.String(20), nullable=True))
        batch.add_column(sa.Column("service_key_id", sa.String(36), nullable=True))
        batch.add_column(sa.Column("service_key_name", sa.String(100), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("wiki_page_revisions") as batch:
        batch.drop_column("service_key_name")
        batch.drop_column("service_key_id")
        batch.drop_column("actor_type")
