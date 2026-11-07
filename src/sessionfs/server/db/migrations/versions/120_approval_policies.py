"""Approval policies: policies, requests and decisions (GitHub #123, phase A1).

Revision ID: 120
Revises: 119

- ``approval_policies``: immutable policy versions. An edit inserts a new row
  (``version`` + 1, ``supersedes_id`` = the old row, same ``lineage_id``) and
  deactivates the old one, so a request always points at the exact version
  that created it. ``lineage_id`` is the id of version 1 and groups every
  version of one policy. ``filter`` and ``requirements`` are JSON text
  validated by ``services/approvals.py``; the filter is capped at 8 KiB.
- ``approval_requests``: one per (policy version, target, fingerprint). For a
  merge the fingerprint is the full head sha; for KB promotion it is the
  entry content sha256. A partial unique index over PENDING rows makes
  concurrent readiness reads insert-or-load the same row, while a rejected,
  expired or superseded request can be raised again beside the old one.
- ``approval_decisions``: one per (request, approver). ``reason`` is
  sanitised and DLP-redacted before storage (<= 2000 chars). The approver
  FK is ON DELETE SET NULL: a deleted user's decision keeps counting.
- ``knowledge_entries.approval_pending`` (A2): a note waiting on a blocking
  kb_promote approval; ``pending_supersede_ids``: the entries an upsert held
  for approval will supersede once it is promoted.

Org/project ids carry no FK beyond the org (CASCADE): the target is
re-authorised on every read.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "120"
down_revision = "119"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "approval_policies",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column(
            "org_id",
            sa.String(64),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("lineage_id", sa.String(64), nullable=False),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("target_kind", sa.String(20), nullable=False),
        sa.Column("filter", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("requirements", sa.Text(), nullable=False),
        sa.Column("blocking", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column(
            "supersedes_id",
            sa.String(64),
            sa.ForeignKey("approval_policies.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "created_by_user_id",
            sa.String(36),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("deactivated_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "target_kind IN ('merge', 'kb_promote')",
            name="ck_approval_policies_target_kind",
        ),
        sa.CheckConstraint("version >= 1", name="ck_approval_policies_version"),
        sa.CheckConstraint(
            "length(filter) <= 8192", name="ck_approval_policies_filter_len"
        ),
        sa.CheckConstraint("length(name) <= 120", name="ck_approval_policies_name_len"),
        sa.Index("idx_approval_policies_org_active", "org_id", "active", "target_kind"),
        sa.Index("idx_approval_policies_lineage", "lineage_id", "version"),
    )
    op.create_table(
        "approval_requests",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column(
            "policy_id",
            sa.String(64),
            sa.ForeignKey("approval_policies.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "org_id",
            sa.String(64),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("project_id", sa.String(64), nullable=False),
        sa.Column("target_kind", sa.String(20), nullable=False),
        sa.Column("target_id", sa.String(64), nullable=False),
        sa.Column("target_fingerprint", sa.String(64), nullable=False),
        sa.Column("state", sa.String(12), nullable=False, server_default="pending"),
        sa.Column(
            "requested_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "target_kind IN ('merge', 'kb_promote')",
            name="ck_approval_requests_target_kind",
        ),
        sa.CheckConstraint(
            "state IN ('pending', 'approved', 'rejected', 'expired', 'superseded')",
            name="ck_approval_requests_state",
        ),
        sa.Index(
            "idx_approval_requests_target",
            "target_kind", "target_id", "target_fingerprint",
        ),
        sa.Index("idx_approval_requests_org_state", "org_id", "state"),
    )
    # One PENDING request per key; rejected/expired/superseded rows stay as
    # history and a new request can be raised beside them.
    op.create_index(
        "uq_approval_requests_pending_target",
        "approval_requests",
        ["policy_id", "target_kind", "target_id", "target_fingerprint"],
        unique=True,
        postgresql_where=sa.text("state = 'pending'"),
        sqlite_where=sa.text("state = 'pending'"),
    )
    op.create_table(
        "approval_decisions",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column(
            "request_id",
            sa.String(64),
            sa.ForeignKey("approval_requests.id", ondelete="CASCADE"),
            nullable=False,
        ),
        # SET NULL: a deleted approver's decision stays and still counts
        # toward quorum ("former member").
        sa.Column(
            "approver_user_id",
            sa.String(36),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("requirement_index", sa.Integer(), nullable=False),
        sa.Column("decision", sa.String(10), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False, server_default=""),
        sa.Column(
            "decided_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint(
            "request_id", "approver_user_id", name="uq_approval_decisions_request_approver"
        ),
        sa.CheckConstraint(
            "decision IN ('approve', 'reject')", name="ck_approval_decisions_decision"
        ),
        sa.CheckConstraint(
            "length(reason) <= 2000", name="ck_approval_decisions_reason_len"
        ),
        sa.CheckConstraint(
            "requirement_index >= 0", name="ck_approval_decisions_requirement_index"
        ),
        sa.Index("idx_approval_decisions_approver", "approver_user_id"),
    )
    # Phase A2: a note held back from promotion by a blocking kb_promote
    # policy. Compile, auto-promotion and bulk promote leave it alone until
    # its request is approved.
    with op.batch_alter_table("knowledge_entries") as batch:
        batch.add_column(
            sa.Column(
                "approval_pending", sa.Boolean(), nullable=False, server_default=sa.false()
            )
        )
        # An entity_ref upsert held for approval supersedes these ids only
        # once it is actually promoted.
        batch.add_column(sa.Column("pending_supersede_ids", sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("knowledge_entries") as batch:
        batch.drop_column("pending_supersede_ids")
        batch.drop_column("approval_pending")
    op.drop_table("approval_decisions")
    op.drop_table("approval_requests")
    op.drop_table("approval_policies")
