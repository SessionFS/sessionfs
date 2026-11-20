"""Review requests and the personal runners that serve them (P1a).

Revision ID: 139
Revises: 138

Two new tables for the auto-reviewer's server half (tk_9078a0e3feaa4e8a):

``review_requests`` — a stored, typed record of "review this target for me".
One live row per ``(project_id, requester_user_id, provider, repo, head_sha,
profile, instructions_version, mode, COALESCE(pr_number, 0))``: pressing Review
twice at the same commit returns the same row, and two distinct PRs with the
same head sha are distinct targets. The target is a provider repo + optional
PR/MR number + exact head sha, with an optional ticket. ``mode`` is kept for
the later ``org_reviewer`` path but P1 accepts only ``draft_for_me``. Findings,
coverage and the packet manifest are JSON-as-Text for cross-DB parity (the
same pattern as ``agent_runs.findings``); code bytes never reach SessionFS, so
only metadata and DLP-scanned finding text is stored here. The reviewer
persona content is pinned at create time in ``instructions_snapshot`` (with a
sha256 in ``instructions_sha256``) so the packet serves the exact instructions
the request was created against, independent of later persona edits.

``review_runners`` — a customer-controlled process that pulls work over HTTP.
P1 runners authenticate with their owner's own user API key and serve only
that owner's ``draft_for_me`` requests. The partial unique index on
``owner_user_id`` where ``status = 'active'`` enforces one active runner per
owner; the Community edition caps the whole installation at one via a route
check (``runner_limit`` 409). ``project_ids`` bounds the runner's work to the
projects its owner listed at enrolment.

New tables only; nothing earlier creates these names.

"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "139"
down_revision = "138"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "review_runners",
        sa.Column("id", sa.String(64), nullable=False),
        sa.Column("owner_user_id", sa.String(64), nullable=False),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("kind", sa.String(20), nullable=False, server_default="local"),
        sa.Column("project_ids", sa.Text(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="active"),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column("retired_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_review_runners"),
        sa.ForeignKeyConstraint(
            ["owner_user_id"],
            ["users.id"],
            name="fk_review_runners_owner_user_id",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(
            "kind IN ('local', 'ci')", name="ck_review_runners_kind"
        ),
        sa.CheckConstraint(
            "status IN ('active', 'retired')", name="ck_review_runners_status"
        ),
    )
    op.create_index("idx_review_runners_owner", "review_runners", ["owner_user_id"])
    op.create_index(
        "uq_review_runners_owner_active",
        "review_runners",
        ["owner_user_id"],
        unique=True,
        postgresql_where=sa.text("status = 'active'"),
        sqlite_where=sa.text("status = 'active'"),
    )

    op.create_table(
        "review_requests",
        sa.Column("id", sa.String(64), nullable=False),
        sa.Column("project_id", sa.String(64), nullable=False),
        sa.Column("requester_user_id", sa.String(64), nullable=False),
        sa.Column("ticket_id", sa.String(64), nullable=True),
        sa.Column("provider", sa.String(20), nullable=False),
        sa.Column("repo", sa.String(200), nullable=False),
        sa.Column("pr_number", sa.Integer(), nullable=True),
        sa.Column("head_sha", sa.String(64), nullable=False),
        sa.Column("base_sha", sa.String(64), nullable=True),
        sa.Column("profile", sa.String(50), nullable=False),
        sa.Column(
            "instructions_version", sa.Integer(), nullable=False, server_default="1"
        ),
        sa.Column("instructions_snapshot", sa.Text(), nullable=True),
        sa.Column("instructions_sha256", sa.String(64), nullable=True),
        sa.Column(
            "mode", sa.String(20), nullable=False, server_default="draft_for_me"
        ),
        sa.Column("budget_tokens", sa.BigInteger(), nullable=True),
        sa.Column("budget_seconds", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(20), nullable=False, server_default="queued"),
        sa.Column("lease_epoch", sa.Integer(), nullable=True),
        sa.Column("lease_runner_id", sa.String(64), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("superseded_by_id", sa.String(64), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("verdict", sa.String(20), nullable=True),
        sa.Column("findings", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("findings_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("coverage", sa.Text(), nullable=True),
        sa.Column("packet_manifest", sa.Text(), nullable=True),
        sa.Column("input_tokens", sa.BigInteger(), nullable=True),
        sa.Column("output_tokens", sa.BigInteger(), nullable=True),
        sa.Column("cache_read_tokens", sa.BigInteger(), nullable=True),
        sa.Column("cache_write_tokens", sa.BigInteger(), nullable=True),
        sa.Column("cost_usd", sa.Numeric(12, 6), nullable=True),
        sa.Column("cost_basis", sa.String(12), nullable=True),
        sa.Column("error", sa.String(200), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.PrimaryKeyConstraint("id", name="pk_review_requests"),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            name="fk_review_requests_project_id",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["requester_user_id"],
            ["users.id"],
            name="fk_review_requests_requester_user_id",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(
            "provider IN ('github', 'gitlab')",
            name="ck_review_requests_provider",
        ),
        sa.CheckConstraint(
            "mode IN ('draft_for_me', 'org_reviewer')",
            name="ck_review_requests_mode",
        ),
        sa.CheckConstraint(
            "status IN ('queued', 'claimed', 'done', 'failed', "
            "'expired', 'superseded')",
            name="ck_review_requests_status",
        ),
        sa.CheckConstraint(
            "verdict IS NULL OR verdict IN ('clean', 'findings', 'incomplete')",
            name="ck_review_requests_verdict",
        ),
        sa.CheckConstraint(
            "cost_basis IS NULL OR cost_basis IN "
            "('reported', 'estimated', 'plan', 'unknown')",
            name="ck_review_requests_cost_basis",
        ),
    )
    op.create_index(
        "idx_review_requests_requester", "review_requests", ["requester_user_id"]
    )
    op.create_index(
        "idx_review_requests_project_status", "review_requests", ["project_id", "status"]
    )
    op.create_index(
        "idx_review_requests_claim",
        "review_requests",
        ["status", "lease_expires_at"],
    )
    op.create_index(
        "uq_review_requests_idempotent",
        "review_requests",
        [
            "project_id",
            "requester_user_id",
            "provider",
            "repo",
            "head_sha",
            "profile",
            "instructions_version",
            "mode",
            sa.text("COALESCE(pr_number, 0)"),
        ],
        unique=True,
        postgresql_where=sa.text("status <> 'superseded'"),
        sqlite_where=sa.text("status <> 'superseded'"),
    )


def downgrade() -> None:
    op.drop_index("uq_review_requests_idempotent", table_name="review_requests")
    op.drop_index("idx_review_requests_claim", table_name="review_requests")
    op.drop_index(
        "idx_review_requests_project_status", table_name="review_requests"
    )
    op.drop_index("idx_review_requests_requester", table_name="review_requests")
    op.drop_table("review_requests")
    op.drop_index("uq_review_runners_owner_active", table_name="review_runners")
    op.drop_index("idx_review_runners_owner", table_name="review_runners")
    op.drop_table("review_runners")
