"""Confluence documentation integration (GitHub #99).

Revision ID: 099
Revises: 098

Three tables for one-way SessionFS -> Confluence Cloud page sync:

- ``org_doc_connections``: the org's Confluence connection (site, account
  email, Fernet-encrypted API token, default template, batch window). One row
  per org.
- ``doc_space_mappings``: maps a SessionFS project to a Confluence space key
  + parent page id + optional template override + the context/auto-publish
  toggles. One per project.
- ``doc_page_links``: 1:1 link from a SessionFS source (a wiki page, the
  context root, or a context section) to its Confluence page. Uniqueness is
  (project_id, source_kind, source_key); ``page_id`` + ``version`` snapshot
  the remote page so a drifted page can be detected before the next update.

Also adds ``publish`` to ``knowledge_pages`` so a wiki page can opt in to
being swept to Confluence when its mapping has ``auto_publish_pages``.

Purely additive — no existing rows change. Downgrade drops the column and the
three tables.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "099"
down_revision = "098"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "org_doc_connections",
        sa.Column("org_id", sa.String(length=64), nullable=False),
        sa.Column(
            "provider", sa.String(length=20), nullable=False, server_default="confluence"
        ),
        sa.Column("site_url", sa.String(length=255), nullable=False),
        sa.Column("account_email", sa.String(length=255), nullable=False),
        sa.Column("encrypted_api_token", sa.Text(), nullable=False),
        sa.Column(
            "default_template", sa.String(length=50), nullable=False, server_default="reference"
        ),
        sa.Column("batch_window_minutes", sa.Integer(), nullable=False, server_default="15"),
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
        "doc_space_mappings",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("org_id", sa.String(length=64), nullable=False),
        sa.Column("project_id", sa.String(length=64), nullable=False),
        sa.Column("space_key", sa.String(length=50), nullable=False),
        # Numeric Confluence space id resolved from space_key when the mapping
        # is saved (v2 creates and space-scoped paths take the id, not the key).
        sa.Column("space_id", sa.String(length=64), nullable=True),
        # The Confluence page under which SessionFS pages are created. The
        # route refuses the space root page id, so pages are always children
        # of a real page the admin picked.
        sa.Column("parent_page_id", sa.String(length=64), nullable=False),
        sa.Column("template", sa.String(length=50), nullable=True),
        sa.Column(
            "publish_context", sa.Boolean(), nullable=False, server_default=sa.text("false")
        ),
        sa.Column(
            "auto_publish_pages", sa.Boolean(), nullable=False, server_default=sa.text("false")
        ),
        sa.Column("created_by_user_id", sa.String(length=64), nullable=False),
        # Stamped by the publish sweep before it works a mapping; the sweep
        # orders by it NULLS FIRST so one failing or huge mapping can't starve
        # the others.
        sa.Column("last_attempted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["org_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("project_id", name="uq_doc_space_mapping_project"),
    )
    op.create_index("idx_doc_space_mapping_org", "doc_space_mappings", ["org_id"])
    op.create_index(
        "idx_doc_space_mapping_attempted", "doc_space_mappings", ["last_attempted_at"]
    )

    op.create_table(
        "doc_page_links",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("org_id", sa.String(length=64), nullable=False),
        sa.Column("project_id", sa.String(length=64), nullable=False),
        # ``wiki_page`` (source_key is the page slug), ``context_root``
        # (source_key is the literal ``"root"``) or ``context_section``
        # (source_key is the section slug).
        sa.Column("source_kind", sa.String(length=20), nullable=False),
        sa.Column("source_key", sa.String(length=100), nullable=False),
        # The Confluence site the link was created against. A link whose org
        # or site differs from the current connection is stale (409
        # confluence_link_stale) and is never read or written.
        sa.Column("site_url", sa.String(length=255), nullable=True),
        # The space the page lives in (page URLs are built from it, so a
        # mapping that moved to another space never mislabels old links).
        sa.Column("space_key", sa.String(length=50), nullable=True),
        sa.Column("page_id", sa.String(length=64), nullable=True),
        sa.Column("version", sa.Integer(), nullable=True),
        sa.Column("last_content_hash", sa.String(length=64), nullable=True),
        # Storage body of the last successful publish, kept so a drift 409 can
        # show what changed in Confluence since.
        sa.Column("last_published_storage", sa.Text(), nullable=True),
        sa.Column("template_slug", sa.String(length=50), nullable=True),
        sa.Column("template_version", sa.Integer(), nullable=True),
        # ``linked`` (confirmed page), ``unknown`` (a timeout, lost response or
        # 5xx left the outcome unconfirmed — the next publish may adopt),
        # ``failed`` (the create was definitively refused — the next publish
        # creates, never adopts), ``unlinked`` (severed by an admin resolve,
        # or the page was deleted in Confluence) and ``drifted`` (Confluence
        # was edited since the last push).
        sa.Column(
            "status", sa.String(length=20), nullable=False, server_default="linked"
        ),
        # Fences a publish claim (see migration 091's rationale): a fresh
        # ``secrets.token_hex(16)`` written on claim/reclaim — for creates AND
        # updates of a linked page — and cleared when the publish settles.
        sa.Column("claim_token", sa.String(length=32), nullable=True),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_pushed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_pushed_by_user_id", sa.String(length=64), nullable=True),
        sa.Column("last_push_failed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_push_failure", sa.String(length=300), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("project_id", "source_kind", "source_key", name="uq_doc_page_link_source"),
    )
    op.create_index("idx_doc_page_link_org", "doc_page_links", ["org_id"])
    op.create_index("idx_doc_page_link_status", "doc_page_links", ["status"])
    # One Confluence page is never shared by two SessionFS sources.
    op.create_index(
        "uq_doc_page_link_site_page",
        "doc_page_links",
        ["site_url", "page_id"],
        unique=True,
        postgresql_where=sa.text("page_id IS NOT NULL"),
        sqlite_where=sa.text("page_id IS NOT NULL"),
    )

    op.add_column(
        "knowledge_pages", sa.Column("publish", sa.Boolean(), nullable=False, server_default=sa.text("false"))
    )


def downgrade() -> None:
    op.drop_column("knowledge_pages", "publish")
    op.drop_index("uq_doc_page_link_site_page", table_name="doc_page_links")
    op.drop_index("idx_doc_page_link_status", table_name="doc_page_links")
    op.drop_index("idx_doc_page_link_org", table_name="doc_page_links")
    op.drop_table("doc_page_links")
    op.drop_index("idx_doc_space_mapping_attempted", table_name="doc_space_mappings")
    op.drop_index("idx_doc_space_mapping_org", table_name="doc_space_mappings")
    op.drop_table("doc_space_mappings")
    op.drop_table("org_doc_connections")
