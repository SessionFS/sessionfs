"""Marketplace takedown: abuse reports, revocation events, assignment revocation.

Revision ID: 066
Revises: 065

- skill_package_reports: user reports against a package (optionally one
  version). At most one OPEN report per (reporter, package), enforced by a
  partial unique index.
- skill_revocation_events: append-only yank / block / restore history. The
  integer id is monotonic and changed_at is written strictly increasing
  under a writer lock (see routes/marketplace_moderation.py), so the public
  revocations feed can page on changed_at alone. The UNIQUE index on
  changed_at is the backstop for that invariant. package_id / version_id /
  org_id are plain strings: the feed must outlive deleted packages and orgs.
- skill_package_reports.scope_kind / scope_org_id: every report is bound
  at creation to the listings its reporter could see. 'platform' (a public
  or personal listing was visible) is moderated by platform admins; 'org'
  only by that org's owners/admins.
- skill_blocked_content_hashes: durable content-hash blocklist, independent
  of package / version / listing rows, so deleting a blocked version and
  re-publishing the same bytes never makes them deliverable. scope_org_id
  NULL is a global (platform admin) block; otherwise the block applies only
  to packages owned by that org. Rows are lifted, never deleted.
- skill_marketplace_listings.owner_state / skill_packages.owner_state:
  moderation by the listing's OWNER (an org's owners/admins, or a personal
  package's owner), which is listing-scoped: it hides only that owner's
  non-public listings and never a public listing. Platform moderation keeps
  using the version-wide listing flags (is_yanked / verification_state) and
  skill_packages.is_yanked. Values: NULL | 'yanked' | 'blocked'.
- skill_packages.platform_state: the platform layer's whole-package state
  (NULL | 'yanked' | 'blocked'), persisted so a platform block keeps
  refusing publishes and deletions of versions published after it.
  is_yanked mirrors it; existing is_yanked rows are backfilled 'yanked'.
- org_skill_assignments.version_id loses its ON DELETE CASCADE foreign key
  (it becomes a plain string, like install requests and consent receipts):
  a revoked assignment is audit history and must survive the deletion of
  its version. PostgreSQL drops the constraint. SQLite cannot drop a
  foreign key, so the table is rebuilt (rename, create, copy, drop, then
  re-create every index) from the exact DDL a fresh create_all emits for
  the current model - all CHECK constraints, the other foreign keys, and
  the revoked_at-aware partial unique indexes included. Downgrade rebuilds
  it with the cascading version FK again.
- skill_grant_generations: per-collection change counters ('project' install
  requests, 'org' assignments) behind the grant lists' cursors.
- org_skill_assignments.revoked_at: blocking a version marks its org
  assignments revoked instead of deleting them. The two partial unique
  indexes are rebuilt to ignore revoked rows so an admin can re-assign a
  version after it is restored.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "066"
down_revision = "065"
branch_labels = None
depends_on = None


def _event_id_type() -> sa.types.TypeEngine:
    # BIGINT identity on PostgreSQL; SQLite only autoincrements INTEGER PKs.
    return sa.BigInteger().with_variant(sa.Integer(), "sqlite")


def upgrade() -> None:
    op.create_table(
        "skill_package_reports",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("package_id", sa.String(length=64), nullable=False),
        sa.Column("version_id", sa.String(length=64), nullable=True),
        sa.Column(
            "reporter_user_id",
            sa.String(length=36),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("reason", sa.String(length=20), nullable=False),
        sa.Column("details", sa.Text(), nullable=False, server_default=""),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="open"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "resolved_by_user_id",
            sa.String(length=36),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("resolution_note", sa.Text(), nullable=True),
        sa.Column("scope_kind", sa.String(length=20), nullable=False),
        sa.Column("scope_org_id", sa.String(length=64), nullable=True),
        sa.CheckConstraint(
            "(scope_kind = 'platform' AND scope_org_id IS NULL) OR "
            "(scope_kind = 'org' AND scope_org_id IS NOT NULL)",
            name="ck_skill_report_scope",
        ),
        sa.CheckConstraint(
            "reason IN ('malicious', 'secrets', 'impersonation', 'broken', "
            "'license', 'spam', 'other')",
            name="ck_skill_report_reason",
        ),
        sa.CheckConstraint(
            "status IN ('open', 'actioned', 'dismissed')", name="ck_skill_report_status"
        ),
    )
    op.create_index(
        "uq_skill_report_open_reporter_package",
        "skill_package_reports",
        ["reporter_user_id", "package_id"],
        unique=True,
        postgresql_where=sa.text("status = 'open'"),
        sqlite_where=sa.text("status = 'open'"),
    )
    op.create_index(
        "idx_skill_report_package_status", "skill_package_reports", ["package_id", "status"]
    )
    op.create_index(
        "idx_skill_report_status_created", "skill_package_reports", ["status", "created_at"]
    )
    op.create_index(
        "idx_skill_report_reporter_created",
        "skill_package_reports",
        ["reporter_user_id", "created_at"],
    )
    op.create_index(
        "idx_skill_report_resolved_by", "skill_package_reports", ["resolved_by_user_id"]
    )
    op.create_index(
        "idx_skill_report_scope",
        "skill_package_reports",
        ["scope_kind", "scope_org_id", "status", "created_at"],
    )

    op.create_table(
        "skill_blocked_content_hashes",
        sa.Column("id", _event_id_type(), primary_key=True, autoincrement=True),
        sa.Column("content_hash", sa.String(length=80), nullable=False),
        sa.Column("scope_org_id", sa.String(length=64), nullable=True),
        sa.Column("source_package_id", sa.String(length=64), nullable=False),
        sa.Column("source_version_id", sa.String(length=64), nullable=True),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column(
            "actor_user_id",
            sa.String(length=36),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("actor_role", sa.String(length=20), nullable=False),
        sa.Column("blocked_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("lifted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "lifted_by_user_id",
            sa.String(length=36),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.create_index(
        "idx_skill_blocked_hash_active",
        "skill_blocked_content_hashes",
        ["content_hash", "lifted_at"],
    )
    op.create_index(
        "idx_skill_blocked_hash_source",
        "skill_blocked_content_hashes",
        ["source_package_id", "source_version_id"],
    )
    op.create_index(
        "idx_skill_blocked_hash_actor", "skill_blocked_content_hashes", ["actor_user_id"]
    )
    op.create_index(
        "idx_skill_blocked_hash_lifted_by", "skill_blocked_content_hashes", ["lifted_by_user_id"]
    )

    op.create_table(
        "skill_grant_generations",
        sa.Column("scope_type", sa.String(length=20), primary_key=True),
        sa.Column("scope_id", sa.String(length=64), primary_key=True),
        sa.Column("generation", sa.BigInteger(), nullable=False, server_default="0"),
    )

    op.create_table(
        "skill_revocation_events",
        sa.Column("id", _event_id_type(), primary_key=True, autoincrement=True),
        # 'version' | 'package' | 'content'. Content events carry only the
        # hash (package_id / version_id NULL) so a global block reaches every
        # tenant without disclosing a private package.
        sa.Column("kind", sa.String(length=20), nullable=False),
        sa.Column("package_id", sa.String(length=64), nullable=True),
        sa.Column("version_id", sa.String(length=64), nullable=True),
        sa.Column("content_hash", sa.String(length=80), nullable=True),
        sa.Column("state", sa.String(length=20), nullable=False),
        # 'global' | 'org' | 'owner' for block / restore-of-block; NULL otherwise.
        sa.Column("block_scope", sa.String(length=20), nullable=True),
        # The org an 'org' block is confined to (always set for 'org'); for an
        # 'owner' (owner-layer yank) event the owning org, or NULL for a
        # personal package; NULL for everything else.
        sa.Column("scope_org_id", sa.String(length=64), nullable=True),
        # Internal provenance of content events; never served by the feed.
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
        sa.CheckConstraint(
            "state IN ('yanked', 'blocked', 'restored')", name="ck_skill_revocation_state"
        ),
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
    )
    op.create_index(
        "uq_skill_revocation_changed_at",
        "skill_revocation_events",
        ["changed_at"],
        unique=True,
    )
    op.create_index(
        "idx_skill_revocation_package",
        "skill_revocation_events",
        ["package_id", "version_id", "id"],
    )
    op.create_index(
        "idx_skill_revocation_org", "skill_revocation_events", ["org_id", "changed_at"]
    )
    op.create_index(
        "idx_skill_revocation_owner", "skill_revocation_events", ["owner_user_id", "changed_at"]
    )
    op.create_index(
        "idx_skill_revocation_actor", "skill_revocation_events", ["actor_user_id"]
    )

    op.add_column(
        "skill_marketplace_listings",
        sa.Column("owner_state", sa.String(length=20), nullable=True),
    )
    op.add_column(
        "skill_packages",
        sa.Column("owner_state", sa.String(length=20), nullable=True),
    )
    op.add_column(
        "skill_packages",
        sa.Column("platform_state", sa.String(length=20), nullable=True),
    )
    op.execute(sa.text("UPDATE skill_packages SET platform_state = 'yanked' WHERE is_yanked"))
    op.add_column(
        "org_skill_assignments",
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
    )
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        for name in _assignment_version_fk_names(bind):
            op.drop_constraint(name, "org_skill_assignments", type_="foreignkey")
    # Rebuild the two partial unique indexes (created by 063) so a revoked
    # assignment never blocks a fresh one for the same target.
    op.drop_index("uq_org_skill_assignment_org_user", table_name="org_skill_assignments")
    op.drop_index("uq_org_skill_assignment_device", table_name="org_skill_assignments")
    _create_assignment_unique_indexes(revoked_filter=True)
    if bind.dialect.name == "sqlite":
        _rebuild_sqlite_assignments(version_fk=False)


_ASSIGNMENT_VERSION_FK = "org_skill_assignments_version_id_fkey"

# The exact DDL SQLAlchemy's create_all emits for OrgSkillAssignment at 066
# (kept verbatim so an upgraded SQLite database is identical to a fresh one;
# test_migration_066 compares sqlite_master). The version FK is absent at 066
# and restored (063's cascading FK) on downgrade.
_SQLITE_ASSIGNMENT_ITEMS = (
    "id VARCHAR(64) NOT NULL",
    "org_id VARCHAR(64) NOT NULL",
    "version_id VARCHAR(64) NOT NULL",
    "content_hash VARCHAR(80) NOT NULL",
    "target_type VARCHAR(20) NOT NULL",
    "target_id VARCHAR(100) DEFAULT '*' NOT NULL",
    "target_user_id VARCHAR(36)",
    "mode VARCHAR(20) DEFAULT 'optional' NOT NULL",
    "target_tools TEXT DEFAULT '[]' NOT NULL",
    "policy_snapshot TEXT DEFAULT '{}' NOT NULL",
    "created_by_user_id VARCHAR(36)",
    "created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL",
    "revoked_at DATETIME",
    "PRIMARY KEY (id)",
    "CONSTRAINT ck_org_skill_assignment_target_type CHECK (target_type IN ('org', 'user', 'device'))",
    "CONSTRAINT ck_org_skill_assignment_target_binding CHECK ("
    "(target_type = 'org' AND target_id = '*' AND target_user_id IS NULL) OR "
    "(target_type = 'user' AND target_user_id IS NOT NULL AND target_id = target_user_id) OR "
    "(target_type = 'device' AND target_user_id IS NOT NULL AND target_id != '*'))",
    "CONSTRAINT ck_org_skill_assignment_mode CHECK (mode IN ('required', 'optional'))",
    "FOREIGN KEY(org_id) REFERENCES organizations (id) ON DELETE CASCADE",
    "{version_fk}",
    "FOREIGN KEY(target_user_id) REFERENCES users (id) ON DELETE CASCADE",
    "FOREIGN KEY(created_by_user_id) REFERENCES users (id) ON DELETE SET NULL",
)
_SQLITE_VERSION_FK = (
    "FOREIGN KEY(version_id) REFERENCES skill_package_versions (id) ON DELETE CASCADE"
)


def _sqlite_assignments_ddl(*, version_fk: bool) -> str:
    """The exact text create_all emits (items joined by ", \\n\\t")."""
    items = [
        (_SQLITE_VERSION_FK if version_fk else None) if item == "{version_fk}" else item
        for item in _SQLITE_ASSIGNMENT_ITEMS
    ]
    body = ", \n\t".join(item for item in items if item is not None)
    return "CREATE TABLE org_skill_assignments (\n\t" + body + "\n)"


_SQLITE_ASSIGNMENT_COLUMNS = (
    "id, org_id, version_id, content_hash, target_type, target_id, target_user_id, mode, "
    "target_tools, policy_snapshot, created_by_user_id, created_at, revoked_at"
)
_SQLITE_ASSIGNMENT_INDEXES = (
    "CREATE INDEX idx_org_skill_assignment_created_by ON org_skill_assignments (created_by_user_id)",
    "CREATE INDEX idx_org_skill_assignment_target ON org_skill_assignments "
    "(org_id, target_type, target_id)",
    "CREATE INDEX idx_org_skill_assignment_target_user ON org_skill_assignments (target_user_id)",
    "CREATE INDEX idx_org_skill_assignment_version ON org_skill_assignments (version_id)",
    "CREATE UNIQUE INDEX uq_org_skill_assignment_device ON org_skill_assignments "
    "(org_id, version_id, target_user_id, target_id) "
    "WHERE target_type = 'device' AND revoked_at IS NULL",
    "CREATE UNIQUE INDEX uq_org_skill_assignment_org_user ON org_skill_assignments "
    "(org_id, version_id, target_type, target_id) "
    "WHERE target_type != 'device' AND revoked_at IS NULL",
)


def _rebuild_sqlite_assignments(*, version_fk: bool) -> None:
    """Rebuild org_skill_assignments on SQLite (which cannot drop or add a
    foreign key in place) with or without the version FK, preserving every
    row, CHECK constraint, other FK and index."""
    op.execute(sa.text("ALTER TABLE org_skill_assignments RENAME TO _org_skill_assignments_066"))
    op.execute(sa.text(_sqlite_assignments_ddl(version_fk=version_fk)))
    op.execute(
        sa.text(
            f"INSERT INTO org_skill_assignments ({_SQLITE_ASSIGNMENT_COLUMNS}) "
            f"SELECT {_SQLITE_ASSIGNMENT_COLUMNS} FROM _org_skill_assignments_066"
        )
    )
    # Dropping the old table drops its indexes, freeing the names.
    op.execute(sa.text("DROP TABLE _org_skill_assignments_066"))
    for statement in _SQLITE_ASSIGNMENT_INDEXES:
        op.execute(sa.text(statement))


def _assignment_version_fk_names(bind) -> list[str]:
    """Name(s) of the org_skill_assignments.version_id -> versions FK as it
    actually exists (063 declared it inline, so PostgreSQL named it)."""
    inspector = sa.inspect(bind)
    return [
        fk["name"]
        for fk in inspector.get_foreign_keys("org_skill_assignments")
        if fk.get("referred_table") == "skill_package_versions"
        and fk.get("constrained_columns") == ["version_id"]
        and fk.get("name")
    ]


def _create_assignment_unique_indexes(*, revoked_filter: bool) -> None:
    suffix = " AND revoked_at IS NULL" if revoked_filter else ""
    op.create_index(
        "uq_org_skill_assignment_org_user",
        "org_skill_assignments",
        ["org_id", "version_id", "target_type", "target_id"],
        unique=True,
        postgresql_where=sa.text(f"target_type != 'device'{suffix}"),
        sqlite_where=sa.text(f"target_type != 'device'{suffix}"),
    )
    op.create_index(
        "uq_org_skill_assignment_device",
        "org_skill_assignments",
        ["org_id", "version_id", "target_user_id", "target_id"],
        unique=True,
        postgresql_where=sa.text(f"target_type = 'device'{suffix}"),
        sqlite_where=sa.text(f"target_type = 'device'{suffix}"),
    )


def downgrade() -> None:
    # Revoked assignments cannot coexist with the stricter 063 indexes (a
    # re-assignment after restore duplicates the revoked row), and without
    # the column they would silently grant again. Remove them first.
    op.execute(sa.text("DELETE FROM org_skill_assignments WHERE revoked_at IS NOT NULL"))
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        op.execute(sa.text(
            "DELETE FROM org_skill_assignments WHERE NOT EXISTS "
            "(SELECT 1 FROM skill_package_versions v WHERE v.id = org_skill_assignments.version_id)"
        ))
        _rebuild_sqlite_assignments(version_fk=True)
    if bind.dialect.name == "postgresql":
        # Restore 063's cascading FK. Rows whose version is already gone
        # cannot satisfy it (only revoked rows could outlive a version, and
        # those were removed above; this is the backstop).
        op.execute(sa.text(
            "DELETE FROM org_skill_assignments a WHERE NOT EXISTS "
            "(SELECT 1 FROM skill_package_versions v WHERE v.id = a.version_id)"
        ))
        if not _assignment_version_fk_names(bind):
            op.create_foreign_key(
                _ASSIGNMENT_VERSION_FK,
                "org_skill_assignments",
                "skill_package_versions",
                ["version_id"],
                ["id"],
                ondelete="CASCADE",
            )
    op.drop_index("uq_org_skill_assignment_org_user", table_name="org_skill_assignments")
    op.drop_index("uq_org_skill_assignment_device", table_name="org_skill_assignments")
    # Plain DROP COLUMN on both dialects (SQLite >= 3.35). Batch mode would
    # rebuild the SQLite table from reflection and lose its CHECK constraints.
    op.execute(sa.text("ALTER TABLE org_skill_assignments DROP COLUMN revoked_at"))
    op.execute(sa.text("ALTER TABLE skill_packages DROP COLUMN owner_state"))
    op.execute(sa.text("ALTER TABLE skill_packages DROP COLUMN platform_state"))
    op.execute(sa.text("ALTER TABLE skill_marketplace_listings DROP COLUMN owner_state"))
    _create_assignment_unique_indexes(revoked_filter=False)

    op.drop_table("skill_grant_generations")
    op.drop_index("idx_skill_revocation_actor", table_name="skill_revocation_events")
    op.drop_index("idx_skill_revocation_owner", table_name="skill_revocation_events")
    op.drop_index("idx_skill_revocation_org", table_name="skill_revocation_events")
    op.drop_index("idx_skill_revocation_package", table_name="skill_revocation_events")
    op.drop_index("uq_skill_revocation_changed_at", table_name="skill_revocation_events")
    op.drop_table("skill_revocation_events")

    op.drop_index("idx_skill_blocked_hash_lifted_by", table_name="skill_blocked_content_hashes")
    op.drop_index("idx_skill_blocked_hash_actor", table_name="skill_blocked_content_hashes")
    op.drop_index("idx_skill_blocked_hash_source", table_name="skill_blocked_content_hashes")
    op.drop_index("idx_skill_blocked_hash_active", table_name="skill_blocked_content_hashes")
    op.drop_table("skill_blocked_content_hashes")

    op.drop_index("idx_skill_report_scope", table_name="skill_package_reports")
    op.drop_index("idx_skill_report_resolved_by", table_name="skill_package_reports")
    op.drop_index("idx_skill_report_reporter_created", table_name="skill_package_reports")
    op.drop_index("idx_skill_report_status_created", table_name="skill_package_reports")
    op.drop_index("idx_skill_report_package_status", table_name="skill_package_reports")
    op.drop_index("uq_skill_report_open_reporter_package", table_name="skill_package_reports")
    op.drop_table("skill_package_reports")
