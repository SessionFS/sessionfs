"""Add handoffs table for session handoff workflow.

Revision ID: 005
Revises: 004
"""
from alembic import op
import sqlalchemy as sa

revision = "005"
down_revision = "004"


def upgrade():
    op.create_table(
        "handoffs",
        sa.Column("id", sa.String(20), primary_key=True),
        sa.Column("session_id", sa.String(64), sa.ForeignKey("sessions.id"), nullable=False),
        sa.Column("sender_id", sa.String(36), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("recipient_email", sa.String(255), nullable=False),
        sa.Column("recipient_id", sa.String(36), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("message", sa.Text(), nullable=True),
        sa.Column("status", sa.String(20), server_default="pending", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("idx_handoffs_session_id", "handoffs", ["session_id"])
    op.create_index("idx_handoffs_sender_id", "handoffs", ["sender_id"])
    op.create_index("idx_handoffs_recipient_email", "handoffs", ["recipient_email"])
    op.create_index("idx_handoffs_status", "handoffs", ["status"])


def downgrade():
    op.drop_index("idx_handoffs_status")
    op.drop_index("idx_handoffs_recipient_email")
    op.drop_index("idx_handoffs_sender_id")
    op.drop_index("idx_handoffs_session_id")
    op.drop_table("handoffs")
