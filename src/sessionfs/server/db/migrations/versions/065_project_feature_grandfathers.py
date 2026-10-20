"""Grandfather existing usage of project-scoped features.

Revision ID: 065
Revises: 064

Project-scoped features (project_context, agent_personas, agent_tickets,
agent_runs) are now gated on the plan of the project's owner instead of
the caller's own plan. So that no one loses access they already rely on,
this migration records every project that already uses each feature;
for those projects a caller whose own plan includes the feature keeps it.

- project_context: every existing project (creating one required it).
- agent_personas: projects with at least one persona.
- agent_tickets:  projects with at least one ticket.
- agent_runs:     projects with at least one agent run or feedback record
                  (agent feedback is gated as agent_runs).
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "065"
down_revision = "064"
branch_labels = None
depends_on = None


_SNAPSHOTS = (
    ("project_context", "SELECT id AS project_id FROM projects"),
    ("agent_personas", "SELECT DISTINCT project_id FROM agent_personas"),
    ("agent_tickets", "SELECT DISTINCT project_id FROM tickets"),
    (
        "agent_runs",
        "SELECT project_id FROM agent_runs "
        "UNION SELECT project_id FROM agent_feedback",
    ),
)


def upgrade() -> None:
    op.create_table(
        "project_feature_grandfathers",
        sa.Column(
            "project_id",
            sa.String(length=64),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("feature", sa.String(length=64), primary_key=True),
        sa.Column(
            "granted_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    for feature, source in _SNAPSHOTS:
        op.execute(
            sa.text(
                "INSERT INTO project_feature_grandfathers (project_id, feature) "
                f"SELECT src.project_id, '{feature}' FROM ({source}) AS src "
                "WHERE src.project_id IS NOT NULL"
            )
        )


def downgrade() -> None:
    op.drop_table("project_feature_grandfathers")
