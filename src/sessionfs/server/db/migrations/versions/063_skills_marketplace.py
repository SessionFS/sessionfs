"""Portable skills and plugins marketplace.

Revision ID: 063
Revises: 062

Adds immutable package versions, visibility-scoped marketplace listings,
external registry references, consent receipts, install requests, and exact
organization assignments. Package bytes live in the configured blob store.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "063"
down_revision = "062"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "skill_packages",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("owner_type", sa.String(length=20), nullable=False),
        sa.Column("owner_id", sa.String(length=64), nullable=False),
        sa.Column(
            "org_id",
            sa.String(length=64),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("namespace", sa.String(length=100), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("display_name", sa.String(length=200), nullable=False),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("source_type", sa.String(length=30), nullable=False),
        sa.Column("source_locator", sa.String(length=500), nullable=False),
        sa.Column("source_subdir", sa.String(length=500), nullable=False, server_default=""),
        sa.Column("current_version_id", sa.String(length=64), nullable=True),
        sa.Column("is_yanked", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column(
            "created_by_user_id",
            sa.String(length=36),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint(
            "owner_type",
            "owner_id",
            "namespace",
            "source_type",
            "source_locator",
            "source_subdir",
            "name",
            name="uq_skill_package_source_identity",
        ),
        sa.CheckConstraint(
            "owner_type IN ('system', 'org', 'user')", name="ck_skill_package_owner_type"
        ),
        sa.CheckConstraint(
            "(owner_type = 'system' AND org_id IS NULL) OR "
            "(owner_type = 'org' AND org_id IS NOT NULL AND owner_id = org_id) OR "
            "(owner_type = 'user' AND org_id IS NULL)",
            name="ck_skill_package_owner_scope",
        ),
    )
    op.create_index("idx_skill_packages_org_name", "skill_packages", ["org_id", "name"])

    op.create_table(
        "skill_package_versions",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column(
            "package_id",
            sa.String(length=64),
            sa.ForeignKey("skill_packages.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("version_label", sa.String(length=100), nullable=True),
        sa.Column("content_hash", sa.String(length=80), nullable=False),
        sa.Column("source_commit", sa.String(length=40), nullable=True),
        sa.Column("agent_plugin_schema", sa.String(length=200), nullable=False),
        sa.Column("manifest_json", sa.Text(), nullable=False),
        sa.Column("risk_summary", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("compatibility", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("instruction_bundle", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("license_metadata", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("signature_metadata", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("dlp_status", sa.String(length=20), nullable=False),
        sa.Column("blob_key", sa.String(length=700), nullable=False, unique=True),
        sa.Column("blob_size_bytes", sa.BigInteger(), nullable=False),
        sa.Column(
            "created_by_user_id",
            sa.String(length=36),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint("package_id", "content_hash", name="uq_skill_version_hash"),
        sa.CheckConstraint(
            "dlp_status IN ('passed', 'blocked')", name="ck_skill_version_dlp_status"
        ),
    )
    op.create_index(
        "idx_skill_versions_package_created",
        "skill_package_versions",
        ["package_id", "created_at"],
    )
    op.create_index(
        "idx_skill_versions_content_hash", "skill_package_versions", ["content_hash"]
    )

    op.create_table(
        "skill_marketplace_listings",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("collection", sa.String(length=100), nullable=False, server_default="sessionfs"),
        sa.Column(
            "package_id",
            sa.String(length=64),
            sa.ForeignKey("skill_packages.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "version_id",
            sa.String(length=64),
            sa.ForeignKey("skill_package_versions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "org_id",
            sa.String(length=64),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column(
            "owner_user_id",
            sa.String(length=36),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("visibility", sa.String(length=20), nullable=False),
        sa.Column("is_published", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("is_yanked", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("verification_state", sa.String(length=20), nullable=False, server_default="verified"),
        sa.Column("search_text", sa.Text(), nullable=False, server_default=""),
        sa.Column(
            "published_by_user_id",
            sa.String(length=36),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "published_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint(
            "collection", "version_id", name="uq_skill_listing_collection_version"
        ),
        sa.CheckConstraint(
            "visibility IN ('public', 'user', 'org')", name="ck_skill_listing_visibility"
        ),
        sa.CheckConstraint(
            "(visibility = 'public' AND org_id IS NULL AND owner_user_id IS NULL) OR "
            "(visibility = 'user' AND org_id IS NULL AND owner_user_id IS NOT NULL) OR "
            "(visibility = 'org' AND org_id IS NOT NULL AND owner_user_id IS NULL)",
            name="ck_skill_listing_visibility_owner",
        ),
        sa.CheckConstraint(
            "verification_state IN ('verified', 'unverified', 'blocked')",
            name="ck_skill_listing_verification_state",
        ),
    )
    op.create_index(
        "idx_skill_listing_visibility_state",
        "skill_marketplace_listings",
        ["visibility", "org_id", "is_published", "is_yanked"],
    )
    op.create_index(
        "idx_skill_listing_search_name", "skill_marketplace_listings", ["name"]
    )
    op.create_index(
        "idx_skill_listing_package", "skill_marketplace_listings", ["package_id"]
    )
    op.create_index(
        "idx_skill_listing_owner_user", "skill_marketplace_listings", ["owner_user_id"]
    )

    op.create_table(
        "skill_consent_receipts",
        sa.Column("id", sa.String(length=64), primary_key=True),
        # Plain string, deliberately NOT a ForeignKey: a consent receipt is an
        # AUDIT ROW that must outlive the version it consented to. An
        # ON DELETE CASCADE destroyed the consent evidence at the exact moment
        # the version was deleted; deletion now sets `deleted_at` instead.
        sa.Column("version_id", sa.String(length=64), nullable=False),
        sa.Column("content_hash", sa.String(length=80), nullable=False),
        sa.Column(
            "actor_user_id",
            sa.String(length=36),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("actor_type", sa.String(length=20), nullable=False),
        sa.Column("capability", sa.String(length=50), nullable=False),
        sa.Column("target_tool", sa.String(length=50), nullable=True),
        sa.Column("scope", sa.String(length=20), nullable=False),
        sa.Column("device_id", sa.String(length=100), nullable=True),
        sa.Column("managed_policy_id", sa.String(length=64), nullable=True),
        # Receipts are bound to the request (and project) that produced them so
        # revoking one grant never invalidates another project's receipts.
        sa.Column("install_request_id", sa.String(length=64), nullable=True),
        sa.Column("project_id", sa.String(length=64), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        # Archived when the consented version is deleted; reads filter it out.
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint(
            "actor_type IN ('user', 'service_key')", name="ck_skill_consent_actor_type"
        ),
        sa.CheckConstraint(
            "scope IN ('project', 'user', 'org', 'device')", name="ck_skill_consent_scope"
        ),
        sa.CheckConstraint(
            "capability IN ('instructions', 'assets', 'scripts', 'mcp', 'hooks')",
            name="ck_skill_consent_capability",
        ),
    )
    op.create_index(
        "idx_skill_consent_actor_hash",
        "skill_consent_receipts",
        ["actor_user_id", "content_hash"],
    )
    op.create_index("idx_skill_consent_version", "skill_consent_receipts", ["version_id"])
    op.create_index(
        "idx_skill_consent_request", "skill_consent_receipts", ["install_request_id"]
    )

    op.create_table(
        "skill_marketplace_registries",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("owner_type", sa.String(length=20), nullable=False),
        sa.Column("owner_id", sa.String(length=64), nullable=False),
        sa.Column(
            "org_id",
            sa.String(length=64),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("alias", sa.String(length=50), nullable=False),
        sa.Column("source_url", sa.String(length=500), nullable=False),
        sa.Column("refresh_commit", sa.String(length=40), nullable=True),
        sa.Column("policy_state", sa.String(length=20), nullable=False, server_default="approved"),
        sa.Column(
            "created_by_user_id",
            sa.String(length=36),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint(
            "owner_type", "owner_id", "alias", name="uq_skill_registry_alias"
        ),
        sa.CheckConstraint(
            "owner_type IN ('user', 'project', 'org')", name="ck_skill_registry_owner_type"
        ),
        sa.CheckConstraint(
            "policy_state IN ('approved', 'review', 'blocked')",
            name="ck_skill_registry_policy_state",
        ),
        sa.CheckConstraint(
            "(owner_type = 'org' AND org_id IS NOT NULL AND owner_id = org_id) OR "
            "(owner_type = 'user' AND org_id IS NULL) OR "
            "owner_type = 'project'",
            name="ck_skill_registry_owner_scope",
        ),
    )
    op.create_index(
        "idx_skill_registry_org", "skill_marketplace_registries", ["org_id", "policy_state"]
    )

    op.create_table(
        "skill_install_requests",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column(
            "project_id",
            sa.String(length=64),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        # Plain string, deliberately NOT a ForeignKey: a decided request is the
        # ATTRIBUTION record ("who asked for this package") and must outlive the
        # version. An ON DELETE CASCADE erased that trail on version delete.
        sa.Column("version_id", sa.String(length=64), nullable=False),
        sa.Column("content_hash", sa.String(length=80), nullable=False),
        sa.Column(
            "requested_by_user_id",
            sa.String(length=36),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("requested_by_session_id", sa.String(length=64), nullable=True),
        # Service-key provenance (mirrors the actor triple on other audit rows).
        sa.Column("requested_actor_type", sa.String(length=20), nullable=False, server_default="user"),
        sa.Column("requested_by_service_key_id", sa.String(length=36), nullable=True),
        sa.Column("requested_by_service_key_name", sa.String(length=100), nullable=True),
        sa.Column(
            "target_user_id",
            sa.String(length=36),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("target_device_id", sa.String(length=100), nullable=True),
        sa.Column("target_scope", sa.String(length=20), nullable=False),
        sa.Column("target_tools", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("requested_capabilities", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="pending"),
        # Origin of the request ('api' | 'mcp'), recorded for DISPLAY and audit
        # only. It is caller-supplied under bearer auth, so it is never a
        # security input: ANY same-principal approval is gated by the emailed
        # single-use confirmation code below, regardless of origin.
        sa.Column("requested_via", sa.String(length=10), nullable=False, server_default="api"),
        # Same-principal approval requires a single-use emailed confirmation code;
        # only its sha256 is stored (design §9.2 consent challenge).
        sa.Column("approval_challenge_hash", sa.String(length=64), nullable=True),
        sa.Column("approval_challenge_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("approval_challenge_issued_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "approval_challenge_attempts", sa.Integer(), nullable=False, server_default="0"
        ),
        sa.Column(
            "approved_by_user_id",
            sa.String(length=36),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        # Archived when the requested version is deleted (TERMINAL requests
        # only; pending ones are hard-deleted). Reads filter it out.
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'approved', 'rejected', 'cancelled', 'applied', 'revoked')",
            name="ck_skill_request_status",
        ),
        sa.CheckConstraint(
            "target_scope IN ('project', 'user', 'device')",
            name="ck_skill_request_target_scope",
        ),
        sa.CheckConstraint(
            "target_scope != 'device' OR target_device_id IS NOT NULL",
            name="ck_skill_request_device_target",
        ),
        sa.CheckConstraint(
            "requested_via IN ('api', 'mcp')", name="ck_skill_request_requested_via"
        ),
    )
    op.create_index(
        "idx_skill_request_project_status",
        "skill_install_requests",
        ["project_id", "status", "created_at"],
    )
    op.create_index(
        "idx_skill_request_user_status",
        "skill_install_requests",
        ["requested_by_user_id", "status"],
    )

    op.create_table(
        "org_skill_assignments",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column(
            "org_id",
            sa.String(length=64),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "version_id",
            sa.String(length=64),
            sa.ForeignKey("skill_package_versions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("content_hash", sa.String(length=80), nullable=False),
        sa.Column("target_type", sa.String(length=20), nullable=False),
        sa.Column("target_id", sa.String(length=100), nullable=False, server_default="*"),
        sa.Column(
            "target_user_id",
            sa.String(length=36),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("mode", sa.String(length=20), nullable=False, server_default="optional"),
        sa.Column("target_tools", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("policy_snapshot", sa.Text(), nullable=False, server_default="{}"),
        sa.Column(
            "created_by_user_id",
            sa.String(length=36),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint(
            "target_type IN ('org', 'user', 'device')",
            name="ck_org_skill_assignment_target_type",
        ),
        sa.CheckConstraint(
            "(target_type = 'org' AND target_id = '*' AND target_user_id IS NULL) OR "
            "(target_type = 'user' AND target_user_id IS NOT NULL AND "
            "target_id = target_user_id) OR "
            "(target_type = 'device' AND target_user_id IS NOT NULL AND target_id != '*')",
            name="ck_org_skill_assignment_target_binding",
        ),
        sa.CheckConstraint(
            "mode IN ('required', 'optional')", name="ck_org_skill_assignment_mode"
        ),
    )
    op.create_index(
        "idx_org_skill_assignment_target",
        "org_skill_assignments",
        ["org_id", "target_type", "target_id"],
    )
    op.create_index(
        "idx_org_skill_assignment_version", "org_skill_assignments", ["version_id"]
    )
    # Uniqueness: org/user targets are unique per (org, version, type, target);
    # device targets are bound to their user, so two members may both register
    # a device called "runner-1" without colliding.
    op.create_index(
        "uq_org_skill_assignment_org_user",
        "org_skill_assignments",
        ["org_id", "version_id", "target_type", "target_id"],
        unique=True,
        postgresql_where=sa.text("target_type != 'device'"),
        sqlite_where=sa.text("target_type != 'device'"),
    )
    op.create_index(
        "uq_org_skill_assignment_device",
        "org_skill_assignments",
        ["org_id", "version_id", "target_user_id", "target_id"],
        unique=True,
        postgresql_where=sa.text("target_type = 'device'"),
        sqlite_where=sa.text("target_type = 'device'"),
    )

    # Normalized per-tool compatibility so the marketplace tool filter runs in
    # SQL (filter-then-paginate) instead of on a Python slice of the newest N.
    op.create_table(
        "skill_version_tools",
        sa.Column(
            "version_id",
            sa.String(length=64),
            sa.ForeignKey("skill_package_versions.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("tool", sa.String(length=50), primary_key=True),
    )
    op.create_index("idx_skill_version_tools_tool", "skill_version_tools", ["tool", "version_id"])
    # Every FK column that joins / lookups filter on carries a LEADING index.
    for name, table, columns in _FK_INDEXES:
        op.create_index(name, table, columns)
    _backfill_version_tools()


_FK_INDEXES = (
    ("idx_skill_packages_created_by", "skill_packages", ["created_by_user_id"]),
    ("idx_skill_versions_created_by", "skill_package_versions", ["created_by_user_id"]),
    ("idx_skill_listing_version", "skill_marketplace_listings", ["version_id"]),
    ("idx_skill_listing_org", "skill_marketplace_listings", ["org_id"]),
    ("idx_skill_listing_published_by", "skill_marketplace_listings", ["published_by_user_id"]),
    ("idx_skill_registry_created_by", "skill_marketplace_registries", ["created_by_user_id"]),
    ("idx_skill_request_version", "skill_install_requests", ["version_id"]),
    ("idx_skill_request_target_user", "skill_install_requests", ["target_user_id"]),
    ("idx_skill_request_approved_by", "skill_install_requests", ["approved_by_user_id"]),
    ("idx_org_skill_assignment_target_user", "org_skill_assignments", ["target_user_id"]),
    ("idx_org_skill_assignment_created_by", "org_skill_assignments", ["created_by_user_id"]),
)


def _backfill_version_tools() -> None:
    """Populate skill_version_tools from each version's compatibility JSON.

    Dialect-agnostic on purpose (the JSON is parsed in Python): the table is
    created by this same migration, so on a fresh upgrade there is nothing to
    backfill, but a database that ran an earlier build of 063 gets its rows.
    """
    import json

    bind = op.get_bind()
    rows = bind.execute(
        sa.text("SELECT id, compatibility FROM skill_package_versions")
    ).fetchall()
    for version_id, compatibility in rows:
        try:
            value = json.loads(compatibility or "{}")
        except (TypeError, ValueError):
            continue
        if not isinstance(value, dict):
            continue
        for tool in sorted(value):
            if isinstance(tool, str) and tool:
                bind.execute(
                    sa.text(
                        "INSERT INTO skill_version_tools (version_id, tool) "
                        "VALUES (:version_id, :tool)"
                    ),
                    {"version_id": version_id, "tool": tool[:50]},
                )


def downgrade() -> None:
    for name, table, _columns in reversed(_FK_INDEXES):
        op.drop_index(name, table_name=table)
    op.drop_index("idx_skill_version_tools_tool", table_name="skill_version_tools")
    op.drop_table("skill_version_tools")
    op.drop_table("org_skill_assignments")
    op.drop_table("skill_install_requests")
    op.drop_table("skill_marketplace_registries")
    op.drop_table("skill_consent_receipts")
    op.drop_table("skill_marketplace_listings")
    op.drop_table("skill_package_versions")
    op.drop_table("skill_packages")
