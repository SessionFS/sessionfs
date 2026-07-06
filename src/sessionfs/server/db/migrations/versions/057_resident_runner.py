"""Resident R0 — memory primitive + F1 implementer-identity columns.

Revision ID: 057
Revises: 056

Resident reviewer/implementer foundation (docs/design/resident-reviewer.md R3):
  1. CREATE TABLE residents — durable resident identity (reviewer|implementer).
  2. CREATE TABLE resident_memory_entries — append-only durable mind.
  3. ALTER TABLE work_queue_items ADD COLUMNs for F1 self-review enforcement:
     implementer_service_key_id, implementer_user_id,
     closed_by_service_key_id, closed_by_user_id, auto_close_review_kind.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "057"
down_revision = "056"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── 1. residents — durable resident identity ─────────────────────
    op.create_table(
        "residents",
        sa.Column("id", sa.String(length=64), primary_key=True),  # res_<hex>
        sa.Column(
            "org_id",
            sa.String(length=64),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "project_id",
            sa.String(length=64),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("kind", sa.String(length=20), nullable=False),
        sa.Column("persona_name", sa.String(length=50), nullable=False),
        sa.Column(
            "service_key_id",
            sa.String(length=36),
            nullable=False,
        ),
        sa.Column(
            "work_queue_id",
            sa.String(length=64),
            sa.ForeignKey("work_queues.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "status",
            sa.String(length=20),
            nullable=False,
            server_default="active",
        ),
        sa.Column(
            "mind_token_budget",
            sa.Integer(),
            nullable=False,
            server_default="8000",
        ),
        sa.Column(
            "max_uncompacted_entries",
            sa.Integer(),
            nullable=False,
            server_default="500",
        ),
        sa.Column("created_by_user_id", sa.String(length=64), nullable=False),
        sa.Column("actor_type", sa.String(length=20), nullable=True),
        sa.Column("service_key_name", sa.String(length=100), nullable=True),
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
        # Enum invariants (mirror the ORM __table_args__ — inline in
        # create_table so they hold on SQLite too, per the 055/056 pattern).
        sa.CheckConstraint(
            "kind IN ('reviewer', 'implementer')",
            name="ck_resident_kind",
        ),
        sa.CheckConstraint(
            "status IN ('active', 'paused', 'retired')",
            name="ck_resident_status",
        ),
    )
    # One service key drives at most one resident.
    op.create_index(
        "uq_resident_service_key",
        "residents",
        ["service_key_id"],
        unique=True,
    )
    op.create_index(
        "idx_resident_org_project",
        "residents",
        ["org_id", "project_id"],
    )

    # ── 2. resident_memory_entries — append-only durable mind ────────
    op.create_table(
        "resident_memory_entries",
        sa.Column("id", sa.String(length=64), primary_key=True),  # rme_<hex>
        sa.Column(
            "resident_id",
            sa.String(length=64),
            sa.ForeignKey("residents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "org_id",
            sa.String(length=64),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("kind", sa.String(length=20), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column(
            "token_estimate",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        sa.Column("superseded_by", sa.String(length=64), nullable=True),
        sa.Column("compacted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("quarantined", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "kind IN ('reasoning', 'digest', 'observation')",
            name="ck_rme_kind",
        ),
    )
    op.create_index(
        "idx_rme_resident_kind_seq",
        "resident_memory_entries",
        ["resident_id", "kind", "seq"],
    )
    op.create_index(
        "idx_rme_resident_created",
        "resident_memory_entries",
        ["resident_id", "created_at"],
    )
    op.create_index(
        "idx_rme_org",
        "resident_memory_entries",
        ["org_id"],
    )
    op.create_index(
        "uq_resident_memory_seq",
        "resident_memory_entries",
        ["resident_id", "seq"],
        unique=True,
    )

    # ── 3. work_queue_items — F1 implementer-identity columns ────────
    op.add_column(
        "work_queue_items",
        sa.Column("implementer_service_key_id", sa.String(length=36), nullable=True),
    )
    op.add_column(
        "work_queue_items",
        sa.Column("implementer_user_id", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "work_queue_items",
        sa.Column("closed_by_service_key_id", sa.String(length=36), nullable=True),
    )
    op.add_column(
        "work_queue_items",
        sa.Column("closed_by_user_id", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "work_queue_items",
        sa.Column("auto_close_review_kind", sa.String(length=20), nullable=True),
    )
    # Enforce the auto_close_review_kind enum (mirror ck_wqi_auto_close_review_kind
    # on the ORM). PostgreSQL (prod) adds the CHECK to the existing table cleanly;
    # SQLite cannot add a table-level CHECK without a full table rebuild, and the
    # value is strictly server-set (never user input), so we skip it there — the
    # ORM __table_args__ still declares it for any create_all-built SQLite DB.
    if op.get_bind().dialect.name == "postgresql":
        op.create_check_constraint(
            "ck_wqi_auto_close_review_kind",
            "work_queue_items",
            "auto_close_review_kind IS NULL OR "
            "auto_close_review_kind IN ('resident_trusted', 'human')",
        )


def downgrade() -> None:
    # ── 3 reverse: drop work_queue_items F1 columns ──────────────────
    if op.get_bind().dialect.name == "postgresql":
        op.drop_constraint(
            "ck_wqi_auto_close_review_kind", "work_queue_items", type_="check"
        )
    op.drop_column("work_queue_items", "auto_close_review_kind")
    op.drop_column("work_queue_items", "closed_by_user_id")
    op.drop_column("work_queue_items", "closed_by_service_key_id")
    op.drop_column("work_queue_items", "implementer_user_id")
    op.drop_column("work_queue_items", "implementer_service_key_id")

    # ── 2 reverse: drop resident_memory_entries ──────────────────────
    op.drop_index("uq_resident_memory_seq", table_name="resident_memory_entries")
    op.drop_index("idx_rme_org", table_name="resident_memory_entries")
    op.drop_index("idx_rme_resident_created", table_name="resident_memory_entries")
    op.drop_index("idx_rme_resident_kind_seq", table_name="resident_memory_entries")
    op.drop_table("resident_memory_entries")

    # ── 1 reverse: drop residents ────────────────────────────────────
    op.drop_index("idx_resident_org_project", table_name="residents")
    op.drop_index("uq_resident_service_key", table_name="residents")
    op.drop_table("residents")
