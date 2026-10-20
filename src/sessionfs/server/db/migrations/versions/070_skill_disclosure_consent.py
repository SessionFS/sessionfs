"""Marketplace install disclosure, capability manifests and D2 re-consent.

Revision ID: 070
Revises: 069

(Marketplace v2 §3.6, tk_d8bc543da8344124. Chained from 069 while other
phase-2 migrations are built in parallel; the merge re-chains the number,
the name stays.)

- skill_package_versions: capability_manifest (byte-derived manifest,
  bounded JSON), capability_digest (sha256 of the access part; two versions
  with the same digest grant the same access), manifest_version (0 = not
  computed yet: an update from such a baseline is treated as widening, fail
  closed, until the manifest is computed lazily from the stored blob), and
  publication_seq (monotonic, package-scoped publication order, backfilled
  from created_at then id for already-published versions).
- skill_consent_receipts: capability_digest + consented_manifest (what was
  consented to), carried_forward_from_receipt_id (a non-widening update's
  inherited consent; plain string like every other audit reference so the
  chain survives archival), consent_rule_version (1 = v0.16 §7.3 any byte
  change resets consent; 2 = marketplace v2 D2).
- org_skill_assignments: capability_digest + consent_rule_version (the
  access the admin approved, bound when assigning or advancing; NULL on
  older rows, which never count as a digest approval), superseded_at /
  superseded_by_assignment_id (an advanced assignment is kept as history;
  it is also revoked, so it never grants and never holds the active unique
  index).
- org_skill_assignments.package_id, backfilled from the assigned version,
  and partial unique indexes enforcing ONE active assignment per (org,
  package, target). Existing active duplicates (several versions of one
  package assigned to the same target) are superseded down to the newest
  before the indexes are created.
- skill_install_requests.package_id + grant_scope_key (backfilled) +
  superseded_at / superseded_by_request_id, and a partial unique index:
  ONE active (approved / applied) approval per (project, package, grant
  scope). Older active duplicates are superseded (revoked, receipts
  invalidated, history kept) down to the newest before it is created. When
  two active duplicates tie for "newest" the migration fails closed instead
  of choosing by random id.
- skill_install_requests: approved_capability_digest +
  approved_consent_rule_version (the access the approver confirmed, bound
  immutably at approval; NULL on older approvals: fail closed).

Additive columns only; the publication_seq backfill is the one data write.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "070"
down_revision = "069"
branch_labels = None
depends_on = None


def _fail_on_ambiguous_winner(bind, *, label: str, sql: str) -> None:
    """Fail closed when two or more active duplicate rows tie for "newest":
    a random id must never choose which assignment / approval stays active.
    ``sql`` selects at least one row only when such a tie exists."""
    if bind.execute(sa.text(sql)).first() is not None:
        raise RuntimeError(
            f"Migration 070 cannot choose which {label} stays active: two or more active "
            "rows tie for the newest. Give the duplicates distinct timestamps and rerun."
        )


def upgrade() -> None:
    op.add_column(
        "skill_package_versions",
        sa.Column("capability_manifest", sa.Text(), nullable=False, server_default="{}"),
    )
    op.add_column(
        "skill_package_versions",
        sa.Column("capability_digest", sa.String(length=80), nullable=True),
    )
    op.add_column(
        "skill_package_versions",
        sa.Column("manifest_version", sa.SmallInteger(), nullable=False, server_default="0"),
    )
    op.add_column(
        "skill_package_versions",
        sa.Column("publication_seq", sa.Integer(), nullable=True),
    )
    # Backfill the publication order for already-published versions. Within a
    # package the sequence is 1..N in (created_at, id) order: the best signal
    # the existing rows carry. New publishes allocate MAX + 1 under the
    # publisher lock, so they always sort after every backfilled row.
    op.execute(
        sa.text(
            "UPDATE skill_package_versions SET publication_seq = ("
            "SELECT COUNT(*) FROM skill_package_versions older "
            "WHERE older.package_id = skill_package_versions.package_id "
            "AND (older.created_at < skill_package_versions.created_at "
            "     OR (older.created_at = skill_package_versions.created_at "
            "         AND older.id < skill_package_versions.id))) + 1"
        )
    )
    op.create_index(
        "idx_skill_versions_capability_digest",
        "skill_package_versions",
        ["package_id", "capability_digest"],
    )
    op.create_index(
        "idx_skill_versions_publication_seq",
        "skill_package_versions",
        ["package_id", "publication_seq"],
    )
    op.add_column(
        "skill_consent_receipts",
        sa.Column("capability_digest", sa.String(length=80), nullable=True),
    )
    op.add_column(
        "skill_consent_receipts",
        sa.Column("consented_manifest", sa.Text(), nullable=True),
    )
    op.add_column(
        "skill_consent_receipts",
        sa.Column("carried_forward_from_receipt_id", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "skill_consent_receipts",
        sa.Column("consent_rule_version", sa.SmallInteger(), nullable=False, server_default="1"),
    )
    op.add_column(
        "org_skill_assignments",
        sa.Column("capability_digest", sa.String(length=80), nullable=True),
    )
    op.add_column(
        "org_skill_assignments",
        sa.Column("consent_rule_version", sa.SmallInteger(), nullable=True),
    )
    op.add_column(
        "org_skill_assignments",
        sa.Column("superseded_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "org_skill_assignments",
        sa.Column("superseded_by_assignment_id", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "org_skill_assignments",
        sa.Column("package_id", sa.String(length=64), nullable=True),
    )
    op.execute(
        sa.text(
            "UPDATE org_skill_assignments SET package_id = ("
            "SELECT v.package_id FROM skill_package_versions v "
            "WHERE v.id = org_skill_assignments.version_id)"
        )
    )
    # Supersede older active duplicates (same org, package and target),
    # keeping the newest; they never grant again but stay as history. A tie
    # for the newest is ambiguous: fail closed rather than pick by random id.
    _fail_on_ambiguous_winner(
        op.get_bind(),
        label="skill assignment",
        sql=(
            "SELECT 1 FROM ("
            "SELECT org_id, package_id, target_type, target_id, COALESCE(target_user_id, '') AS tuid, "
            "RANK() OVER (PARTITION BY org_id, package_id, target_type, target_id, "
            "COALESCE(target_user_id, '') ORDER BY created_at DESC) AS rnk "
            "FROM org_skill_assignments WHERE revoked_at IS NULL AND package_id IS NOT NULL"
            ") ranked WHERE rnk = 1 GROUP BY org_id, package_id, target_type, target_id, tuid "
            "HAVING COUNT(*) > 1 LIMIT 1"
        ),
    )
    op.execute(
        sa.text(
            "UPDATE org_skill_assignments SET "
            "superseded_at = CURRENT_TIMESTAMP, revoked_at = CURRENT_TIMESTAMP, "
            "superseded_by_assignment_id = ("
            "  SELECT newer.id FROM org_skill_assignments newer "
            "  WHERE newer.org_id = org_skill_assignments.org_id "
            "  AND newer.package_id = org_skill_assignments.package_id "
            "  AND newer.target_type = org_skill_assignments.target_type "
            "  AND newer.target_id = org_skill_assignments.target_id "
            "  AND COALESCE(newer.target_user_id, '') = COALESCE(org_skill_assignments.target_user_id, '') "
            "  AND newer.revoked_at IS NULL "
            "  ORDER BY newer.created_at DESC, newer.id DESC LIMIT 1) "
            "WHERE revoked_at IS NULL AND package_id IS NOT NULL AND EXISTS ("
            "  SELECT 1 FROM org_skill_assignments newer "
            "  WHERE newer.org_id = org_skill_assignments.org_id "
            "  AND newer.package_id = org_skill_assignments.package_id "
            "  AND newer.target_type = org_skill_assignments.target_type "
            "  AND newer.target_id = org_skill_assignments.target_id "
            "  AND COALESCE(newer.target_user_id, '') = COALESCE(org_skill_assignments.target_user_id, '') "
            "  AND newer.revoked_at IS NULL "
            "  AND (newer.created_at > org_skill_assignments.created_at "
            "       OR (newer.created_at = org_skill_assignments.created_at "
            "           AND newer.id > org_skill_assignments.id)))"
        )
    )
    op.create_index(
        "uq_org_skill_assignment_package_target",
        "org_skill_assignments",
        ["org_id", "package_id", "target_type", "target_id"],
        unique=True,
        postgresql_where=sa.text(
            "target_type != 'device' AND revoked_at IS NULL AND package_id IS NOT NULL"
        ),
        sqlite_where=sa.text(
            "target_type != 'device' AND revoked_at IS NULL AND package_id IS NOT NULL"
        ),
    )
    op.create_index(
        "uq_org_skill_assignment_package_device",
        "org_skill_assignments",
        ["org_id", "package_id", "target_user_id", "target_id"],
        unique=True,
        postgresql_where=sa.text(
            "target_type = 'device' AND revoked_at IS NULL AND package_id IS NOT NULL"
        ),
        sqlite_where=sa.text(
            "target_type = 'device' AND revoked_at IS NULL AND package_id IS NOT NULL"
        ),
    )
    op.add_column(
        "skill_install_requests",
        sa.Column("approved_capability_digest", sa.String(length=80), nullable=True),
    )
    op.add_column(
        "skill_install_requests",
        sa.Column("approved_consent_rule_version", sa.SmallInteger(), nullable=True),
    )
    for column in (
        sa.Column("package_id", sa.String(length=64), nullable=True),
        sa.Column("grant_scope_key", sa.String(length=250), nullable=True),
        sa.Column("superseded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("superseded_by_request_id", sa.String(length=64), nullable=True),
    ):
        op.add_column("skill_install_requests", column)
    op.execute(
        sa.text(
            "UPDATE skill_install_requests SET "
            "package_id = (SELECT v.package_id FROM skill_package_versions v "
            "WHERE v.id = skill_install_requests.version_id), "
            "grant_scope_key = CASE target_scope "
            "WHEN 'project' THEN 'project' "
            "WHEN 'user' THEN 'user:' || COALESCE(target_user_id, '') "
            "ELSE 'device:' || COALESCE(target_user_id, '') || ':' || COALESCE(target_device_id, '') END"
        )
    )
    _newer = (
        "SELECT newer.id FROM skill_install_requests newer "
        "WHERE newer.project_id = skill_install_requests.project_id "
        "AND newer.package_id = skill_install_requests.package_id "
        "AND newer.grant_scope_key = skill_install_requests.grant_scope_key "
        "AND newer.status IN ('approved', 'applied') AND newer.deleted_at IS NULL "
        "AND (COALESCE(newer.decided_at, newer.created_at) > COALESCE(skill_install_requests.decided_at, "
        "skill_install_requests.created_at) OR (COALESCE(newer.decided_at, newer.created_at) = "
        "COALESCE(skill_install_requests.decided_at, skill_install_requests.created_at) "
        "AND newer.id > skill_install_requests.id))"
    )
    _active = (
        "status IN ('approved', 'applied') AND package_id IS NOT NULL AND deleted_at IS NULL"
    )
    # A tie for the newest approval in a grant scope is ambiguous: fail
    # closed rather than pick by random id.
    _fail_on_ambiguous_winner(
        op.get_bind(),
        label="skill install approval",
        sql=(
            "SELECT 1 FROM ("
            "SELECT project_id, package_id, grant_scope_key, "
            "RANK() OVER (PARTITION BY project_id, package_id, grant_scope_key "
            "ORDER BY COALESCE(decided_at, created_at) DESC) AS rnk "
            "FROM skill_install_requests "
            "WHERE status IN ('approved', 'applied') AND package_id IS NOT NULL "
            "AND grant_scope_key IS NOT NULL AND deleted_at IS NULL"
            ") ranked WHERE rnk = 1 GROUP BY project_id, package_id, grant_scope_key "
            "HAVING COUNT(*) > 1 LIMIT 1"
        ),
    )
    # Receipts of the superseded approvals first (while they are still active).
    op.execute(
        sa.text(
            "UPDATE skill_consent_receipts SET revoked_at = CURRENT_TIMESTAMP "
            "WHERE revoked_at IS NULL AND install_request_id IN ("
            f"SELECT id FROM skill_install_requests WHERE {_active} AND EXISTS ({_newer}))"
        )
    )
    op.execute(
        sa.text(
            "UPDATE skill_install_requests SET status = 'revoked', "
            "superseded_at = CURRENT_TIMESTAMP, superseded_by_request_id = ("
            f"{_newer.replace('SELECT newer.id', 'SELECT newer.id', 1)} "
            "ORDER BY COALESCE(newer.decided_at, newer.created_at) DESC, newer.id DESC LIMIT 1) "
            f"WHERE {_active} AND EXISTS ({_newer})"
        )
    )
    op.create_index(
        "uq_skill_request_active_grant",
        "skill_install_requests",
        ["project_id", "package_id", "grant_scope_key"],
        unique=True,
        postgresql_where=sa.text(
            "status IN ('approved', 'applied') AND package_id IS NOT NULL "
            "AND grant_scope_key IS NOT NULL AND deleted_at IS NULL"
        ),
        sqlite_where=sa.text(
            "status IN ('approved', 'applied') AND package_id IS NOT NULL "
            "AND grant_scope_key IS NOT NULL AND deleted_at IS NULL"
        ),
    )


def downgrade() -> None:
    # Plain DROP COLUMN on both dialects (SQLite >= 3.35): no table rebuild,
    # so every table keeps its CHECK constraints and partial indexes.
    op.drop_index("uq_skill_request_active_grant", table_name="skill_install_requests")
    for column in (
        "superseded_by_request_id",
        "superseded_at",
        "grant_scope_key",
        "package_id",
        "approved_consent_rule_version",
        "approved_capability_digest",
    ):
        op.execute(sa.text(f"ALTER TABLE skill_install_requests DROP COLUMN {column}"))
    op.drop_index("uq_org_skill_assignment_package_device", table_name="org_skill_assignments")
    op.drop_index("uq_org_skill_assignment_package_target", table_name="org_skill_assignments")
    for column in (
        "package_id",
        "superseded_by_assignment_id",
        "superseded_at",
        "consent_rule_version",
        "capability_digest",
    ):
        op.execute(sa.text(f"ALTER TABLE org_skill_assignments DROP COLUMN {column}"))
    for column in (
        "consent_rule_version",
        "carried_forward_from_receipt_id",
        "consented_manifest",
        "capability_digest",
    ):
        op.execute(sa.text(f"ALTER TABLE skill_consent_receipts DROP COLUMN {column}"))
    op.drop_index("idx_skill_versions_capability_digest", table_name="skill_package_versions")
    op.drop_index("idx_skill_versions_publication_seq", table_name="skill_package_versions")
    for column in ("publication_seq", "manifest_version", "capability_digest", "capability_manifest"):
        op.execute(sa.text(f"ALTER TABLE skill_package_versions DROP COLUMN {column}"))
