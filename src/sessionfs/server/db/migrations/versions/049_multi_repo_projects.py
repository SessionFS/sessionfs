"""Multi-repo projects: project_repos join table, merge audit, tombstone columns.

Revision ID: 049
Revises: 048

P1 of Issue tk_4732d94b2c034739 (Phase 1 ticket tk_0113bd826fb1492e).

Additive-only migration (zero downtime):
- project_repos: join table that breaks the 1:1 repo↔project constraint.
  Each repo belongs to exactly one project (global UNIQUE on
  git_remote_normalized). Three indexes enforce the invariants:
  uq_project_repos_remote (global uniqueness),
  uq_project_repos_primary (partial: one primary per project),
  uq_project_repos_provider_repo (partial: one project per provider+repo_id
  when provider_repo_id IS NOT NULL — best-effort rename-survival nicety).
- projects: three new nullable columns — merged_into_project_id (tombstone
  FK, set when this project is merged into another), merged_at (when the
  merge occurred), repo_reclaimed_at (set when ALL repos were reclaimed by
  verified owners via displacement — the project is orphaned but keeps its
  own KB/personas/tickets/rules, distinct from a merge tombstone).
- project_merge_audit: durable audit trail for merge operations (status,
  stats, persona_renames, slug_renames, skipped_ke_ids, skipped_link_ids).
  Status is 'started'|'completed'|'failed'. Written in a SEPARATE
  transaction before mutation begins, so the audit row survives rollback.
- Backfill: one project_repos row per existing project from
  projects.git_remote_normalized (skip null/empty), with is_primary=true,
  verified=false, verification_method='legacy_backfill'. Existing rows are
  grandfathered: displaceable by verified claims, not by unverified claims
  (Sentinel F1).

Downgrade: drop project_merge_audit, drop project_repos, drop the three new
projects columns. Clean.
"""

import uuid
from datetime import datetime, timezone

from alembic import op
import sqlalchemy as sa


revision = "049"
down_revision = "048"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. Create project_repos table
    op.create_table(
        "project_repos",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column(
            "project_id",
            sa.String(length=64),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("git_remote_normalized", sa.String(length=255), nullable=False),
        # Provider identity for rename survival (v1). Best-effort only —
        # frequently NULL. The load-bearing anti-hijack control is
        # verified + verification_method (Sentinel F1/F2).
        sa.Column("provider", sa.String(length=20), nullable=True),
        sa.Column("provider_repo_id", sa.String(length=100), nullable=True),
        sa.Column(
            "is_primary",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        # Sentinel F1: verified=true only for github_app. owner_attested
        # and legacy_backfill are ALWAYS verified=false (Sentinel S2 MED-3).
        sa.Column(
            "verified",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column("verification_method", sa.String(length=20), nullable=True),
        sa.Column(
            "added_by_user_id",
            sa.String(length=64),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )

    # Plain index on project_id
    op.create_index(
        "idx_project_repos_project",
        "project_repos",
        ["project_id"],
    )

    # Global uniqueness: each remote belongs to exactly one project
    op.create_index(
        "uq_project_repos_remote",
        "project_repos",
        ["git_remote_normalized"],
        unique=True,
    )

    # Partial unique: one primary per project
    op.create_index(
        "uq_project_repos_primary",
        "project_repos",
        ["project_id"],
        unique=True,
        postgresql_where=sa.text("is_primary IS TRUE"),
        sqlite_where=sa.text("is_primary IS TRUE"),
    )

    # Partial unique: one project per provider+repo_id (rename survival).
    # Only fires when provider_repo_id IS NOT NULL — frequently NULL
    # (requires GitHub App installed on the repo). Best-effort nicety,
    # NOT a hijack/DoS defense (Sentinel F2).
    op.create_index(
        "uq_project_repos_provider_repo",
        "project_repos",
        ["provider", "provider_repo_id"],
        unique=True,
        postgresql_where=sa.text("provider_repo_id IS NOT NULL"),
        sqlite_where=sa.text("provider_repo_id IS NOT NULL"),
    )

    # 2. Add tombstone + repo_reclaimed columns to projects.
    # Use batch_alter_table for SQLite compatibility — SQLite's ALTER TABLE
    # cannot add columns with FK constraints inline (Alembic batch mode
    # recreates the table transparently on SQLite, no-op on PG).
    with op.batch_alter_table("projects") as batch_op:
        batch_op.add_column(
            sa.Column(
                "merged_into_project_id",
                sa.String(length=64),
                nullable=True,
            ),
        )
        batch_op.add_column(
            sa.Column("merged_at", sa.DateTime(timezone=True), nullable=True),
        )
        batch_op.add_column(
            sa.Column("repo_reclaimed_at", sa.DateTime(timezone=True), nullable=True),
        )
        batch_op.create_foreign_key(
            "fk_projects_merged_into",
            "projects",
            ["merged_into_project_id"],
            ["id"],
            ondelete="SET NULL",
        )

    # 3. Create project_merge_audit table (§5.10)
    op.create_table(
        "project_merge_audit",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("source_project_id", sa.String(length=64), nullable=True),
        sa.Column("target_project_id", sa.String(length=64), nullable=True),
        sa.Column(
            "initiated_by_user_id",
            sa.String(length=64),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "dry_run",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
        sa.Column(
            "status",
            sa.String(length=20),
            nullable=False,
            server_default=sa.text("'completed'"),
        ),
        sa.Column(
            "persona_policy",
            sa.String(length=20),
            nullable=False,
        ),
        # House convention: Text columns with NOT NULL DEFAULT '{}'/'[]'
        # for JSON payloads, matching agent_runs.findings pattern.
        sa.Column(
            "stats",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
        sa.Column(
            "persona_renames",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'[]'"),
        ),
        sa.Column(
            "slug_renames",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'[]'"),
        ),
        sa.Column(
            "skipped_ke_ids",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'[]'"),
        ),
        sa.Column(
            "skipped_link_ids",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'[]'"),
        ),
        sa.Column("rules_action", sa.String(length=20), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )

    # 4. Backfill: one project_repos row per existing project.
    # Use raw connection for parameterized inserts (cross-DB compatible).
    conn = op.get_bind()
    rows = conn.execute(
        sa.text(
            "SELECT id, git_remote_normalized FROM projects "
            "WHERE git_remote_normalized IS NOT NULL "
            "AND git_remote_normalized != ''"
        )
    ).fetchall()

    now = datetime.now(timezone.utc)
    for row in rows:
        project_id = row[0]
        git_remote = row[1]
        repo_id = str(uuid.uuid4())
        conn.execute(
            sa.text(
                "INSERT INTO project_repos "
                "(id, project_id, git_remote_normalized, is_primary, "
                " verified, verification_method, created_at) "
                "VALUES "
                "(:id, :project_id, :git_remote_normalized, :is_primary, "
                " :verified, :verification_method, :created_at)"
            ),
            {
                "id": repo_id,
                "project_id": project_id,
                "git_remote_normalized": git_remote,
                "is_primary": True,
                "verified": False,
                "verification_method": "legacy_backfill",
                "created_at": now,
            },
        )


def downgrade() -> None:
    # Reverse order: drop audit table, drop repo table, drop projects columns
    op.drop_table("project_merge_audit")

    op.drop_index("uq_project_repos_provider_repo", table_name="project_repos")
    op.drop_index("uq_project_repos_primary", table_name="project_repos")
    op.drop_index("uq_project_repos_remote", table_name="project_repos")
    op.drop_index("idx_project_repos_project", table_name="project_repos")
    op.drop_table("project_repos")

    with op.batch_alter_table("projects") as batch_op:
        batch_op.drop_constraint("fk_projects_merged_into", type_="foreignkey")
        batch_op.drop_column("repo_reclaimed_at")
        batch_op.drop_column("merged_at")
        batch_op.drop_column("merged_into_project_id")
