"""v0.10.9 Handoff Best-Case Scenario — comprehensive handoff redesign.

Revision ID: 041
Revises: 040

Scope per parent ticket tk_89e90060e6314311 with Codex R1 scope-review
(tc_889eefb930d04ea8) + R2 schema-review corrections applied.

R2 corrections applied:
- recipient_team_id is a real FK to teams.id with ON DELETE SET NULL
  (was plain String(64); Codex MEDIUM #1). Teams table is created
  BEFORE the FK column is added so the constraint validates.
- handoff_attachments gains a project_id column so wiki_page refs
  validate unambiguously at claim time (slugs are project-local;
  Codex MEDIUM #3).
- Helpers extend project access to org members (Codex MEDIUM #2 —
  in handoff_helpers.py, not this migration).
- Schema validators normalize whitespace on all recipient ID fields
  (Codex LOW #4 — in schemas/handoffs.py).

ADDITIVE SCHEMA — zero existing data loss. All UniqueConstraints
defined INSIDE op.create_table per v0.10.7 R7 lesson. All FKs to
users.id use SET NULL to preserve audit rows after user deletion.
"""

from alembic import op
import sqlalchemy as sa


revision = "041"
down_revision = "040"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # --- new table: teams (must exist BEFORE handoffs.recipient_team_id FK) ---
    op.create_table(
        "teams",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column(
            "org_id",
            sa.String(64),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("slug", sa.String(50), nullable=False),
        sa.Column(
            "created_by",
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
        sa.UniqueConstraint("org_id", "slug", name="uq_teams_org_slug"),
    )
    op.create_index("idx_teams_org_id", "teams", ["org_id"])

    # --- new table: team_members (join — users belong to multiple teams) ---
    op.create_table(
        "team_members",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "team_id",
            sa.String(64),
            sa.ForeignKey("teams.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "user_id",
            sa.String(36),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "added_by",
            sa.String(36),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "added_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "team_id", "user_id", name="uq_team_members_team_user"
        ),
    )
    op.create_index("idx_team_members_team_id", "team_members", ["team_id"])
    op.create_index("idx_team_members_user_id", "team_members", ["user_id"])

    # --- handoffs column additions (now teams exists for the FK) ---
    # recipient_email becomes nullable. SQLite cannot ALTER COLUMN to drop
    # NOT NULL — use batch mode (only takes effect on SQLite; PG runs the
    # direct ALTER). Existing rows retain their non-null values.
    with op.batch_alter_table("handoffs") as batch:
        batch.alter_column(
            "recipient_email",
            existing_type=sa.String(255),
            nullable=True,
        )

    op.add_column(
        "handoffs",
        sa.Column(
            "recipient_user_id",
            sa.String(36),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    # Codex R2 MEDIUM #1 — recipient_team_id IS a real FK with SET NULL.
    op.add_column(
        "handoffs",
        sa.Column(
            "recipient_team_id",
            sa.String(64),
            sa.ForeignKey("teams.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.add_column(
        "handoffs",
        sa.Column("ticket_id", sa.String(64), nullable=True),
    )
    op.add_column(
        "handoffs",
        sa.Column("persona_name", sa.String(50), nullable=True),
    )
    op.add_column(
        "handoffs",
        sa.Column(
            "revoked_at", sa.DateTime(timezone=True), nullable=True
        ),
    )
    op.add_column(
        "handoffs",
        sa.Column(
            "revoked_by_user_id",
            sa.String(36),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.add_column(
        "handoffs", sa.Column("revoke_reason", sa.Text(), nullable=True)
    )
    op.add_column(
        "handoffs",
        sa.Column(
            "handoff_kind",
            sa.String(20),
            nullable=False,
            server_default="individual",
        ),
    )
    op.add_column(
        "handoffs",
        sa.Column(
            "viewed_at", sa.DateTime(timezone=True), nullable=True
        ),
    )
    op.add_column(
        "handoffs",
        sa.Column("snapshot_persona_name", sa.String(50), nullable=True),
    )
    op.add_column(
        "handoffs",
        sa.Column("snapshot_ticket_title", sa.String(500), nullable=True),
    )
    op.add_column(
        "handoffs",
        sa.Column("sender_tier_snapshot", sa.String(20), nullable=True),
    )
    op.create_index(
        "idx_handoffs_recipient_team_id",
        "handoffs",
        ["recipient_team_id"],
    )
    op.create_index(
        "idx_handoffs_recipient_user_id",
        "handoffs",
        ["recipient_user_id"],
    )

    # --- new table: handoff_comments ---
    op.create_table(
        "handoff_comments",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column(
            "handoff_id",
            sa.String(20),
            sa.ForeignKey("handoffs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "author_user_id",
            sa.String(36),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index(
        "idx_handoff_comments_handoff_id",
        "handoff_comments",
        ["handoff_id", "created_at"],
    )

    # --- new table: handoff_events ---
    op.create_table(
        "handoff_events",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "handoff_id",
            sa.String(20),
            sa.ForeignKey("handoffs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("event_type", sa.String(40), nullable=False),
        sa.Column(
            "actor_user_id",
            sa.String(36),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("payload", sa.Text(), nullable=False, server_default="{}"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index(
        "idx_handoff_events_handoff_id",
        "handoff_events",
        ["handoff_id", "created_at"],
    )

    # --- new table: handoff_attachments ---
    # Codex R2 MEDIUM #3 — project_id column so wiki_page refs validate
    # unambiguously at recipient time (slugs are project-local; without
    # this two projects with the same auth-flow slug could collide).
    op.create_table(
        "handoff_attachments",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "handoff_id",
            sa.String(20),
            sa.ForeignKey("handoffs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "project_id",
            sa.String(64),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("ref_id", sa.String(255), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index(
        "idx_handoff_attachments_handoff_id",
        "handoff_attachments",
        ["handoff_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "idx_handoff_attachments_handoff_id", table_name="handoff_attachments"
    )
    op.drop_table("handoff_attachments")
    op.drop_index(
        "idx_handoff_events_handoff_id", table_name="handoff_events"
    )
    op.drop_table("handoff_events")
    op.drop_index(
        "idx_handoff_comments_handoff_id", table_name="handoff_comments"
    )
    op.drop_table("handoff_comments")
    op.drop_index("idx_handoffs_recipient_user_id", table_name="handoffs")
    op.drop_index("idx_handoffs_recipient_team_id", table_name="handoffs")
    op.drop_column("handoffs", "sender_tier_snapshot")
    op.drop_column("handoffs", "snapshot_ticket_title")
    op.drop_column("handoffs", "snapshot_persona_name")
    op.drop_column("handoffs", "viewed_at")
    op.drop_column("handoffs", "handoff_kind")
    op.drop_column("handoffs", "revoke_reason")
    op.drop_column("handoffs", "revoked_by_user_id")
    op.drop_column("handoffs", "revoked_at")
    op.drop_column("handoffs", "persona_name")
    op.drop_column("handoffs", "ticket_id")
    op.drop_column("handoffs", "recipient_team_id")
    op.drop_column("handoffs", "recipient_user_id")
    # recipient_email NOT NULL rollback — skip for SQLite (would need
    # batch mode); PG will succeed. Acceptable asymmetry since downgrade
    # is dev-only and existing rows already have non-null values.
    with op.batch_alter_table("handoffs") as batch:
        batch.alter_column(
            "recipient_email",
            existing_type=sa.String(255),
            nullable=False,
        )
    op.drop_index("idx_team_members_user_id", table_name="team_members")
    op.drop_index("idx_team_members_team_id", table_name="team_members")
    op.drop_table("team_members")
    op.drop_index("idx_teams_org_id", table_name="teams")
    op.drop_table("teams")
