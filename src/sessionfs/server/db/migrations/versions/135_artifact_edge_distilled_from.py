"""Lineage edges gain ``distilled_from``.

Revision ID: 135
Revises: 134

``ck_artifact_edges_relation`` widens to accept ``distilled_from`` (a student
model distilled from a teacher model: parent = teacher, child = student, the
same direction as ``fine_tuned_from``).

Migration 131 declared the CHECK inline on ``create_table``, so it exists on
both dialects. PostgreSQL: DROP CONSTRAINT then ADD CONSTRAINT (never DROP
INDEX, the 056 lesson). SQLite cannot alter a CHECK in place, so the table is
rebuilt from an explicit copy of the 131 shape (the pattern 128 uses).

Downgrade keeps the lineage instead of dropping it: each ``distilled_from``
edge becomes ``derived_from`` (still true, just less specific). Where the
same (parent, child) pair already has a ``derived_from`` edge, the
``distilled_from`` row is deleted first so the unique key holds. Cycles can't
appear because no new (parent, child) pair is created.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "135"
down_revision = "134"
branch_labels = None
depends_on = None

_TABLE = "artifact_edges"
_CHECK = "ck_artifact_edges_relation"
_RELATION_OLD = (
    "relation IN ('derived_from', 'trained_on', 'fine_tuned_from', "
    "'evaluated_against', 'uses')"
)
_RELATION_NEW = (
    "relation IN ('derived_from', 'trained_on', 'fine_tuned_from', "
    "'distilled_from', 'evaluated_against', 'uses')"
)


def _table(relation_check: str) -> sa.Table:
    """The artifact_edges shape from 131 with the given relation CHECK."""
    return sa.Table(
        _TABLE,
        sa.MetaData(),
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
        sa.CheckConstraint(relation_check, name=_CHECK),
        sa.CheckConstraint("parent_id <> child_id", name="ck_artifact_edges_no_self"),
        sa.CheckConstraint(
            "actor_type IN ('user', 'service_key')", name="ck_artifact_edges_actor_type"
        ),
        sa.UniqueConstraint("parent_id", "child_id", "relation", name="uq_artifact_edges_pair"),
        sa.Index("idx_artifact_edges_child", "child_id"),
        sa.Index("idx_artifact_edges_project", "project_id"),
    )


def _swap(old: str, new: str) -> None:
    if op.get_context().dialect.name == "sqlite":
        with op.batch_alter_table(_TABLE, recreate="always", copy_from=_table(old)) as batch:
            batch.drop_constraint(_CHECK, type_="check")
            batch.create_check_constraint(_CHECK, new)
        return
    op.drop_constraint(_CHECK, _TABLE, type_="check")
    op.create_check_constraint(_CHECK, _TABLE, new)


def upgrade() -> None:
    _swap(_RELATION_OLD, _RELATION_NEW)


def downgrade() -> None:
    op.execute(
        sa.text(
            "DELETE FROM artifact_edges WHERE relation = 'distilled_from' AND EXISTS ("
            "SELECT 1 FROM artifact_edges d WHERE d.relation = 'derived_from' "
            "AND d.parent_id = artifact_edges.parent_id "
            "AND d.child_id = artifact_edges.child_id)"
        )
    )
    op.execute(
        sa.text(
            "UPDATE artifact_edges SET relation = 'derived_from' "
            "WHERE relation = 'distilled_from'"
        )
    )
    _swap(_RELATION_NEW, _RELATION_OLD)
