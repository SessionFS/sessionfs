"""Backfill an owner for every organization that has none.

Revision ID: 074
Revises: 073

Since v0.11.0 every org is meant to have exactly one owner (owner transfer,
break-glass, SSO enforcement exemption and the last-owner guards all key on
it). Migration 050 promoted one admin per org at the time, but two creation
paths kept inserting the creator as ``admin`` afterwards:

- ``POST /api/v1/org`` (``sfs org create``)
- ``POST /api/v1/admin/orgs`` (platform-admin provisioning)

Orgs created through either path since 050 have no owner. Both paths now
create the creator as ``owner``; this migration repairs the existing rows.

For each org with zero owners the owner is resolved from the strongest
evidence available, in this order (the first tier that names anyone wins):

1. Durable ownership evidence:
   a. the most recent ownership change in ``org_audit_events``
      (owner_transferred / owner_force_transferred /
      owner_auto_promoted_on_deactivation / owner_backfilled — target_id is
      the new owner);
   b. a ``license_activated`` event recorded with
      ``actor_role_at_time = 'owner'`` (the activating user);
   c. the ``admin_create_org`` admin action's ``owner_user_id``.
2. The org's billing user: the user whose ``stripe_subscription_id`` or
   ``stripe_customer_id`` matches the org's.
3. The earliest active admin (``joined_at``, then ``user_id``).

Every subtype collects all the distinct users it names (no first-match):
two license activations by different users, two provisioning records with
different owner_user_ids, or two ownership changes sharing the latest
timestamp are ambiguous and go to manual repair.

The chosen user is promoted only if they are an ACTIVE ADMIN of that org
(``org_members.role = 'admin'`` and ``users.is_active``). If a stronger
tier names someone who is not — or names more than one user — nobody is
promoted and the org is logged for manual repair, rather than falling
through to a weaker signal. Orgs with no evidence and no active admin are
logged the same way.

Each promotion writes an ``owner_backfilled`` row to ``org_audit_events``
recording which evidence was used. Every org left without an owner gets an
``owner_backfill_skipped`` row (target_type 'organization') carrying the
reason — written once per org, not repeated on re-runs — and the complete
list is printed to the migration log.

Idempotent: an org that already has an owner is never touched, so a re-run
is a no-op. The one-owner-per-org partial unique index
(``uq_org_members_one_owner_per_org``) is the structural backstop.
"""

from __future__ import annotations

import json
import secrets

import sqlalchemy as sa
from alembic import op


revision = "074"
down_revision = "073"
branch_labels = None
depends_on = None


_OWNERSHIP_EVENTS = (
    "owner_transferred",
    "owner_force_transferred",
    "owner_auto_promoted_on_deactivation",
    "owner_backfilled",
)


def _ownership_evidence(conn, org_id: str) -> tuple[str, list[str]] | None:
    """Tier 1: durable ownership/creator evidence. (source, [user_ids]).

    Each subtype returns every distinct user it names, never just the first
    match, so the caller can refuse an ambiguous signal instead of guessing.
    """
    placeholders = ", ".join(f":e{i}" for i in range(len(_OWNERSHIP_EVENTS)))
    params: dict = {f"e{i}": e for i, e in enumerate(_OWNERSHIP_EVENTS)}
    params["org_id"] = org_id
    rows = conn.execute(
        sa.text(
            "SELECT target_id, event_type, created_at FROM org_audit_events "
            "WHERE org_id = :org_id AND target_id IS NOT NULL "
            f"AND event_type IN ({placeholders}) "
            "ORDER BY created_at DESC, id DESC"
        ),
        params,
    ).fetchall()
    if rows:
        # The most recent ownership change wins; several different targets
        # sharing that latest timestamp are ambiguous.
        latest = rows[0][2]
        targets = sorted({r[0] for r in rows if r[2] == latest})
        return f"audit:{rows[0][1]}", targets

    rows = conn.execute(
        sa.text(
            "SELECT DISTINCT actor_user_id FROM org_audit_events "
            "WHERE org_id = :org_id AND event_type = 'license_activated' "
            "AND actor_role_at_time = 'owner' AND actor_user_id IS NOT NULL"
        ),
        {"org_id": org_id},
    ).fetchall()
    if rows:
        return "audit:license_activated", sorted(r[0] for r in rows)

    rows = conn.execute(
        sa.text(
            "SELECT details FROM admin_actions "
            "WHERE action = 'admin_create_org' AND target_id = :org_id"
        ),
        {"org_id": org_id},
    ).fetchall()
    owners: set[str] = set()
    for (details,) in rows:
        try:
            owner = json.loads(details or "{}").get("owner_user_id")
        except (ValueError, AttributeError):
            owner = None
        if isinstance(owner, str) and owner:
            owners.add(owner)
    if owners:
        return "admin_create_org", sorted(owners)
    return None


def _billing_evidence(
    conn, sub_id: str | None, customer_id: str | None
) -> tuple[str, list[str]] | None:
    """Tier 2: the user(s) holding the org's Stripe subscription/customer."""
    if not sub_id and not customer_id:
        return None
    clauses = []
    params: dict = {}
    if sub_id:
        clauses.append("stripe_subscription_id = :sub")
        params["sub"] = sub_id
    if customer_id:
        clauses.append("stripe_customer_id = :cus")
        params["cus"] = customer_id
    rows = conn.execute(
        sa.text(
            "SELECT DISTINCT id FROM users WHERE "
            + " OR ".join(clauses)
            + " ORDER BY id"
        ),
        params,
    ).fetchall()
    if not rows:
        return None
    return "billing", [r[0] for r in rows]


def _is_active_admin(conn, org_id: str, user_id: str) -> bool:
    return (
        conn.execute(
            sa.text(
                "SELECT 1 FROM org_members om "
                "JOIN users u ON u.id = om.user_id "
                "WHERE om.org_id = :org_id AND om.user_id = :user_id "
                "AND om.role = 'admin' AND u.is_active = :true"
            ),
            {"org_id": org_id, "user_id": user_id, "true": True},
        ).fetchone()
        is not None
    )


def _earliest_active_admin(conn, org_id: str) -> str | None:
    row = conn.execute(
        sa.text(
            "SELECT om.user_id FROM org_members om "
            "JOIN users u ON u.id = om.user_id "
            "WHERE om.org_id = :org_id AND om.role = 'admin' "
            "AND u.is_active = :true "
            "ORDER BY om.joined_at ASC, om.user_id ASC LIMIT 1"
        ),
        {"org_id": org_id, "true": True},
    ).fetchone()
    return row[0] if row else None


def upgrade() -> None:
    conn = op.get_bind()

    ownerless = conn.execute(
        sa.text(
            "SELECT o.id, o.name, o.stripe_subscription_id, o.stripe_customer_id "
            "FROM organizations o "
            "WHERE NOT EXISTS ("
            "  SELECT 1 FROM org_members om "
            "  WHERE om.org_id = o.id AND om.role = 'owner'"
            ") ORDER BY o.id"
        )
    ).fetchall()

    promoted: list[str] = []
    manual: list[tuple[str, str, str]] = []

    for org_id, org_name, sub_id, customer_id in ownerless:
        evidence = _ownership_evidence(conn, org_id) or _billing_evidence(
            conn, sub_id, customer_id
        )
        if evidence is not None:
            source, users = evidence
            if len(users) != 1:
                manual.append((org_id, org_name, f"{source} names {len(users)} users"))
                continue
            candidate = users[0]
            if not _is_active_admin(conn, org_id, candidate):
                manual.append((org_id, org_name, f"{source} names {candidate}, not an active admin"))
                continue
        else:
            source = "earliest_active_admin"
            candidate = _earliest_active_admin(conn, org_id)
            if candidate is None:
                manual.append((org_id, org_name, "no evidence and no active admin"))
                continue

        # Guarded promotion: only while the org still has no owner and the
        # candidate is still an admin.
        result = conn.execute(
            sa.text(
                "UPDATE org_members SET role = 'owner' "
                "WHERE org_id = :org_id AND user_id = :user_id "
                "AND role = 'admin' "
                "AND NOT EXISTS ("
                "  SELECT 1 FROM org_members o2 "
                "  WHERE o2.org_id = :org_id AND o2.role = 'owner'"
                ")"
            ),
            {"org_id": org_id, "user_id": candidate},
        )
        if result.rowcount != 1:
            manual.append((org_id, org_name, f"promotion of {candidate} did not apply"))
            continue

        email_row = conn.execute(
            sa.text("SELECT email FROM users WHERE id = :id"), {"id": candidate}
        ).fetchone()
        conn.execute(
            sa.text(
                "INSERT INTO org_audit_events "
                "(id, org_id, org_name_snapshot, event_type, actor_user_id, "
                " actor_email_snapshot, actor_role_at_time, target_type, "
                " target_id, target_email_snapshot, \"before\", \"after\") "
                "VALUES (:id, :org_id, :org_name, 'owner_backfilled', NULL, "
                " NULL, 'system', 'user', :user_id, :email, :before, :after)"
            ),
            {
                "id": f"oae_{secrets.token_hex(12)}",
                "org_id": org_id,
                "org_name": org_name or "",
                "user_id": candidate,
                "email": email_row[0] if email_row else None,
                "before": json.dumps({"role": "admin"}),
                "after": json.dumps(
                    {"role": "owner", "evidence": source, "migration": "074"}
                ),
            },
        )
        promoted.append(f"{org_id}->{candidate} ({source})")

    # Durable diagnostic for every org left without an owner: one
    # owner_backfill_skipped audit row per org (not repeated on re-runs),
    # so the repair list survives beyond the migrate-job log.
    for org_id, org_name, reason in manual:
        already = conn.execute(
            sa.text(
                "SELECT 1 FROM org_audit_events "
                "WHERE org_id = :org_id AND event_type = 'owner_backfill_skipped'"
            ),
            {"org_id": org_id},
        ).fetchone()
        if already is not None:
            continue
        conn.execute(
            sa.text(
                "INSERT INTO org_audit_events "
                "(id, org_id, org_name_snapshot, event_type, actor_user_id, "
                " actor_email_snapshot, actor_role_at_time, target_type, "
                " target_id, target_email_snapshot, \"before\", \"after\") "
                "VALUES (:id, :org_id, :org_name, 'owner_backfill_skipped', NULL, "
                " NULL, 'system', 'organization', :org_id, NULL, NULL, :after)"
            ),
            {
                "id": f"oae_{secrets.token_hex(12)}",
                "org_id": org_id,
                "org_name": org_name or "",
                "after": json.dumps(
                    {
                        "reason": reason,
                        "action": "assign an owner manually (platform-admin force transfer)",
                        "migration": "074",
                    }
                ),
            },
        )

    print(
        f"[migration 074] ownerless orgs: {len(ownerless)}; "
        f"owners backfilled: {len(promoted)}; "
        f"need manual owner assignment: {len(manual)}"
    )
    for line in promoted:
        print(f"[migration 074] promoted: {line}")
    for org_id, _name, reason in manual:
        print(f"[migration 074] manual repair: {org_id} ({reason})")


def downgrade() -> None:
    # Intentional no-op. After upgrade we cannot tell a backfilled owner from
    # one who was made owner legitimately (activation, transfer, or the fixed
    # creation paths), and demoting would recreate ownerless orgs. The
    # owner_backfilled audit rows record exactly which members were promoted
    # if a manual revert is ever needed.
    pass
