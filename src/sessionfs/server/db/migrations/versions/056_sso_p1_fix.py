"""SSO-P1-fix: re-scope external_identities key + break-glass + membership unique + email normalize.

Revision ID: 056
Revises: 055

tk_cb774646864f414b — Sentinel HIGH corrective:
  1. DROP uq_external_identity_issuer_sub (global (provider_issuer, subject));
     CREATE uq_external_identity_idp_sub UNIQUE (org_idp_id, subject).
     org_idp_id already exists from 055 — this only swaps the unique key.
  2. ADD UNIQUE uq_org_members_org_user (org_id, user_id) on org_members.
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

    # ── 2. OrgMember uniqueness — one membership per (org, user) ────
    # Duplicate (org_id, user_id) rows are a KNOWN real production state
    # (v0.11.2 added the /me `.first()`-over-`.one_or_none()` hardening
    # precisely because transient duplicate memberships occur). Creating
    # the unique index against dirty data would abort the migrate-job.
    # Dedupe FIRST, keeping exactly one survivor per group: highest role
    # privilege (owner > admin > member), then earliest joined_at, then
    # lowest id. A row is deleted iff some sibling in its group ranks
    # strictly better — portable across PG + SQLite (no row-value tuples,
    # no LIMIT-in-subquery).
    # SQLite forbids aliasing the DELETE target table, so the outer
    # (target) rows are referenced by the bare name `org_members` in the
    # correlated subquery; only the sibling scan is aliased (`o`).
    _role_priority = (
        "CASE {alias}.role WHEN 'owner' THEN 0 "
        "WHEN 'admin' THEN 1 ELSE 2 END"
    )
    _rp_other = _role_priority.format(alias="o")
    _rp_self = _role_priority.format(alias="org_members")
    op.execute(
        "DELETE FROM org_members "  # noqa: S608 — no user input; static SQL
        "WHERE EXISTS ("
        "  SELECT 1 FROM org_members AS o "
        "  WHERE o.org_id = org_members.org_id "
        "    AND o.user_id = org_members.user_id "
        "    AND o.id <> org_members.id "
        f"    AND ( {_rp_other} < {_rp_self} "
        f"      OR ({_rp_other} = {_rp_self} "
        "          AND o.joined_at < org_members.joined_at) "
        f"      OR ({_rp_other} = {_rp_self} "
        "          AND o.joined_at = org_members.joined_at "
        "          AND o.id < org_members.id) "
        "    )"
        ")"
    )
    op.create_index(
        "uq_org_members_org_user",
        "org_members",
        ["org_id", "user_id"],
        unique=True,
    )

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

    # ── 2 reverse: drop org_members unique constraint ───────────────
    op.drop_index(
        "uq_org_members_org_user", table_name="org_members"
    )

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
