"""Migration 074: backfill an owner for every ownerless org.

074 changes no schema, so the prior schema is the real SQLAlchemy metadata
stamped at 069. `SEED` covers every evidence branch (see the migration
docstring): ownership audit events, license activation, the admin
provisioning record, the org's billing user, the earliest active admin, and
each manual-repair outcome. The same seed is replayed on PostgreSQL 14 by the
release proof; `test_full_chain_on_postgres` runs when SFS_TEST_PG_URL is set.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
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


def _ts(day: int) -> datetime:
    return datetime(2026, 1, day, tzinfo=timezone.utc)


# (user_id, is_active)
USERS = [
    ("u_a", True), ("u_b", True), ("u_c", True), ("u_d", True), ("u_e", True),
    ("u_f", True), ("u_g", True), ("u_h", True), ("u_i", True), ("u_j", True),
    ("u_k", True), ("u_l", True), ("u_m", True), ("u_n", True), ("u_o", True),
    ("u_p", True), ("u_q", True), ("u_r", True),
    ("u_s", True), ("u_t", True), ("u_u", True), ("u_v", True), ("u_w", True),
    ("u_x", True), ("u_y", True),
    ("u_dead", False), ("u_dead2", False), ("u_dead3", False), ("u_platform", True),
]

# (org_id, stripe_subscription_id, stripe_customer_id)
ORGS = [
    ("org_plain", None, None),
    ("org_tie", None, None),
    ("org_owned", None, None),
    ("org_members_only", None, None),
    ("org_empty", None, None),
    ("org_skip_inactive", None, None),
    ("org_only_inactive", None, None),
    ("org_transfer", None, None),
    ("org_license", None, None),
    ("org_provisioned", None, None),
    ("org_provisioned_gone", None, None),
    ("org_billing", "sub_billing", "cus_billing"),
    ("org_billing_inactive", "sub_billing_dead", None),
    ("org_billing_ambiguous", "sub_amb", "cus_amb"),
    ("org_license_ambiguous", None, None),
    ("org_license_repeat", None, None),
    ("org_provisioned_ambiguous", None, None),
    ("org_transfer_tie", None, None),
]

# (org_id, user_id, role, joined_day)
MEMBERS = [
    # no evidence -> earliest active admin (u_b)
    ("org_plain", "u_a", "admin", 3),
    ("org_plain", "u_b", "admin", 2),
    ("org_plain", "u_c", "member", 1),
    # tie on joined_at -> lowest user_id
    ("org_tie", "u_g", "admin", 1),
    ("org_tie", "u_a", "admin", 1),
    # already owned -> untouched
    ("org_owned", "u_d", "owner", 3),
    ("org_owned", "u_e", "admin", 1),
    # nobody to promote
    ("org_members_only", "u_f", "member", 1),
    # earliest admin is inactive -> next active admin
    ("org_skip_inactive", "u_dead", "admin", 1),
    ("org_skip_inactive", "u_h", "admin", 2),
    # only an inactive admin -> manual
    ("org_only_inactive", "u_dead2", "admin", 1),
    # ownership transfer event names the later admin
    ("org_transfer", "u_i", "admin", 1),
    ("org_transfer", "u_j", "admin", 5),
    # license activation by the later admin
    ("org_license", "u_k", "admin", 1),
    ("org_license", "u_l", "admin", 5),
    # provisioning record names the later admin
    ("org_provisioned", "u_m", "admin", 1),
    ("org_provisioned", "u_n", "admin", 5),
    # provisioning record names a plain member -> manual despite an admin
    ("org_provisioned_gone", "u_o", "admin", 1),
    ("org_provisioned_gone", "u_p", "member", 5),
    # billing user is the later admin
    ("org_billing", "u_q", "admin", 1),
    ("org_billing", "u_r", "admin", 5),
    # billing user is an inactive admin -> manual despite an active admin
    ("org_billing_inactive", "u_a", "admin", 1),
    ("org_billing_inactive", "u_dead3", "admin", 5),
    # two users share the org's billing ids -> manual
    ("org_billing_ambiguous", "u_b", "admin", 1),
    ("org_billing_ambiguous", "u_c", "admin", 5),
    # two different users recorded as activating owner -> manual
    ("org_license_ambiguous", "u_s", "admin", 1),
    ("org_license_ambiguous", "u_t", "admin", 5),
    # the same user activated twice -> one distinct candidate, promoted
    ("org_license_repeat", "u_a", "admin", 1),
    ("org_license_repeat", "u_u", "admin", 5),
    # two provisioning records naming different owners -> manual
    ("org_provisioned_ambiguous", "u_v", "admin", 1),
    ("org_provisioned_ambiguous", "u_w", "admin", 5),
    # two ownership changes share the latest timestamp -> manual
    ("org_transfer_tie", "u_x", "admin", 1),
    ("org_transfer_tie", "u_y", "admin", 5),
]

USER_BILLING = {
    "u_r": ("sub_billing", None),
    "u_dead3": ("sub_billing_dead", None),
    "u_b": ("sub_amb", None),
    "u_c": (None, "cus_amb"),
}

AUDIT = [
    # (id, org_id, event_type, actor_user_id, actor_role, target_id, day)
    ("oae_t1", "org_transfer", "owner_transferred", "u_i", "owner", "u_i", 2),
    ("oae_t2", "org_transfer", "owner_transferred", "u_j", "owner", "u_j", 4),
    ("oae_l1", "org_license", "license_activated", "u_l", "owner", "lic_abc…", 1),
    ("oae_la1", "org_license_ambiguous", "license_activated", "u_s", "owner", "lic_1…", 1),
    ("oae_la2", "org_license_ambiguous", "license_activated", "u_t", "owner", "lic_2…", 2),
    ("oae_lr1", "org_license_repeat", "license_activated", "u_u", "owner", "lic_3…", 1),
    ("oae_lr2", "org_license_repeat", "license_activated", "u_u", "owner", "lic_4…", 2),
    ("oae_tt1", "org_transfer_tie", "owner_transferred", "u_x", "owner", "u_x", 3),
    ("oae_tt2", "org_transfer_tie", "owner_force_transferred", "u_platform",
     "platform_admin", "u_y", 3),
]

ADMIN_ACTIONS = [
    ("aa_1", "u_platform", "org_provisioned", {"owner_user_id": "u_n"}),
    ("aa_2", "u_platform", "org_provisioned_gone", {"owner_user_id": "u_p"}),
    ("aa_3", "u_platform", "org_provisioned_ambiguous", {"owner_user_id": "u_v"}),
    ("aa_4", "u_platform", "org_provisioned_ambiguous", {"owner_user_id": "u_w"}),
]

EXPECTED_OWNERS = {
    "org_plain": "u_b",
    "org_tie": "u_a",
    "org_owned": "u_d",
    "org_skip_inactive": "u_h",
    "org_transfer": "u_j",
    "org_license": "u_l",
    "org_provisioned": "u_n",
    "org_billing": "u_r",
    "org_license_repeat": "u_u",
}
EXPECTED_MANUAL = {
    "org_members_only",
    "org_empty",
    "org_only_inactive",
    "org_provisioned_gone",
    "org_billing_inactive",
    "org_billing_ambiguous",
    "org_license_ambiguous",
    "org_provisioned_ambiguous",
    "org_transfer_tie",
}
EXPECTED_EVIDENCE = {
    "org_plain": "earliest_active_admin",
    "org_tie": "earliest_active_admin",
    "org_skip_inactive": "earliest_active_admin",
    "org_transfer": "audit:owner_transferred",
    "org_license": "audit:license_activated",
    "org_provisioned": "admin_create_org",
    "org_billing": "billing",
    "org_license_repeat": "audit:license_activated",
}


def seed(execute: Callable[[str, dict], None]) -> None:
    """Insert the fixture through `execute(sql, params)` (any dialect)."""
    for uid, active in USERS:
        sub, cus = USER_BILLING.get(uid, (None, None))
        execute(
            "INSERT INTO users (id, email, tier, is_active, email_verified, created_at, "
            "stripe_subscription_id, stripe_customer_id) "
            "VALUES (:id, :email, 'team', :active, :verified, :created, :sub, :cus)",
            {"id": uid, "email": f"{uid}@example.com", "active": active, "verified": True,
             "created": _ts(1), "sub": sub, "cus": cus},
        )
    for org_id, sub, cus in ORGS:
        execute(
            "INSERT INTO organizations (id, name, slug, tier, stripe_subscription_id, "
            "stripe_customer_id) VALUES (:id, :name, :slug, 'team', :sub, :cus)",
            {"id": org_id, "name": org_id.upper(), "slug": org_id.replace("_", "-"),
             "sub": sub, "cus": cus},
        )
    for org_id, uid, role, day in MEMBERS:
        execute(
            "INSERT INTO org_members (org_id, user_id, role, joined_at) "
            "VALUES (:org, :uid, :role, :joined)",
            {"org": org_id, "uid": uid, "role": role, "joined": _ts(day)},
        )
    for aid, org_id, event, actor, role, target, day in AUDIT:
        execute(
            "INSERT INTO org_audit_events (id, org_id, org_name_snapshot, event_type, "
            "actor_user_id, actor_role_at_time, target_type, target_id, created_at) "
            "VALUES (:id, :org, :name, :event, :actor, :role, 'user', :target, :created)",
            {"id": aid, "org": org_id, "name": org_id.upper(), "event": event,
             "actor": actor, "role": role, "target": target, "created": _ts(day)},
        )
    for aid, admin_id, org_id, details in ADMIN_ACTIONS:
        execute(
            "INSERT INTO admin_actions (id, admin_id, action, target_type, target_id, "
            "details, created_at) VALUES (:id, :admin, 'admin_create_org', 'org', :org, "
            ":details, :created)",
            {"id": aid, "admin": admin_id, "org": org_id, "details": json.dumps(details),
             "created": _ts(1)},
        )


def _cfg(path: Path) -> Config:
    config = Config()
    config.set_main_option("script_location", _MIGRATIONS)
    config.set_main_option("sqlalchemy.url", f"sqlite+aiosqlite:///{path}")
    return config


@pytest.fixture
def pre_074_db(tmp_path: Path) -> Path:
    path = tmp_path / "m074.db"
    engine = sa.create_engine(f"sqlite:///{path}")
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        seed(lambda sql, params: conn.execute(sa.text(sql), params))
    engine.dispose()
    command.stamp(_cfg(path), "073")
    return path


def _query(path: Path, sql: str) -> list[tuple]:
    engine = sa.create_engine(f"sqlite:///{path}")
    try:
        with engine.connect() as conn:
            return [tuple(r) for r in conn.execute(sa.text(sql))]
    finally:
        engine.dispose()


def _owners(path: Path) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for org_id, uid in _query(
        path, "SELECT org_id, user_id FROM org_members WHERE role = 'owner' ORDER BY user_id"
    ):
        out.setdefault(org_id, []).append(uid)
    return out


def _backfill_audit(path: Path) -> list[tuple]:
    return _query(
        path,
        "SELECT org_id, target_id, target_email_snapshot, actor_role_at_time, "
        "\"before\", \"after\" FROM org_audit_events "
        "WHERE event_type = 'owner_backfilled' ORDER BY org_id",
    )


def test_owner_resolution_follows_the_evidence_order(pre_074_db: Path, capsys) -> None:
    command.upgrade(_cfg(pre_074_db), "074")

    owners = _owners(pre_074_db)
    assert owners == {org: [uid] for org, uid in EXPECTED_OWNERS.items()}
    for org in EXPECTED_MANUAL:
        assert org not in owners

    audit = _backfill_audit(pre_074_db)
    promoted = {org: uid for org, uid in EXPECTED_OWNERS.items() if org != "org_owned"}
    assert {(a[0], a[1]) for a in audit} == set(promoted.items())
    for org_id, uid, email, role, before, after in audit:
        assert email == f"{uid}@example.com"
        assert role == "system"
        assert json.loads(before) == {"role": "admin"}
        after_doc = json.loads(after)
        assert after_doc["role"] == "owner"
        assert after_doc["evidence"] == EXPECTED_EVIDENCE[org_id]

    out = capsys.readouterr().out
    assert f"owners backfilled: {len(promoted)}" in out
    assert f"need manual owner assignment: {len(EXPECTED_MANUAL)}" in out
    for org in EXPECTED_MANUAL:
        assert org in out


def test_stronger_evidence_never_falls_through_to_a_weaker_signal(pre_074_db: Path) -> None:
    command.upgrade(_cfg(pre_074_db), "074")
    roles = dict(
        ((o, u), r) for o, u, r in _query(pre_074_db, "SELECT org_id, user_id, role FROM org_members")
    )
    # Each of these has an active admin the earliest-admin rule would have
    # picked; the stronger signal named someone unsafe, so nobody moved.
    assert roles[("org_provisioned_gone", "u_o")] == "admin"
    assert roles[("org_billing_inactive", "u_a")] == "admin"
    assert roles[("org_billing_ambiguous", "u_b")] == "admin"
    assert roles[("org_billing_ambiguous", "u_c")] == "admin"
    # Inactive admins are never promoted.
    assert roles[("org_skip_inactive", "u_dead")] == "admin"
    assert roles[("org_only_inactive", "u_dead2")] == "admin"
    assert roles[("org_billing_inactive", "u_dead3")] == "admin"


def test_ambiguous_evidence_goes_to_manual_repair(pre_074_db: Path, capsys) -> None:
    command.upgrade(_cfg(pre_074_db), "074")
    owners = _owners(pre_074_db)
    for org in ("org_license_ambiguous", "org_provisioned_ambiguous", "org_transfer_tie"):
        assert org not in owners
    # Repeated evidence for one user is not ambiguous.
    assert owners["org_license_repeat"] == ["u_u"]
    out = capsys.readouterr().out
    assert "org_license_ambiguous (audit:license_activated names 2 users)" in out
    assert "org_provisioned_ambiguous (admin_create_org names 2 users)" in out
    assert "org_transfer_tie (audit:" in out and "names 2 users)" in out


def _skipped(path: Path) -> list[tuple]:
    return _query(
        path,
        "SELECT org_id, target_type, target_id, actor_role_at_time, \"after\" "
        "FROM org_audit_events WHERE event_type = 'owner_backfill_skipped' ORDER BY org_id",
    )


def test_every_unresolved_org_gets_a_durable_skipped_row(pre_074_db: Path, capsys) -> None:
    config = _cfg(pre_074_db)
    command.upgrade(config, "074")
    rows = _skipped(pre_074_db)
    assert [r[0] for r in rows] == sorted(EXPECTED_MANUAL)
    for org_id, target_type, target_id, role, after in rows:
        assert (target_type, target_id, role) == ("organization", org_id, "system")
        doc = json.loads(after)
        assert doc["migration"] == "074" and doc["reason"]
    reasons = {r[0]: json.loads(r[4])["reason"] for r in rows}
    assert reasons["org_license_ambiguous"] == "audit:license_activated names 2 users"
    assert reasons["org_empty"] == "no evidence and no active admin"
    # The log lists every unresolved org, one line each (no truncation).
    out = capsys.readouterr().out
    assert out.count("[migration 074] manual repair: ") == len(EXPECTED_MANUAL)

    # Re-running does not duplicate the diagnostics.
    command.downgrade(config, "073")
    command.upgrade(config, "074")
    assert len(_skipped(pre_074_db)) == len(EXPECTED_MANUAL)


def test_upgrade_is_idempotent(pre_074_db: Path) -> None:
    config = _cfg(pre_074_db)
    command.upgrade(config, "074")
    owners, audit = _owners(pre_074_db), _backfill_audit(pre_074_db)
    command.downgrade(config, "073")
    command.upgrade(config, "074")
    assert _owners(pre_074_db) == owners
    assert len(_backfill_audit(pre_074_db)) == len(audit)


def test_downgrade_is_a_noop(pre_074_db: Path) -> None:
    config = _cfg(pre_074_db)
    command.upgrade(config, "074")
    after_upgrade = _query(pre_074_db, "SELECT org_id, user_id, role FROM org_members ORDER BY 1, 2")
    command.downgrade(config, "073")
    assert _query(pre_074_db, "SELECT org_id, user_id, role FROM org_members ORDER BY 1, 2") == after_upgrade


@pytest.mark.skipif(
    not os.environ.get("SFS_TEST_PG_URL"),
    reason="set SFS_TEST_PG_URL to a scratch PostgreSQL database to run the full chain",
)
def test_full_chain_on_postgres() -> None:
    config = Config()
    config.set_main_option("script_location", _MIGRATIONS)
    config.set_main_option("sqlalchemy.url", os.environ["SFS_TEST_PG_URL"])
    command.upgrade(config, "head")
    command.downgrade(config, "073")
    command.upgrade(config, "head")
