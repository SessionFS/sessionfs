"""Jira sprint + parent/epic mapping (GitHub #113, C7/C8).

Revision ID: 103
Revises: 102

Additive only.

- ``org_tracker_connections.sprint_field_id`` / ``epic_link_field_id``: the
  per-connection custom-field ids used to read/write sprint and epic link.
  Discovered from ``GET /rest/api/3/field`` and overridable on connection save;
  nullable because the fields may not exist on the site (notably epic-link on
  Jira Cloud).
- ``ticket_tracker_links.jira_sprint_id`` / ``jira_sprint_name`` /
  ``jira_sprint_state``: the cached sprint for a linked issue, refreshed on
  pull and inbound sprint changelogs (no ticket mutation).
- ``ticket_tracker_links.jira_parent_key``: the parent/epic issue key read
  from ``fields.parent.key`` (or the epic-link field), surfaced on link GET.
- ``ticket_tracker_links.jira_project_key``: the destination Jira project key
  the issue was published into, so a timed-out create's adopt retry searches
  the same project by label.
- ``ticket_tracker_links.unknown_since``: the durable timestamp of the first
  publish attempt whose remote outcome is uncertain (a timed-out create, or a
  dead worker's stale claim). Set once and never overwritten while the row
  stays unknown/pending-adopt; cleared when the row becomes linked. Kept
  separate from ``last_pushed_at`` (the claim-staleness clock) so a failed
  adopt retry never erases the original cut-off.

No indexes: these are read per-link/per-connection via the existing PKs, never
scanned. Downgrade drops the eight columns.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "103"
down_revision = "102"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "org_tracker_connections", sa.Column("sprint_field_id", sa.String(length=64), nullable=True)
    )
    op.add_column(
        "org_tracker_connections",
        sa.Column("epic_link_field_id", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "ticket_tracker_links", sa.Column("jira_sprint_id", sa.String(length=32), nullable=True)
    )
    op.add_column(
        "ticket_tracker_links", sa.Column("jira_sprint_name", sa.String(length=200), nullable=True)
    )
    op.add_column(
        "ticket_tracker_links", sa.Column("jira_sprint_state", sa.String(length=20), nullable=True)
    )
    op.add_column(
        "ticket_tracker_links", sa.Column("jira_parent_key", sa.String(length=50), nullable=True)
    )
    op.add_column(
        "ticket_tracker_links", sa.Column("jira_project_key", sa.String(length=50), nullable=True)
    )
    op.add_column(
        "ticket_tracker_links", sa.Column("unknown_since", sa.DateTime(timezone=True), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("ticket_tracker_links", "unknown_since")
    op.drop_column("ticket_tracker_links", "jira_project_key")
    op.drop_column("ticket_tracker_links", "jira_parent_key")
    op.drop_column("ticket_tracker_links", "jira_sprint_state")
    op.drop_column("ticket_tracker_links", "jira_sprint_name")
    op.drop_column("ticket_tracker_links", "jira_sprint_id")
    op.drop_column("org_tracker_connections", "epic_link_field_id")
    op.drop_column("org_tracker_connections", "sprint_field_id")
