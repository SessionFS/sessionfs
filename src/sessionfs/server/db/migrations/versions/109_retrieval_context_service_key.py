"""Record service-key provenance on retrieval audit contexts (Review Loop R3).

Revision ID: 109
Revises: 108

Additive only.

- retrieval_audit_contexts.actor_type: 'user' or 'service_key' for the
  key that started the ticket's lease. Existing rows back-fill to 'user'
  via the server default.
- retrieval_audit_contexts.service_key_id: the minted key id when the
  start was a service-key call (NULL for user-key and pre-109 rows).

Previously start_ticket wrote only the key's backing user, so a
service-key reviewer could be compared against a user-only start record
whose user is that key's minter and be read as an independent identity.
Storing the key id lets readiness/strict-close fail closed on that case.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "109"
down_revision = "108"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("retrieval_audit_contexts") as batch:
        batch.add_column(
            sa.Column(
                "actor_type",
                sa.String(20),
                nullable=False,
                server_default="user",
            )
        )
        batch.add_column(
            sa.Column("service_key_id", sa.String(36), nullable=True)
        )


def downgrade() -> None:
    with op.batch_alter_table("retrieval_audit_contexts") as batch:
        batch.drop_column("service_key_id")
        batch.drop_column("actor_type")
