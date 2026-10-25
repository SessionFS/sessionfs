"""Chunked, resumable session uploads (GitHub #91).

Revision ID: 088
Revises: 083

Adds the two tables that back chunked uploads:

- session_uploads: one row per initiated upload. id is ``up_`` + 24 hex.
  user_id CASCADEs with the user. session_id is a plain string (not an FK):
  an upload initiates before the session row exists, and the assembled
  archive is what creates/updates the session at complete. status is
  pending|completing|completed|aborted; expires_at = created + 24 h. Once
  complete succeeds the push result is stored on the row (result_etag,
  result_size, result_synced_at, result_created) so a retried complete
  returns the same 200/201 without re-pushing. ``completion_token`` fences
  the stale-completing reclaim against a slow, live complete.
- session_upload_parts: the received parts, keyed by (upload_id, part_number)
  and CASCADEd from the upload. Part payloads live in blob storage under
  ``uploads/{upload_id}/part-{n:05d}``.

Downgrade drops the child table first, then the upload table.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "088"
down_revision = "087"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "session_uploads",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column(
            "user_id",
            sa.String(length=36),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("session_id", sa.String(length=64), nullable=False),
        sa.Column("total_size", sa.BigInteger(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("part_size", sa.Integer(), nullable=False),
        sa.Column("part_count", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="pending"),
        sa.Column("completing_since", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completion_token", sa.String(length=64), nullable=True),
        sa.Column("result_etag", sa.String(length=64), nullable=True),
        sa.Column("result_size", sa.BigInteger(), nullable=True),
        sa.Column("result_synced_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("result_created", sa.Boolean(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "idx_session_uploads_user_status", "session_uploads", ["user_id", "status"]
    )
    op.create_index(
        "idx_session_uploads_status_expires", "session_uploads", ["status", "expires_at"]
    )

    op.create_table(
        "session_upload_parts",
        sa.Column(
            "upload_id",
            sa.String(length=64),
            sa.ForeignKey("session_uploads.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("part_number", sa.Integer(), primary_key=True),
        sa.Column("size", sa.Integer(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("session_upload_parts")
    op.drop_table("session_uploads")
