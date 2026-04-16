"""Rules portability + session instruction provenance.

Adds:
- project_rules: canonical project rules (one row per project)
- rules_versions: immutable compiled-output history
- sessions.rules_version, rules_hash, rules_source, instruction_artifacts

Uses JSON (not JSONB) and string IDs for SQLite compatibility.

Revision ID: 028
Revises: 027
"""

from alembic import op
import sqlalchemy as sa

revision = "028"
down_revision = "027"


def upgrade() -> None:
    op.create_table(
        "project_rules",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column(
            "project_id",
            sa.String(64),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
        ),
        sa.Column("version", sa.Integer, nullable=False, server_default="1"),
        sa.Column("static_rules", sa.Text, nullable=False, server_default=""),
        sa.Column(
            "include_knowledge",
            sa.Boolean,
            nullable=False,
            server_default=sa.text("true"),
        ),
        # JSON array of knowledge types to inject (defaults: convention, decision)
        sa.Column(
            "knowledge_types",
            sa.Text,
            nullable=False,
            server_default='["convention", "decision"]',
        ),
        sa.Column(
            "knowledge_max_tokens",
            sa.Integer,
            nullable=False,
            server_default="1500",
        ),
        sa.Column(
            "include_context",
            sa.Boolean,
            nullable=False,
            server_default=sa.text("true"),
        ),
        # JSON array of section slugs (defaults: overview, architecture)
        sa.Column(
            "context_sections",
            sa.Text,
            nullable=False,
            server_default='["overview", "architecture"]',
        ),
        sa.Column(
            "context_max_tokens",
            sa.Integer,
            nullable=False,
            server_default="1500",
        ),
        # JSON object: per-tool overrides/extra text, e.g.
        #   {"claude-code": {"extra": "…"}, "cursor": {"extra": "…"}}
        sa.Column(
            "tool_overrides",
            sa.Text,
            nullable=False,
            server_default="{}",
        ),
        # JSON array of enabled tool slugs
        sa.Column(
            "enabled_tools",
            sa.Text,
            nullable=False,
            server_default="[]",
        ),
        sa.Column(
            "created_by",
            sa.String(36),
            sa.ForeignKey("users.id"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index("idx_project_rules_project", "project_rules", ["project_id"])

    op.create_table(
        "rules_versions",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column(
            "rules_id",
            sa.String(64),
            sa.ForeignKey("project_rules.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("version", sa.Integer, nullable=False),
        sa.Column("static_rules", sa.Text, nullable=False, server_default=""),
        # JSON object: {tool_slug: {"filename": str, "content": str, "hash": str,
        # "token_count": int}}
        sa.Column(
            "compiled_outputs",
            sa.Text,
            nullable=False,
            server_default="{}",
        ),
        # JSON array snapshot of knowledge entry summaries at compile time
        sa.Column(
            "knowledge_snapshot",
            sa.Text,
            nullable=False,
            server_default="[]",
        ),
        # JSON object snapshot of project context doc sections at compile time
        sa.Column(
            "context_snapshot",
            sa.Text,
            nullable=False,
            server_default="{}",
        ),
        sa.Column(
            "compiled_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "compiled_by",
            sa.String(36),
            sa.ForeignKey("users.id"),
            nullable=False,
        ),
        # Hash over all compiled_outputs — used for no-op detection
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.UniqueConstraint("rules_id", "version", name="uq_rules_versions_rid_ver"),
    )
    op.create_index(
        "idx_rules_versions_rules_id",
        "rules_versions",
        ["rules_id", "version"],
    )

    # sessions additions — instruction provenance
    op.add_column("sessions", sa.Column("rules_version", sa.Integer, nullable=True))
    op.add_column("sessions", sa.Column("rules_hash", sa.String(80), nullable=True))
    op.add_column(
        "sessions",
        sa.Column(
            "rules_source",
            sa.String(16),
            nullable=False,
            server_default="none",
        ),
    )
    # JSON array of instruction artifact descriptors
    op.add_column(
        "sessions",
        sa.Column(
            "instruction_artifacts",
            sa.Text,
            nullable=False,
            server_default="[]",
        ),
    )


def downgrade() -> None:
    op.drop_column("sessions", "instruction_artifacts")
    op.drop_column("sessions", "rules_source")
    op.drop_column("sessions", "rules_hash")
    op.drop_column("sessions", "rules_version")
    op.drop_index("idx_rules_versions_rules_id", table_name="rules_versions")
    op.drop_table("rules_versions")
    op.drop_index("idx_project_rules_project", table_name="project_rules")
    op.drop_table("project_rules")
