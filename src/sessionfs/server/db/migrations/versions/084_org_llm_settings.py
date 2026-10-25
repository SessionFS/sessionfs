"""Org-wide LLM key for the session helper.

Revision ID: 084
Revises: 083

docs/design/session-helper.md §"Migration 084". One row per org holding the
provider / model / encrypted API key that any active member can fall back to
when they have no personal judge settings. The key is Fernet-encrypted with
the same derivation as ``user_judge_settings`` (``server/llm_keys.py``), so
both tables stay readable through one path.

- ``enabled`` defaults to true; an admin can switch the org key off without
  deleting it.
- ``base_url`` is nullable and re-validated by the SSRF guard at resolve
  time.
- ``updated_by_user_id`` is the last admin who saved the row (SET NULL on
  user deletion).

Downgrade drops the table.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "084"
down_revision = "083"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "org_llm_settings",
        sa.Column(
            "org_id",
            sa.String(64),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("model", sa.String(128), nullable=False),
        sa.Column("encrypted_api_key", sa.Text(), nullable=False),
        sa.Column("base_url", sa.String(500), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column(
            "updated_by_user_id",
            sa.String(36),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )


def downgrade() -> None:
    op.drop_table("org_llm_settings")
