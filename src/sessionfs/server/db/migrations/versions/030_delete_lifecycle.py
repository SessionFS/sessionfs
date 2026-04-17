"""Add delete lifecycle columns to sessions table.

Three-scope delete model: cloud / local / everywhere.
Tracks who deleted, the scope, and when the session is eligible for purge.

Revision ID: 030
Revises: 029
"""

from alembic import op
import sqlalchemy as sa

revision = "030"
down_revision = "029"


def upgrade() -> None:
    with op.batch_alter_table("sessions") as batch_op:
        batch_op.add_column(
            sa.Column("deleted_by", sa.String(36), nullable=True)
        )
        batch_op.add_column(
            sa.Column("delete_scope", sa.String(16), nullable=True)
        )
        batch_op.add_column(
            sa.Column(
                "purge_after",
                sa.DateTime(timezone=True),
                nullable=True,
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("sessions") as batch_op:
        batch_op.drop_column("purge_after")
        batch_op.drop_column("delete_scope")
        batch_op.drop_column("deleted_by")
