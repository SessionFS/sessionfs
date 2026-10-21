"""Server-side polling of registered Git marketplace registries (GitHub #69).

Revision ID: 079
Revises: 078

Additive only.

- skill_marketplace_registries gains the polling settings
  (poll_interval_seconds, tag_pattern, branch_only_mode, branch,
  credential_secret_ref, publish_namespace, poll_actor_user_id) and the
  poll state (last_poll_at, last_poll_status, last_poll_error_summary,
  consecutive_failures, next_poll_at, poll_requested_at, poll_lease_owner,
  poll_lease_expires_at). Existing rows get poll_interval_seconds = 0
  (metadata-only), so an upgrade never starts fetching anything.
- skill_registry_poll_refs: one row per processed (registry, ref, commit),
  the idempotency record of the poller (UNIQUE(registry_id, ref_name,
  commit_sha)). A force-moved tag is a new row.

credential_secret_ref holds a reference (env:NAME / k8s:NAME), never a
secret. Downgrade drops the table, the indexes, the checks and the columns.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "079"
down_revision = "078"
branch_labels = None
depends_on = None

_TABLE = "skill_marketplace_registries"

def _columns() -> tuple[sa.Column, ...]:
    # Fresh Column objects per call: a Column binds to one Table, and a test
    # may run upgrade -> downgrade -> upgrade in one process.
    return (
        sa.Column("poll_interval_seconds", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("tag_pattern", sa.String(200), nullable=True),
        sa.Column("branch_only_mode", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("branch", sa.String(200), nullable=True),
        sa.Column("credential_secret_ref", sa.String(255), nullable=True),
        sa.Column("publish_namespace", sa.String(64), nullable=True),
        sa.Column("poll_actor_user_id", sa.String(36), nullable=True),
        sa.Column("last_poll_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_poll_status", sa.String(20), nullable=True),
        sa.Column("last_poll_error_summary", sa.String(500), nullable=True),
        sa.Column("consecutive_failures", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("next_poll_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("poll_requested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("poll_lease_owner", sa.String(64), nullable=True),
        sa.Column("poll_lease_expires_at", sa.DateTime(timezone=True), nullable=True),
    )


def upgrade() -> None:
    bind = op.get_bind()
    sqlite = bind.dialect.name == "sqlite"
    # Plain ADD COLUMN (no SQLite table rebuild, so the table keeps its CHECK
    # constraints). The FK and CHECKs are PostgreSQL-only, like 076's.
    for column in _columns():
        op.add_column(_TABLE, column)
    if not sqlite:
        op.create_foreign_key(
            "fk_skill_registry_poll_actor",
            _TABLE,
            "users",
            ["poll_actor_user_id"],
            ["id"],
            ondelete="SET NULL",
        )
        op.create_check_constraint(
            "ck_skill_registry_poll_interval",
            _TABLE,
            "poll_interval_seconds = 0 OR poll_interval_seconds >= 300",
        )
        op.create_check_constraint(
            "ck_skill_registry_poll_status",
            _TABLE,
            "last_poll_status IS NULL OR last_poll_status IN ('ok', 'partial', 'error')",
        )
    op.create_index("idx_skill_registry_next_poll", _TABLE, ["next_poll_at"])
    op.create_index("idx_skill_registry_poll_actor", _TABLE, ["poll_actor_user_id"])

    op.create_table(
        "skill_registry_poll_refs",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column(
            "registry_id",
            sa.String(64),
            sa.ForeignKey(f"{_TABLE}.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("ref_name", sa.String(255), nullable=False),
        sa.Column("commit_sha", sa.String(40), nullable=False),
        sa.Column("outcome", sa.String(20), nullable=False),
        sa.Column("version_ids", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("detail", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint("registry_id", "ref_name", "commit_sha", name="uq_skill_registry_poll_ref"),
        sa.CheckConstraint(
            "outcome IN ('published', 'partial', 'rejected', 'skipped')",
            name="ck_skill_registry_poll_ref_outcome",
        ),
    )
    op.create_index(
        "idx_skill_registry_poll_ref_created",
        "skill_registry_poll_refs",
        ["registry_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("idx_skill_registry_poll_ref_created", table_name="skill_registry_poll_refs")
    op.drop_table("skill_registry_poll_refs")
    op.drop_index("idx_skill_registry_poll_actor", table_name=_TABLE)
    op.drop_index("idx_skill_registry_next_poll", table_name=_TABLE)
    if op.get_bind().dialect.name != "sqlite":
        op.drop_constraint("ck_skill_registry_poll_status", _TABLE, type_="check")
        op.drop_constraint("ck_skill_registry_poll_interval", _TABLE, type_="check")
        op.drop_constraint("fk_skill_registry_poll_actor", _TABLE, type_="foreignkey")
    # Plain DROP COLUMN on both dialects (SQLite >= 3.35): no table rebuild.
    for column in reversed(_columns()):
        op.execute(sa.text(f"ALTER TABLE {_TABLE} DROP COLUMN {column.name}"))
