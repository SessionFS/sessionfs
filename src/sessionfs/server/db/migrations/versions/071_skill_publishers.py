"""Marketplace publishers: global namespaces, verification, trusted publishing.

Revision ID: 071
Revises: 070

(Marketplace v2 §4.7, specified there as 068. Chained from 069 while other
phase-2/3 migrations are built in parallel; the merge re-chains the number,
the name stays.)

- skill_publishers: owner of a globally unique, never-released namespace
  (UNIQUE namespace and UNIQUE confusable skeleton). Seeded with the platform
  system publisher ``sessionfs``.
- skill_publisher_verifications: DNS TXT / GitHub account proofs, separate
  from listing moderation; partial unique (method, subject) WHERE verified.
- skill_reserved_names: admin-managed reserved skeletons (brand / popular /
  tombstone), on top of the vendored list in skills/confusables.py.
- skill_trusted_publishers: GitHub Actions workflows (numeric repository id
  and owner id, workflow path, optional environment / ref pattern).
- skill_publish_tokens: hashed single-use publish tokens; UNIQUE(issuer, jti)
  is the OIDC replay guard.
- skill_packages.publisher_id.
- skill_marketplace_listings.moderation_state (+ reason, reviewer, time),
  backfilled from verification_state: blocked -> blocked, else approved.
  verification_state is kept for one release.

Only platform-admin public packages in the `sessionfs` namespace are attached
to the system publisher; every other legacy package keeps a NULL publisher.
Legacy per-owner namespaces can be claimed only by the owners already using
them (enforced live at claim time against skill_packages).
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "071"
down_revision = "070"
branch_labels = None
depends_on = None

_MODERATION_STATES = "('approved', 'pending_review', 'quarantined', 'blocked', 'rejected')"
SYSTEM_PUBLISHER_ID = "pub_sessionfs"


def _ts(name: str, nullable: bool = True) -> sa.Column:
    return sa.Column(name, sa.DateTime(timezone=True), nullable=nullable)


def _created_at() -> sa.Column:
    return sa.Column(
        "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )


def _user_fk(name: str) -> sa.Column:
    return sa.Column(
        name, sa.String(length=36), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )


def _canonical(domain: str) -> str:
    # Same spelling as services.domain_verification.canonical_domain() (kept
    # inline: migrations must not depend on application code).
    return (domain or "").strip().lower().rstrip(".")


def _normalize_sso_domains(bind) -> None:
    """Canonicalize org_domain_verifications.domain (lowercase, no trailing
    root dot) before cross-system domain exclusivity is enforced, and dedupe:

    - per (org, canonical domain): keep the verified row (else the oldest),
      delete the rest;
    - per canonical domain across orgs: keep the oldest verified row, mark
      the other verified rows 'failed' (their org must re-verify).

    Every removed / demoted row is logged. Irreversible (downgrade keeps the
    canonical spellings)."""
    rows = bind.execute(
        sa.text(
            "SELECT id, org_id, domain, status, created_at FROM org_domain_verifications "
            "ORDER BY created_at, id"
        )
    ).all()
    by_org: dict[tuple[str, str], list] = {}
    for row in rows:
        by_org.setdefault((row.org_id, _canonical(row.domain)), []).append(row)
    deleted: set[str] = set()
    for (org_id, domain), group in by_org.items():
        if len(group) < 2:
            continue
        keep = next((row for row in group if row.status == "verified"), group[0])
        for row in group:
            if row.id != keep.id:
                deleted.add(row.id)
                print(f"[migration 071] removed duplicate SSO domain row {row.id} ({row.domain!r}) for org {org_id}")
    for row_id in sorted(deleted):
        bind.execute(sa.text("DELETE FROM org_domain_verifications WHERE id = :id"), {"id": row_id})
    verified: dict[str, list] = {}
    for row in rows:
        if row.id not in deleted and row.status == "verified":
            verified.setdefault(_canonical(row.domain), []).append(row)
    demoted: set[str] = set()
    for domain, group in verified.items():
        for row in group[1:]:
            demoted.add(row.id)
            print(
                f"[migration 071] domain {domain!r} verified by several orgs; kept {group[0].id}, "
                f"marked {row.id} (org {row.org_id}) failed"
            )
    for row_id in sorted(demoted):
        bind.execute(
            sa.text("UPDATE org_domain_verifications SET status = 'failed' WHERE id = :id"), {"id": row_id}
        )
    for row in rows:
        if row.id in deleted:
            continue
        canonical = _canonical(row.domain)
        if canonical != row.domain:
            bind.execute(
                sa.text("UPDATE org_domain_verifications SET domain = :domain WHERE id = :id"),
                {"domain": canonical, "id": row.id},
            )


def upgrade() -> None:
    bind = op.get_bind()
    sqlite = bind.dialect.name == "sqlite"
    _normalize_sso_domains(bind)

    op.create_table(
        "skill_publishers",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("owner_type", sa.String(length=20), nullable=False),
        sa.Column("org_id", sa.String(length=64), nullable=True),
        sa.Column("user_id", sa.String(length=36), nullable=True),
        sa.Column("namespace", sa.String(length=39), nullable=False),
        sa.Column("namespace_normalized", sa.String(length=39), nullable=False),
        sa.Column("display_name", sa.String(length=200), nullable=False, server_default=""),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="active"),
        sa.Column("suspended_reason", sa.Text(), nullable=True),
        sa.Column("suspended_listing_ids", sa.Text(), nullable=False, server_default="[]"),
        sa.Column(
            "public_publishing", sa.Boolean(), nullable=False, server_default=sa.text("false")
        ),
        sa.Column("review_flags", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("required_domains", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("auth_generation", sa.Integer(), nullable=False, server_default="0"),
        _user_fk("created_by_user_id"),
        _created_at(),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint("namespace", name="uq_skill_publishers_namespace"),
        sa.UniqueConstraint("namespace_normalized", name="uq_skill_publishers_namespace_normalized"),
        sa.CheckConstraint(
            "owner_type IN ('org', 'user', 'system')", name="ck_skill_publisher_owner_type"
        ),
        sa.CheckConstraint(
            "(owner_type = 'system' AND org_id IS NULL AND user_id IS NULL) OR "
            "(owner_type = 'org' AND org_id IS NOT NULL AND user_id IS NULL) OR "
            "(owner_type = 'user' AND user_id IS NOT NULL AND org_id IS NULL)",
            name="ck_skill_publisher_owner_scope",
        ),
        sa.CheckConstraint(
            "status IN ('active', 'suspended', 'tombstoned')", name="ck_skill_publisher_status"
        ),
    )
    op.create_index("idx_skill_publishers_org", "skill_publishers", ["org_id"])
    op.create_index("idx_skill_publishers_user", "skill_publishers", ["user_id"])

    op.create_table(
        "skill_publisher_verifications",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column(
            "publisher_id",
            sa.String(length=64),
            sa.ForeignKey("skill_publishers.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("method", sa.String(length=20), nullable=False),
        sa.Column("subject", sa.String(length=255), nullable=False),
        sa.Column("subject_display", sa.String(length=255), nullable=False),
        sa.Column("verification_token", sa.String(length=128), nullable=True),
        sa.Column(
            "org_domain_verification_id",
            sa.String(length=64),
            sa.ForeignKey("org_domain_verifications.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("github_installation_id", sa.BigInteger(), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("status_reason", sa.String(length=200), nullable=True),
        _ts("verified_at"),
        _ts("last_checked_at"),
        _ts("last_success_at"),
        _ts("first_failure_at"),
        sa.Column("consecutive_failures", sa.Integer(), nullable=False, server_default="0"),
        _user_fk("created_by_user_id"),
        _created_at(),
        sa.CheckConstraint(
            "method IN ('dns_txt', 'github_org', 'github_user')",
            name="ck_skill_publisher_verification_method",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'verified', 'lapsed', 'revoked')",
            name="ck_skill_publisher_verification_status",
        ),
    )
    op.create_index(
        "idx_skill_publisher_verifications_publisher",
        "skill_publisher_verifications",
        ["publisher_id"],
    )
    op.create_index(
        "idx_skill_publisher_verifications_installation",
        "skill_publisher_verifications",
        ["github_installation_id"],
    )
    op.create_index(
        "uq_skill_publisher_verification_subject",
        "skill_publisher_verifications",
        ["method", "subject"],
        unique=True,
        postgresql_where=sa.text("status = 'verified'"),
        sqlite_where=sa.text("status = 'verified'"),
    )

    op.create_table(
        "skill_reserved_names",
        sa.Column("normalized", sa.String(length=39), primary_key=True),
        sa.Column("name", sa.String(length=39), nullable=False),
        sa.Column("reason", sa.String(length=20), nullable=False),
        sa.Column("required_subject", sa.String(length=255), nullable=True),
        _user_fk("created_by_user_id"),
        _created_at(),
        sa.CheckConstraint(
            "reason IN ('brand', 'popular', 'tombstone')", name="ck_skill_reserved_name_reason"
        ),
    )

    op.create_table(
        "skill_trusted_publishers",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column(
            "publisher_id",
            sa.String(length=64),
            sa.ForeignKey("skill_publishers.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("package_name", sa.String(length=100), nullable=True),
        sa.Column("provider", sa.String(length=20), nullable=False, server_default="github"),
        sa.Column("repository_owner_id", sa.BigInteger(), nullable=False),
        sa.Column("repository_id", sa.BigInteger(), nullable=False),
        sa.Column("repository", sa.String(length=200), nullable=False),
        sa.Column("workflow_path", sa.String(length=255), nullable=False),
        sa.Column("reusable_workflow", sa.String(length=300), nullable=True),
        sa.Column(
            "allow_workflow_run", sa.Boolean(), nullable=False, server_default=sa.text("false")
        ),
        sa.Column("environment", sa.String(length=255), nullable=True),
        sa.Column("ref_pattern", sa.String(length=255), nullable=True),
        _user_fk("created_by_user_id"),
        _created_at(),
        _ts("revoked_at"),
        _user_fk("revoked_by_user_id"),
        sa.CheckConstraint("provider IN ('github')", name="ck_skill_trusted_publisher_provider"),
    )
    op.create_index(
        "idx_skill_trusted_publishers_repo",
        "skill_trusted_publishers",
        ["provider", "repository_id"],
    )
    op.create_index(
        "idx_skill_trusted_publishers_publisher", "skill_trusted_publishers", ["publisher_id"]
    )

    op.create_table(
        "skill_publish_tokens",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "trusted_publisher_id",
            sa.String(length=64),
            sa.ForeignKey("skill_trusted_publishers.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "publisher_id",
            sa.String(length=64),
            sa.ForeignKey("skill_publishers.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("package_name", sa.String(length=100), nullable=True),
        sa.Column("oidc_issuer", sa.String(length=255), nullable=False),
        sa.Column("oidc_jti", sa.String(length=255), nullable=False),
        sa.Column("claims", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("auth_generation", sa.Integer(), nullable=False, server_default="0"),
        _ts("expires_at", nullable=False),
        _ts("used_at"),
        sa.Column("used_for_version_id", sa.String(length=64), nullable=True),
        _created_at(),
        sa.UniqueConstraint("token_hash", name="uq_skill_publish_tokens_hash"),
        sa.UniqueConstraint("oidc_issuer", "oidc_jti", name="uq_skill_publish_tokens_jti"),
    )
    op.create_index("idx_skill_publish_tokens_expires", "skill_publish_tokens", ["expires_at"])

    op.create_table(
        "skill_github_installation_states",
        sa.Column("installation_id", sa.BigInteger(), primary_key=True, autoincrement=False),
        _ts("suspended_at"),
        _ts("deleted_at"),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )

    # Column additions. SQLite: plain columns with a column-level CHECK (so a
    # later DROP COLUMN still works) and no REFERENCES (SQLite cannot drop a
    # column that takes part in a foreign key). PostgreSQL: real constraints.
    if sqlite:
        op.execute(sa.text("ALTER TABLE skill_packages ADD COLUMN publisher_id VARCHAR(64)"))
        op.execute(
            sa.text(
                "ALTER TABLE skill_marketplace_listings ADD COLUMN moderation_state VARCHAR(20) "
                f"NOT NULL DEFAULT 'approved' CHECK (moderation_state IN {_MODERATION_STATES})"
            )
        )
        op.execute(
            sa.text("ALTER TABLE skill_marketplace_listings ADD COLUMN moderation_reason TEXT")
        )
        op.execute(
            sa.text(
                "ALTER TABLE skill_marketplace_listings ADD COLUMN reviewed_by_user_id VARCHAR(36)"
            )
        )
        op.execute(
            sa.text("ALTER TABLE skill_marketplace_listings ADD COLUMN reviewed_at DATETIME")
        )
        op.execute(
            sa.text("ALTER TABLE skill_marketplace_listings ADD COLUMN signature_metadata TEXT")
        )
    else:
        op.add_column(
            "skill_packages", sa.Column("publisher_id", sa.String(length=64), nullable=True)
        )
        op.create_foreign_key(
            "fk_skill_packages_publisher",
            "skill_packages",
            "skill_publishers",
            ["publisher_id"],
            ["id"],
            ondelete="SET NULL",
        )
        op.add_column(
            "skill_marketplace_listings",
            sa.Column(
                "moderation_state",
                sa.String(length=20),
                nullable=False,
                server_default="approved",
            ),
        )
        op.create_check_constraint(
            "ck_skill_listing_moderation_state",
            "skill_marketplace_listings",
            f"moderation_state IN {_MODERATION_STATES}",
        )
        op.add_column(
            "skill_marketplace_listings", sa.Column("moderation_reason", sa.Text(), nullable=True)
        )
        op.add_column(
            "skill_marketplace_listings",
            sa.Column("reviewed_by_user_id", sa.String(length=36), nullable=True),
        )
        op.create_foreign_key(
            "fk_skill_listings_reviewed_by",
            "skill_marketplace_listings",
            "users",
            ["reviewed_by_user_id"],
            ["id"],
            ondelete="SET NULL",
        )
        op.add_column(
            "skill_marketplace_listings",
            sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        )
        op.add_column(
            "skill_marketplace_listings",
            sa.Column("signature_metadata", sa.Text(), nullable=True),
        )
    op.create_index(
        "idx_skill_listing_moderation",
        "skill_marketplace_listings",
        ["moderation_state", "visibility"],
    )

    # No moderation_state backfill: existing platform blocks stay expressed by
    # verification_state, which is still authoritative for blocks (delivery
    # requires BOTH axes to allow a listing), so the existing restore path
    # (which resets verification_state) keeps working for legacy blocks.
    # moderation_state starts 'approved' everywhere and carries review only.
    # The platform system publisher. 'sessionfs' is its own confusable
    # skeleton (no folded characters).
    op.execute(
        sa.text(
            "INSERT INTO skill_publishers (id, owner_type, namespace, namespace_normalized, "
            "display_name, status, public_publishing) VALUES (:id, 'system', 'sessionfs', "
            "'sessionfs', 'SessionFS', 'active', :pp)"
        ).bindparams(id=SYSTEM_PUBLISHER_ID, pp=True)
    )
    # Only content SessionFS itself controls goes under the platform publisher:
    # packages in the `sessionfs` namespace with a public listing published by
    # a platform administrator (the launch catalog). Every other legacy
    # package keeps publisher_id NULL (no badge). Legacy namespaces are not
    # claimable by anyone but the owners already publishing in them: the claim
    # path refuses a namespace (or a lookalike) in use by another owner.
    op.execute(
        sa.text(
            "UPDATE skill_packages SET publisher_id = :id "
            "WHERE namespace = 'sessionfs' AND id IN ("
            "SELECT l.package_id FROM skill_marketplace_listings l "
            "JOIN users u ON u.id = l.published_by_user_id "
            "WHERE l.visibility = 'public' AND u.tier = 'admin')"
        ).bindparams(id=SYSTEM_PUBLISHER_ID)
    )


def downgrade() -> None:
    bind = op.get_bind()
    sqlite = bind.dialect.name == "sqlite"
    op.drop_index("idx_skill_listing_moderation", table_name="skill_marketplace_listings")
    if not sqlite:
        op.drop_constraint(
            "fk_skill_listings_reviewed_by", "skill_marketplace_listings", type_="foreignkey"
        )
        op.drop_constraint(
            "ck_skill_listing_moderation_state", "skill_marketplace_listings", type_="check"
        )
        op.drop_constraint("fk_skill_packages_publisher", "skill_packages", type_="foreignkey")
    # Listings a reviewer rejected or that still await review were never
    # deliverable; keep them undeliverable under the 069 model by unpublishing.
    op.execute(
        sa.text(
            "UPDATE skill_marketplace_listings SET is_published = false "
            "WHERE moderation_state IN ('pending_review', 'rejected')"
        )
    )
    for column in ("signature_metadata", "reviewed_at", "reviewed_by_user_id", "moderation_reason", "moderation_state"):
        op.drop_column("skill_marketplace_listings", column)
    op.drop_column("skill_packages", "publisher_id")
    op.drop_table("skill_github_installation_states")
    op.drop_index("idx_skill_publish_tokens_expires", table_name="skill_publish_tokens")
    op.drop_table("skill_publish_tokens")
    op.drop_index("idx_skill_trusted_publishers_publisher", table_name="skill_trusted_publishers")
    op.drop_index("idx_skill_trusted_publishers_repo", table_name="skill_trusted_publishers")
    op.drop_table("skill_trusted_publishers")
    op.drop_table("skill_reserved_names")
    op.drop_index(
        "uq_skill_publisher_verification_subject", table_name="skill_publisher_verifications"
    )
    op.drop_index(
        "idx_skill_publisher_verifications_installation",
        table_name="skill_publisher_verifications",
    )
    op.drop_index(
        "idx_skill_publisher_verifications_publisher", table_name="skill_publisher_verifications"
    )
    op.drop_table("skill_publisher_verifications")
    op.drop_index("idx_skill_publishers_user", table_name="skill_publishers")
    op.drop_index("idx_skill_publishers_org", table_name="skill_publishers")
    op.drop_table("skill_publishers")
