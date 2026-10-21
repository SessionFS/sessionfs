"""Org skills policy: immutable revisions, compilations, devices, reports.

Revision ID: 078
Revises: 077

Marketplace v2 phase 3b (docs/design/marketplace-v2.md §8.5).

- org_skill_policies: one immutable row per policy revision of an org
  (UNIQUE(org_id, revision)). A PUT creates a new revision; nothing ever
  updates a revision in place. policy_hash is "sha256:" + hex of the
  canonical policy JSON.
- org_policy_compilations: the compiled per-tool output of a revision
  (artifact hash plus the enforceable / report_only / unsupported field
  lists), one row per (policy, tool, compiler_version).
- skill_devices: a user's device as seen by the advisory reconcile path
  (UNIQUE(user_id, device_key)). device_key is the device id the client
  reports (SESSIONFS_DEVICE_ID or a locally generated id).
- device_policy_reports: the LATEST compliance report of each device for
  each org (UNIQUE(device_id, org_id); replaced on every report).
  Advisory telemetry only; never an access-control input.
- device_policy_report_history: at most one report per device, org and
  UTC day (UNIQUE(device_id, org_id, report_day)), retained 90 days.

Backfill: an organizations.settings "skills" block (v0.16 §7.4 shape) whose
mode is one of the four known modes is copied, unchanged, into revision 1
for that org. The settings block itself is left in place (one release as a
pointer; nothing reads it any more). Malformed blocks are skipped and
listed in the migration log.

Signing keys for MDM bundles (§8.5 org_policy_signing_keys) are not created
here: signed bundles are a follow-up.

Downgrade drops the five tables.
"""

from __future__ import annotations

import hashlib
import json
import secrets

import sqlalchemy as sa
from alembic import op

revision = "078"
down_revision = "077"
branch_labels = None
depends_on = None

_MODES = {"open", "allowlist", "library_only", "disabled"}


def _backfill() -> None:
    bind = op.get_bind()
    rows = bind.execute(sa.text("SELECT id, settings FROM organizations")).fetchall()
    skipped: list[str] = []
    for org_id, raw in rows:
        try:
            settings = json.loads(raw or "{}")
        except (TypeError, ValueError):
            continue
        if not isinstance(settings, dict) or "skills" not in settings:
            continue
        block = settings.get("skills")
        if not isinstance(block, dict) or block.get("mode") not in _MODES:
            skipped.append(str(org_id))
            continue
        existing = bind.execute(
            sa.text("SELECT 1 FROM org_skill_policies WHERE org_id = :org"), {"org": org_id}
        ).first()
        if existing is not None:
            continue
        policy = {"skills": block}
        canonical = json.dumps(policy, sort_keys=True, separators=(",", ":"))
        bind.execute(
            sa.text(
                "INSERT INTO org_skill_policies "
                "(id, org_id, revision, policy_json, policy_hash, created_by_user_id, created_at) "
                "VALUES (:id, :org, 1, :policy, :hash, NULL, CURRENT_TIMESTAMP)"
            ),
            {
                "id": f"osp_{secrets.token_hex(12)}",
                "org": org_id,
                "policy": canonical,
                "hash": "sha256:" + hashlib.sha256(canonical.encode()).hexdigest(),
            },
        )
    if skipped:
        print(  # noqa: T201 - migration log
            "078: skipped malformed organizations.settings skills blocks for orgs: "
            + ", ".join(sorted(skipped))
        )


def upgrade() -> None:
    op.create_table(
        "org_skill_policies",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column(
            "org_id",
            sa.String(64),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("policy_json", sa.Text(), nullable=False),
        sa.Column("policy_hash", sa.String(71), nullable=False),
        sa.Column(
            "created_by_user_id",
            sa.String(36),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint("org_id", "revision", name="uq_org_skill_policy_revision"),
        sa.CheckConstraint("revision >= 1", name="ck_org_skill_policy_revision"),
    )
    op.create_table(
        "org_policy_compilations",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column(
            "policy_id",
            sa.String(64),
            sa.ForeignKey("org_skill_policies.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("tool", sa.String(50), nullable=False),
        sa.Column("compiler_version", sa.String(20), nullable=False),
        sa.Column("artifact_hash", sa.String(71), nullable=False),
        sa.Column("enforceable", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("report_only", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("unsupported", sa.Text(), nullable=False, server_default="[]"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint(
            "policy_id", "tool", "compiler_version", name="uq_org_policy_compilation"
        ),
    )
    op.create_table(
        "skill_devices",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column(
            "user_id",
            sa.String(36),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("device_key", sa.String(100), nullable=False),
        sa.Column("display_name", sa.String(200), nullable=False, server_default=""),
        sa.Column("platform", sa.String(30), nullable=False, server_default=""),
        sa.Column(
            "first_seen_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "last_seen_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint("user_id", "device_key", name="uq_skill_device_user_key"),
    )
    op.create_table(
        "device_policy_reports",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column(
            "device_id",
            sa.String(64),
            sa.ForeignKey("skill_devices.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "org_id",
            sa.String(64),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "policy_id",
            sa.String(64),
            sa.ForeignKey("org_skill_policies.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("policy_revision", sa.Integer(), nullable=True),
        sa.Column(
            "reported_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("report_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("compliant", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.UniqueConstraint("device_id", "org_id", name="uq_device_policy_report_device_org"),
    )
    op.create_index(
        "idx_device_policy_reports_org_time", "device_policy_reports", ["org_id", "reported_at"]
    )
    op.create_table(
        "device_policy_report_history",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column(
            "device_id",
            sa.String(64),
            sa.ForeignKey("skill_devices.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "org_id",
            sa.String(64),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("policy_revision", sa.Integer(), nullable=True),
        sa.Column("report_day", sa.String(10), nullable=False),
        sa.Column(
            "reported_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("report_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("compliant", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.UniqueConstraint(
            "device_id", "org_id", "report_day", name="uq_device_policy_report_history_day"
        ),
    )
    op.create_index(
        "idx_device_policy_report_history_time", "device_policy_report_history", ["reported_at"]
    )
    _backfill()


def downgrade() -> None:
    op.drop_index(
        "idx_device_policy_report_history_time", table_name="device_policy_report_history"
    )
    op.drop_table("device_policy_report_history")
    op.drop_index("idx_device_policy_reports_org_time", table_name="device_policy_reports")
    op.drop_table("device_policy_reports")
    op.drop_table("skill_devices")
    op.drop_table("org_policy_compilations")
    op.drop_table("org_skill_policies")
