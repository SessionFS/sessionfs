"""Admin-proposed project transfer with owner consent (GitHub #96).

Revision ID: 087
Revises: 083

Additive only. `project_transfers` (migration 035) gains three columns to
support the "org admin proposes a transfer, the project owner consents"
flow:

- ``proposed_by_user_id`` (nullable, FK users SET NULL): the destination-org
  admin who proposed the move. Its presence is the load-bearing flag that
  distinguishes a proposal from an owner-initiated transfer: a pending row
  with a non-NULL ``proposed_by_user_id`` is an incoming proposal in the
  project owner's inbox, and its acceptor must be the project owner (not an
  org admin). ON DELETE SET NULL so a deleted proposer does not destroy the
  audit row.
- ``proposal_reason`` (nullable Text): the proposer's justification, shown to
  the owner in the inbox and quoted in the decline/expiry email back to the
  proposer.
- ``expires_at`` (nullable timestamptz): a proposal expires 7 days after
  creation, lazily flipped pending → expired (the same pattern as the
  org-owner-transfer expiry in migration 051 / org_members.py).

No data migration: every existing row is an owner-initiated transfer, so the
three columns are NULL for it. The existing one-pending-per-project partial
unique index (``idx_project_transfers_pending_unique``) already covers
proposals, since they sit in ``state='pending'`` like any other transfer.

The FK is PostgreSQL-only, matching 079/080: SQLite cannot add a foreign key
to an existing table without a full table rebuild, and a plain
``ALTER TABLE ... DROP COLUMN`` on a column that still carries a child FK
fails there. Downgrade drops the index, the constraint (PG) and the columns.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "087"
down_revision = "086"
branch_labels = None
depends_on = None

_TABLE = "project_transfers"


def upgrade() -> None:
    sqlite = op.get_bind().dialect.name == "sqlite"
    op.add_column(
        _TABLE,
        sa.Column("proposed_by_user_id", sa.String(36), nullable=True),
    )
    if not sqlite:
        op.create_foreign_key(
            "fk_project_transfers_proposed_by_user_id",
            _TABLE,
            "users",
            ["proposed_by_user_id"],
            ["id"],
            ondelete="SET NULL",
        )
    op.add_column(
        _TABLE,
        sa.Column("proposal_reason", sa.Text(), nullable=True),
    )
    op.add_column(
        _TABLE,
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "idx_project_transfers_proposed_by", _TABLE, ["proposed_by_user_id"]
    )


def downgrade() -> None:
    op.drop_index("idx_project_transfers_proposed_by", table_name=_TABLE)
    if op.get_bind().dialect.name != "sqlite":
        op.drop_constraint(
            "fk_project_transfers_proposed_by_user_id", _TABLE, type_="foreignkey"
        )
    # Plain DROP COLUMN on both dialects (SQLite >= 3.35): no table rebuild.
    # The FK is PostgreSQL-only, so the column carries no child FK on SQLite.
    for column in ("expires_at", "proposal_reason", "proposed_by_user_id"):
        op.execute(sa.text(f"ALTER TABLE {_TABLE} DROP COLUMN {column}"))
