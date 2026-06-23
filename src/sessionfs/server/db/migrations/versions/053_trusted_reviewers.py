"""Trusted-reviewer provenance: verdict_trusted column + trusted_reviewers table.

Revision ID: 053
Revises: 052

tk_d42170b4670f4448 — close the review-verdict spoofing weakness
(docs/security/review-verdict-provenance.md). `TicketComment.author_persona`
is caller-supplied request-body data, yet `compute_review_state` historically
treated any comment authored as 'codex-reviewer' with a VERIFIED-CLEAN header
as an authoritative review round. Any tickets:write caller could forge a clean
review state. This migration introduces a server-stamped trust marker.

Strictly additive (down_revision='052'):
  1. ticket_comments.verdict_trusted BOOLEAN NOT NULL DEFAULT false
     — records the server's trust decision at write time. Fail-closed:
       every existing row is non-authoritative unless explicitly backfilled.
  2. trusted_reviewers — the registry binding an authenticated identity
     (user_id and/or service_key_id) to the reviewer persona it may speak as,
     scoped to a project OR org-wide. Inline CheckConstraints at create_table
     (SQLite-safe, same pattern as migrations 050–052). App-assigned PK
     ('tr_<hex>'), no lastrowid.
  3. Seed + identity-grounded backfill for the known operator reviewer
     (user_id='f973f29e-6da1-483e-b9f3-2851a90bf3c9').

Seed-scope decision (per design §5 + ticket): the operator's historical
'codex-reviewer' comments must remain authoritative after the cut-over.
Rather than guess whether each of the operator's projects is org-scoped or
personal, the seed is DERIVED FROM THE ACTUAL DATA: for every distinct project
on which the operator has historically posted a 'codex-reviewer' comment, we
seed a trusted_reviewers row scoped to that project's ORG if the project is
org-scoped (one org-wide row covers all of the org's projects — the simplest
shape for a single shared reviewer), otherwise scoped to that personal project
directly. This is identity-grounded (it trusts the operator user_id we
explicitly register, never the old 'codex-reviewer' string) and exactly
matches the backfill set below. Comments authored as 'codex-reviewer' by ANY
OTHER user_id stay verdict_trusted=false — if such rows exist they were forged
or out-of-band, and excluding them is the correct, secure outcome (logged via
print(), per the migration-050 diagnostic lesson in CLAUDE.md v0.11.1).

verdict_trusted is stamped once and never recomputed retroactively against a
later registry state — revoking a reviewer stops FUTURE verdicts only, it does
not rewrite settled history (which would itself be a tampering/audit-determinism
vector). See design §5.

Downgrade: drop the table + the column (reverse dependency order).
"""

from __future__ import annotations

import uuid

from alembic import op
import sqlalchemy as sa


revision = "053"
down_revision = "052"
branch_labels = None
depends_on = None


# The operator account that has historically posted 'codex-reviewer'
# verdicts via a human-driven user key (per docs/security/review-verdict-
# provenance.md §0/§5 and CEO confirmation on tk_d42170b4670f4448).
_OPERATOR_USER_ID = "f973f29e-6da1-483e-b9f3-2851a90bf3c9"
_OPERATOR_PERSONA = "codex-reviewer"


def upgrade() -> None:
    bind = op.get_bind()

    # ── 1. ticket_comments.verdict_trusted (fail-closed) ──────────
    # batch_alter_table so SQLite (test path) can add the column.
    with op.batch_alter_table("ticket_comments") as batch:
        batch.add_column(
            sa.Column(
                "verdict_trusted",
                sa.Boolean(),
                nullable=False,
                server_default=sa.text("false"),
            )
        )

    # ── 2. trusted_reviewers registry ─────────────────────────────
    op.create_table(
        "trusted_reviewers",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column(
            "org_id",
            sa.String(length=64),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column(
            "project_id",
            sa.String(length=64),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("user_id", sa.String(length=64), nullable=True),
        sa.Column("service_key_id", sa.String(length=36), nullable=True),
        sa.Column(
            "reviewer_persona",
            sa.String(length=50),
            nullable=False,
            server_default="codex-reviewer",
        ),
        sa.Column(
            "is_active",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
        sa.Column(
            "created_by_user_id", sa.String(length=64), nullable=False
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        # Inline CHECK constraints (SQLite-safe — declared at create_table,
        # not ALTER-ADD; same pattern as migrations 050–052). At least one
        # identity AND at least one scope must be present.
        sa.CheckConstraint(
            "(user_id IS NOT NULL) OR (service_key_id IS NOT NULL)",
            name="ck_trusted_reviewer_identity_present",
        ),
        sa.CheckConstraint(
            "(project_id IS NOT NULL) OR (org_id IS NOT NULL)",
            name="ck_trusted_reviewer_scope_present",
        ),
    )
    op.create_index(
        "idx_trusted_reviewer_project", "trusted_reviewers", ["project_id"]
    )
    op.create_index(
        "idx_trusted_reviewer_org", "trusted_reviewers", ["org_id"]
    )

    # ── 3. Seed + identity-grounded backfill ──────────────────────
    # Find the distinct projects on which the operator has historically
    # posted 'codex-reviewer' comments, joined to each project's org_id.
    rows = bind.execute(
        sa.text(
            """
            SELECT DISTINCT p.id AS project_id, p.org_id AS org_id
            FROM ticket_comments tc
            JOIN tickets t ON t.id = tc.ticket_id
            JOIN projects p ON p.id = t.project_id
            WHERE tc.author_persona = :persona
              AND tc.author_user_id = :uid
            """
        ),
        {"persona": _OPERATOR_PERSONA, "uid": _OPERATOR_USER_ID},
    ).fetchall()

    # De-dup the seed: one org-wide row per org; one project row per
    # personal project. (Multiple org-scoped projects collapse to a single
    # org-wide registry row — the simplest shape for a single shared
    # reviewer.)
    seeded_org_ids: set[str] = set()
    seeded_project_ids: set[str] = set()
    seed_count = 0
    for row in rows:
        project_id = row[0]
        org_id = row[1]
        if org_id:
            if org_id in seeded_org_ids:
                continue
            seeded_org_ids.add(org_id)
            scope_org, scope_project = org_id, None
        else:
            if project_id in seeded_project_ids:
                continue
            seeded_project_ids.add(project_id)
            scope_org, scope_project = None, project_id

        bind.execute(
            sa.text(
                """
                INSERT INTO trusted_reviewers
                    (id, org_id, project_id, user_id, service_key_id,
                     reviewer_persona, is_active, created_by_user_id)
                VALUES
                    (:id, :org_id, :project_id, :user_id, NULL,
                     :persona, :is_active, :created_by)
                """
            ),
            {
                "id": f"tr_{uuid.uuid4().hex[:16]}",
                "org_id": scope_org,
                "project_id": scope_project,
                "user_id": _OPERATOR_USER_ID,
                "persona": _OPERATOR_PERSONA,
                # SQLite stores booleans as 0/1; pass 1 for portability.
                "is_active": True,
                "created_by": _OPERATOR_USER_ID,
            },
        )
        seed_count += 1

    # Backfill verdict_trusted=true ONLY for the operator's own historical
    # 'codex-reviewer' comments (identity-grounded — trusts the registered
    # operator identity, NOT the old caller-supplied string). Anyone else's
    # 'codex-reviewer' comments stay false.
    backfill_result = bind.execute(
        sa.text(
            """
            UPDATE ticket_comments
               SET verdict_trusted = :truthy
             WHERE author_persona = :persona
               AND author_user_id = :uid
            """
        ),
        {"truthy": True, "persona": _OPERATOR_PERSONA, "uid": _OPERATOR_USER_ID},
    )

    # Diagnostic: count any 'codex-reviewer' comments authored by OTHER
    # users — these stay untrusted (forged / out-of-band). Emit to the
    # migration log via print (op.execute can't take bind params; the
    # migration-050 v0.11.1 lesson).
    other_count = bind.execute(
        sa.text(
            """
            SELECT COUNT(*) FROM ticket_comments
             WHERE author_persona = :persona
               AND author_user_id != :uid
            """
        ),
        {"persona": _OPERATOR_PERSONA, "uid": _OPERATOR_USER_ID},
    ).scalar()

    print(
        f"[migration 053] trusted_reviewers seeded={seed_count}; "
        f"operator codex-reviewer comments backfilled trusted="
        f"{backfill_result.rowcount}; non-operator codex-reviewer "
        f"comments left UNTRUSTED={other_count}"
    )


def downgrade() -> None:
    op.drop_index("idx_trusted_reviewer_org", table_name="trusted_reviewers")
    op.drop_index(
        "idx_trusted_reviewer_project", table_name="trusted_reviewers"
    )
    op.drop_table("trusted_reviewers")
    with op.batch_alter_table("ticket_comments") as batch:
        batch.drop_column("verdict_trusted")
