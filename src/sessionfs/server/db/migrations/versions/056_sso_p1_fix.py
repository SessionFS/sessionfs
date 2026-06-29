"""SSO-P1-fix: re-scope external_identities key + break-glass + membership unique + email normalize.

Revision ID: 056
Revises: 055

tk_cb774646864f414b — Sentinel HIGH corrective:
  1. DROP uq_external_identity_issuer_sub (global (provider_issuer, subject));
     CREATE uq_external_identity_idp_sub UNIQUE (org_idp_id, subject).
     org_idp_id already exists from 055 — this only swaps the unique key.
     Dialect-aware: 055 declared the old key as a UniqueConstraint, which is
     a CONSTRAINT on PostgreSQL (DROP CONSTRAINT) and an INDEX on SQLite.
  2. (no-op) uq_org_members_org_user already exists from migration 016 —
     056 must NOT recreate it (DuplicateTableError on PostgreSQL).
  3. CREATE TABLE sso_break_glass_grants — durable admin break-glass.
  4. One-time users.email → lower(email) normalization.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "056"
down_revision = "055"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── 1. Re-scope external_identities unique key ──────────────────
    # The old key (provider_issuer, subject) is wrong for shared-issuer
    # IdPs (Google Workspace). Swap it to (org_idp_id, subject).
    # org_idp_id column + FK already exist from 055; this is index-only.
    #
    # 055 declared uq_external_identity_issuer_sub as an inline
    # UniqueConstraint inside create_table. On PostgreSQL that is a real
    # CONSTRAINT (backed by an index) — `DROP INDEX` fails with
    # DependentObjectsStillExistError; the constraint must be dropped.
    # On SQLite Alembic renders it as a plain UNIQUE INDEX, so DROP INDEX
    # is correct. Branch on the dialect.
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.drop_constraint(
            "uq_external_identity_issuer_sub",
            "external_identities",
            type_="unique",
        )
    else:
        op.drop_index(
            "uq_external_identity_issuer_sub",
            table_name="external_identities",
        )
    # Recreate as a plain UNIQUE INDEX (not a constraint) so a future
    # migration can drop it uniformly across dialects.
    op.create_index(
        "uq_external_identity_idp_sub",
        "external_identities",
        ["org_idp_id", "subject"],
        unique=True,
    )

    # ── 2. OrgMember uniqueness — ALREADY EXISTS (migration 016) ────
    # uq_org_members_org_user is created by migration 016
    # (016_tier_gating_orgs_billing.py) as an inline UniqueConstraint on
    # org_members, so it has been present + enforced since long before SSO.
    # 056 must NOT recreate it — doing so raised DuplicateTableError on
    # PostgreSQL (prod already had 001–054). Because that constraint has
    # always enforced one-membership-per-(org,user), no duplicate rows can
    # exist, so the dedupe this step previously performed is also
    # unnecessary. The JIT `ON CONFLICT (org_id, user_id) DO NOTHING` in
    # routes/auth_sso.py targets the 016 constraint. (Sentinel's §2.6
    # premise that the constraint was missing was incorrect.)

    # ── 3. sso_break_glass_grants — durable admin break-glass ───────
    op.create_table(
        "sso_break_glass_grants",
        sa.Column("id", sa.String(length=64), primary_key=True),  # sbg_<hex>
        sa.Column(
            "org_id",
            sa.String(length=64),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "admin_user_id",
            sa.String(length=64),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "issued_by_user_id",
            sa.String(length=64),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "expires_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.Column(
            "revoked_at",
            sa.DateTime(timezone=True),
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
        "idx_sbg_org", "sso_break_glass_grants", ["org_id"]
    )
    # Partial-unique: at most one active (non-revoked) grant per admin.
    op.create_index(
        "uq_sbg_one_active_per_admin",
        "sso_break_glass_grants",
        ["org_id", "admin_user_id"],
        unique=True,
        postgresql_where=sa.text("revoked_at IS NULL"),
        sqlite_where=sa.text("revoked_at IS NULL"),
    )

    # ── 4. Normalize legacy users.email to lowercase ────────────────
    # Signup already lowercases; this catches any pre-normalization rows.
    op.execute(
        "UPDATE users SET email = lower(email) WHERE email <> lower(email)"
    )


def downgrade() -> None:
    # ── 4 reverse: email normalization is not reversed (harmless) ───

    # ── 3 reverse: drop sso_break_glass_grants ──────────────────────
    op.drop_index(
        "uq_sbg_one_active_per_admin", table_name="sso_break_glass_grants"
    )
    op.drop_index("idx_sbg_org", table_name="sso_break_glass_grants")
    op.drop_table("sso_break_glass_grants")

    # ── 2 reverse: nothing — uq_org_members_org_user is owned by
    #    migration 016, not 056, so 056's downgrade must not drop it.

    # ── 1 reverse: swap back to (provider_issuer, subject) key ──────
    op.drop_index(
        "uq_external_identity_idp_sub",
        table_name="external_identities",
    )
    # Recreate the original key as a CONSTRAINT on PostgreSQL (matching how
    # 055 declared it) / a UNIQUE INDEX on SQLite, so a re-upgrade's
    # dialect-aware drop in upgrade() finds the right object type.
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.create_unique_constraint(
            "uq_external_identity_issuer_sub",
            "external_identities",
            ["provider_issuer", "subject"],
        )
    else:
        op.create_index(
            "uq_external_identity_issuer_sub",
            "external_identities",
            ["provider_issuer", "subject"],
            unique=True,
        )
