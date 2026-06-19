"""Licensing entitlements: entitlements, org_audit_events, activation_attempt,
pending_license_claim tables; HelmLicense.org_id; entitlement FKs; single-owner
backfill; entitlements backfill.

Revision ID: 050
Revises: 049

P1 of the licensing + org-management redesign
(docs/design/licensing-org-redesign.md).

Additive-only migration:
- entitlements: single source of truth for tier/seats/storage/expiry.
  Two partial unique indexes (one-active-per-owner, one-source-ref-per-source).
  CHECK constraint blocks 'admin' as a tier (Sentinel MEDIUM-3).
- org_audit_events: append-only audit trail modeled on ProjectMergeAudit.
  org_id ON DELETE SET NULL so audit rows survive org deletion.
- activation_attempt: durable single-use token store for license activation.
  token_hash only — raw token NEVER stored.
- pending_license_claim: lightweight migration-era table for unmatched
  HelmLicenses.
- HelmLicense.org_id: nullable FK, UNIQUE (one org per license).
- User.entitlement_id + Organization.entitlement_id: denormalized pointers
  populated by backfill.
- Deterministic single-owner backfill: (1) creator from AdminAction, else
  (2) earliest admin by join date, else (3) lowest-id admin.
- Entitlements backfill: one per Organization, one per paid User without
  org, one per matched HelmLicense. NULL/invalid tier coerced to 'free'
  with diagnostic log entries.
- Partial unique index uq_org_members_one_owner_per_org created AFTER the
  single-owner backfill.
- Old tier columns are NOT dropped — they remain as denormalized caches.

Downgrade: reverse order — drop the org_members partial unique index first
(after UPDATE owner→admin), then drop FKs/tables/columns.
"""

from datetime import datetime, timezone

from alembic import op
import sqlalchemy as sa


revision = "050"
down_revision = "049"
branch_labels = None
depends_on = None


# ────────────────────────────────────────────────────────────────
# Helpers
# ────────────────────────────────────────────────────────────────

VALID_TIERS = {"free", "starter", "pro", "team", "enterprise"}
DIAGNOSTIC_ENTRIES: list[str] = []


def _coerce_tier(raw: str | None, context: str) -> str:
    """Coerce NULL or invalid tier to 'free' with a diagnostic entry."""
    if raw is None:
        DIAGNOSTIC_ENTRIES.append(
            f"NULL tier coerced to 'free': {context}"
        )
        return "free"
    normalized = raw.strip().lower()
    if normalized in VALID_TIERS:
        return normalized
    DIAGNOSTIC_ENTRIES.append(
        f"Invalid tier '{raw}' coerced to 'free': {context}"
    )
    return "free"


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ────────────────────────────────────────────────────────────────
# Upgrade
# ────────────────────────────────────────────────────────────────


def upgrade() -> None:
    conn = op.get_bind()
    now = _now()

    # ── 1. Create entitlements table ──────────────────────────
    op.create_table(
        "entitlements",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "owner_type",
            sa.String(length=10),
            nullable=False,
            comment="'user' | 'org'",
        ),
        sa.Column(
            "owner_id",
            sa.String(length=64),
            nullable=False,
            comment="users.id | organizations.id",
        ),
        sa.Column(
            "source",
            sa.String(length=20),
            nullable=False,
            comment="'stripe' | 'helm_license' | 'manual' | 'admin_provisioned'",
        ),
        sa.Column(
            "source_ref",
            sa.String(length=64),
            nullable=True,
            comment="stripe_subscription_id | helm_licenses.id | NULL (manual)",
        ),
        sa.Column(
            "tier",
            sa.String(length=20),
            nullable=False,
            server_default="free",
        ),
        sa.Column(
            "seats_limit",
            sa.Integer(),
            nullable=True,
            comment="NULL = unlimited/default",
        ),
        sa.Column(
            "storage_limit_bytes",
            sa.BigInteger(),
            nullable=True,
            comment="NULL = tier default",
        ),
        sa.Column(
            "status",
            sa.String(length=20),
            nullable=False,
            server_default="active",
            comment="active | canceled | expired | revoked",
        ),
        sa.Column(
            "billing_status",
            sa.String(length=20),
            nullable=False,
            server_default="current",
            comment="current | past_due",
        ),
        sa.Column(
            "current_period_start",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column(
            "current_period_end",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="Stripe=renewal date, HelmLicense=expiry date, NULL=perpetual",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )

    # Partial unique index: at most one active entitlement per owner.
    op.create_index(
        "uq_entitlements_one_active_per_owner",
        "entitlements",
        ["owner_type", "owner_id"],
        unique=True,
        postgresql_where=sa.text("status = 'active'"),
        sqlite_where=sa.text("status = 'active'"),
    )

    # Partial unique index: each external source_ref maps to one entitlement.
    op.create_index(
        "uq_entitlements_source_ref",
        "entitlements",
        ["source", "source_ref"],
        unique=True,
        postgresql_where=sa.text("source_ref IS NOT NULL"),
        sqlite_where=sa.text("source_ref IS NOT NULL"),
    )

    # CHECK constraint: 'admin' is NOT a valid entitlement tier.
    op.create_check_constraint(
        "ck_entitlements_tier",
        "entitlements",
        "tier IN ('free', 'starter', 'pro', 'team', 'enterprise')",
    )

    # ── 2. Create org_audit_events table ──────────────────────
    op.create_table(
        "org_audit_events",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column(
            "org_id",
            sa.String(length=64),
            sa.ForeignKey("organizations.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("org_name_snapshot", sa.String(length=255), nullable=False),
        sa.Column("event_type", sa.String(length=50), nullable=False),
        sa.Column(
            "actor_user_id",
            sa.String(length=36),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "actor_email_snapshot", sa.String(length=255), nullable=True
        ),
        sa.Column(
            "actor_role_at_time",
            sa.String(length=20),
            nullable=True,
            comment="OrgMember.role at event time, or 'platform_admin'",
        ),
        sa.Column(
            "target_type",
            sa.String(length=50),
            nullable=True,
            comment="'user' | 'license' | 'entitlement' | 'invite' | 'settings' | 'organization'",
        ),
        sa.Column("target_id", sa.String(length=255), nullable=True),
        sa.Column(
            "target_email_snapshot", sa.String(length=255), nullable=True
        ),
        sa.Column("before", sa.Text(), nullable=True),
        sa.Column("after", sa.Text(), nullable=True),
        sa.Column(
            "entitlement_id",
            sa.Integer(),
            sa.ForeignKey("entitlements.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )

    op.create_index(
        "idx_org_audit_events_org", "org_audit_events", ["org_id"]
    )

    # ── 3. Create activation_attempt table ────────────────────
    op.create_table(
        "activation_attempt",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "helm_license_id",
            sa.String(length=64),
            sa.ForeignKey("helm_licenses.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "token_hash",
            sa.String(length=128),
            nullable=False,
            comment="Hash of single-use token — raw token NEVER stored",
        ),
        sa.Column(
            "contact_email_snapshot", sa.String(length=255), nullable=False
        ),
        sa.Column(
            "requested_by_user_id",
            sa.String(length=36),
            sa.ForeignKey("users.id"),
            nullable=False,
        ),
        sa.Column(
            "status",
            sa.String(length=20),
            nullable=False,
            server_default="pending",
            comment="pending | verified | consumed | expired",
        ),
        sa.Column(
            "expires_at", sa.DateTime(timezone=True), nullable=False
        ),
        sa.Column(
            "consumed_at", sa.DateTime(timezone=True), nullable=True
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )

    op.create_index(
        "idx_activation_attempt_lookup",
        "activation_attempt",
        ["token_hash", "status", "expires_at"],
    )

    # ── 4. Create pending_license_claim table ─────────────────
    op.create_table(
        "pending_license_claim",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "helm_license_id",
            sa.String(length=64),
            sa.ForeignKey("helm_licenses.id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
        ),
        sa.Column("org_name", sa.String(length=255), nullable=False),
        sa.Column("contact_email", sa.String(length=255), nullable=True),
        sa.Column("tier", sa.String(length=20), nullable=False),
        sa.Column("seats_limit", sa.Integer(), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )

    # ── 5. Add HelmLicense.org_id column + FK + UNIQUE ────────
    with op.batch_alter_table("helm_licenses") as batch_op:
        batch_op.add_column(
            sa.Column("org_id", sa.String(length=64), nullable=True)
        )
        batch_op.create_foreign_key(
            "fk_helm_licenses_org_id",
            "organizations",
            ["org_id"],
            ["id"],
            ondelete="SET NULL",
        )
        batch_op.create_unique_constraint(
            "uq_helm_licenses_org_id", ["org_id"]
        )

    # ── 6. Add entitlement_id FKs to users + organizations ────
    with op.batch_alter_table("users") as batch_op:
        batch_op.add_column(
            sa.Column("entitlement_id", sa.Integer(), nullable=True)
        )
        batch_op.create_foreign_key(
            "fk_users_entitlement_id",
            "entitlements",
            ["entitlement_id"],
            ["id"],
            ondelete="SET NULL",
        )

    with op.batch_alter_table("organizations") as batch_op:
        batch_op.add_column(
            sa.Column("entitlement_id", sa.Integer(), nullable=True)
        )
        batch_op.create_foreign_key(
            "fk_organizations_entitlement_id",
            "entitlements",
            ["entitlement_id"],
            ["id"],
            ondelete="SET NULL",
        )

    # ── 7. Entitlements backfill ──────────────────────────────

    # 7a. One entitlement per Organization.
    org_rows = conn.execute(
        sa.text(
            "SELECT id, tier, stripe_subscription_id, seats_limit, "
            "storage_limit_bytes FROM organizations"
        )
    ).fetchall()

    org_entitlement_map: dict[str, int] = {}  # org_id -> entitlement.id

    for row in org_rows:
        org_id = row[0]
        raw_tier = row[1]
        stripe_sub = row[2]
        seats = row[3]
        storage = row[4]

        tier = _coerce_tier(raw_tier, f"Organization id={org_id}")
        source = "stripe" if stripe_sub else "manual"
        source_ref = stripe_sub if stripe_sub else None

        result = conn.execute(
            sa.text(
                "INSERT INTO entitlements "
                "(owner_type, owner_id, source, source_ref, tier, "
                "seats_limit, storage_limit_bytes, status, billing_status, "
                "current_period_start, current_period_end, created_at, updated_at) "
                "VALUES "
                "(:owner_type, :owner_id, :source, :source_ref, :tier, "
                ":seats_limit, :storage_limit_bytes, 'active', 'current', "
                ":now, NULL, :now, :now)"
            ),
            {
                "owner_type": "org",
                "owner_id": org_id,
                "source": source,
                "source_ref": source_ref,
                "tier": tier,
                "seats_limit": seats,
                "storage_limit_bytes": storage,
                "now": now,
            },
        )
        ent_id = result.lastrowid
        org_entitlement_map[org_id] = ent_id

    # 7b. One entitlement per User with tier!='free' and NO OrgMember row.
    paid_user_rows = conn.execute(
        sa.text(
            "SELECT u.id, u.tier FROM users u "
            "WHERE u.tier != 'free' "
            "AND u.tier IS NOT NULL "
            "AND NOT EXISTS (SELECT 1 FROM org_members om WHERE om.user_id = u.id)"
        )
    ).fetchall()

    user_entitlement_map: dict[str, int] = {}  # user_id -> entitlement.id

    for row in paid_user_rows:
        user_id = row[0]
        raw_tier = row[1]
        tier = _coerce_tier(raw_tier, f"User id={user_id} (paid, no org)")

        result = conn.execute(
            sa.text(
                "INSERT INTO entitlements "
                "(owner_type, owner_id, source, source_ref, tier, "
                "seats_limit, storage_limit_bytes, status, billing_status, "
                "current_period_start, current_period_end, created_at, updated_at) "
                "VALUES "
                "('user', :owner_id, 'manual', NULL, :tier, "
                "NULL, NULL, 'active', 'current', "
                ":now, NULL, :now, :now)"
            ),
            {"owner_id": user_id, "tier": tier, "now": now},
        )
        user_entitlement_map[user_id] = result.lastrowid

    # 7c. HelmLicense backfill: auto-link high-confidence matches;
    # unmatched → pending_license_claim.
    license_rows = conn.execute(
        sa.text(
            "SELECT hl.id, hl.org_name, hl.contact_email, hl.tier, "
            "hl.seats_limit, hl.expires_at "
            "FROM helm_licenses hl "
            "WHERE hl.status = 'active' "
            "AND (hl.expires_at IS NULL OR hl.expires_at > :now) "
            "AND hl.org_id IS NULL"
        ),
        {"now": now},
    ).fetchall()

    for lr in license_rows:
        license_id = lr[0]
        license_org_name = lr[1]
        license_contact_email = lr[2]
        raw_tier = lr[3]
        seats = lr[4]
        expires = lr[5]
        tier = _coerce_tier(raw_tier, f"HelmLicense id={license_id}")

        # Try high-confidence match: same org_name AND contact_email
        # matches an org member.
        matched_org = conn.execute(
            sa.text(
                "SELECT o.id FROM organizations o "
                "JOIN org_members om ON om.org_id = o.id "
                "JOIN users u ON u.id = om.user_id "
                "WHERE o.name = :org_name "
                "AND LOWER(u.email) = LOWER(:contact_email) "
                "LIMIT 1"
            ),
            {
                "org_name": license_org_name,
                "contact_email": license_contact_email,
            },
        ).fetchone()

        if matched_org:
            matched_org_id = matched_org[0]
            # Create entitlement for the matched org (if not already).
            # The org may already have a 'manual' entitlement from 7a;
            # we create a helm_license-sourced one. The org's old manual
            # entitlement stays as a historical row.
            result = conn.execute(
                sa.text(
                    "INSERT INTO entitlements "
                    "(owner_type, owner_id, source, source_ref, tier, "
                    "seats_limit, storage_limit_bytes, status, billing_status, "
                    "current_period_start, current_period_end, created_at, updated_at) "
                    "VALUES "
                    "('org', :owner_id, 'helm_license', :source_ref, :tier, "
                    ":seats_limit, NULL, 'active', 'current', "
                    ":now, :current_period_end, :now, :now)"
                ),
                {
                    "owner_id": matched_org_id,
                    "source_ref": license_id,
                    "tier": tier,
                    "seats_limit": seats,
                    "current_period_end": expires,
                    "now": now,
                },
            )
            ent_id = result.lastrowid

            # Update HelmLicense.org_id to bind.
            conn.execute(
                sa.text(
                    "UPDATE helm_licenses SET org_id = :org_id "
                    "WHERE id = :license_id"
                ),
                {"org_id": matched_org_id, "license_id": license_id},
            )

            # Update org entitlement pointer to helm_license-sourced row.
            conn.execute(
                sa.text(
                    "UPDATE organizations SET entitlement_id = :ent_id "
                    "WHERE id = :org_id"
                ),
                {"ent_id": ent_id, "org_id": matched_org_id},
            )
            org_entitlement_map[matched_org_id] = ent_id
        else:
            # No match — create pending_license_claim.
            conn.execute(
                sa.text(
                    "INSERT INTO pending_license_claim "
                    "(helm_license_id, org_name, contact_email, tier, "
                    "seats_limit, expires_at, created_at) "
                    "VALUES "
                    "(:license_id, :org_name, :contact_email, :tier, "
                    ":seats_limit, :expires_at, :now)"
                ),
                {
                    "license_id": license_id,
                    "org_name": license_org_name,
                    "contact_email": license_contact_email,
                    "tier": tier,
                    "seats_limit": seats,
                    "expires_at": expires,
                    "now": now,
                },
            )

    # ── 8. Set entitlement_id on Users + Organizations ────────

    for org_id, ent_id in org_entitlement_map.items():
        conn.execute(
            sa.text(
                "UPDATE organizations SET entitlement_id = :ent_id WHERE id = :org_id"
            ),
            {"ent_id": ent_id, "org_id": org_id},
        )

    for user_id, ent_id in user_entitlement_map.items():
        conn.execute(
            sa.text(
                "UPDATE users SET entitlement_id = :ent_id WHERE id = :user_id"
            ),
            {"ent_id": ent_id, "user_id": user_id},
        )

    # ── 9. Deterministic single-owner backfill ────────────────

    all_orgs = conn.execute(
        sa.text("SELECT id FROM organizations")
    ).fetchall()

    owner_promoted = 0
    for (org_id,) in all_orgs:
        # 9a. Creator from AdminAction (admin_create_org).
        creator = conn.execute(
            sa.text(
                "SELECT admin_id FROM admin_actions "
                "WHERE action = 'admin_create_org' "
                "AND target_type = 'organization' "
                "AND target_id = :org_id "
                "ORDER BY created_at LIMIT 1"
            ),
            {"org_id": org_id},
        ).fetchone()

        owner_user_id = None

        if creator:
            creator_id = creator[0]
            is_member = conn.execute(
                sa.text(
                    "SELECT 1 FROM org_members "
                    "WHERE org_id = :org_id AND user_id = :user_id "
                    "AND role = 'admin'"
                ),
                {"org_id": org_id, "user_id": creator_id},
            ).fetchone()
            if is_member:
                owner_user_id = creator_id

        # 9b. Earliest admin by join date.
        if owner_user_id is None:
            earliest = conn.execute(
                sa.text(
                    "SELECT user_id FROM org_members "
                    "WHERE org_id = :org_id AND role = 'admin' "
                    "ORDER BY COALESCE(joined_at, invited_at) ASC, user_id ASC "
                    "LIMIT 1"
                ),
                {"org_id": org_id},
            ).fetchone()
            if earliest:
                owner_user_id = earliest[0]

        # 9c. Lowest-id admin (deterministic fallback).
        if owner_user_id is None:
            lowest = conn.execute(
                sa.text(
                    "SELECT user_id FROM org_members "
                    "WHERE org_id = :org_id AND role = 'admin' "
                    "ORDER BY user_id ASC LIMIT 1"
                ),
                {"org_id": org_id},
            ).fetchone()
            if lowest:
                owner_user_id = lowest[0]

        # Promote the selected admin to owner.
        if owner_user_id:
            conn.execute(
                sa.text(
                    "UPDATE org_members SET role = 'owner' "
                    "WHERE org_id = :org_id AND user_id = :user_id"
                ),
                {"org_id": org_id, "user_id": owner_user_id},
            )
            owner_promoted += 1

    # ── 10. Create partial unique index on org_members ────────
    # AFTER the single-owner backfill.

    op.create_index(
        "uq_org_members_one_owner_per_org",
        "org_members",
        ["org_id"],
        unique=True,
        postgresql_where=sa.text("role = 'owner'"),
        sqlite_where=sa.text("role = 'owner'"),
    )

    # ── Diagnostic summary ────────────────────────────────────
    if DIAGNOSTIC_ENTRIES:
        op.execute(
            sa.text(
                "SELECT 'migration_050_diagnostics: ' || :msg"
            ),
            {"msg": "; ".join(DIAGNOSTIC_ENTRIES[:20])},
        )
    print(
        f"\n[migration 050] entitlements backfilled: "
        f"{len(org_entitlement_map)} orgs, {len(user_entitlement_map)} users, "
        f"{len(license_rows)} licenses processed. "
        f"{owner_promoted} org owners backfilled. "
        f"{len(DIAGNOSTIC_ENTRIES)} tier coercion(s)."
    )


# ────────────────────────────────────────────────────────────────
# Downgrade
# ────────────────────────────────────────────────────────────────


def downgrade() -> None:
    conn = op.get_bind()

    # 1. Drop the org_members partial unique index FIRST,
    #    after reverting owner → admin.
    op.drop_index(
        "uq_org_members_one_owner_per_org",
        table_name="org_members",
        postgresql_where=sa.text("role = 'owner'"),
        sqlite_where=sa.text("role = 'owner'"),
    )

    # Revert all owners back to admin (Option A — see design §3.2).
    conn.execute(
        sa.text(
            "UPDATE org_members SET role = 'admin' WHERE role = 'owner'"
        )
    )

    # 2. Drop entitlement_id FKs from users + organizations.
    with op.batch_alter_table("users") as batch_op:
        batch_op.drop_constraint("fk_users_entitlement_id", type_="foreignkey")
        batch_op.drop_column("entitlement_id")

    with op.batch_alter_table("organizations") as batch_op:
        batch_op.drop_constraint(
            "fk_organizations_entitlement_id", type_="foreignkey"
        )
        batch_op.drop_column("entitlement_id")

    # 3. Drop HelmLicense.org_id.
    with op.batch_alter_table("helm_licenses") as batch_op:
        batch_op.drop_constraint(
            "uq_helm_licenses_org_id", type_="unique"
        )
        batch_op.drop_constraint(
            "fk_helm_licenses_org_id", type_="foreignkey"
        )
        batch_op.drop_column("org_id")

    # 4. Drop tables in reverse dependency order.
    op.drop_table("pending_license_claim")
    op.drop_index(
        "idx_activation_attempt_lookup", table_name="activation_attempt"
    )
    op.drop_table("activation_attempt")
    op.drop_index("idx_org_audit_events_org", table_name="org_audit_events")
    op.drop_table("org_audit_events")

    op.drop_constraint("ck_entitlements_tier", "entitlements")
    op.drop_index(
        "uq_entitlements_source_ref",
        table_name="entitlements",
        postgresql_where=sa.text("source_ref IS NOT NULL"),
        sqlite_where=sa.text("source_ref IS NOT NULL"),
    )
    op.drop_index(
        "uq_entitlements_one_active_per_owner",
        table_name="entitlements",
        postgresql_where=sa.text("status = 'active'"),
        sqlite_where=sa.text("status = 'active'"),
    )
    op.drop_table("entitlements")
