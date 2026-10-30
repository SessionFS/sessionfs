"""Add `needs_human` to the work-queue item status vocabulary (Review Loop D2).

Revision ID: 111
Revises: 110

When a review loop's token/dollar budget or its max_review_rounds cap is hit,
the work-queue item for that ticket moves to `needs_human` instead of the
server emitting another directive. `needs_human` is a constrained enum value
(`ck_work_queue_item_status`), so this widens the CHECK.

PostgreSQL: drop + recreate the named CHECK. SQLite cannot alter a CHECK in
place, so the table is rebuilt with the widened CHECK (same pattern as 069);
the ORM model already carries the new value, so create_all covers fresh DBs.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "111"
down_revision = "110"
branch_labels = None
depends_on = None

_STATUS_OLD = "item_status IN ('pending', 'active', 'waiting', 'done', 'failed')"
_STATUS_NEW = (
    "item_status IN "
    "('pending', 'active', 'waiting', 'done', 'failed', 'needs_human')"
)


def _item_table(status_check: str) -> sa.Table:
    """The current work_queue_items shape for the SQLite table rebuild. Only
    the item_status CHECK differs between 110 and 111."""
    return sa.Table(
        "work_queue_items",
        sa.MetaData(),
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column(
            "work_queue_id",
            sa.String(length=64),
            sa.ForeignKey("work_queues.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("ticket_id", sa.String(length=64), nullable=False),
        sa.Column(
            "item_status",
            sa.String(length=20),
            nullable=False,
            server_default="pending",
        ),
        sa.Column("last_seen_comment_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_seen_comment_id", sa.String(length=64), nullable=True),
        sa.Column("last_acked_comment_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_acked_comment_id", sa.String(length=64), nullable=True),
        sa.Column("open_directive_id", sa.String(length=64), nullable=True),
        sa.Column("open_directive_run_id", sa.String(length=64), nullable=True),
        sa.Column("last_agent_run_id", sa.String(length=64), nullable=True),
        sa.Column("last_verdict", sa.String(length=20), nullable=True),
        sa.Column("implementer_service_key_id", sa.String(length=36), nullable=True),
        sa.Column("implementer_user_id", sa.String(length=64), nullable=True),
        sa.Column("closed_by_service_key_id", sa.String(length=36), nullable=True),
        sa.Column("closed_by_user_id", sa.String(length=64), nullable=True),
        sa.Column("auto_close_review_kind", sa.String(length=20), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("next_eligible_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint("work_queue_id", "ticket_id", name="uq_work_queue_item"),
        sa.CheckConstraint(status_check, name="ck_work_queue_item_status"),
        sa.CheckConstraint(
            "auto_close_review_kind IS NULL OR "
            "auto_close_review_kind IN ('resident_trusted', 'human')",
            name="ck_wqi_auto_close_review_kind",
        ),
        sa.Index("idx_wqi_claim", "work_queue_id", "item_status", "next_eligible_at"),
        sa.Index("idx_wqi_ticket", "ticket_id"),
    )


def _set_item_status(status_check: str) -> None:
    if op.get_context().dialect.name == "sqlite":
        with op.batch_alter_table(
            "work_queue_items",
            recreate="always",
            copy_from=_item_table(status_check),
        ):
            pass
        return
    op.drop_constraint(
        "ck_work_queue_item_status", "work_queue_items", type_="check"
    )
    op.create_check_constraint(
        "ck_work_queue_item_status", "work_queue_items", status_check
    )


def upgrade() -> None:
    _set_item_status(_STATUS_NEW)


def downgrade() -> None:
    # Rows already parked in needs_human would violate the narrower CHECK on
    # the way down; the queue re-claims pending/active rows only, so parking
    # them back at 'waiting' keeps them resumable without losing the signal.
    op.execute(
        sa.text(
            "UPDATE work_queue_items SET item_status = 'waiting' "
            "WHERE item_status = 'needs_human'"
        )
    )
    _set_item_status(_STATUS_OLD)
