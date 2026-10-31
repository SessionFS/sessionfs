"""Direct release → ticket links (Review Loop R3, Brief R1).

Revision ID: 117
Revises: 116

One join table for the PR-less release flow: a ticket can be linked straight
to a release without going through a pull request. The composite primary key
``(release_id, ticket_id)`` is the unique pair, so ``ON CONFLICT DO NOTHING``
makes re-linking idempotent. ``linked_by_user_id`` / ``service_key_id`` stamp
provenance (a service key leaves the user stamp NULL, review checklist #3).
Both FKs cascade so a link never outlives either endpoint.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "117"
down_revision = "116"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "release_tickets",
        sa.Column(
            "release_id",
            sa.String(length=64),
            sa.ForeignKey("releases.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "ticket_id",
            sa.String(length=64),
            sa.ForeignKey("tickets.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "linked_by_user_id",
            sa.String(length=64),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("service_key_id", sa.String(length=36), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index(
        "idx_release_tickets_ticket", "release_tickets", ["ticket_id"]
    )


def downgrade() -> None:
    op.drop_index("idx_release_tickets_ticket", table_name="release_tickets")
    op.drop_table("release_tickets")
