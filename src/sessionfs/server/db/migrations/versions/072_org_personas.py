"""Org-level personas (GitHub #70).

Revision ID: 072
Revises: 071

`agent_personas` gains a nullable `org_id` next to `project_id`; exactly
one of the two is set (ck_agent_personas_scope). Purely additive for
existing rows: every persona today has project_id set and org_id NULL,
which satisfies the CHECK, so nothing changes meaning.

- project_id becomes nullable (org personas have none).
- org_id: FK organizations.id ON DELETE CASCADE.
- uq_persona_org_lower_name: partial UNIQUE (org_id, lower(name))
  WHERE org_id IS NOT NULL — case-insensitive name uniqueness per org.
  Project personas keep uq_persona_project_name (project_id, name)
  unchanged; their case-insensitive uniqueness stays an app-level check
  (legacy rows may hold case-distinct duplicates, so a lower() index
  could fail on live data). The upgrade prints any such collisions
  (data check only; it never fails) so they can be cleaned up before a
  later migration adds the index.
- idx_persona_org_active (org_id, is_active) for the org list path.

`handoffs` gains a nullable `source_project_id` (FK projects.id ON
DELETE SET NULL, indexed) — a durable project anchor for the handoff's
ticket/persona provenance, so the persona stranding guards and the
merge persona-reference rewrite can attribute PENDING handoffs to a
project without re-resolving the source session's git remote. Backfill
order: sessions.project_id first (authoritative), then the session's
git_remote_normalized through the dual-read remote resolution that
mirrors `resolve_project_by_remote` — project_repos first, then the
legacy projects.git_remote_normalized column (both UNIQUE per remote,
so at most one project matches) — and finally a bounded follow of the
merge-tombstone chain so the durable anchor names the LIVE project.
Rows with no resolvable project keep NULL and fall back to the legacy
session.project_id match in the guards.

Downgrade deletes org-scoped rows (they cannot satisfy project_id NOT
NULL) before restoring the old shape.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "072"
down_revision = "071"
branch_labels = None
depends_on = None

_SCOPE_CHECK = (
    "(project_id IS NOT NULL AND org_id IS NULL) "
    "OR (project_id IS NULL AND org_id IS NOT NULL)"
)

# Mirrors project_resolver._HOP_CAP: bound the tombstone chain so a
# corrupt merged_into_project_id cycle can never loop the backfill.
_HOP_CAP = 8


def _live_project(bind, project_id: str) -> str | None:
    """Follow `merged_into_project_id` to the LIVE project (≤ _HOP_CAP hops).

    Returns None when the chain is corrupt (loop or overlong) — the caller
    leaves the anchor untouched rather than guessing.
    """
    current = project_id
    for _ in range(_HOP_CAP):
        nxt = bind.execute(
            sa.text(
                "SELECT merged_into_project_id FROM projects WHERE id = :id"
            ),
            {"id": current},
        ).scalar()
        if nxt is None:
            return current
        current = nxt
    return None


def _follow_merge_tombstones(bind) -> None:
    """Rewrite any backfilled anchor that names a merge tombstone to the
    LIVE project, mirroring `resolve_project_by_remote`."""
    rows = bind.execute(
        sa.text(
            "SELECT h.id, h.source_project_id FROM handoffs h"
            " JOIN projects p ON p.id = h.source_project_id"
            " WHERE p.merged_into_project_id IS NOT NULL"
        )
    ).fetchall()
    for handoff_id, anchor in rows:
        live = _live_project(bind, anchor)
        if live is not None and live != anchor:
            bind.execute(
                sa.text(
                    "UPDATE handoffs SET source_project_id = :live WHERE id = :id"
                ),
                {"live": live, "id": handoff_id},
            )


def _backfill_legacy_remote_anchors() -> None:
    """Backfill `handoffs.source_project_id` for legacy sessions whose
    project_id is NULL via the dual-read remote resolution (project_repos
    first, then projects.git_remote_normalized), then follow merge
    tombstones so the anchor names the LIVE project."""
    bind = op.get_bind()
    # project_repos first (source of truth; UNIQUE on git_remote_normalized).
    bind.execute(
        sa.text(
            "UPDATE handoffs SET source_project_id = ("
            "  SELECT pr.project_id FROM sessions s"
            "  JOIN project_repos pr"
            "    ON pr.git_remote_normalized = s.git_remote_normalized"
            "  WHERE s.id = handoffs.session_id"
            ") WHERE source_project_id IS NULL AND EXISTS ("
            "  SELECT 1 FROM sessions s2"
            "  WHERE s2.id = handoffs.session_id"
            "    AND s2.git_remote_normalized IS NOT NULL"
            ")"
        )
    )
    # Legacy fallback: projects.git_remote_normalized (UNIQUE column).
    bind.execute(
        sa.text(
            "UPDATE handoffs SET source_project_id = ("
            "  SELECT p.id FROM sessions s"
            "  JOIN projects p"
            "    ON p.git_remote_normalized = s.git_remote_normalized"
            "  WHERE s.id = handoffs.session_id"
            ") WHERE source_project_id IS NULL AND EXISTS ("
            "  SELECT 1 FROM sessions s2"
            "  WHERE s2.id = handoffs.session_id"
            "    AND s2.git_remote_normalized IS NOT NULL"
            ")"
        )
    )
    _follow_merge_tombstones(bind)


def _report_project_case_collisions() -> None:
    """Report (never fail on) legacy project personas whose names differ
    only in case. Project-level case-insensitive uniqueness is enforced in
    the app (create holds a project-scoped lock); a later migration can add
    a (project_id, lower(name)) unique index once these are resolved."""
    rows = op.get_bind().execute(
        sa.text(
            "SELECT project_id, lower(name) AS lname, count(*) AS n "
            "FROM agent_personas WHERE project_id IS NOT NULL "
            "GROUP BY project_id, lower(name) HAVING count(*) > 1 "
            "ORDER BY project_id, lower(name)"
        )
    ).fetchall()
    if not rows:
        print("072: no case-insensitive project persona name collisions")
        return
    print(
        f"072: {len(rows)} project persona name(s) collide case-insensitively; "
        "rename or deactivate them before adding a lower(name) unique index:"
    )
    for project_id, lname, n in rows:
        print(f"072:   project={project_id} name={lname!r} rows={n}")


def upgrade() -> None:
    _report_project_case_collisions()
    with op.batch_alter_table("agent_personas") as batch:
        batch.add_column(sa.Column("org_id", sa.String(64), nullable=True))
        batch.alter_column(
            "project_id", existing_type=sa.String(64), nullable=True
        )
        batch.create_foreign_key(
            "fk_agent_personas_org_id",
            "organizations",
            ["org_id"],
            ["id"],
            ondelete="CASCADE",
        )
        batch.create_check_constraint("ck_agent_personas_scope", _SCOPE_CHECK)

    op.create_index(
        "idx_persona_org_active", "agent_personas", ["org_id", "is_active"]
    )
    op.create_index(
        "uq_persona_org_lower_name",
        "agent_personas",
        ["org_id", sa.text("lower(name)")],
        unique=True,
        postgresql_where=sa.text("org_id IS NOT NULL"),
        sqlite_where=sa.text("org_id IS NOT NULL"),
    )

    # handoffs.source_project_id — durable project anchor for provenance.
    with op.batch_alter_table("handoffs") as batch:
        batch.add_column(
            sa.Column("source_project_id", sa.String(64), nullable=True)
        )
        batch.create_foreign_key(
            "fk_handoffs_source_project_id",
            "projects",
            ["source_project_id"],
            ["id"],
            ondelete="SET NULL",
        )
    op.create_index(
        "idx_handoffs_source_project_id", "handoffs", ["source_project_id"]
    )
    # Backfill: authoritative session.project_id first, then the legacy
    # remote resolution mirroring resolve_project_by_remote (project_repos
    # first, then projects.git_remote_normalized — both UNIQUE per remote),
    # then follow merge tombstones so the anchor names the LIVE project.
    op.execute(
        sa.text(
            "UPDATE handoffs SET source_project_id = ("
            "  SELECT s.project_id FROM sessions s"
            "  WHERE s.id = handoffs.session_id AND s.project_id IS NOT NULL"
            ") WHERE source_project_id IS NULL"
        )
    )
    _backfill_legacy_remote_anchors()


def downgrade() -> None:
    op.drop_index("idx_handoffs_source_project_id", table_name="handoffs")
    with op.batch_alter_table("handoffs") as batch:
        batch.drop_constraint("fk_handoffs_source_project_id", type_="foreignkey")
        batch.drop_column("source_project_id")
    op.drop_index("uq_persona_org_lower_name", table_name="agent_personas")
    op.drop_index("idx_persona_org_active", table_name="agent_personas")
    op.execute(sa.text("DELETE FROM agent_personas WHERE project_id IS NULL"))
    with op.batch_alter_table("agent_personas") as batch:
        batch.drop_constraint("ck_agent_personas_scope", type_="check")
        batch.drop_constraint("fk_agent_personas_org_id", type_="foreignkey")
        batch.drop_column("org_id")
        batch.alter_column(
            "project_id", existing_type=sa.String(64), nullable=False
        )
