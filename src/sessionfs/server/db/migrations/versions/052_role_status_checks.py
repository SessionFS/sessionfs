"""Vocabulary CHECK constraints for org_members.role + org_owner_transfer.status.

Revision ID: 052
Revises: 051

P4 follow-up (Sentinel L2) — structural guard so a future direct-write path
can't insert an out-of-vocabulary role/status. The application chokepoint
(perform_role_change) remains the primary control; this is defense in depth.

PostgreSQL only: SQLite cannot ALTER-ADD a CHECK constraint without a full
table rebuild (batch mode), which on org_members risks dropping the partial
unique index uq_org_members_one_owner_per_org. The constraint IS defined on the
ORM models, so create_all (the SQLite test path) enforces it; production runs
PostgreSQL where this migration adds the real constraint. Guarded + no-op on
SQLite.

Downgrade: drop the constraints (PG only).
"""

from alembic import op


revision = "052"
down_revision = "051"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        # SQLite/others: enforced via the ORM CheckConstraint on create_all.
        return
    op.create_check_constraint(
        "ck_org_members_role",
        "org_members",
        "role IN ('owner', 'admin', 'member')",
    )
    op.create_check_constraint(
        "ck_org_owner_transfer_status",
        "org_owner_transfer",
        "status IN ('pending', 'accepted', 'cancelled', 'expired')",
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    op.drop_constraint(
        "ck_org_owner_transfer_status", "org_owner_transfer", type_="check"
    )
    op.drop_constraint("ck_org_members_role", "org_members", type_="check")
