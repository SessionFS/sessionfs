"""Add session.project_id for v0.10.0 Phase 5 session→project linkage.

Server resolves a session's project association at sync time from the
workspace's git remote (already written to workspace.json by the
daemon). The lookup matches `Project.git_remote_normalized` and
validates the caller has access (owner OR member of project.org_id),
then stores the linkage on the session row so queries can filter by
`WHERE project_id = ?` and the org-scope of the session is recoverable
via `projects.org_id`.

Nullable for backward compatibility:
  - Existing sessions captured before Phase 5 have project_id = NULL.
  - Workspaces that aren't linked to a project (untracked git repo or
    no matching Project row) keep project_id = NULL — the session
    still uploads (sessions are user-owned), it just isn't attached.
    There is NO default-org fallback for unmatched remotes in v0.10.0;
    a v0.10.x follow-up may add one if user demand warrants.

ON DELETE SET NULL — a hard project delete clears the linkage but the
session survives (sessions are user-owned per the CEO data-stays
invariant, KB 230 #3). Same durability shape as ProjectTransfer.

Revision ID: 036
Revises: 035
"""

from alembic import op
import sqlalchemy as sa


revision = "036"
down_revision = "035"


def upgrade() -> None:
    op.add_column(
        "sessions",
        sa.Column("project_id", sa.String(64), nullable=True),
    )
    op.create_foreign_key(
        "fk_sessions_project_id",
        "sessions",
        "projects",
        ["project_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index("idx_sessions_project_id", "sessions", ["project_id"])


def downgrade() -> None:
    op.drop_index("idx_sessions_project_id", table_name="sessions")
    op.drop_constraint("fk_sessions_project_id", "sessions", type_="foreignkey")
    op.drop_column("sessions", "project_id")
