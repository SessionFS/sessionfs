"""Add agent_personas, tickets, ticket_dependencies, ticket_comments tables.

v0.10.1 Phase 1 — Agent Personas + Ticketing. Brief in KB entry under
entity_ref=agent-personas-tickets-v0.10.1-phase-{1..7}.

Four new tables + two new Session columns:

1. `agent_personas` — portable AI roles scoped to a project.
   UNIQUE(project_id, name) so a project can't have two personas with
   the same name. ON DELETE CASCADE on project_id (deleting a project
   takes its personas with it). Soft-delete via is_active=false.

2. `tickets` — self-contained task units assigned to a persona.
   Status FSM enforced server-side at the routes layer; the column
   is a plain VARCHAR with a default of 'open'. Reporter provenance
   is structured into three fields (user_id always set, session_id
   optional, persona optional). `assigned_to` is the persona NAME
   (not FK) because tickets may be created before the persona row
   exists; start_ticket() validates at execution time.

3. `ticket_dependencies` — many-to-many join. Composite PK
   (ticket_id, depends_on_id), ON DELETE CASCADE on both sides, CHECK
   constraint preventing self-dependency. DAG enforcement is
   application-layer (too expensive to compute in SQL on every insert).

4. `ticket_comments` — append-only comment thread. `author_persona`
   distinguishes human from AI authors. `session_id` is plain String
   not FK (sessions may be deleted independently and the comment
   should survive).

Plus two new columns on `sessions`:
  - `persona_name` (String(50), nullable) — persona active at capture.
  - `ticket_id` (String(64), nullable, indexed) — ticket worked on.

Both are plain String columns (no FK) — the capture pipeline reads
~/.sessionfs/active_ticket.json and tags the session row from the
manifest. The persona/ticket row may not exist yet on the target
server at capture time, and may be hard-deleted later; the session
should carry the tag forward either way (same pattern as the existing
rules_hash / rules_version columns).

Revision ID: 037
Revises: 036
"""

from alembic import op
import sqlalchemy as sa


revision = "037"
down_revision = "036"


def upgrade() -> None:
    # ── agent_personas ────────────────────────────────────────
    op.create_table(
        "agent_personas",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column(
            "project_id",
            sa.String(64),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(50), nullable=False),
        sa.Column("role", sa.String(100), nullable=False),
        sa.Column("content", sa.Text(), nullable=False, server_default=""),
        sa.Column(
            "specializations", sa.Text(), nullable=False, server_default="[]"
        ),
        sa.Column(
            "is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")
        ),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_by", sa.String(64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint("project_id", "name", name="uq_persona_project_name"),
    )
    op.create_index(
        "idx_persona_project_active",
        "agent_personas",
        ["project_id", "is_active"],
    )

    # ── tickets ───────────────────────────────────────────────
    op.create_table(
        "tickets",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column(
            "project_id",
            sa.String(64),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        # Task.
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column(
            "priority", sa.String(20), nullable=False, server_default="medium"
        ),
        sa.Column("assigned_to", sa.String(50), nullable=True),
        # Reporter provenance.
        sa.Column("created_by_user_id", sa.String(64), nullable=False),
        sa.Column("created_by_session_id", sa.String(64), nullable=True),
        sa.Column("created_by_persona", sa.String(50), nullable=True),
        # Status FSM.
        sa.Column(
            "status", sa.String(20), nullable=False, server_default="open"
        ),
        # Context. NOT NULL with server_default='[]' — the ORM model
        # declares Mapped[str] (non-optional) and route code does
        # json.loads() on these fields, so a stray NULL from a raw SQL
        # write would crash Phase 2/3 handlers. Codex Phase 1 Round 1
        # (KB entry 316).
        sa.Column(
            "context_refs", sa.Text(), nullable=False, server_default="[]"
        ),
        sa.Column(
            "file_refs", sa.Text(), nullable=False, server_default="[]"
        ),
        sa.Column(
            "related_sessions", sa.Text(), nullable=False, server_default="[]"
        ),
        sa.Column(
            "acceptance_criteria", sa.Text(), nullable=False, server_default="[]"
        ),
        # Resolution.
        sa.Column("resolver_session_id", sa.String(64), nullable=True),
        sa.Column("resolver_user_id", sa.String(64), nullable=True),
        sa.Column("completion_notes", sa.Text(), nullable=True),
        sa.Column(
            "changed_files", sa.Text(), nullable=False, server_default="[]"
        ),
        sa.Column(
            "knowledge_entry_ids", sa.Text(), nullable=False, server_default="[]"
        ),
        # Timestamps.
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "idx_ticket_project_status",
        "tickets",
        ["project_id", "status"],
    )
    op.create_index(
        "idx_ticket_assigned",
        "tickets",
        ["project_id", "assigned_to", "status"],
    )

    # ── ticket_dependencies ───────────────────────────────────
    op.create_table(
        "ticket_dependencies",
        sa.Column(
            "ticket_id",
            sa.String(64),
            sa.ForeignKey("tickets.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "depends_on_id",
            sa.String(64),
            sa.ForeignKey("tickets.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint("ticket_id != depends_on_id", name="ck_no_self_dep"),
    )
    # Reverse-lookup index for "which tickets depend on X?". The
    # composite PK orders (ticket_id, depends_on_id), so PG can't
    # range-scan it on a depends_on_id-only predicate efficiently.
    # The dependency-enrichment hot path (Phase 3) walks this side on
    # every ticket accept. Codex Phase 1 Round 1 (KB entry 316).
    op.create_index(
        "idx_ticket_deps_depends_on",
        "ticket_dependencies",
        ["depends_on_id"],
    )

    # ── ticket_comments ───────────────────────────────────────
    op.create_table(
        "ticket_comments",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column(
            "ticket_id",
            sa.String(64),
            sa.ForeignKey("tickets.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("author_user_id", sa.String(64), nullable=False),
        sa.Column("author_persona", sa.String(50), nullable=True),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("session_id", sa.String(64), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index("idx_comment_ticket", "ticket_comments", ["ticket_id"])

    # ── sessions.persona_name + sessions.ticket_id ────────────
    op.add_column(
        "sessions",
        sa.Column("persona_name", sa.String(50), nullable=True),
    )
    op.add_column(
        "sessions",
        sa.Column("ticket_id", sa.String(64), nullable=True),
    )
    op.create_index("idx_sessions_ticket_id", "sessions", ["ticket_id"])


def downgrade() -> None:
    op.drop_index("idx_sessions_ticket_id", table_name="sessions")
    op.drop_column("sessions", "ticket_id")
    op.drop_column("sessions", "persona_name")

    op.drop_index("idx_comment_ticket", table_name="ticket_comments")
    op.drop_table("ticket_comments")

    op.drop_index("idx_ticket_deps_depends_on", table_name="ticket_dependencies")
    op.drop_table("ticket_dependencies")

    op.drop_index("idx_ticket_assigned", table_name="tickets")
    op.drop_index("idx_ticket_project_status", table_name="tickets")
    op.drop_table("tickets")

    op.drop_index("idx_persona_project_active", table_name="agent_personas")
    op.drop_table("agent_personas")
