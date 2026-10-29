"""Meeting transcripts (GitHub #116, M1a): stored, scoped meeting records.

Revision ID: 104
Revises: 103

Three new tables; no existing table changes.

- ``meeting_transcripts`` — the stored transcript. Exactly one of
  ``project_id`` / ``group_id`` is set (ck_meeting_transcripts_scope).
  ``content`` is the DLP-redacted, newline-normalised text and the ONLY
  stored copy. ``org_id`` is the owning org of the project/group (NULL for
  a personal project), kept denormalised so the retention sweep can page
  per-org without joining through the scope. Soft-delete via ``deleted_at``;
  the retention sweep hard-deletes expired and long-soft-deleted rows.
- ``meeting_participants`` — one row per speaker named in the transcript.
  ``resolution_method`` records how the name was matched (or that it wasn't);
  a manual override also sets ``resolved_by_user_id``.
- ``meeting_action_items`` — extracted action items and decisions (``kind``),
  sharing one table so extraction/citation/UI code has a single path.

Downgrade drops the three tables (indexes first, per Alembic order).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "104"
down_revision = "103"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "meeting_transcripts",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column(
            "org_id",
            sa.String(64),
            sa.ForeignKey("organizations.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "project_id",
            sa.String(64),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column(
            "group_id",
            sa.String(64),
            sa.ForeignKey("project_groups.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("meeting_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("duration_minutes", sa.Integer(), nullable=True),
        sa.Column("source", sa.String(20), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("content_sha256", sa.String(64), nullable=False),
        sa.Column("line_count", sa.Integer(), nullable=False),
        sa.Column(
            "dlp_findings",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "extraction_status",
            sa.String(20),
            nullable=False,
            server_default=sa.text("'none'"),
        ),
        sa.Column("extracted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("extraction_model", sa.String(120), nullable=True),
        sa.Column("extraction_error", sa.String(200), nullable=True),
        sa.Column(
            "created_by_user_id",
            sa.String(36),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "(project_id IS NOT NULL AND group_id IS NULL) "
            "OR (project_id IS NULL AND group_id IS NOT NULL)",
            name="ck_meeting_transcripts_scope",
        ),
        sa.CheckConstraint(
            "source IN ('manual_paste', 'file_upload')",
            name="ck_meeting_transcripts_source",
        ),
        sa.CheckConstraint(
            "extraction_status IN ('none', 'done', 'failed')",
            name="ck_meeting_transcripts_extraction_status",
        ),
    )
    op.create_index(
        "idx_meeting_transcripts_project_created",
        "meeting_transcripts",
        ["project_id", "created_at"],
        postgresql_where=sa.text("deleted_at IS NULL"),
        sqlite_where=sa.text("deleted_at IS NULL"),
    )
    op.create_index(
        "idx_meeting_transcripts_group_created",
        "meeting_transcripts",
        ["group_id", "created_at"],
        postgresql_where=sa.text("deleted_at IS NULL"),
        sqlite_where=sa.text("deleted_at IS NULL"),
    )
    op.create_index(
        "idx_meeting_transcripts_org_created",
        "meeting_transcripts",
        ["org_id", "created_at"],
        postgresql_where=sa.text("deleted_at IS NULL"),
        sqlite_where=sa.text("deleted_at IS NULL"),
    )

    op.create_table(
        "meeting_participants",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column(
            "transcript_id",
            sa.String(64),
            sa.ForeignKey("meeting_transcripts.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name_raw", sa.String(200), nullable=False),
        sa.Column("email_raw", sa.String(320), nullable=True),
        sa.Column(
            "user_id",
            sa.String(36),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("persona_name", sa.String(64), nullable=True),
        sa.Column(
            "resolution_method",
            sa.String(20),
            nullable=False,
            server_default=sa.text("'unresolved'"),
        ),
        sa.Column(
            "resolved_by_user_id",
            sa.String(36),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.CheckConstraint(
            "resolution_method IN ('exact_email', 'display_name', 'persona', 'manual', 'unresolved')",
            name="ck_meeting_participants_resolution_method",
        ),
    )
    op.create_index(
        "idx_meeting_participants_transcript",
        "meeting_participants",
        ["transcript_id"],
    )

    op.create_table(
        "meeting_action_items",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column(
            "transcript_id",
            sa.String(64),
            sa.ForeignKey("meeting_transcripts.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "kind",
            sa.String(10),
            nullable=False,
            server_default=sa.text("'action'"),
        ),
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
        sa.Column(
            "status",
            sa.String(12),
            nullable=False,
            server_default=sa.text("'pending'"),
        ),
        sa.Column("promoted_ticket_id", sa.String(64), nullable=True),
        sa.Column(
            "promoted_by_user_id",
            sa.String(36),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("extracted_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "kind IN ('action', 'decision')",
            name="ck_meeting_action_items_kind",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'promoted', 'dismissed')",
            name="ck_meeting_action_items_status",
        ),
    )
    op.create_index(
        "idx_meeting_action_items_transcript",
        "meeting_action_items",
        ["transcript_id"],
    )


def downgrade() -> None:
    op.drop_index("idx_meeting_action_items_transcript", table_name="meeting_action_items")
    op.drop_table("meeting_action_items")
    op.drop_index("idx_meeting_participants_transcript", table_name="meeting_participants")
    op.drop_table("meeting_participants")
    op.drop_index(
        "idx_meeting_transcripts_org_created", table_name="meeting_transcripts"
    )
    op.drop_index(
        "idx_meeting_transcripts_group_created", table_name="meeting_transcripts"
    )
    op.drop_index(
        "idx_meeting_transcripts_project_created", table_name="meeting_transcripts"
    )
    op.drop_table("meeting_transcripts")
