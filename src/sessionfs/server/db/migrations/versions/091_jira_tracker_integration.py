"""Jira tracker integration (GitHub #98).

Revision ID: 091
Revises: 090

Three tables for one-way SessionFS -> Jira Cloud promotion:

- ``org_tracker_connections``: the org's Jira connection (site, account
  email, Fernet-encrypted API token, default template). One row per org.
- ``tracker_project_mappings``: maps a SessionFS project to a Jira
  project key + issue type + optional template override. One per project.
- ``ticket_tracker_links``: strict 1:1 link from a promoted ticket to its
  Jira issue (ticket_id is the PK and FK CASCADE).

Purely additive — no existing rows change. Downgrade drops the tables.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "091"
down_revision = "090"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "org_tracker_connections",
        sa.Column("org_id", sa.String(length=64), nullable=False),
        sa.Column(
            "provider", sa.String(length=20), nullable=False, server_default="jira"
        ),
        sa.Column("site_url", sa.String(length=255), nullable=False),
        sa.Column("account_email", sa.String(length=255), nullable=False),
        sa.Column("encrypted_api_token", sa.Text(), nullable=False),
        sa.Column(
            "default_template", sa.String(length=50), nullable=False, server_default="agile"
        ),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("connected_by_user_id", sa.String(length=64), nullable=False),
        sa.Column("last_verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["org_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("org_id"),
    )

    op.create_table(
        "tracker_project_mappings",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("org_id", sa.String(length=64), nullable=False),
        sa.Column("project_id", sa.String(length=64), nullable=False),
        sa.Column("jira_project_key", sa.String(length=50), nullable=False),
        sa.Column("issue_type", sa.String(length=50), nullable=False, server_default="Story"),
        sa.Column("template", sa.String(length=50), nullable=True),
        sa.Column("done_status_name", sa.String(length=50), nullable=True),
        sa.Column("created_by_user_id", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["org_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("project_id", name="uq_tracker_mapping_project"),
    )
    op.create_index("idx_tracker_mapping_org", "tracker_project_mappings", ["org_id"])

    op.create_table(
        "ticket_tracker_links",
        sa.Column("ticket_id", sa.String(length=64), nullable=False),
        sa.Column("org_id", sa.String(length=64), nullable=False),
        sa.Column(
            "provider", sa.String(length=20), nullable=False, server_default="jira"
        ),
        sa.Column(
            "status", sa.String(length=20), nullable=False, server_default="linked"
        ),
        # Fences a concurrent publish claim: a fresh ``secrets.token_hex(16)`` is
        # written when the pending row is claimed (or reclaimed), and every later
        # write by that request is fenced on this token so a worker whose claim
        # was reclaimed can't overwrite the new owner's link.
        sa.Column("claim_token", sa.String(length=32), nullable=True),
        # ``site_url`` snapshots the connection's site at publish time. A link
        # whose org/site no longer match the current connection is stale (the
        # org/site check in the routes raises 409 ``jira_link_stale``) — a
        # project transfer or a connection re-point only leaves the old link in
        # place; it never writes to the unrelated issue on the new site.
        sa.Column("site_url", sa.String(length=255), nullable=True),
        # The remote issue's numeric ``id`` (not its key), so a stale link can
        # be inspected without trusting the issue key as an identifier.
        sa.Column("issue_id", sa.String(length=50), nullable=True),
        sa.Column("issue_key", sa.String(length=50), nullable=True),
        sa.Column("issue_url", sa.String(length=500), nullable=True),
        sa.Column("last_pushed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_pushed_by_user_id", sa.String(length=64), nullable=True),
        sa.Column("last_known_status", sa.String(length=50), nullable=True),
        sa.Column("last_status_checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("payload_hash", sa.String(length=64), nullable=True),
        # Re-publish diff: the full summary + description text that was last
        # pushed, so the preview can show a unified diff against the new draft.
        sa.Column("last_published_text", sa.Text(), nullable=True),
        # Health: the last push failure (time + sanitized reason) is persisted
        # here so /health can report real failures rather than an empty list.
        sa.Column("last_push_failed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_push_failure", sa.String(length=300), nullable=True),
        sa.ForeignKeyConstraint(["ticket_id"], ["tickets.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("ticket_id"),
    )
    op.create_index("idx_ticket_tracker_link_org", "ticket_tracker_links", ["org_id"])
    op.create_index(
        "idx_ticket_tracker_link_status_checked",
        "ticket_tracker_links",
        ["last_status_checked_at"],
    )


def downgrade() -> None:
    op.drop_table("ticket_tracker_links")
    op.drop_table("tracker_project_mappings")
    op.drop_table("org_tracker_connections")
