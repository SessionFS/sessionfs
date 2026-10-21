"""Native per-tool marketplace catalogs and "works in" verification.

Revision ID: 077
Revises: 076

Marketplace v2 §7.5 (tk_86084118c8ee453c). Additive only:

- skill_feed_catalogs: one generated catalog git repository per scope
  (public, org, user), keyed by scope_key so the public row is unique too.
- skill_feed_entries: the (version, tool) entries of each catalog's current
  commit, with the projection hash that binds them to the canonical bytes.
- skill_tool_verifications: works-in evidence per version x tool x tool
  version (L0 static in-process; L1/L2 from the sandboxed harness).
- skill_verification_jobs: queued harness work, one open job per
  (version, tool, level) (partial unique index); ``outcome`` records how a
  done job ended (a verdict, ``skipped`` when the package became
  undeliverable, ``errored`` after the attempt cap).
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "077"
down_revision = "076"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "skill_feed_catalogs",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("scope_key", sa.String(100), nullable=False),
        sa.Column("scope_type", sa.String(20), nullable=False),
        sa.Column("scope_id", sa.String(64), nullable=True),
        sa.Column("current_commit_sha", sa.String(40), nullable=True),
        sa.Column("input_digest", sa.String(64), nullable=True),
        sa.Column("plugin_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("blob_prefix", sa.String(500), nullable=False),
        sa.Column("export_remote", sa.String(500), nullable=True),
        sa.Column("generated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint("scope_key", name="uq_skill_feed_catalogs_scope_key"),
        sa.CheckConstraint(
            "scope_type IN ('public', 'org', 'user')", name="ck_skill_feed_catalog_scope_type"
        ),
        sa.CheckConstraint(
            "(scope_type = 'public' AND scope_id IS NULL) OR "
            "(scope_type != 'public' AND scope_id IS NOT NULL)",
            name="ck_skill_feed_catalog_scope_id",
        ),
    )
    op.create_table(
        "skill_feed_entries",
        sa.Column(
            "catalog_id",
            sa.String(64),
            sa.ForeignKey("skill_feed_catalogs.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "version_id",
            sa.String(64),
            sa.ForeignKey("skill_package_versions.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("tool", sa.String(50), primary_key=True),
        sa.Column("plugin_name", sa.String(100), nullable=False),
        sa.Column("projection_hash", sa.String(71), nullable=False),
        sa.Column("plugin_path", sa.String(300), nullable=False),
    )
    op.create_index("idx_skill_feed_entries_version", "skill_feed_entries", ["version_id"])

    op.create_table(
        "skill_tool_verifications",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column(
            "version_id",
            sa.String(64),
            sa.ForeignKey("skill_package_versions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("tool", sa.String(50), nullable=False),
        sa.Column("tool_version", sa.String(50), nullable=False),
        sa.Column("level", sa.String(2), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False, server_default=""),
        sa.Column("evidence", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("projection_hash", sa.String(71), nullable=True),
        sa.Column("harness_image_digest", sa.String(80), nullable=True),
        sa.Column(
            "run_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint("level IN ('L0', 'L1', 'L2')", name="ck_skill_tool_verification_level"),
        sa.CheckConstraint(
            "status IN ('pass', 'fail', 'regressed', 'skipped')",
            name="ck_skill_tool_verification_status",
        ),
    )
    op.create_index(
        "idx_skill_tool_verifications_lookup",
        "skill_tool_verifications",
        ["version_id", "tool", "run_at"],
    )

    op.create_table(
        "skill_verification_jobs",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column(
            "version_id",
            sa.String(64),
            sa.ForeignKey("skill_package_versions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("tool", sa.String(50), nullable=False),
        sa.Column("level", sa.String(2), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="queued"),
        sa.Column("trigger", sa.String(30), nullable=False, server_default="manual"),
        sa.Column("owner_org_id", sa.String(64), nullable=True),
        sa.Column(
            "requested_by_user_id",
            sa.String(36),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("claim_token_hash", sa.String(64), nullable=True),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("outcome", sa.String(20), nullable=True),
        sa.Column("outcome_reason", sa.String(500), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint("level IN ('L1', 'L2')", name="ck_skill_verification_job_level"),
        sa.CheckConstraint(
            "status IN ('queued', 'claimed', 'done')", name="ck_skill_verification_job_status"
        ),
        sa.CheckConstraint(
            "outcome IS NULL OR outcome IN ('pass', 'fail', 'regressed', 'skipped', 'errored')",
            name="ck_skill_verification_job_outcome",
        ),
    )
    op.create_index(
        "uq_skill_verification_jobs_open",
        "skill_verification_jobs",
        ["version_id", "tool", "level"],
        unique=True,
        postgresql_where=sa.text("status IN ('queued', 'claimed')"),
        sqlite_where=sa.text("status IN ('queued', 'claimed')"),
    )
    op.create_index(
        "idx_skill_verification_jobs_status",
        "skill_verification_jobs",
        ["status", "tool", "created_at"],
    )
    op.create_index(
        "idx_skill_verification_jobs_org", "skill_verification_jobs", ["owner_org_id", "created_at"]
    )


def downgrade() -> None:
    op.drop_index("idx_skill_verification_jobs_org", table_name="skill_verification_jobs")
    op.drop_index("idx_skill_verification_jobs_status", table_name="skill_verification_jobs")
    op.drop_index("uq_skill_verification_jobs_open", table_name="skill_verification_jobs")
    op.drop_table("skill_verification_jobs")
    op.drop_index("idx_skill_tool_verifications_lookup", table_name="skill_tool_verifications")
    op.drop_table("skill_tool_verifications")
    op.drop_index("idx_skill_feed_entries_version", table_name="skill_feed_entries")
    op.drop_table("skill_feed_entries")
    op.drop_table("skill_feed_catalogs")
