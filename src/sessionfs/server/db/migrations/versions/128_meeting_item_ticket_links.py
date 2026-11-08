"""Meeting and chat action items suggest and link existing tickets.

Revision ID: 128
Revises: 127

On ``meeting_action_items``:

- ``suggested_ticket_id`` (VARCHAR 64, no FK like ``promoted_ticket_id``),
  ``suggestion_source`` (``model`` | ``text``, CHECK
  ``ck_meeting_action_items_suggestion_source``), ``suggestion_score``
  (float 0..1) and ``suggestion_dismissed_at``: an existing ticket the item
  looks like. Suggest only, never auto-link.
- ``linked_ticket_id`` (VARCHAR 64, no FK), ``linked_at`` and
  ``linked_by_user_id`` (FK users ON DELETE SET NULL,
  ``fk_meeting_action_items_linked_by_user``): a person tied the item to an
  existing ticket.
- ``ck_meeting_action_items_status`` gains ``linked``.

All columns are nullable, so existing rows need no backfill.

PostgreSQL: add the columns, the FK and the new CHECK, then drop + recreate
the named status CHECK. SQLite cannot alter a CHECK in place, so the table is
rebuilt from an explicit copy of the 128 shape (the pattern 111 uses).

Downgrade turns linked rows back into pending items (the ticket comment the
link wrote stays: comments are append-only), then removes the columns and
narrows the CHECK again.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "128"
down_revision = "127"
branch_labels = None
depends_on = None

_TABLE = "meeting_action_items"
_STATUS_OLD = "status IN ('pending', 'promoted', 'dismissed')"
_STATUS_NEW = "status IN ('pending', 'promoted', 'dismissed', 'linked')"
_SOURCE_CHECK = "suggestion_source IS NULL OR suggestion_source IN ('model', 'text')"
_FK_NAME = "fk_meeting_action_items_linked_by_user"


def _link_columns() -> list[sa.Column]:
    """The columns 128 adds (fresh objects each call)."""
    return [
        sa.Column("suggested_ticket_id", sa.String(64), nullable=True),
        sa.Column("suggestion_source", sa.String(10), nullable=True),
        sa.Column("suggestion_score", sa.Float(), nullable=True),
        sa.Column("suggestion_dismissed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("linked_ticket_id", sa.String(64), nullable=True),
        sa.Column("linked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "linked_by_user_id",
            sa.String(36),
            sa.ForeignKey("users.id", ondelete="SET NULL", name=_FK_NAME),
            nullable=True,
        ),
    ]


def _table(*, with_links: bool) -> sa.Table:
    """The meeting_action_items shape for the SQLite rebuild: 104 + 121, plus
    the 128 columns and CHECKs when ``with_links``."""
    columns: list = [
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column(
            "transcript_id",
            sa.String(64),
            sa.ForeignKey("meeting_transcripts.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("kind", sa.String(10), nullable=False, server_default=sa.text("'action'")),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column(
            "owner_participant_id",
            sa.String(64),
            sa.ForeignKey("meeting_participants.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("due_date", sa.Date(), nullable=True),
        sa.Column("line_start", sa.Integer(), nullable=False),
        sa.Column("line_end", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(12), nullable=False, server_default=sa.text("'pending'")),
        sa.Column("promoted_ticket_id", sa.String(64), nullable=True),
        sa.Column(
            "promoted_by_user_id",
            sa.String(36),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("extracted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("owner_hint", sa.String(200), nullable=True),
        sa.CheckConstraint("kind IN ('action', 'decision')", name="ck_meeting_action_items_kind"),
    ]
    if with_links:
        columns += _link_columns()
        columns += [
            sa.CheckConstraint(_STATUS_NEW, name="ck_meeting_action_items_status"),
            sa.CheckConstraint(_SOURCE_CHECK, name="ck_meeting_action_items_suggestion_source"),
        ]
    else:
        columns.append(sa.CheckConstraint(_STATUS_OLD, name="ck_meeting_action_items_status"))
    return sa.Table(
        _TABLE,
        sa.MetaData(),
        *columns,
        sa.Index("idx_meeting_action_items_transcript", "transcript_id"),
    )


def _sqlite_rebuild(*, with_links: bool) -> None:
    """Rebuild the table from the current shape (``copy_from``) into the
    target one: the batch copies every surviving column's data across."""
    with op.batch_alter_table(
        _TABLE, recreate="always", copy_from=_table(with_links=not with_links)
    ) as batch:
        if with_links:
            for column in _link_columns():
                batch.add_column(column)
            batch.drop_constraint("ck_meeting_action_items_status", type_="check")
            batch.create_check_constraint("ck_meeting_action_items_status", _STATUS_NEW)
            batch.create_check_constraint(
                "ck_meeting_action_items_suggestion_source", _SOURCE_CHECK
            )
        else:
            batch.drop_constraint("ck_meeting_action_items_suggestion_source", type_="check")
            batch.drop_constraint("ck_meeting_action_items_status", type_="check")
            batch.create_check_constraint("ck_meeting_action_items_status", _STATUS_OLD)
            for column in _link_columns():
                batch.drop_column(column.name)


def upgrade() -> None:
    if op.get_context().dialect.name == "sqlite":
        _sqlite_rebuild(with_links=True)
        return
    op.add_column(_TABLE, sa.Column("suggested_ticket_id", sa.String(64), nullable=True))
    op.add_column(_TABLE, sa.Column("suggestion_source", sa.String(10), nullable=True))
    op.add_column(_TABLE, sa.Column("suggestion_score", sa.Float(), nullable=True))
    op.add_column(
        _TABLE, sa.Column("suggestion_dismissed_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(_TABLE, sa.Column("linked_ticket_id", sa.String(64), nullable=True))
    op.add_column(_TABLE, sa.Column("linked_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column(_TABLE, sa.Column("linked_by_user_id", sa.String(36), nullable=True))
    op.create_foreign_key(
        _FK_NAME, _TABLE, "users", ["linked_by_user_id"], ["id"], ondelete="SET NULL"
    )
    op.create_check_constraint(
        "ck_meeting_action_items_suggestion_source", _TABLE, _SOURCE_CHECK
    )
    op.drop_constraint("ck_meeting_action_items_status", _TABLE, type_="check")
    op.create_check_constraint("ck_meeting_action_items_status", _TABLE, _STATUS_NEW)


def downgrade() -> None:
    # Linked rows can't satisfy the narrower CHECK: they go back to pending
    # (the item is still there to promote or link again).
    op.execute(sa.text(f"UPDATE {_TABLE} SET status = 'pending' WHERE status = 'linked'"))
    if op.get_context().dialect.name == "sqlite":
        _sqlite_rebuild(with_links=False)
        return
    op.drop_constraint("ck_meeting_action_items_status", _TABLE, type_="check")
    op.create_check_constraint("ck_meeting_action_items_status", _TABLE, _STATUS_OLD)
    op.drop_constraint("ck_meeting_action_items_suggestion_source", _TABLE, type_="check")
    op.drop_constraint(_FK_NAME, _TABLE, type_="foreignkey")
    for name in (
        "linked_by_user_id",
        "linked_at",
        "linked_ticket_id",
        "suggestion_dismissed_at",
        "suggestion_score",
        "suggestion_source",
        "suggested_ticket_id",
    ):
        op.drop_column(_TABLE, name)
