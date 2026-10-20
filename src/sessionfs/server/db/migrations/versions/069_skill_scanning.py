"""Marketplace instruction scanning: scan results, rescans, automated quarantine.

Revision ID: 069
Revises: 066

(Marketplace v2 §5.7. Chained from 066 while 067/068 are built in parallel;
the merge re-chains the number, the name stays.)

- skill_scan_rulesets: one row per scanner ruleset version used.
- skill_version_scans: one scan of one version under one ruleset
  (UNIQUE(version_id, ruleset_version); a weekly re-run updates in place).
- skill_scan_findings: value-free findings (rule, location, explanation,
  sha256 of the flagged line). Matched text is never stored.
- skill_package_versions: scan_summary (latest scan, bounded JSON),
  quarantined_at / quarantine_reason (automated quarantine by a rescan).
- skill_revocation_events.state gains 'quarantined' (additive feed value;
  clients ignore states they do not know).
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "069"
down_revision = "066"
branch_labels = None
depends_on = None

_STATES_OLD = "state IN ('yanked', 'blocked', 'restored')"
_STATES_NEW = "state IN ('yanked', 'blocked', 'restored', 'quarantined')"


def _id_type() -> sa.types.TypeEngine:
    return sa.BigInteger().with_variant(sa.Integer(), "sqlite")


def _revocation_table(state_check: str) -> sa.Table:
    """The 066 skill_revocation_events shape (columns, every CHECK, every
    index), for the SQLite table rebuild: SQLite cannot alter a CHECK
    constraint in place. Only the state CHECK differs between 066 and 069."""
    return sa.Table(
        "skill_revocation_events",
        sa.MetaData(),
        sa.Column("id", _id_type(), primary_key=True, autoincrement=True),
        sa.Column("kind", sa.String(length=20), nullable=False),
        sa.Column("package_id", sa.String(length=64), nullable=True),
        sa.Column("version_id", sa.String(length=64), nullable=True),
        sa.Column("content_hash", sa.String(length=80), nullable=True),
        sa.Column("state", sa.String(length=20), nullable=False),
        sa.Column("block_scope", sa.String(length=20), nullable=True),
        sa.Column("scope_org_id", sa.String(length=64), nullable=True),
        sa.Column("source_package_id", sa.String(length=64), nullable=True),
        sa.Column("source_version_id", sa.String(length=64), nullable=True),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column(
            "actor_user_id",
            sa.String(length=36),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("actor_role", sa.String(length=20), nullable=False),
        sa.Column("org_id", sa.String(length=64), nullable=True),
        sa.Column("owner_user_id", sa.String(length=36), nullable=True),
        sa.Column("visibility", sa.String(length=20), nullable=False),
        sa.Column("changed_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(state_check, name="ck_skill_revocation_state"),
        sa.CheckConstraint(
            "kind IN ('version', 'package', 'content')", name="ck_skill_revocation_kind"
        ),
        sa.CheckConstraint(
            "block_scope IS NULL OR block_scope IN ('global', 'org', 'owner')",
            name="ck_skill_revocation_block_scope",
        ),
        sa.CheckConstraint(
            "(COALESCE(block_scope, '') = 'org' AND scope_org_id IS NOT NULL) OR "
            "(COALESCE(block_scope, '') = 'owner') OR "
            "(COALESCE(block_scope, '') NOT IN ('org', 'owner') AND scope_org_id IS NULL)",
            name="ck_skill_revocation_block_scope_org",
        ),
        sa.CheckConstraint(
            "(kind = 'version' AND package_id IS NOT NULL AND version_id IS NOT NULL) OR "
            "(kind = 'package' AND package_id IS NOT NULL AND version_id IS NULL) OR "
            "(kind = 'content' AND package_id IS NULL AND version_id IS NULL "
            "AND content_hash IS NOT NULL)",
            name="ck_skill_revocation_kind_target",
        ),
        sa.CheckConstraint(
            "visibility IN ('public', 'user', 'org')", name="ck_skill_revocation_visibility"
        ),
        sa.CheckConstraint(
            "(visibility = 'public') OR "
            "(visibility = 'org' AND org_id IS NOT NULL) OR "
            "(visibility = 'user' AND owner_user_id IS NOT NULL)",
            name="ck_skill_revocation_scope",
        ),
        sa.Index("uq_skill_revocation_changed_at", "changed_at", unique=True),
        sa.Index("idx_skill_revocation_package", "package_id", "version_id", "id"),
        sa.Index("idx_skill_revocation_org", "org_id", "changed_at"),
        sa.Index("idx_skill_revocation_owner", "owner_user_id", "changed_at"),
        sa.Index("idx_skill_revocation_actor", "actor_user_id"),
    )


def _set_revocation_states(state_check: str) -> None:
    if op.get_context().dialect.name == "sqlite":
        with op.batch_alter_table(
            "skill_revocation_events",
            recreate="always",
            copy_from=_revocation_table(state_check),
        ):
            pass
        return
    op.drop_constraint("ck_skill_revocation_state", "skill_revocation_events", type_="check")
    op.create_check_constraint(
        "ck_skill_revocation_state", "skill_revocation_events", state_check
    )


def upgrade() -> None:
    op.create_table(
        "skill_scan_rulesets",
        sa.Column("version", sa.String(length=20), primary_key=True),
        sa.Column(
            "released_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("rule_count", sa.Integer(), nullable=False),
        sa.Column("notes", sa.Text(), nullable=False, server_default=""),
    )
    op.create_table(
        "skill_version_scans",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column(
            "version_id",
            sa.String(length=64),
            sa.ForeignKey("skill_package_versions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "ruleset_version",
            sa.String(length=20),
            sa.ForeignKey("skill_scan_rulesets.version"),
            nullable=False,
        ),
        sa.Column(
            "scanned_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("max_severity", sa.String(length=10), nullable=False),
        sa.Column("finding_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("new_finding_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("truncated", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("trigger", sa.String(length=10), nullable=False),
        sa.UniqueConstraint("version_id", "ruleset_version", name="uq_skill_version_scan_ruleset"),
        sa.CheckConstraint(
            "max_severity IN ('none', 'low', 'medium', 'high', 'critical')",
            name="ck_skill_version_scan_severity",
        ),
        sa.CheckConstraint(
            "trigger IN ('publish', 'rescan', 'manual')", name="ck_skill_version_scan_trigger"
        ),
    )
    op.create_index(
        "idx_skill_version_scans_version_scanned",
        "skill_version_scans",
        ["version_id", "scanned_at"],
    )
    op.create_index(
        "idx_skill_version_scans_ruleset_scanned",
        "skill_version_scans",
        ["ruleset_version", "scanned_at"],
    )
    op.create_table(
        "skill_scan_findings",
        sa.Column("id", _id_type(), primary_key=True, autoincrement=True),
        sa.Column(
            "scan_id",
            sa.String(length=64),
            sa.ForeignKey("skill_version_scans.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("rule_id", sa.String(length=80), nullable=False),
        sa.Column("family", sa.String(length=40), nullable=False),
        sa.Column("severity", sa.String(length=10), nullable=False),
        sa.Column("file_path", sa.String(length=500), nullable=False),
        sa.Column("line", sa.Integer(), nullable=False),
        sa.Column("col", sa.Integer(), nullable=False),
        sa.Column("message", sa.String(length=300), nullable=False),
        sa.Column("line_sha", sa.String(length=64), nullable=False),
        sa.Column("suppressed", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("suppression_justification", sa.Text(), nullable=True),
        sa.Column("first_seen_scan_id", sa.String(length=64), nullable=True),
        sa.CheckConstraint(
            "severity IN ('low', 'medium', 'high', 'critical')",
            name="ck_skill_scan_finding_severity",
        ),
    )
    op.create_index("idx_skill_scan_findings_scan", "skill_scan_findings", ["scan_id"])

    op.add_column(
        "skill_package_versions",
        sa.Column("scan_summary", sa.Text(), nullable=False, server_default="{}"),
    )
    op.add_column(
        "skill_package_versions",
        sa.Column("quarantined_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "skill_package_versions",
        sa.Column("quarantine_reason", sa.Text(), nullable=True),
    )
    _set_revocation_states(_STATES_NEW)


def downgrade() -> None:
    # The 066 CHECK cannot hold quarantine events. They are an automated,
    # human-reviewable state; dropping them (and the quarantine flag) puts
    # the versions back to their pre-069 deliverable state.
    op.execute(sa.text("DELETE FROM skill_revocation_events WHERE state = 'quarantined'"))
    _set_revocation_states(_STATES_OLD)
    # Plain DROP COLUMN on both dialects (SQLite >= 3.35): no table rebuild,
    # so skill_package_versions keeps its CHECK constraints.
    for column in ("quarantine_reason", "quarantined_at", "scan_summary"):
        op.execute(sa.text(f"ALTER TABLE skill_package_versions DROP COLUMN {column}"))
    op.drop_index("idx_skill_scan_findings_scan", table_name="skill_scan_findings")
    op.drop_table("skill_scan_findings")
    op.drop_index("idx_skill_version_scans_ruleset_scanned", table_name="skill_version_scans")
    op.drop_index("idx_skill_version_scans_version_scanned", table_name="skill_version_scans")
    op.drop_table("skill_version_scans")
    op.drop_table("skill_scan_rulesets")
