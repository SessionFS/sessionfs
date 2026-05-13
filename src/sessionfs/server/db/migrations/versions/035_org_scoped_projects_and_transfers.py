"""Add org-scoped projects + per-user default org + project transfers table.

v0.10.0 Org Admin Console phase 1. CEO-approved scope in KB entry 230,
implementation brief in KB entry 231 + repo file
`v0.10.0-org-admin-console-brief`.

Three additions:

1. `projects.org_id` (nullable String FK → organizations.id, ON DELETE
   SET NULL). NULL = personal project (existing behavior preserved for
   all current rows). NON-NULL = team project, gated by org-admin role.

2. `users.default_org_id` (nullable String FK → organizations.id, ON
   DELETE SET NULL). Per-user preference: the CLI uses this org when
   no `--org` flag is passed and the user belongs to multiple orgs.
   Null on a single-org or no-org user is the no-op default.

3. `project_transfers` table — durable audit + state machine for the
   "transfer a project between personal and an org, or between orgs"
   flow. Two-phase: initiator creates a pending row, target user
   accepts/rejects, initiator can cancel while pending. ON-disk audit
   record stays after the transfer commits — compliance requirement
   from Baptist (KB entry 230, decision 1).

NO data migration: existing projects keep org_id = NULL, existing
users keep default_org_id = NULL. Zero-downtime DDL on PG; SQLite
adds columns trivially.

Revision ID: 035
Revises: 034
"""

from alembic import op
import sqlalchemy as sa


revision = "035"
down_revision = "034"


def upgrade() -> None:
    # projects.org_id — nullable, ON DELETE SET NULL so an org delete
    # demotes its projects to personal-scope rather than deleting
    # them (data-stays-access-revoked invariant from KB 230 #3).
    op.add_column(
        "projects",
        sa.Column("org_id", sa.String(64), nullable=True),
    )
    op.create_foreign_key(
        "fk_projects_org_id",
        "projects",
        "organizations",
        ["org_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index("idx_projects_org_id", "projects", ["org_id"])

    # users.default_org_id — same shape. ON DELETE SET NULL so a user
    # whose default org is deleted (or who is removed from it) falls
    # back to personal-scope rather than carrying a dangling FK.
    op.add_column(
        "users",
        sa.Column("default_org_id", sa.String(64), nullable=True),
    )
    op.create_foreign_key(
        "fk_users_default_org_id",
        "users",
        "organizations",
        ["default_org_id"],
        ["id"],
        ondelete="SET NULL",
    )

    # project_transfers — state machine + audit. Created on initiate,
    # mutated only by accept/reject/cancel. Never deleted; the audit
    # row is the compliance artifact (CEO directive, KB entry 230 #1).
    #
    # IMPORTANT — durability invariants the schema must hold:
    #   - project_id is FK ON DELETE SET NULL (nullable). A hard
    #     project delete (routes/projects.py) clears the linkage but
    #     does NOT cascade-destroy the audit row.
    #   - project_git_remote_snapshot is the STABLE durable identifier
    #     captured at create-time. `projects.git_remote_normalized` is
    #     `unique=True` so a snapshot of it disambiguates an audit row
    #     even if two deleted projects share a display name. Codex
    #     Phase-1 round-3 catch (KB entry 238): name alone isn't
    #     unique — would have left audit history ambiguous.
    #   - project_name_snapshot is the human-readable label, captured
    #     alongside git_remote for display purposes. Not load-bearing
    #     for identity.
    #   - Both snapshot columns are populated by the Phase 2 route at
    #     create-time. They're not CHECK constraints (route enforces);
    #     nullability keeps DDL ALTER simple.
    op.create_table(
        "project_transfers",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column(
            "project_id",
            sa.String(64),
            sa.ForeignKey("projects.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "project_git_remote_snapshot", sa.String(255), nullable=True
        ),
        sa.Column("project_name_snapshot", sa.String(255), nullable=True),
        sa.Column(
            "initiated_by",
            sa.String(36),
            sa.ForeignKey("users.id"),
            nullable=False,
        ),
        # target_user_id — the user who must accept while pending.
        # Populated at initiate. Required for the "inbox for user X"
        # query: a pending row's accepted_by is NULL by definition, so
        # the inbox needs a separate pending-recipient column. For an
        # auto-accept shape (e.g. user transfers their own personal
        # project to an org they belong to), the route layer sets
        # target_user_id = initiated_by and immediately advances the
        # state to accepted at create time. ON DELETE SET NULL so a
        # deleted target user doesn't destroy the audit row.
        sa.Column(
            "target_user_id",
            sa.String(36),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        # from_scope / to_scope hold either the literal "personal" or an
        # organizations.id. Stored as TEXT (not FK) so we can keep the
        # historical record intact even if the org is later deleted.
        sa.Column("from_scope", sa.String(64), nullable=False),
        sa.Column("to_scope", sa.String(64), nullable=False),
        # State machine: pending → accepted | rejected | cancelled
        sa.Column(
            "state",
            sa.String(20),
            nullable=False,
            server_default="pending",
        ),
        # accepted_by is the target user at the moment of acceptance —
        # frozen for audit even if target_user_id later goes NULL.
        # Null on pending / rejected / cancelled.
        sa.Column(
            "accepted_by",
            sa.String(36),
            sa.ForeignKey("users.id"),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "accepted_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index(
        "idx_project_transfers_project",
        "project_transfers",
        ["project_id"],
    )
    op.create_index(
        "idx_project_transfers_state",
        "project_transfers",
        ["state"],
    )
    # Composite index for the dashboard's "incoming pending transfers
    # for user X" query. Driven by target_user_id (set on initiate)
    # rather than accepted_by (NULL on pending rows). Codex catch on
    # Phase 1 round 1 — using accepted_by would have shipped a dead
    # index because pending rows never match it.
    op.create_index(
        "idx_project_transfers_inbox",
        "project_transfers",
        ["state", "target_user_id"],
    )

    # Partial unique index — at most one pending transfer per project
    # at a time. The route does a SELECT precheck for a friendly 409
    # message, but this DB-level constraint is the concurrency-safe
    # backstop: two concurrent initiates that both pass the SELECT
    # will collide here on commit and the route catches the
    # IntegrityError to also return 409. Codex Phase-2 round-2 catch
    # (KB entry 248): SELECT-then-INSERT alone is racy.
    #
    # Both PG and SQLite ≥ 3.8.0 support partial indexes. The
    # `postgresql_where` / `sqlite_where` clauses generate the WHERE
    # filter on each dialect.
    op.create_index(
        "idx_project_transfers_pending_unique",
        "project_transfers",
        ["project_id"],
        unique=True,
        postgresql_where=sa.text("state = 'pending'"),
        sqlite_where=sa.text("state = 'pending'"),
    )


def downgrade() -> None:
    op.drop_index(
        "idx_project_transfers_pending_unique", table_name="project_transfers"
    )
    op.drop_index("idx_project_transfers_inbox", table_name="project_transfers")
    op.drop_index("idx_project_transfers_state", table_name="project_transfers")
    op.drop_index("idx_project_transfers_project", table_name="project_transfers")
    op.drop_table("project_transfers")

    op.drop_constraint("fk_users_default_org_id", "users", type_="foreignkey")
    op.drop_column("users", "default_org_id")

    op.drop_index("idx_projects_org_id", table_name="projects")
    op.drop_constraint("fk_projects_org_id", "projects", type_="foreignkey")
    op.drop_column("projects", "org_id")
