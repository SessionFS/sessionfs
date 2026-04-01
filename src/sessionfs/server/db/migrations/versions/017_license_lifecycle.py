"""Add license lifecycle columns and validation tracking.

Revision ID: 017
Revises: 016
"""

from alembic import op
import sqlalchemy as sa

revision = "017"
down_revision = "016"


def upgrade() -> None:
    # --- helm_licenses: add lifecycle columns ---
    op.add_column("helm_licenses", sa.Column("license_type", sa.String(20), nullable=False, server_default="paid"))
    op.add_column("helm_licenses", sa.Column("cluster_id", sa.String(128), nullable=True))
    op.add_column("helm_licenses", sa.Column("last_validated_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("helm_licenses", sa.Column("validation_count", sa.Integer, nullable=False, server_default="0"))
    op.add_column("helm_licenses", sa.Column("metadata", sa.Text, nullable=False, server_default="{}"))

    # --- license_validations table ---
    op.create_table(
        "license_validations",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("license_id", sa.String(64), sa.ForeignKey("helm_licenses.id", ondelete="CASCADE"), nullable=False),
        sa.Column("cluster_id", sa.String(128), nullable=True),
        sa.Column("ip_address", sa.String(45), nullable=True),
        sa.Column("result", sa.String(20), nullable=False),
        sa.Column("version", sa.String(20), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("idx_license_validations_license_id", "license_validations", ["license_id"])
    op.create_index("idx_license_validations_created_at", "license_validations", ["created_at"])


def downgrade() -> None:
    op.drop_table("license_validations")
    op.drop_column("helm_licenses", "metadata")
    op.drop_column("helm_licenses", "validation_count")
    op.drop_column("helm_licenses", "last_validated_at")
    op.drop_column("helm_licenses", "cluster_id")
    op.drop_column("helm_licenses", "license_type")
