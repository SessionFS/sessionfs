"""Billing: checkout intents, reconciliation records, Stripe sync columns.

Revision ID: 075
Revises: 074

- billing_checkout_intents: one open Stripe Checkout (pending or
  unresolved) per billing owner AND per Stripe customer (partial unique
  indexes), so concurrent or post-org-creation checkout requests reuse or
  refuse instead of opening a second subscription. 'unresolved' marks a
  checkout Stripe completed that was not applied; it blocks new checkouts
  until ops resolves it.
- billing_reconciliations: durable records for billing events that must not
  be guessed at — a duplicate subscription for an owner that already has
  one (never auto-canceled, never rebound), a stale subscription event, an
  event no owner claims, or a permanently malformed event (dead letter).
- stripe_events.outcome: 'processed' | 'dead_letter'. Existing rows are
  'processed'.
- users/organizations.stripe_pending_subscription_id: an 'incomplete'
  (not yet paid) subscription claim. It routes later events and blocks a
  second checkout but grants nothing and never revokes anything.
- entitlements.stripe_synced_generation: fetch generation of the Stripe
  state applied to the row; a fetch with a lower generation is not applied.
- stripe_fetch_generations: one row inserted (and committed) before every
  Stripe retrieve/list; its id orders fetches by when they STARTED using the
  database as the clock — webhooks fetch outside any transaction, so wall
  clocks cannot order them.
- stripe_subscription_observations: the latest canonical Stripe state seen
  per subscription id, independent of ownership (a deletion seen before its
  checkout still governs it); generation / terminal_generation carry the
  fetch generation behind observed_at / terminal_at, and a grant fetched at
  a lower generation than the latest terminal observation is refused.

Backfill (revocation now only ever goes through an exact active Stripe
binding). Pointer claims are first grouped by subscription. A subscription
claimed by more than one owner is ambiguous and left untouched (a
billing_reconciliations 'ambiguous_subscription_pointer' row plus a log
line) — except the provable legacy duplicate: one org plus users who are
members of that org AND carry the org's Stripe customer, where the org is
the claimant. A sole claimant with no (stripe, subscription) entitlement —
rows migration 050 backfilled as source='manual' — gets that binding:
  * its active manual entitlement with no source_ref is rebound to
    ('stripe', subscription id);
  * with no active entitlement and a paid tier cache, an active Stripe
    entitlement is created from the cache;
  * anything else (an active entitlement from another source, or a
    subscription id already bound elsewhere) is left alone and listed in
    the migration log for review.

Downgrade drops what upgrade adds; the backfilled bindings stay (they are
valid entitlement rows either way).
"""

from __future__ import annotations

import json
import secrets

import sqlalchemy as sa
from alembic import op


revision = "075"
down_revision = "074"
branch_labels = None
depends_on = None

_OPEN = "status IN ('pending', 'unresolved')"
_OPEN_WITH_CUSTOMER = "status IN ('pending', 'unresolved') AND stripe_customer_id IS NOT NULL"
_PAID = ("starter", "pro", "team", "enterprise")


def _record(conn, kind: str, subscription_id: str, details: dict, owner=None) -> None:
    """Durable reconciliation row (once per kind + subscription)."""
    seen = conn.execute(
        sa.text(
            "SELECT 1 FROM billing_reconciliations "
            "WHERE kind = :kind AND stripe_subscription_id = :sub AND status = 'open'"
        ),
        {"kind": kind, "sub": subscription_id},
    ).fetchone()
    if seen is not None:
        return
    conn.execute(
        sa.text(
            "INSERT INTO billing_reconciliations (id, kind, status, stripe_subscription_id, "
            "owner_type, owner_id, details) VALUES (:id, :kind, 'open', :sub, :ot, :oid, :details)"
        ),
        {
            "id": f"brc_{secrets.token_hex(12)}",
            "kind": kind,
            "sub": subscription_id,
            "ot": owner[0] if owner else None,
            "oid": owner[1] if owner else None,
            "details": json.dumps(details, default=str),
        },
    )


def _legacy_org_duplicate(conn, claims: list[tuple]) -> tuple | None:
    """The one provable duplicate: exactly one org claim, and every other
    claimant is a user who is a member of that org AND carries the org's
    Stripe customer. Returns the org claim, else None."""
    orgs = [c for c in claims if c[0] == "org"]
    if len(orgs) != 1 or not orgs[0][6]:
        return None
    org = orgs[0]
    for claim in claims:
        if claim is org:
            continue
        if claim[0] != "user" or claim[6] != org[6]:
            return None
        member = conn.execute(
            sa.text("SELECT 1 FROM org_members WHERE org_id = :org AND user_id = :uid"),
            {"org": org[1], "uid": claim[1]},
        ).fetchone()
        if member is None:
            return None
    return org


def _backfill_stripe_bindings(conn) -> None:
    rebound: list[str] = []
    created: list[str] = []
    review: list[str] = []
    ambiguous: list[str] = []

    # (owner_type, owner_id, sub, tier, seats, storage, customer)
    claims = [
        ("org",) + tuple(row)
        for row in conn.execute(
            sa.text(
                "SELECT id, stripe_subscription_id, tier, seats_limit, storage_limit_bytes, "
                "stripe_customer_id FROM organizations "
                "WHERE stripe_subscription_id IS NOT NULL ORDER BY id"
            )
        ).fetchall()
    ] + [
        ("user",) + tuple(row)
        for row in conn.execute(
            sa.text(
                "SELECT id, stripe_subscription_id, tier, NULL, NULL, stripe_customer_id "
                "FROM users WHERE stripe_subscription_id IS NOT NULL ORDER BY id"
            )
        ).fetchall()
    ]

    # Group every pointer claim by subscription BEFORE changing anything, so
    # a duplicated pointer is never assigned to whichever owner sorts first.
    by_sub: dict[str, list[tuple]] = {}
    for claim in claims:
        by_sub.setdefault(claim[2], []).append(claim)

    for sub_id in sorted(by_sub):
        group = by_sub[sub_id]
        bound = conn.execute(
            sa.text(
                "SELECT owner_type, owner_id FROM entitlements "
                "WHERE source = 'stripe' AND source_ref = :sub"
            ),
            {"sub": sub_id},
        ).fetchone()
        if bound is not None:
            # Already bound; any other claimant is left for review.
            for claim in group:
                if (claim[0], claim[1]) != (bound[0], bound[1]):
                    review.append(f"{claim[0]}:{claim[1]} ({sub_id} bound to {bound[0]}:{bound[1]})")
            continue

        if len(group) > 1:
            org_claim = _legacy_org_duplicate(conn, group)
            if org_claim is None:
                owners = [f"{c[0]}:{c[1]}" for c in group]
                _record(conn, "ambiguous_subscription_pointer", sub_id, {
                    "claimants": owners,
                    "action": "no binding created; assign the subscription manually",
                })
                ambiguous.append(f"{sub_id} claimed by {', '.join(owners)}")
                continue
            for claim in group:
                if claim is not org_claim:
                    review.append(
                        f"{claim[0]}:{claim[1]} ({sub_id}: legacy duplicate of org:{org_claim[1]})"
                    )
            group = [org_claim]

        owner_type, owner_id, _sub, tier, seats, storage, _cus = group[0]
        active = conn.execute(
            sa.text(
                "SELECT id, source, source_ref FROM entitlements "
                "WHERE owner_type = :t AND owner_id = :id AND status = 'active'"
            ),
            {"t": owner_type, "id": owner_id},
        ).fetchone()
        if active is not None:
            ent_id, source, source_ref = active
            if source == "manual" and source_ref is None:
                conn.execute(
                    sa.text(
                        "UPDATE entitlements SET source = 'stripe', source_ref = :sub "
                        "WHERE id = :id"
                    ),
                    {"sub": sub_id, "id": ent_id},
                )
                rebound.append(f"{owner_type}:{owner_id}")
            else:
                review.append(
                    f"{owner_type}:{owner_id} ({sub_id}; active entitlement is {source}"
                    f"{'/' + source_ref if source_ref else ''})"
                )
            continue

        if tier in _PAID:
            conn.execute(
                sa.text(
                    "INSERT INTO entitlements (owner_type, owner_id, source, source_ref, tier, "
                    "seats_limit, storage_limit_bytes, status, billing_status) "
                    "VALUES (:t, :id, 'stripe', :sub, :tier, :seats, :storage, 'active', 'current')"
                ),
                {
                    "t": owner_type, "id": owner_id, "sub": sub_id, "tier": tier,
                    "seats": seats, "storage": storage,
                },
            )
            new_id = conn.execute(
                sa.text(
                    "SELECT id FROM entitlements WHERE source = 'stripe' AND source_ref = :sub"
                ),
                {"sub": sub_id},
            ).scalar()
            table = "organizations" if owner_type == "org" else "users"
            conn.execute(
                sa.text(f"UPDATE {table} SET entitlement_id = :eid WHERE id = :id"),
                {"eid": new_id, "id": owner_id},
            )
            created.append(f"{owner_type}:{owner_id}")
        else:
            review.append(f"{owner_type}:{owner_id} ({sub_id}; no entitlement, tier cache {tier!r})")

    print(
        f"[migration 075] Stripe bindings backfilled: {len(rebound)} rebound, "
        f"{len(created)} created, {len(review)} left for review, "
        f"{len(ambiguous)} ambiguous"
    )
    for line in rebound:
        print(f"[migration 075] rebound manual entitlement to its subscription: {line}")
    for line in created:
        print(f"[migration 075] created Stripe entitlement from tier cache: {line}")
    for line in review:
        print(f"[migration 075] review: {line}")
    for line in ambiguous:
        print(f"[migration 075] ambiguous (reconciliation row written): {line}")


def upgrade() -> None:
    op.create_table(
        "billing_checkout_intents",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("owner_type", sa.String(length=10), nullable=False),
        sa.Column("owner_id", sa.String(length=64), nullable=False),
        sa.Column("scope", sa.String(length=20), nullable=False),
        sa.Column("tier", sa.String(length=20), nullable=False),
        sa.Column("seats", sa.Integer(), nullable=False, server_default="1"),
        sa.Column(
            "created_by_user_id",
            sa.String(length=36),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("stripe_customer_id", sa.String(length=64), nullable=True),
        sa.Column("stripe_session_id", sa.String(length=255), nullable=True),
        sa.Column("stripe_subscription_id", sa.String(length=64), nullable=True),
        sa.Column("checkout_url", sa.Text(), nullable=True),
        sa.Column("idempotency_key", sa.String(length=255), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="pending"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('pending', 'completed', 'expired', 'unresolved')",
            name="ck_billing_checkout_intents_status",
        ),
        sa.CheckConstraint(
            "owner_type IN ('user', 'org')",
            name="ck_billing_checkout_intents_owner_type",
        ),
    )
    op.create_index(
        "uq_billing_checkout_intents_one_pending",
        "billing_checkout_intents",
        ["owner_type", "owner_id"],
        unique=True,
        postgresql_where=sa.text(_OPEN),
        sqlite_where=sa.text(_OPEN),
    )
    op.create_index(
        "uq_billing_checkout_intents_one_pending_per_customer",
        "billing_checkout_intents",
        ["stripe_customer_id"],
        unique=True,
        postgresql_where=sa.text(_OPEN_WITH_CUSTOMER),
        sqlite_where=sa.text(_OPEN_WITH_CUSTOMER),
    )
    op.create_index(
        "idx_billing_checkout_intents_session",
        "billing_checkout_intents",
        ["stripe_session_id"],
    )

    op.create_table(
        "billing_reconciliations",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("kind", sa.String(length=40), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="open"),
        sa.Column("stripe_event_id", sa.String(length=64), nullable=True),
        sa.Column("stripe_event_type", sa.String(length=100), nullable=True),
        sa.Column("stripe_customer_id", sa.String(length=64), nullable=True),
        sa.Column("stripe_subscription_id", sa.String(length=64), nullable=True),
        sa.Column("owner_type", sa.String(length=10), nullable=True),
        sa.Column("owner_id", sa.String(length=64), nullable=True),
        sa.Column("details", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('open', 'resolved')",
            name="ck_billing_reconciliations_status",
        ),
    )
    op.create_index(
        "idx_billing_reconciliations_status",
        "billing_reconciliations",
        ["status", "created_at"],
    )
    op.create_index(
        "idx_billing_reconciliations_event", "billing_reconciliations", ["stripe_event_id"]
    )
    op.create_index(
        "idx_billing_reconciliations_subscription",
        "billing_reconciliations",
        ["stripe_subscription_id"],
    )

    op.create_table(
        "stripe_subscription_observations",
        sa.Column("subscription_id", sa.String(length=64), primary_key=True),
        sa.Column("status", sa.String(length=40), nullable=False),
        sa.Column("customer_id", sa.String(length=64), nullable=True),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("terminal_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("generation", sa.BigInteger(), nullable=True),
        sa.Column("terminal_generation", sa.BigInteger(), nullable=True),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )

    op.create_table(
        "stripe_fetch_generations",
        sa.Column(
            "id",
            sa.BigInteger().with_variant(sa.Integer, "sqlite"),
            primary_key=True,
            autoincrement=True,
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )

    with op.batch_alter_table("stripe_events") as batch_op:
        batch_op.add_column(
            sa.Column(
                "outcome", sa.String(length=20), nullable=False, server_default="processed"
            )
        )
    with op.batch_alter_table("users") as batch_op:
        batch_op.add_column(
            sa.Column("stripe_pending_subscription_id", sa.String(length=64), nullable=True)
        )
    with op.batch_alter_table("organizations") as batch_op:
        batch_op.add_column(
            sa.Column("stripe_pending_subscription_id", sa.String(length=64), nullable=True)
        )
    with op.batch_alter_table("entitlements") as batch_op:
        batch_op.add_column(
            sa.Column("stripe_synced_generation", sa.BigInteger(), nullable=True)
        )

    _backfill_stripe_bindings(op.get_bind())


def downgrade() -> None:
    with op.batch_alter_table("entitlements") as batch_op:
        batch_op.drop_column("stripe_synced_generation")
    with op.batch_alter_table("organizations") as batch_op:
        batch_op.drop_column("stripe_pending_subscription_id")
    with op.batch_alter_table("users") as batch_op:
        batch_op.drop_column("stripe_pending_subscription_id")
    with op.batch_alter_table("stripe_events") as batch_op:
        batch_op.drop_column("outcome")

    op.drop_table("stripe_fetch_generations")
    op.drop_table("stripe_subscription_observations")
    op.drop_table("billing_reconciliations")

    op.drop_index("idx_billing_checkout_intents_session", table_name="billing_checkout_intents")
    op.drop_index(
        "uq_billing_checkout_intents_one_pending_per_customer",
        table_name="billing_checkout_intents",
        postgresql_where=sa.text(_OPEN_WITH_CUSTOMER),
        sqlite_where=sa.text(_OPEN_WITH_CUSTOMER),
    )
    op.drop_index(
        "uq_billing_checkout_intents_one_pending",
        table_name="billing_checkout_intents",
        postgresql_where=sa.text(_OPEN),
        sqlite_where=sa.text(_OPEN),
    )
    op.drop_table("billing_checkout_intents")
