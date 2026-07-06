"""SSO browser-login handoff — one-time-code exchange.

Revision ID: 058
Revises: 057

Adds the browser SSO login handoff (docs: the /callback JSON response is
CLI-only; a browser flow instead redirects with a one-time code traded at
POST /auth/sso/exchange for a freshly-minted key — no key ever in a URL/at rest):
  1. ALTER oidc_login_attempts ADD client_flow ('cli' default | 'browser').
     Server-set + app-validated (Literal at /start) — no DB CHECK, so this stays
     a plain ADD COLUMN with server_default and never needs a SQLite
     ALTER-ADD-CONSTRAINT (the v0.13.1/.2 dialect class).
  2. CREATE TABLE sso_exchange_codes — the single-use code rows. Inline status
     CHECK renders on both PG and SQLite via create_table.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "058"
down_revision = "057"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── 1. oidc_login_attempts.client_flow ───────────────────────────
    # server_default backfills existing rows to 'cli' on both dialects.
    op.add_column(
        "oidc_login_attempts",
        sa.Column(
            "client_flow",
            sa.String(length=20),
            nullable=False,
            server_default="cli",
        ),
    )

    # ── 2. sso_exchange_codes — one-time browser-login codes ─────────
    op.create_table(
        "sso_exchange_codes",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("code_hash", sa.String(length=128), nullable=False),
        sa.Column("binding_hash", sa.String(length=128), nullable=False),
        sa.Column(
            "user_id",
            sa.String(length=64),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("org_id", sa.String(length=64), nullable=True),
        sa.Column("link_method", sa.String(length=40), nullable=False),
        sa.Column(
            "status",
            sa.String(length=20),
            nullable=False,
            server_default="pending",
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'consumed')",
            name="ck_sso_exchange_code_status",
        ),
        sa.UniqueConstraint("code_hash", name="uq_sso_exchange_codes_code_hash"),
    )
    op.create_index(
        "ix_sso_exchange_codes_expires_at", "sso_exchange_codes", ["expires_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_sso_exchange_codes_expires_at", table_name="sso_exchange_codes")
    op.drop_table("sso_exchange_codes")
    op.drop_column("oidc_login_attempts", "client_flow")
