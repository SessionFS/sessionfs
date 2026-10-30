"""The project checklist that learns (Review Loop R3, track E1).

Revision ID: 112
Revises: 111

A project-scoped, append-mostly table of review traps that the loop has
learned. A checklist item is one trap (``title``) plus how to check for it
(``check_text``); it carries a lifecycle status, its provenance (seeded,
derived from a review finding, or added by hand) and how often the loop has
re-hit the same trap (``times_found`` / ``last_found_at``).

- ``status``: proposed → active → retired. Proposed items are the inbox the
  project owner accepts or skips (design §10.1 — SessionFS ships a small
  generic seed that a project accepts or skips); only ``active`` items render
  into briefs via the markdown endpoint.
- ``source``: seed (shipped generic), finding (derived from a review finding
  whose ``class`` was ``new``), or manual (added by an editor).
- ``source_finding_refs``: JSON list of ``{run_id, title}`` provenance for
  the runs/findings that produced (or re-hit) this trap, with the title
  redacted. Text column for cross-DB SQLite/PG parity, same as
  ``agent_runs.findings``.
- ``(project_id, slug)`` is unique: proposals dedupe by slug within a project
  (design §7.3), so a re-hit bumps ``times_found`` instead of creating a
  duplicate row.

Inline CHECK constraints (declared at create_table) are SQLite-safe — the
same pattern as migrations 050–054. Downgrade drops the index then the table.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "112"
down_revision = "111"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "loop_checklist_items",
        sa.Column("id", sa.String(length=64), primary_key=True),  # lci_<hex>
        sa.Column(
            "project_id",
            sa.String(length=64),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        # Lowercase URL-safe slug, derived from the finding title for
        # finding-sourced items; `new` is never a slug (it means "unclassified").
        sa.Column("slug", sa.String(length=64), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("check_text", sa.Text(), nullable=False),
        sa.Column(
            "status",
            sa.String(length=20),
            nullable=False,
            server_default="proposed",
        ),
        sa.Column(
            "source",
            sa.String(length=20),
            nullable=False,
            server_default="manual",
        ),
        # JSON list of {"run_id": ..., "finding": {...}} provenance. Nullable —
        # manual and seed items have no finding to point at.
        sa.Column("source_finding_refs", sa.Text(), nullable=True),
        sa.Column(
            "times_found",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        sa.Column("last_found_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_by_user_id",
            sa.String(length=64),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "accepted_by_user_id",
            sa.String(length=64),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        # Actor provenance (review checklist #3): service keys leave the
        # user_id NULL and record their key id/name + actor_type instead.
        sa.Column(
            "created_actor_type",
            sa.String(length=20),
            nullable=False,
            server_default="user",
        ),
        sa.Column("created_service_key_id", sa.String(length=36), nullable=True),
        sa.Column("created_service_key_name", sa.String(length=100), nullable=True),
        sa.Column("accepted_actor_type", sa.String(length=20), nullable=True),
        sa.Column("accepted_service_key_id", sa.String(length=36), nullable=True),
        sa.Column("accepted_service_key_name", sa.String(length=100), nullable=True),
        sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=True),
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
        sa.UniqueConstraint(
            "project_id", "slug", name="uq_loop_checklist_project_slug"
        ),
        sa.CheckConstraint(
            "status IN ('proposed', 'active', 'retired')",
            name="ck_loop_checklist_status",
        ),
        sa.CheckConstraint(
            "source IN ('seed', 'finding', 'manual')",
            name="ck_loop_checklist_source",
        ),
    )
    op.create_index(
        "idx_loop_checklist_project_status",
        "loop_checklist_items",
        ["project_id", "status"],
    )


def downgrade() -> None:
    op.drop_index(
        "idx_loop_checklist_project_status", table_name="loop_checklist_items"
    )
    op.drop_table("loop_checklist_items")
