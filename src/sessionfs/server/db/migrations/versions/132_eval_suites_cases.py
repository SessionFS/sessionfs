"""Evaluation suites, cases and family promotion requirements (GitHub #121).

Revision ID: 132
Revises: 131

- ``eval_suites``: one immutable row per suite version, same shape as
  ``workflow_templates`` (124): an edit or a case add/retire inserts
  version + 1 in the same ``lineage_id``; only ``active`` changes on an
  existing row; one active row per lineage and one active slug per project
  (partial unique indexes).
- ``eval_cases``: one row per case in a lineage, live in version v when
  ``added_in_version <= v`` and ``retired_in_version`` is NULL or > v, so a
  new version copies no rows and an old version's case set is still
  reproducible. ``content_sha256`` is always set; reference-mode suites keep
  ``input`` / ``expected`` NULL.
- ``artifact_family_requirements``: the suite lineages that must have a
  trusted pass on an artifact version before it is promoted, per family
  (project, kind, name).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "132"
down_revision = "131"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "eval_suites",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column(
            "project_id",
            sa.String(64),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("lineage_id", sa.String(64), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("supersedes_id", sa.String(64), nullable=True),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("slug", sa.String(64), nullable=False),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("grader", sa.Text(), nullable=False),
        sa.Column("acceptance", sa.Text(), nullable=False),
        sa.Column("case_storage", sa.String(12), nullable=False, server_default="inline"),
        sa.Column("case_bank_artifact_id", sa.String(64), nullable=True),
        sa.Column("definition_sha256", sa.String(64), nullable=False),
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
        sa.CheckConstraint("version >= 1", name="ck_eval_suites_version"),
        sa.CheckConstraint(
            "case_storage IN ('inline', 'reference')", name="ck_eval_suites_case_storage"
        ),
        sa.CheckConstraint(
            "actor_type IN ('user', 'service_key')", name="ck_eval_suites_actor_type"
        ),
        sa.UniqueConstraint("lineage_id", "version", name="uq_eval_suites_lineage_version"),
    )
    op.create_index("idx_eval_suites_project", "eval_suites", ["project_id"])
    op.create_index(
        "uq_eval_suites_lineage_active",
        "eval_suites",
        ["lineage_id"],
        unique=True,
        postgresql_where=sa.text("active = true"),
        sqlite_where=sa.text("active = 1"),
    )
    op.create_index(
        "uq_eval_suites_project_slug_active",
        "eval_suites",
        ["project_id", "slug"],
        unique=True,
        postgresql_where=sa.text("active = true"),
        sqlite_where=sa.text("active = 1"),
    )

    op.create_table(
        "eval_cases",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column(
            "project_id",
            sa.String(64),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("lineage_id", sa.String(64), nullable=False),
        sa.Column("case_key", sa.String(64), nullable=False),
        sa.Column("added_in_version", sa.Integer(), nullable=False),
        sa.Column("retired_in_version", sa.Integer(), nullable=True),
        sa.Column("name", sa.String(200), nullable=False, server_default=""),
        sa.Column("input", sa.Text(), nullable=True),
        sa.Column("expected", sa.Text(), nullable=True),
        sa.Column("rubric_notes", sa.Text(), nullable=False, server_default=""),
        sa.Column("tags", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("content_sha256", sa.String(64), nullable=False),
        sa.Column(
            "created_by_user_id",
            sa.String(36),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint("added_in_version >= 1", name="ck_eval_cases_added"),
        sa.CheckConstraint(
            "retired_in_version IS NULL OR retired_in_version > added_in_version",
            name="ck_eval_cases_retired",
        ),
        sa.UniqueConstraint("lineage_id", "case_key", name="uq_eval_cases_lineage_key"),
    )
    op.create_index("idx_eval_cases_project", "eval_cases", ["project_id"])

    op.create_table(
        "artifact_family_requirements",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column(
            "project_id",
            sa.String(64),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("suite_lineage_ids", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("require_tags", sa.Text(), nullable=False, server_default="{}"),
        sa.Column(
            "updated_by_user_id",
            sa.String(36),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "project_id", "kind", "name", name="uq_artifact_family_requirements"
        ),
    )


def downgrade() -> None:
    op.drop_table("artifact_family_requirements")
    op.drop_index("idx_eval_cases_project", table_name="eval_cases")
    op.drop_table("eval_cases")
    op.drop_index("uq_eval_suites_project_slug_active", table_name="eval_suites")
    op.drop_index("uq_eval_suites_lineage_active", table_name="eval_suites")
    op.drop_index("idx_eval_suites_project", table_name="eval_suites")
    op.drop_table("eval_suites")
