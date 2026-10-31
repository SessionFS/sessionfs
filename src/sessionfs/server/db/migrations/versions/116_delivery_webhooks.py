"""Per-org delivery webhooks and idempotency events (Review Loop R3, track F2).

Revision ID: 116
Revises: 115

Two tables for per-org, per-provider delivery webhooks (GitHub #119):

- ``org_delivery_webhooks`` — one row per (org, provider) holding the
  Fernet-encrypted inbound secret used to verify the provider's signature
  (``X-Hub-Signature-256`` for GitHub, ``X-Gitlab-Token`` for GitLab). The
  plaintext is returned exactly once at rotation time; only ``secret_last4``
  is persisted as a display hint.
- ``delivery_webhook_events`` — the idempotency ledger. The composite
  primary key ``(provider, org_id, idempotency_key)`` makes a re-delivered
  event a no-op under ``ON CONFLICT DO NOTHING``.

Both carry an inline CHECK constraint (the same SQLite-safe pattern as
migrations 050-054 and 114). Downgrade drops in reverse order.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "116"
down_revision = "115"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "org_delivery_webhooks",
        sa.Column(
            "org_id",
            sa.String(length=64),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("provider", sa.String(length=20), primary_key=True),
        sa.Column("encrypted_secret", sa.Text(), nullable=False),
        sa.Column("secret_last4", sa.String(length=4), nullable=False),
        sa.Column(
            "created_by_user_id",
            sa.String(length=64),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "rotated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "provider IN ('github', 'gitlab')",
            name="ck_org_delivery_webhooks_provider",
        ),
    )

    op.create_table(
        "delivery_webhook_events",
        sa.Column("provider", sa.String(length=20), primary_key=True),
        sa.Column(
            "org_id",
            sa.String(length=64),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("idempotency_key", sa.String(length=64), primary_key=True),
        sa.Column(
            "received_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "provider IN ('github', 'gitlab')",
            name="ck_delivery_webhook_events_provider",
        ),
    )


def downgrade() -> None:
    op.drop_table("delivery_webhook_events")
    op.drop_table("org_delivery_webhooks")
