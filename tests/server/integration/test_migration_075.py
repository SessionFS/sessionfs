"""Migration 075: billing intents, reconciliation rows, Stripe sync columns,
and the backfill of exact Stripe bindings for legacy pointer-only owners.

The prior schema is the real SQLAlchemy metadata minus what 075 adds (three
tables; stripe_events.outcome; users/organizations
.stripe_pending_subscription_id; entitlements.stripe_synced_generation).
PostgreSQL is covered by `test_full_chain_on_postgres` in
test_migration_074.py when SFS_TEST_PG_URL is set, by the release PG proof,
and by the `migrations-postgres` CI gate. `seed_legacy` is shared with the
PG proof.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config

from sessionfs.server.db.models import Base

_MIGRATIONS = str(
    Path(__file__).resolve().parents[3] / "src" / "sessionfs" / "server" / "db" / "migrations"
)
NEW_TABLES = {
    "billing_checkout_intents",
    "billing_reconciliations",
    "stripe_fetch_generations",
    "stripe_subscription_observations",
}
NEW_COLUMNS = {
    "stripe_events": "outcome",
    "users": "stripe_pending_subscription_id",
    "organizations": "stripe_pending_subscription_id",
    "entitlements": "stripe_synced_generation",
}

# Expected backfill outcome per owner: ("rebound"|"created"|"untouched", detail)
EXPECTED = {
    ("org", "o_manual"): ("rebound", "sub_o_manual"),
    ("org", "o_cache"): ("created", "sub_o_cache"),
    ("user", "u_manual"): ("rebound", "sub_u_manual"),
    ("user", "u_cache"): ("created", "sub_u_cache"),
    ("user", "u_admin_prov"): ("untouched", "admin_provisioned"),
    ("user", "u_free_cache"): ("untouched", None),
    ("user", "u_already"): ("untouched", "sub_u_already"),
    ("user", "u_legacy_dup"): ("untouched", None),  # member of o_manual, same customer
    # sub_amb_users: two unrelated users claim it -> ambiguous, untouched
    ("user", "u_amb1"): ("untouched", "manual"),
    ("user", "u_amb2"): ("untouched", None),
    # sub_amb_org: an org and a NON-member user with a different customer
    ("org", "o_amb"): ("untouched", "manual"),
    ("user", "u_amb_outsider"): ("untouched", None),
}
AMBIGUOUS_SUBS = {"sub_amb_users", "sub_amb_org"}


def seed_legacy(execute: Callable[[str, dict], None]) -> None:
    """Pre-075 rows covering every backfill branch (any dialect)."""
    users = [
        ("u_manual", "pro", "sub_u_manual"),
        ("u_cache", "starter", "sub_u_cache"),
        ("u_admin_prov", "pro", "sub_u_admin_prov"),
        ("u_free_cache", "free", "sub_u_free"),
        ("u_already", "pro", "sub_u_already"),
        # legacy duplicate: member of o_manual and carries the org's customer
        ("u_legacy_dup", "team", "sub_o_manual", "cus_o_manual"),
        ("u_amb1", "pro", "sub_amb_users", None),
        ("u_amb2", "pro", "sub_amb_users", None),
        ("u_amb_outsider", "team", "sub_amb_org", None),
        ("u_none", "free", None, None),
    ]
    for uid, tier, sub, *cus in users:
        customer = cus[0] if cus and cus[0] else (f"cus_{uid}" if sub else None)
        execute(
            "INSERT INTO users (id, email, tier, is_active, email_verified, created_at, "
            "stripe_subscription_id, stripe_customer_id) "
            "VALUES (:id, :email, :tier, :t, :t, CURRENT_TIMESTAMP, :sub, :cus)",
            {"id": uid, "email": f"{uid}@example.com", "tier": tier, "t": True,
             "sub": sub, "cus": customer},
        )
    for oid, tier, sub, seats in (("o_manual", "team", "sub_o_manual", 7),
                                  ("o_cache", "enterprise", "sub_o_cache", 30),
                                  ("o_amb", "team", "sub_amb_org", 5)):
        execute(
            "INSERT INTO organizations (id, name, slug, tier, stripe_subscription_id, "
            "stripe_customer_id, seats_limit, storage_limit_bytes) "
            "VALUES (:id, :id, :id, :tier, :sub, :cus, :seats, 0)",
            {"id": oid, "tier": tier, "sub": sub, "cus": f"cus_{oid}", "seats": seats},
        )
    execute(
        "INSERT INTO org_members (org_id, user_id, role) VALUES ('o_manual', 'u_legacy_dup', 'admin')",
        {},
    )
    ents = [
        ("org", "o_manual", "manual", None, "team"),
        ("user", "u_manual", "manual", None, "pro"),
        ("user", "u_admin_prov", "admin_provisioned", None, "pro"),
        ("user", "u_already", "stripe", "sub_u_already", "pro"),
        ("user", "u_amb1", "manual", None, "pro"),
        ("org", "o_amb", "manual", None, "team"),
    ]
    for owner_type, owner_id, source, ref, tier in ents:
        execute(
            "INSERT INTO entitlements (owner_type, owner_id, source, source_ref, tier, status, "
            "billing_status) VALUES (:ot, :oid, :src, :ref, :tier, 'active', 'current')",
            {"ot": owner_type, "oid": owner_id, "src": source, "ref": ref, "tier": tier},
        )


def _cfg(path: Path) -> Config:
    config = Config()
    config.set_main_option("script_location", _MIGRATIONS)
    config.set_main_option("sqlalchemy.url", f"sqlite+aiosqlite:///{path}")
    return config


@pytest.fixture
def pre_075_db(tmp_path: Path) -> Path:
    path = tmp_path / "m075.db"
    engine = sa.create_engine(f"sqlite:///{path}")
    prior = [t for name, t in Base.metadata.tables.items() if name not in NEW_TABLES]
    assert len(prior) == len(Base.metadata.tables) - len(NEW_TABLES)
    Base.metadata.create_all(engine, tables=prior)
    with engine.begin() as conn:
        for table, column in NEW_COLUMNS.items():
            conn.execute(sa.text(f"ALTER TABLE {table} DROP COLUMN {column}"))
        conn.execute(
            sa.text("INSERT INTO stripe_events (event_id, event_type) VALUES ('evt_old', 'x')")
        )
        seed_legacy(lambda sql, params: conn.execute(sa.text(sql), params))
    engine.dispose()
    command.stamp(_cfg(path), "074")
    return path


def _q(path: Path, sql: str) -> list[tuple]:
    engine = sa.create_engine(f"sqlite:///{path}")
    try:
        with engine.connect() as conn:
            return [tuple(r) for r in conn.execute(sa.text(sql))]
    finally:
        engine.dispose()


def _inspect(path: Path):
    engine = sa.create_engine(f"sqlite:///{path}")
    try:
        insp = sa.inspect(engine)
        tables = set(insp.get_table_names())
        cols = {t: {c["name"] for c in insp.get_columns(t)} for t in NEW_COLUMNS}
        idx = {i["name"] for t in NEW_TABLES & tables for i in insp.get_indexes(t)}
        return tables, cols, idx
    finally:
        engine.dispose()


def check_backfill(rows: list[tuple]) -> None:
    """rows: (owner_type, owner_id, source, source_ref, tier, status)."""
    by_owner: dict = {}
    for owner_type, owner_id, source, ref, tier, status in rows:
        by_owner.setdefault((owner_type, owner_id), []).append((source, ref, tier, status))
    for owner, (kind, detail) in EXPECTED.items():
        got = by_owner.get(owner, [])
        if kind == "rebound":
            assert got == [("stripe", detail, got[0][2], "active")], (owner, got)
        elif kind == "created":
            assert len(got) == 1 and got[0][:2] == ("stripe", detail) and got[0][3] == "active", (owner, got)
        elif detail is None:
            assert got == [], (owner, got)
        else:
            assert len(got) == 1 and detail in (got[0][0], got[0][1]), (owner, got)


def test_upgrade_adds_schema_and_backfills_bindings(pre_075_db: Path, capsys) -> None:
    command.upgrade(_cfg(pre_075_db), "075")
    tables, cols, idx = _inspect(pre_075_db)
    assert NEW_TABLES <= tables
    for table, column in NEW_COLUMNS.items():
        assert column in cols[table]
    assert {
        "uq_billing_checkout_intents_one_pending",
        "uq_billing_checkout_intents_one_pending_per_customer",
        "idx_billing_checkout_intents_session",
        "idx_billing_reconciliations_status",
        "idx_billing_reconciliations_event",
        "idx_billing_reconciliations_subscription",
    } <= idx
    assert _q(pre_075_db, "SELECT event_id, outcome FROM stripe_events") == [("evt_old", "processed")]

    check_backfill(_q(
        pre_075_db,
        "SELECT owner_type, owner_id, source, source_ref, tier, status FROM entitlements",
    ))
    created = dict(_q(pre_075_db, "SELECT owner_id, tier FROM entitlements WHERE owner_id IN ('o_cache', 'u_cache')"))
    assert created == {"o_cache": "enterprise", "u_cache": "starter"}
    seats = _q(pre_075_db, "SELECT seats_limit FROM entitlements WHERE owner_id = 'o_cache'")
    assert seats == [(30,)]
    pointers = dict(_q(pre_075_db, "SELECT id, entitlement_id FROM users WHERE id = 'u_cache'"))
    assert pointers["u_cache"] is not None
    out = capsys.readouterr().out
    assert "2 rebound, 2 created" in out and "2 ambiguous" in out
    assert "review: user:u_admin_prov" in out
    assert "review: user:u_legacy_dup" in out
    recon = _q(pre_075_db, "SELECT kind, stripe_subscription_id, details FROM billing_reconciliations")
    assert {(r[0], r[1]) for r in recon} == {
        ("ambiguous_subscription_pointer", sub) for sub in AMBIGUOUS_SUBS
    }
    amb = [r for r in recon if r[1] == "sub_amb_users"][0]
    assert "user:u_amb1" in amb[2] and "user:u_amb2" in amb[2]


def test_one_open_checkout_per_owner_and_per_customer(pre_075_db: Path) -> None:
    command.upgrade(_cfg(pre_075_db), "075")
    engine = sa.create_engine(f"sqlite:///{pre_075_db}")
    insert = sa.text(
        "INSERT INTO billing_checkout_intents (id, owner_type, owner_id, scope, tier, "
        "idempotency_key, status, expires_at, stripe_customer_id) VALUES (:id, :ot, :oid, "
        "'personal', 'pro', :id, :status, '2030-01-01', :cus)"
    )
    try:
        with engine.begin() as conn:
            conn.execute(insert, {"id": "a", "ot": "user", "oid": "u1", "status": "pending", "cus": "c1"})
            conn.execute(insert, {"id": "b", "ot": "user", "oid": "u1", "status": "completed", "cus": "c1"})
        for row in (
            {"id": "c", "ot": "user", "oid": "u1", "status": "unresolved", "cus": "c9"},  # same owner
            {"id": "d", "ot": "org", "oid": "o1", "status": "pending", "cus": "c1"},  # same customer
        ):
            with pytest.raises(sa.exc.IntegrityError):
                with engine.begin() as conn:
                    conn.execute(insert, row)
    finally:
        engine.dispose()


def test_downgrade_and_reupgrade(pre_075_db: Path) -> None:
    config = _cfg(pre_075_db)
    command.upgrade(config, "075")
    command.downgrade(config, "074")
    tables, cols, _ = _inspect(pre_075_db)
    assert not (NEW_TABLES & tables)
    for table, column in NEW_COLUMNS.items():
        assert column not in cols[table]
    command.upgrade(config, "075")
    tables, cols, _ = _inspect(pre_075_db)
    assert NEW_TABLES <= tables
    # The backfill is idempotent: bindings created the first time are found,
    # and ambiguous subscriptions aren't recorded twice.
    assert len(_q(pre_075_db, "SELECT id FROM billing_reconciliations")) == len(AMBIGUOUS_SUBS)
    check_backfill(_q(
        pre_075_db,
        "SELECT owner_type, owner_id, source, source_ref, tier, status FROM entitlements",
    ))
