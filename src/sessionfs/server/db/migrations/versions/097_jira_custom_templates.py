"""Org-custom Jira story templates (GitHub #98, part A).

Revision ID: 097
Revises: 096

Adds two tables for org-admin-authored Jira story templates:

- ``jira_templates``: one row per custom template. ``slug`` is unique within
  the org and must not collide with a built-in slug (enforced in the route).
  ``sections`` is a JSON array of ``{key, heading, mode, source_fields,
  guidance}`` validated against the fixed source-field vocabulary. ``version``
  is the current version (starts at 1); every mutation appends a
  ``JiraTemplateVersion`` snapshot. ``archived_at`` soft-deletes the template.
- ``jira_template_versions``: immutable point-in-time snapshots, one per
  (template_id, version), so a ticket link pinned to an older version keeps
  its section layout reproducible after the template is edited again.

Also adds ``template_slug`` / ``template_version`` to ``ticket_tracker_links``
so a published issue records which template (and, for custom templates, which
version) produced it — this powers the "Published with <name> v<n>; current is
v<m>" re-publish hint.

Purely additive — no existing rows change. Downgrade drops the two columns
and the two tables.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "097"
down_revision = "096"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "jira_templates",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("org_id", sa.String(length=64), nullable=False),
        sa.Column("slug", sa.String(length=50), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("sections", sa.Text(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_by_user_id", sa.String(length=64), nullable=False),
        sa.Column("updated_by_user_id", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["org_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("org_id", "slug", name="uq_jira_template_org_slug"),
    )
    op.create_index("idx_jira_templates_org", "jira_templates", ["org_id"])

    op.create_table(
        "jira_template_versions",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("template_id", sa.String(length=64), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("sections", sa.Text(), nullable=False),
        sa.Column("created_by_user_id", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["template_id"], ["jira_templates.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("template_id", "version", name="uq_jira_template_version"),
    )

    op.add_column(
        "ticket_tracker_links", sa.Column("template_slug", sa.String(length=50), nullable=True)
    )
    op.add_column(
        "ticket_tracker_links", sa.Column("template_version", sa.Integer(), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("ticket_tracker_links", "template_version")
    op.drop_column("ticket_tracker_links", "template_slug")
    op.drop_table("jira_template_versions")
    op.drop_table("jira_templates")
