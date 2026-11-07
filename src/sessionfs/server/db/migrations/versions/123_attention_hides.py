"""For You: per-user hidden attention items.

Revision ID: 123
Revises: 122

``attention_hides`` records "Hide until it changes" (and optional
"until <time>") for one For You item. ``item_key`` is the opaque item key the
aggregator hands out; ``version_key`` is the source's ``updated_at`` or state
at hide time. A hide applies only while the item's current version_key still
matches, so the item comes back as soon as its source changes. Rows are swept
after 90 days. No copy of the item itself is stored.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "123"
down_revision = "122"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "attention_hides",
        sa.Column(
            "user_id",
            sa.String(36),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("item_key", sa.String(200), nullable=False),
        sa.Column("version_key", sa.String(200), nullable=False),
        sa.Column("hidden_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.PrimaryKeyConstraint("user_id", "item_key", name="pk_attention_hides"),
        sa.CheckConstraint("length(item_key) <= 200", name="ck_attention_hides_item_key_len"),
        sa.CheckConstraint(
            "length(version_key) <= 200", name="ck_attention_hides_version_key_len"
        ),
        # The 90-day retention sweep scans by created_at.
        sa.Index("idx_attention_hides_created", "created_at"),
    )


def downgrade() -> None:
    op.drop_table("attention_hides")
