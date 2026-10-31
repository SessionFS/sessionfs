"""Loop share links (Review Loop R3, Brief C3).

Revision ID: 115
Revises: 112

A project-scoped, revocable public link to a loop artifact. ``kind`` is
``story`` (one ticket's ordered event stream, rendered through the existing
``ticket_story`` builder) or ``ship_report`` (a release/month rollup built by
the F-track once its ``releases`` tables land — until then the public view
404s that kind). The raw bearer token is never stored: only its SHA-256
``token_hash`` is persisted, so a DB leak can't mint working links, and the
raw token is returned exactly once at creation.

- ``ticket_id`` is set for ``story`` and NULL for ``ship_report`` (which
  carries ``release_version``/``month`` instead). It is an FK that softens to
  NULL when the ticket goes, so a deleted ticket makes the link 404 rather
  than dangle.
- ``expires_at`` is always set: the route defaults to 30 days and rejects
  anything over 90 (``expires_days`` is capped at 90).
- ``revoked_at`` records a soft revoke (a guarded UPDATE, review checklist
  #11); the token stays hashed and the row stays for audit.

Inline CHECK constraint (declared at create_table) is SQLite-safe — same
pattern as migrations 050–054 and 112. Downgrade drops the indexes then the
table.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "115"
down_revision = "114"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "loop_share_links",
        sa.Column("id", sa.String(length=64), primary_key=True),  # lsl_<hex>
        sa.Column(
            "project_id",
            sa.String(length=64),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "kind",
            sa.String(length=20),
            nullable=False,
        ),
        # story → the ticket whose event stream is shared; NULL for ship_report.
        sa.Column(
            "ticket_id",
            sa.String(length=64),
            sa.ForeignKey("tickets.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("release_version", sa.String(length=100), nullable=True),
        sa.Column("month", sa.String(length=7), nullable=True),
        # SHA-256 hex of the raw token; the raw token is returned once and
        # never stored (so a DB leak can't mint working links).
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "created_by_user_id",
            sa.String(length=64),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        # Actor provenance (review checklist #3): a service key acting as its
        # minter leaves user_id NULL and records its key id/name instead.
        sa.Column(
            "created_actor_type",
            sa.String(length=20),
            nullable=False,
            server_default="user",
        ),
        sa.Column("created_service_key_id", sa.String(length=36), nullable=True),
        sa.Column("created_service_key_name", sa.String(length=100), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "revoked_by_user_id",
            sa.String(length=64),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("revoked_actor_type", sa.String(length=20), nullable=True),
        sa.Column("revoked_service_key_id", sa.String(length=36), nullable=True),
        sa.Column("revoked_service_key_name", sa.String(length=100), nullable=True),
        sa.CheckConstraint(
            "kind IN ('story', 'ship_report')",
            name="ck_loop_share_links_kind",
        ),
        sa.UniqueConstraint("token_hash", name="uq_loop_share_links_token_hash"),
    )
    op.create_index(
        "idx_loop_share_links_project",
        "loop_share_links",
        ["project_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "idx_loop_share_links_project", table_name="loop_share_links"
    )
    op.drop_table("loop_share_links")
