"""Link a rotated service key to the key it replaced.

Revision ID: 118
Revises: 117

Additive only. ``api_keys.rotated_from_key_id`` (nullable, no FK on purpose)
lets the self-review identity check follow a rotation chain so that an org
admin who rotates a member's key is part of that key's identity. Keys rotated
before this release stay NULL and resolve to {user_id, created_by_user_id}.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "118"
down_revision = "117"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("api_keys") as batch:
        batch.add_column(
            sa.Column("rotated_from_key_id", sa.String(length=36), nullable=True)
        )


def downgrade() -> None:
    with op.batch_alter_table("api_keys") as batch:
        batch.drop_column("rotated_from_key_id")
