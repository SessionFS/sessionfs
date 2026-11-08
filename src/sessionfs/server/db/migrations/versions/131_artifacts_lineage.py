"""Artifacts and lineage edges (evaluations + lineage v1, GitHub #122).

Revision ID: 131
Revises: 130

- ``artifacts``: a pointer to one version of an external dataset, model,
  feature set, prompt or checkpoint. Project-scoped; unique per
  (project, kind, name, version). SessionFS stores the URI, an optional
  digest, a schema summary and tags, never the bytes, and never fetches the
  URI.
- ``artifact_edges``: parent -> child lineage inside one project, one row per
  (parent, child, relation); self-edges refused by CHECK (cycles are refused
  by the route under the project scope lock).
- ``artifact_tags``: key/value tags, (artifact_id, key) primary key.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "131"
down_revision = "130"
branch_labels = None
depends_on = None


def _audit() -> list[sa.Column]:
    return [
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
    ]


def upgrade() -> None:
    op.create_table(
        "artifacts",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column(
            "project_id",
            sa.String(64),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("version", sa.String(64), nullable=False),
        sa.Column("external_uri", sa.String(2048), nullable=False),
        sa.Column("digest", sa.String(128), nullable=True),
        sa.Column("schema_summary", sa.Text(), nullable=True),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("stage", sa.String(20), nullable=False, server_default="candidate"),
        sa.Column("produced_by_agent_run_id", sa.String(64), nullable=True),
        *_audit(),
        sa.Column("deprecated_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "kind IN ('dataset', 'model', 'feature_set', 'prompt', 'checkpoint')",
            name="ck_artifacts_kind",
        ),
        sa.CheckConstraint(
            "stage IN ('candidate', 'staging', 'production', 'archived')",
            name="ck_artifacts_stage",
        ),
        sa.CheckConstraint(
            "actor_type IN ('user', 'service_key')", name="ck_artifacts_actor_type"
        ),
        sa.UniqueConstraint(
            "project_id", "kind", "name", "version", name="uq_artifacts_family_version"
        ),
    )
    op.create_index("idx_artifacts_family", "artifacts", ["project_id", "kind", "name"])
    op.create_index(
        "idx_artifacts_project_created", "artifacts", ["project_id", "created_at"]
    )

    op.create_table(
        "artifact_edges",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column(
            "project_id",
            sa.String(64),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "parent_id",
            sa.String(64),
            sa.ForeignKey("artifacts.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "child_id",
            sa.String(64),
            sa.ForeignKey("artifacts.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("relation", sa.String(30), nullable=False),
        sa.Column("context_ref", sa.String(200), nullable=True),
        *_audit(),
        sa.CheckConstraint(
            "relation IN ('derived_from', 'trained_on', 'fine_tuned_from', "
            "'evaluated_against', 'uses')",
            name="ck_artifact_edges_relation",
        ),
        sa.CheckConstraint("parent_id <> child_id", name="ck_artifact_edges_no_self"),
        sa.CheckConstraint(
            "actor_type IN ('user', 'service_key')", name="ck_artifact_edges_actor_type"
        ),
        sa.UniqueConstraint(
            "parent_id", "child_id", "relation", name="uq_artifact_edges_pair"
        ),
    )
    op.create_index("idx_artifact_edges_child", "artifact_edges", ["child_id"])
    op.create_index("idx_artifact_edges_project", "artifact_edges", ["project_id"])

    op.create_table(
        "artifact_tags",
        sa.Column(
            "artifact_id",
            sa.String(64),
            sa.ForeignKey("artifacts.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("key", sa.String(64), primary_key=True),
        sa.Column("value", sa.String(256), nullable=False, server_default=""),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index("idx_artifact_tags_key_value", "artifact_tags", ["key", "value"])


def downgrade() -> None:
    op.drop_index("idx_artifact_tags_key_value", table_name="artifact_tags")
    op.drop_table("artifact_tags")
    op.drop_index("idx_artifact_edges_project", table_name="artifact_edges")
    op.drop_index("idx_artifact_edges_child", table_name="artifact_edges")
    op.drop_table("artifact_edges")
    op.drop_index("idx_artifacts_project_created", table_name="artifacts")
    op.drop_index("idx_artifacts_family", table_name="artifacts")
    op.drop_table("artifacts")
