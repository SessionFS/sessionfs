"""Session-helper configuration and token usage ledger.

Revision ID: 095
Revises: 094

Two new tables; no existing table changes.

- ``helper_configs`` — one row per org (``scope='org'``, ``org_id`` set) or per
  user (``scope='user'``, ``user_id`` set): the helper model settings
  (JSON-as-Text, only the explicitly-set keys), the custom instructions
  (stored DLP-redacted) and, for orgs only, ``monthly_token_budget``. It lives
  apart from the key rows (``org_llm_settings`` / ``user_judge_settings``) so
  deleting or rotating a key never touches the configuration and saving a
  configuration never creates a placeholder key row. CHECKs enforce exactly
  one owner matching the scope and an org-only, non-negative budget; partial
  unique indexes allow one row per org and one per user.
- ``helper_usage`` — append-only token ledger, one row per turn that reported
  tokens (including disconnects, provider errors, deadline and max_steps).
  ``conversation_id`` is a plain column with no foreign key so deleting a
  conversation keeps its ledger rows; the monthly budget and usage summaries
  read only this table. Retention: 13 months (the helper-retention sweep
  deletes rows older than 400 days). Indexed on (org_id, created_at) and
  (user_id, created_at).

Per-message ``input_tokens`` / ``output_tokens`` on ``helper_messages``
(migration 092) stay as they are: the conversation view displays them.

Downgrade drops both tables.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "095"
down_revision = "094"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "helper_configs",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("scope", sa.String(10), nullable=False),
        sa.Column(
            "org_id",
            sa.String(64),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column(
            "user_id",
            sa.String(36),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("settings", sa.Text(), nullable=True),
        sa.Column("instructions", sa.Text(), nullable=True),
        sa.Column("monthly_token_budget", sa.BigInteger(), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_by_user_id",
            sa.String(36),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.CheckConstraint(
            "(scope = 'org' AND org_id IS NOT NULL AND user_id IS NULL) OR "
            "(scope = 'user' AND user_id IS NOT NULL AND org_id IS NULL)",
            name="ck_helper_configs_scope_owner",
        ),
        sa.CheckConstraint(
            "monthly_token_budget IS NULL OR "
            "(scope = 'org' AND monthly_token_budget >= 0)",
            name="ck_helper_configs_budget_org_only",
        ),
    )
    op.create_index(
        "uq_helper_configs_org",
        "helper_configs",
        ["org_id"],
        unique=True,
        postgresql_where=sa.text("org_id IS NOT NULL"),
        sqlite_where=sa.text("org_id IS NOT NULL"),
    )
    op.create_index(
        "uq_helper_configs_user",
        "helper_configs",
        ["user_id"],
        unique=True,
        postgresql_where=sa.text("user_id IS NOT NULL"),
        sqlite_where=sa.text("user_id IS NOT NULL"),
    )

    op.create_table(
        "helper_usage",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column(
            "org_id",
            sa.String(64),
            sa.ForeignKey("organizations.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "user_id",
            sa.String(36),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("key_source", sa.String(20), nullable=False),
        sa.Column("conversation_id", sa.String(64), nullable=True),
        sa.Column("input_tokens", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("output_tokens", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("stop_reason", sa.String(40), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "key_source IN ('user', 'org')", name="ck_helper_usage_key_source"
        ),
    )
    op.create_index(
        "idx_helper_usage_org_created", "helper_usage", ["org_id", "created_at"]
    )
    op.create_index(
        "idx_helper_usage_user_created", "helper_usage", ["user_id", "created_at"]
    )


def downgrade() -> None:
    op.drop_index("idx_helper_usage_user_created", table_name="helper_usage")
    op.drop_index("idx_helper_usage_org_created", table_name="helper_usage")
    op.drop_table("helper_usage")
    op.drop_index("uq_helper_configs_user", table_name="helper_configs")
    op.drop_index("uq_helper_configs_org", table_name="helper_configs")
    op.drop_table("helper_configs")
