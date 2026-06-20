"""Org owner transfer: org_owner_transfer table with partial unique index.

Revision ID: 051
Revises: 050

P4 of the licensing + org-management redesign
(docs/design/licensing-org-redesign.md §2.4.3).

Additive-only migration:
- org_owner_transfer: two-step ownership transfer with status state machine
  (pending → accepted | cancelled | expired).
- Partial unique index uq_org_owner_transfer_one_pending ON (org_id)
  WHERE status='pending' — at most one pending transfer per org.
- All constraints defined inline at create_table time (SQLite+PG safe,
  same pattern as migration 050).

Downgrade: drop table.
"""

from alembic import op
import sqlalchemy as sa


revision = "051"
down_revision = "050"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "org_owner_transfer",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "org_id",
            sa.String(length=64),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "from_user_id",
            sa.String(length=36),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "to_user_id",
            sa.String(length=36),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "status",
            sa.String(length=20),
            nullable=False,
            server_default="pending",
            comment="pending | accepted | cancelled | expired",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "expires_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column(
            "accepted_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )

    op.create_index(
        "idx_org_owner_transfer_org", "org_owner_transfer", ["org_id"]
    )

    # Partial unique index: at most one pending transfer per org.
    op.create_index(
        "uq_org_owner_transfer_one_pending",
        "org_owner_transfer",
        ["org_id"],
        unique=True,
        postgresql_where=sa.text("status = 'pending'"),
        sqlite_where=sa.text("status = 'pending'"),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_org_owner_transfer_one_pending",
        table_name="org_owner_transfer",
        postgresql_where=sa.text("status = 'pending'"),
        sqlite_where=sa.text("status = 'pending'"),
    )
    op.drop_index("idx_org_owner_transfer_org", table_name="org_owner_transfer")
    op.drop_table("org_owner_transfer")
