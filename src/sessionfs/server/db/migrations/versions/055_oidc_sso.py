"""OIDC SSO foundation: 4 additive tables + api_keys.sso_minted.

Revision ID: 055
Revises: 054

tk_c2cdbe7114804403 (SSO-P1) — data model + migration only. NO routes, NO
login flow, NO enforcement (those are P2/P3/P4). This phase is the FOUNDATION:
the tables + the single atomic consume_login_attempt helper.

Design: docs/design/sso-oidc.md §2. Four tables exist so that the v2
SAML/SCIM/groups surface is purely additive:

  1. org_identity_providers    — the IdP config (protocol-tagged; one enabled
                                  per org via partial-unique index)
  2. org_domain_verifications  — DNS-TXT domain ownership proof (globally
                                  unique per verified domain via partial-unique)
  3. external_identities       — links an IdP (provider_issuer, subject) to a
                                  SessionFS User; key is on the IDP-SIDE pair
  4. oidc_login_attempts       — durable single-use state token for the
                                  authorization-code + PKCE CSRF/replay defense;
                                  mirrors the ActivationAttempt proven shape
  5. api_keys.sso_minted        — marks keys minted via the SSO callback so
                                  enforcement can gate legacy (non-SSO) human keys

Strictly additive (down_revision='054'):
  - No edits to migrations 001–054; single head.
  - Inline CheckConstraint declared INSIDE op.create_table (SQLite-safe — same
    pattern as 050–054).
  - App-assigned String(64) PKs (oidp_/odv_/eid_/ola_ + token_hex), no
    lastrowid.
  - Partial-unique indexes for uq_org_idp_one_enabled_per_org and
    uq_org_domain_global_verified use sqlite_where/postgresql_where.

Downgrade: drop the four tables (reverse dep: login_attempts → identities →
domain_verifications → identity_providers) + drop the sso_minted column.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "055"
down_revision = "054"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── 1. org_identity_providers — the IdP config ───────────────
    op.create_table(
        "org_identity_providers",
        sa.Column("id", sa.String(length=64), primary_key=True),  # oidp_<hex>
        sa.Column(
            "org_id",
            sa.String(length=64),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "protocol",
            sa.String(length=20),
            nullable=False,
            server_default="oidc",
        ),
        sa.Column(
            "display_name", sa.String(length=100), nullable=False
        ),
        sa.Column("issuer", sa.String(length=500), nullable=False),
        sa.Column("client_id", sa.String(length=255), nullable=False),
        sa.Column(
            "client_secret_ref",
            sa.String(length=255),
            nullable=False,
            comment="GCP Secret Manager / K8s secret ref — NEVER the plaintext secret",
        ),
        sa.Column(
            "allowed_scopes",
            sa.Text(),
            nullable=False,
            server_default='["openid","email","profile"]',
        ),
        sa.Column("discovery_cache", sa.Text(), nullable=True),
        sa.Column(
            "discovery_fetched_at", sa.DateTime(timezone=True), nullable=True
        ),
        sa.Column("jwks_cache", sa.Text(), nullable=True),
        sa.Column(
            "jwks_fetched_at", sa.DateTime(timezone=True), nullable=True
        ),
        sa.Column(
            "enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column(
            "enforced",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column(
            "created_by_user_id",
            sa.String(length=64),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
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
        # Inline CHECK — protocol vocabulary guard.
        sa.CheckConstraint(
            "protocol IN ('oidc', 'saml')",
            name="ck_org_idp_protocol",
        ),
    )
    op.create_index(
        "idx_org_idp_org", "org_identity_providers", ["org_id"]
    )
    # Partial-unique: at most one enabled IdP per org.
    op.create_index(
        "uq_org_idp_one_enabled_per_org",
        "org_identity_providers",
        ["org_id"],
        unique=True,
        postgresql_where=sa.text("enabled = true"),
        sqlite_where=sa.text("enabled = true"),
    )

    # ── 2. org_domain_verifications — domain ownership proof ─────
    op.create_table(
        "org_domain_verifications",
        sa.Column("id", sa.String(length=64), primary_key=True),  # odv_<hex>
        sa.Column(
            "org_id",
            sa.String(length=64),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "domain",
            sa.String(length=255),
            nullable=False,
            comment="Normalized lowercase, e.g. acme.com",
        ),
        sa.Column(
            "method",
            sa.String(length=20),
            nullable=False,
            server_default="dns_txt",
        ),
        sa.Column(
            "verification_token",
            sa.String(length=128),
            nullable=False,
        ),
        sa.Column(
            "status",
            sa.String(length=20),
            nullable=False,
            server_default="pending",
        ),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "verified_by_user_id",
            sa.String(length=64),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "last_checked_at", sa.DateTime(timezone=True), nullable=True
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        # Inline CHECKs.
        sa.CheckConstraint(
            "status IN ('pending', 'verified', 'failed')",
            name="ck_org_domain_verification_status",
        ),
        sa.CheckConstraint(
            "method IN ('dns_txt', 'meta_tag')",
            name="ck_org_domain_verification_method",
        ),
    )
    op.create_index(
        "idx_org_domain_verification_org",
        "org_domain_verifications",
        ["org_id"],
    )
    # Partial-unique: a verified domain is claimed by AT MOST ONE org.
    op.create_index(
        "uq_org_domain_global_verified",
        "org_domain_verifications",
        ["domain"],
        unique=True,
        postgresql_where=sa.text("status = 'verified'"),
        sqlite_where=sa.text("status = 'verified'"),
    )

    # ── 3. external_identities — IdP subject → SessionFS User link ─
    op.create_table(
        "external_identities",
        sa.Column("id", sa.String(length=64), primary_key=True),  # eid_<hex>
        sa.Column(
            "user_id",
            sa.String(length=64),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "org_idp_id",
            sa.String(length=64),
            sa.ForeignKey("org_identity_providers.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "provider_issuer",
            sa.String(length=500),
            nullable=False,
            comment="Snapshotted issuer — survives IdP config edits",
        ),
        sa.Column(
            "subject",
            sa.String(length=255),
            nullable=False,
            comment="OIDC sub claim — stable, opaque, IdP-assigned",
        ),
        sa.Column(
            "email_at_link",
            sa.String(length=255),
            nullable=False,
            comment="Email asserted by IdP at link time (audit)",
        ),
        sa.Column(
            "linked_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "link_method",
            sa.String(length=30),
            nullable=False,
        ),
        sa.Column(
            "last_login_at", sa.DateTime(timezone=True), nullable=True
        ),
        sa.Column(
            "deactivated_at", sa.DateTime(timezone=True), nullable=True
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        # Inline CHECKs.
        sa.CheckConstraint(
            "link_method IN "
            "('verified_email_match', 'jit_provision', 'explicit_confirm')",
            name="ck_external_identity_link_method",
        ),
        # Identity key is (provider_issuer, subject) — NOT email.
        # Inline UniqueConstraint (SQLite-safe — same pattern as 054's
        # uq_work_queue_item inside create_table).
        sa.UniqueConstraint(
            "provider_issuer", "subject",
            name="uq_external_identity_issuer_sub",
        ),
    )
    op.create_index(
        "idx_external_identity_user", "external_identities", ["user_id"]
    )
    op.create_index(
        "idx_external_identity_org_idp", "external_identities", ["org_idp_id"]
    )

    # ── 4. oidc_login_attempts — CSRF/replay defense (auth code + PKCE)
    op.create_table(
        "oidc_login_attempts",
        sa.Column("id", sa.String(length=64), primary_key=True),  # ola_<hex>
        sa.Column(
            "org_idp_id",
            sa.String(length=64),
            sa.ForeignKey("org_identity_providers.id", ondelete="CASCADE"),
            nullable=True,
            comment="Nullable — resolved at start time; ON DELETE CASCADE so "
                    "deleting an IdP invalidates in-flight attempts",
        ),
        sa.Column(
            "state",
            sa.String(length=128),
            nullable=False,
            unique=True,
            comment="Random; returned in callback, matched exactly",
        ),
        sa.Column(
            "nonce",
            sa.String(length=128),
            nullable=False,
            comment="Echoed in id_token nonce claim, matched (replay defense)",
        ),
        sa.Column(
            "pkce_verifier_hash",
            sa.String(length=128),
            nullable=False,
            comment="Hash of the PKCE code verifier; raw verifier NEVER stored",
        ),
        sa.Column(
            "org_id",
            sa.String(length=64),
            sa.ForeignKey("organizations.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "provider_id",
            sa.String(length=64),
            nullable=True,
        ),
        sa.Column(
            "redirect_after",
            sa.String(length=500),
            nullable=True,
        ),
        sa.Column(
            "status",
            sa.String(length=20),
            nullable=False,
            server_default="pending",
        ),
        sa.Column(
            "expires_at",
            sa.DateTime(timezone=True),
            nullable=False,
            comment="Short TTL (10 min) — mirror of ActivationAttempt.expires_at",
        ),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        # Inline CHECK.
        sa.CheckConstraint(
            "status IN ('pending', 'consumed', 'expired')",
            name="ck_oidc_login_attempt_status",
        ),
    )
    op.create_index(
        "idx_oidc_login_attempt_state",
        "oidc_login_attempts",
        ["state", "status", "expires_at"],
    )

    # ── 5. api_keys.sso_minted — marks keys from SSO callback ─────
    with op.batch_alter_table("api_keys") as batch:
        batch.add_column(
            sa.Column(
                "sso_minted",
                sa.Boolean(),
                nullable=False,
                server_default=sa.text("false"),
            )
        )


def downgrade() -> None:
    # Reverse dependency order: login_attempts → identities →
    # domain_verifications → identity_providers.
    op.drop_index(
        "idx_oidc_login_attempt_state", table_name="oidc_login_attempts"
    )
    op.drop_table("oidc_login_attempts")

    op.drop_index(
        "idx_external_identity_org_idp", table_name="external_identities"
    )
    op.drop_index(
        "idx_external_identity_user", table_name="external_identities"
    )
    op.drop_table("external_identities")

    op.drop_index(
        "uq_org_domain_global_verified", table_name="org_domain_verifications"
    )
    op.drop_index(
        "idx_org_domain_verification_org", table_name="org_domain_verifications"
    )
    op.drop_table("org_domain_verifications")

    op.drop_index(
        "uq_org_idp_one_enabled_per_org", table_name="org_identity_providers"
    )
    op.drop_index("idx_org_idp_org", table_name="org_identity_providers")
    op.drop_table("org_identity_providers")

    with op.batch_alter_table("api_keys") as batch:
        batch.drop_column("sso_minted")
