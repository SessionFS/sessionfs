"""Project groups and restricted project visibility (GitHub #101, phase 1).

Revision ID: 096
Revises: 095

Three new tables; no existing table changes.

- ``project_groups`` — flat org-scoped groups. ``org_id`` is immutable and
  the group can only ever contain projects from that org. ``slug`` is unique
  per org among live (non-soft-deleted) groups via a partial unique index on
  ``(org_id, slug) WHERE deleted_at IS NULL``; a soft-deleted group keeps its
  row for audit continuity but frees its slug.
- ``project_group_members`` — a user's grant inside a group. ``role`` is one
  of ``reader`` (read tier), ``editor`` (edit tier) or ``admin`` (edit +
  manage the group). Grantees must be active members of the group's org
  (enforced in the routes layer, not by a DB constraint, so a membership
  removal can delete the grants in the same transaction first). Composite
  primary key ``(group_id, user_id)`` enforces one grant per user per group.
- ``project_group_access`` — which projects are members of which groups.
  Composite primary key ``(group_id, project_id)``. Only projects whose
  ``project.org_id`` equals the group's ``org_id`` may be added (routes
  layer); the same-org invariant is what keeps a grant from ever leaking a
  project across orgs.

Downgrade drops the three tables (indexes first, per Alembic order).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "096"
down_revision = "095"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "project_groups",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column(
            "org_id",
            sa.String(64),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("slug", sa.String(100), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column(
            "created_by_user_id",
            sa.String(36),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("idx_project_groups_org", "project_groups", ["org_id"])
    op.create_index(
        "uq_project_groups_org_slug",
        "project_groups",
        ["org_id", "slug"],
        unique=True,
        postgresql_where=sa.text("deleted_at IS NULL"),
        sqlite_where=sa.text("deleted_at IS NULL"),
    )

    op.create_table(
        "project_group_members",
        sa.Column(
            "group_id",
            sa.String(64),
            sa.ForeignKey("project_groups.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "user_id",
            sa.String(36),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("role", sa.String(20), nullable=False),
        sa.Column(
            "granted_by_user_id",
            sa.String(36),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "role IN ('reader', 'editor', 'admin')",
            name="ck_project_group_members_role",
        ),
    )
    op.create_index(
        "idx_project_group_members_user", "project_group_members", ["user_id"]
    )

    op.create_table(
        "project_group_access",
        sa.Column(
            "group_id",
            sa.String(64),
            sa.ForeignKey("project_groups.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "project_id",
            sa.String(64),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "added_by_user_id",
            sa.String(36),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index(
        "idx_project_group_access_project", "project_group_access", ["project_id"]
    )


def downgrade() -> None:
    op.drop_index("idx_project_group_access_project", table_name="project_group_access")
    op.drop_table("project_group_access")
    op.drop_index("idx_project_group_members_user", table_name="project_group_members")
    op.drop_table("project_group_members")
    op.drop_index("uq_project_groups_org_slug", table_name="project_groups")
    op.drop_index("idx_project_groups_org", table_name="project_groups")
    op.drop_table("project_groups")
