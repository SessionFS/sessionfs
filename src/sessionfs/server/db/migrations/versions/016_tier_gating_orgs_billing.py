"""Add tier gating, organizations, RBAC, Stripe billing, Helm licenses, and telemetry.

Revision ID: 016
Revises: 015
"""

from alembic import op
import sqlalchemy as sa

revision = "016"
down_revision = "015"


def upgrade() -> None:
    # --- Users: add billing columns ---
    op.add_column("users", sa.Column("stripe_customer_id", sa.String(64), nullable=True))
    op.add_column("users", sa.Column("stripe_subscription_id", sa.String(64), nullable=True))
    op.add_column("users", sa.Column("tier_updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()))
    op.add_column("users", sa.Column("storage_used_bytes", sa.BigInteger, nullable=False, server_default="0"))
    op.add_column("users", sa.Column("beta_pro_expires_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("users", sa.Column("last_client_version", sa.String(20), nullable=True))
    op.add_column("users", sa.Column("last_client_platform", sa.String(50), nullable=True))
    op.add_column("users", sa.Column("last_client_device", sa.String(100), nullable=True))
    op.add_column("users", sa.Column("last_sync_at", sa.DateTime(timezone=True), nullable=True))

    op.create_index("idx_users_tier", "users", ["tier"])
    op.create_index("idx_users_stripe", "users", ["stripe_customer_id"])

    # --- Organizations ---
    op.create_table(
        "organizations",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("slug", sa.String(100), unique=True, nullable=False),
        sa.Column("tier", sa.String(20), nullable=False, server_default="team"),
        sa.Column("stripe_customer_id", sa.String(64), nullable=True),
        sa.Column("stripe_subscription_id", sa.String(64), nullable=True),
        sa.Column("storage_limit_bytes", sa.BigInteger, nullable=False, server_default="0"),
        sa.Column("storage_used_bytes", sa.BigInteger, nullable=False, server_default="0"),
        sa.Column("seats_limit", sa.Integer, nullable=False, server_default="5"),
        sa.Column("settings", sa.Text, nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("idx_organizations_slug", "organizations", ["slug"])

    # --- Organization Members ---
    op.create_table(
        "org_members",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("org_id", sa.String(64), sa.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("role", sa.String(20), nullable=False, server_default="member"),
        sa.Column("invited_by", sa.String(36), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("invited_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("joined_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("org_id", "user_id", name="uq_org_members_org_user"),
    )
    op.create_index("idx_org_members_org", "org_members", ["org_id"])
    op.create_index("idx_org_members_user", "org_members", ["user_id"])

    # --- Organization Invites ---
    op.create_table(
        "org_invites",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("org_id", sa.String(64), sa.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("email", sa.String(255), nullable=False),
        sa.Column("role", sa.String(20), nullable=False, server_default="member"),
        sa.Column("invited_by", sa.String(36), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("org_id", "email", name="uq_org_invites_org_email"),
    )

    # --- Stripe Events (idempotency) ---
    op.create_table(
        "stripe_events",
        sa.Column("event_id", sa.String(64), primary_key=True),
        sa.Column("event_type", sa.String(100), nullable=False),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )

    # --- Helm Licenses ---
    op.create_table(
        "helm_licenses",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("org_name", sa.String(255), nullable=False),
        sa.Column("contact_email", sa.String(255), nullable=False),
        sa.Column("tier", sa.String(20), nullable=False, server_default="enterprise"),
        sa.Column("seats_limit", sa.Integer, server_default="25"),
        sa.Column("status", sa.String(20), nullable=False, server_default="active"),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("notes", sa.Text, nullable=True),
    )

    # --- Telemetry Events ---
    op.create_table(
        "telemetry_events",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("install_id", sa.String(64), nullable=False),
        sa.Column("version", sa.String(20), nullable=False),
        sa.Column("os", sa.String(50), nullable=False),
        sa.Column("tools_active", sa.Text, nullable=False, server_default="[]"),
        sa.Column("sessions_captured_24h", sa.Integer, nullable=False, server_default="0"),
        sa.Column("avg_session_size_bytes", sa.BigInteger, nullable=False, server_default="0"),
        sa.Column("features_used", sa.Text, nullable=False, server_default="[]"),
        sa.Column("errors_24h", sa.Integer, nullable=False, server_default="0"),
        sa.Column("tier", sa.String(20), nullable=False, server_default="free"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("idx_telemetry_install_id", "telemetry_events", ["install_id"])


def downgrade() -> None:
    op.drop_table("telemetry_events")
    op.drop_table("helm_licenses")
    op.drop_table("stripe_events")
    op.drop_table("org_invites")
    op.drop_table("org_members")
    op.drop_table("organizations")
    op.drop_index("idx_users_stripe", "users")
    op.drop_index("idx_users_tier", "users")
    op.drop_column("users", "last_sync_at")
    op.drop_column("users", "last_client_device")
    op.drop_column("users", "last_client_platform")
    op.drop_column("users", "last_client_version")
    op.drop_column("users", "beta_pro_expires_at")
    op.drop_column("users", "storage_used_bytes")
    op.drop_column("users", "tier_updated_at")
    op.drop_column("users", "stripe_subscription_id")
    op.drop_column("users", "stripe_customer_id")
