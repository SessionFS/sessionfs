"""Chat threads as a second conversation kind (GitHub #118, C1).

Revision ID: 122
Revises: 121

Additive only. The meeting tables are generalised in place (table names
unchanged, docs/design/followons-2026-11.md §3):

- meeting_transcripts.kind: ``meeting`` (the backfill default for every
  existing row) or ``chat`` (ck_meeting_transcripts_kind).
- meeting_transcripts.provider: ``slack`` | ``discord`` | ``teams_paste`` |
  ``paste``; NULL for meetings (ck_meeting_transcripts_provider).
- meeting_transcripts.channel_name: a label of at most 80 characters,
  required (with a provider) when kind='chat'
  (ck_meeting_transcripts_chat_fields).
- ck_meeting_transcripts_source gains ``chat_export``.
- ck_helper_usage_source gains ``chat_extract``.
- conversation_message_meta: one row per normalised chat message line
  (source_id, line_no) with the exporter's author label, an optional email
  and the posted time, used only as participant evidence.

Downgrade deletes every chat row together with its action items,
participants, message meta and the reminders sourced on them (meeting_item /
chat_item on its items, meeting on the thread) before narrowing the CHECKs.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "122"
down_revision = "121"
branch_labels = None
depends_on = None

_SOURCE_OLD = "source IN ('manual_paste', 'file_upload')"
_SOURCE_NEW = "source IN ('manual_paste', 'file_upload', 'chat_export')"
_USAGE_OLD = "source IN ('helper', 'meeting_extract')"
_USAGE_NEW = "source IN ('helper', 'meeting_extract', 'chat_extract')"
_KIND_CHECK = "kind IN ('meeting', 'chat')"
_PROVIDER_CHECK = (
    "provider IS NULL OR provider IN ('slack', 'discord', 'teams_paste', 'paste')"
)
_CHAT_FIELDS_CHECK = (
    "kind <> 'chat' OR (provider IS NOT NULL AND channel_name IS NOT NULL "
    "AND length(channel_name) BETWEEN 1 AND 80)"
)


def upgrade() -> None:
    with op.batch_alter_table("meeting_transcripts") as batch:
        batch.add_column(
            sa.Column(
                "kind",
                sa.String(10),
                nullable=False,
                server_default=sa.text("'meeting'"),
            )
        )
        batch.add_column(sa.Column("provider", sa.String(20), nullable=True))
        batch.add_column(sa.Column("channel_name", sa.String(80), nullable=True))
        batch.drop_constraint("ck_meeting_transcripts_source", type_="check")
        batch.create_check_constraint("ck_meeting_transcripts_source", _SOURCE_NEW)
        batch.create_check_constraint("ck_meeting_transcripts_kind", _KIND_CHECK)
        batch.create_check_constraint(
            "ck_meeting_transcripts_provider", _PROVIDER_CHECK
        )
        batch.create_check_constraint(
            "ck_meeting_transcripts_chat_fields", _CHAT_FIELDS_CHECK
        )

    with op.batch_alter_table("helper_usage") as batch:
        batch.drop_constraint("ck_helper_usage_source", type_="check")
        batch.create_check_constraint("ck_helper_usage_source", _USAGE_NEW)

    op.create_table(
        "conversation_message_meta",
        sa.Column(
            "source_id",
            sa.String(64),
            sa.ForeignKey("meeting_transcripts.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("line_no", sa.Integer(), nullable=False),
        sa.Column("author_raw", sa.String(200), nullable=False),
        sa.Column("email_raw", sa.String(320), nullable=True),
        sa.Column("posted_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("source_id", "line_no"),
    )


_CHAT_IDS = "SELECT id FROM meeting_transcripts WHERE kind = 'chat'"
_CHAT_ITEM_IDS = f"SELECT id FROM meeting_action_items WHERE transcript_id IN ({_CHAT_IDS})"
_CHAT_REMINDERS = (
    "SELECT id FROM reminders WHERE "
    f"(source_kind IN ('meeting_item', 'chat_item') AND source_id IN ({_CHAT_ITEM_IDS})) "
    f"OR (source_kind = 'meeting' AND source_id IN ({_CHAT_IDS}))"
)


def downgrade() -> None:
    # Chat rows and everything hanging off them go first, explicitly: SQLite
    # runs without foreign keys, so ON DELETE CASCADE can't be relied on to
    # clear the children (and reminders only point at them by id).
    op.execute(sa.text(f"DELETE FROM reminder_notes WHERE reminder_id IN ({_CHAT_REMINDERS})"))
    op.execute(sa.text(f"DELETE FROM reminders WHERE id IN ({_CHAT_REMINDERS})"))
    op.execute(sa.text(f"DELETE FROM meeting_action_items WHERE transcript_id IN ({_CHAT_IDS})"))
    op.execute(sa.text(f"DELETE FROM meeting_participants WHERE transcript_id IN ({_CHAT_IDS})"))
    op.execute(sa.text(f"DELETE FROM conversation_message_meta WHERE source_id IN ({_CHAT_IDS})"))
    op.drop_table("conversation_message_meta")
    # Chat rows and their ledger tags cannot survive the narrower CHECKs.
    op.execute(sa.text("DELETE FROM meeting_transcripts WHERE kind = 'chat'"))
    op.execute(
        sa.text(
            "UPDATE helper_usage SET source = 'meeting_extract' "
            "WHERE source = 'chat_extract'"
        )
    )
    with op.batch_alter_table("helper_usage") as batch:
        batch.drop_constraint("ck_helper_usage_source", type_="check")
        batch.create_check_constraint("ck_helper_usage_source", _USAGE_OLD)
    with op.batch_alter_table("meeting_transcripts") as batch:
        batch.drop_constraint("ck_meeting_transcripts_chat_fields", type_="check")
        batch.drop_constraint("ck_meeting_transcripts_provider", type_="check")
        batch.drop_constraint("ck_meeting_transcripts_kind", type_="check")
        batch.drop_constraint("ck_meeting_transcripts_source", type_="check")
        batch.create_check_constraint("ck_meeting_transcripts_source", _SOURCE_OLD)
        batch.drop_column("channel_name")
        batch.drop_column("provider")
        batch.drop_column("kind")
