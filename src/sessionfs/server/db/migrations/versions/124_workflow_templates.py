"""Workflow templates (record-only multi-agent workflows, GitHub #120).

Revision ID: 124
Revises: 123

- ``workflow_templates``: one immutable row per template version. A template
  belongs to exactly one scope: an org (``org_id``) or a project
  (``project_id``), never both (CHECK). An edit inserts a new row in the same
  ``lineage_id`` with ``version`` + 1 and ``supersedes_id`` pointing at the
  previous row; only ``active`` ever changes on an existing row. At most one
  active row per lineage (partial unique index) and one row per
  (lineage, version). ``definition`` is the validated JSON definition,
  capped at 32 KiB.
- ``workflow_step_children``: which child ticket of a workflow loop belongs
  to which step and fan-out slot. ``(loop_ticket_id, step_id, step_index)``
  is unique so a start can never create a step slot twice; a child ticket
  belongs to at most one slot.
- ``agent_runs.workflow_step_id``: the template step a reported run belongs
  to (nullable; runs outside a workflow keep NULL).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "124"
down_revision = "123"
branch_labels = None
depends_on = None


_SCOPE_CHECK = (
    "(org_id IS NOT NULL AND project_id IS NULL) "
    "OR (org_id IS NULL AND project_id IS NOT NULL)"
)


def upgrade() -> None:
    op.create_table(
        "workflow_templates",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column(
            "org_id",
            sa.String(64),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column(
            "project_id",
            sa.String(64),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("lineage_id", sa.String(64), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("definition", sa.Text(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("supersedes_id", sa.String(64), nullable=True),
        sa.Column(
            "created_by_user_id",
            sa.String(36),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("actor_type", sa.String(20), nullable=False, server_default="user"),
        sa.Column("service_key_id", sa.String(36), nullable=True),
        sa.Column("service_key_name", sa.String(100), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(_SCOPE_CHECK, name="ck_workflow_templates_scope"),
        sa.CheckConstraint(
            "length(definition) <= 32768", name="ck_workflow_templates_definition_len"
        ),
        sa.CheckConstraint("version >= 1", name="ck_workflow_templates_version"),
        sa.CheckConstraint(
            "actor_type IN ('user', 'service_key')", name="ck_workflow_templates_actor_type"
        ),
        sa.UniqueConstraint(
            "lineage_id", "version", name="uq_workflow_templates_lineage_version"
        ),
    )
    op.create_index("idx_workflow_templates_project", "workflow_templates", ["project_id"])
    op.create_index("idx_workflow_templates_org", "workflow_templates", ["org_id"])
    op.create_index(
        "uq_workflow_templates_lineage_active",
        "workflow_templates",
        ["lineage_id"],
        unique=True,
        postgresql_where=sa.text("active = true"),
        sqlite_where=sa.text("active = 1"),
    )

    op.create_table(
        "workflow_step_children",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column(
            "project_id",
            sa.String(64),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "loop_ticket_id",
            sa.String(64),
            sa.ForeignKey("tickets.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "ticket_id",
            sa.String(64),
            sa.ForeignKey("tickets.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("step_id", sa.String(64), nullable=False),
        sa.Column("step_index", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "step_index >= 0 AND step_index < 10", name="ck_workflow_step_children_index"
        ),
        sa.UniqueConstraint(
            "loop_ticket_id",
            "step_id",
            "step_index",
            name="uq_workflow_step_children_slot",
        ),
        sa.UniqueConstraint("ticket_id", name="uq_workflow_step_children_ticket"),
    )

    op.add_column(
        "agent_runs", sa.Column("workflow_step_id", sa.String(64), nullable=True)
    )


def downgrade() -> None:
    with op.batch_alter_table("agent_runs") as batch:
        batch.drop_column("workflow_step_id")
    op.drop_table("workflow_step_children")
    op.drop_index("uq_workflow_templates_lineage_active", table_name="workflow_templates")
    op.drop_index("idx_workflow_templates_org", table_name="workflow_templates")
    op.drop_index("idx_workflow_templates_project", table_name="workflow_templates")
    op.drop_table("workflow_templates")
