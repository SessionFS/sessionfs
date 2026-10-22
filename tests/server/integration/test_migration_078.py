"""Migration 078: org skills policy revisions, compilations, devices,
latest compliance reports and their daily history, plus the backfill of a legacy organizations.settings
"skills" block into revision 1.

The prior schema is the real SQLAlchemy metadata minus the four new tables.
PostgreSQL: `test_upgrade_on_postgres` runs when SFS_TEST_PG_URL is set.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config

from sessionfs.server.db.models import Base
from tests.pg_helpers import reset_pg_schema

_MIGRATIONS = str(
    Path(__file__).resolve().parents[3] / "src" / "sessionfs" / "server" / "db" / "migrations"
)
NEW_TABLES = {
    "org_skill_policies",
    "org_policy_compilations",
    "skill_devices",
    "device_policy_reports",
    "device_policy_report_history",
}
ORGS = {
    "org_legacy": {"skills": {"mode": "allowlist", "allowlist": [{"source": "github.com/acme/skills", "sha": "any"}]}},
    "org_bad_mode": {"skills": {"mode": "whatever"}},
    "org_not_dict": {"skills": "open"},
    "org_other": {"general": {"kb_retention_days": 30}},
}


def _cfg(url: str) -> Config:
    config = Config()
    config.set_main_option("script_location", _MIGRATIONS)
    config.set_main_option("sqlalchemy.url", url)
    return config


def _seed(conn) -> None:
    for org_id, settings in ORGS.items():
        conn.execute(
            sa.text("INSERT INTO organizations (id, name, slug, settings) VALUES (:id, :name, :slug, :settings)"),
            {"id": org_id, "name": org_id, "slug": org_id.replace("_", "-"), "settings": json.dumps(settings)},
        )
    conn.execute(
        sa.text("INSERT INTO organizations (id, name, slug, settings) VALUES ('org_garbage', 'g', 'g', 'not json')")
    )


@pytest.fixture
def pre_078_db(tmp_path: Path) -> Path:
    path = tmp_path / "m078.db"
    engine = sa.create_engine(f"sqlite:///{path}")
    prior = [t for name, t in Base.metadata.tables.items() if name not in NEW_TABLES]
    assert len(prior) == len(Base.metadata.tables) - len(NEW_TABLES)
    Base.metadata.create_all(engine, tables=prior)
    with engine.begin() as conn:
        _seed(conn)
    engine.dispose()
    command.stamp(_cfg(f"sqlite+aiosqlite:///{path}"), "077")
    return path


def _q(path: Path, sql: str) -> list[tuple]:
    engine = sa.create_engine(f"sqlite:///{path}")
    try:
        with engine.connect() as conn:
            return [tuple(r) for r in conn.execute(sa.text(sql))]
    finally:
        engine.dispose()


def _tables(path: Path) -> set[str]:
    engine = sa.create_engine(f"sqlite:///{path}")
    try:
        return set(sa.inspect(engine).get_table_names())
    finally:
        engine.dispose()


def test_upgrade_creates_tables_and_backfills_legacy_blocks(pre_078_db: Path, capsys) -> None:
    command.upgrade(_cfg(f"sqlite+aiosqlite:///{pre_078_db}"), "078")
    assert NEW_TABLES <= _tables(pre_078_db)
    rows = _q(pre_078_db, "SELECT org_id, revision, policy_json, policy_hash FROM org_skill_policies")
    assert [(org, rev) for org, rev, _, _ in rows] == [("org_legacy", 1)]
    assert json.loads(rows[0][2]) == {"skills": ORGS["org_legacy"]["skills"]}
    assert rows[0][3].startswith("sha256:")
    # The settings block is left in place (one release as a pointer).
    [(settings,)] = _q(pre_078_db, "SELECT settings FROM organizations WHERE id = 'org_legacy'")
    assert json.loads(settings) == ORGS["org_legacy"]
    out = capsys.readouterr().out
    assert "org_bad_mode" in out and "org_not_dict" in out


def test_backfilled_block_validates_under_the_current_schema(pre_078_db: Path) -> None:
    from sessionfs.skills.policy import parse_policy

    command.upgrade(_cfg(f"sqlite+aiosqlite:///{pre_078_db}"), "078")
    [(raw,)] = _q(pre_078_db, "SELECT policy_json FROM org_skill_policies")
    assert parse_policy(json.loads(raw)).skills.mode == "allowlist"


def test_reports_are_unique_per_device_org_and_per_day(pre_078_db: Path) -> None:
    command.upgrade(_cfg(f"sqlite+aiosqlite:///{pre_078_db}"), "078")
    engine = sa.create_engine(f"sqlite:///{pre_078_db}")
    try:
        with engine.begin() as conn:
            conn.execute(sa.text("INSERT INTO users (id, email, tier, is_active, email_verified, created_at) VALUES ('u1', 'u1@example.com', 'free', 1, 1, CURRENT_TIMESTAMP)"))
            conn.execute(sa.text("INSERT INTO skill_devices (id, user_id, device_key) VALUES ('d1', 'u1', 'k')"))
            conn.execute(sa.text("INSERT INTO device_policy_reports (id, device_id, org_id) VALUES ('r1', 'd1', 'org_legacy')"))
            conn.execute(sa.text(
                "INSERT INTO device_policy_report_history (id, device_id, org_id, report_day) "
                "VALUES ('h1', 'd1', 'org_legacy', '2026-10-20')"
            ))
        for sql in (
            "INSERT INTO device_policy_reports (id, device_id, org_id) VALUES ('r2', 'd1', 'org_legacy')",
            "INSERT INTO device_policy_report_history (id, device_id, org_id, report_day) "
            "VALUES ('h2', 'd1', 'org_legacy', '2026-10-20')",
        ):
            with pytest.raises(sa.exc.IntegrityError), engine.begin() as conn:
                conn.execute(sa.text(sql))
    finally:
        engine.dispose()


def test_downgrade_and_reupgrade(pre_078_db: Path) -> None:
    config = _cfg(f"sqlite+aiosqlite:///{pre_078_db}")
    command.upgrade(config, "078")
    command.downgrade(config, "077")
    assert not (NEW_TABLES & _tables(pre_078_db))
    command.upgrade(config, "078")
    assert len(_q(pre_078_db, "SELECT id FROM org_skill_policies")) == 1


def _pg(url: str, work) -> object:
    """Run ``work(conn)`` on a sync facade of an asyncpg connection."""
    import asyncio

    from sqlalchemy.ext.asyncio import create_async_engine

    async def _go():
        engine = create_async_engine(url)
        try:
            async with engine.begin() as conn:
                return await conn.run_sync(work)
        finally:
            await engine.dispose()

    return asyncio.run(_go())


@pytest.mark.skipif(not os.environ.get("SFS_TEST_PG_URL"), reason="SFS_TEST_PG_URL not set")
def test_upgrade_on_postgres() -> None:
    """Against the full real prior chain (001-077) on PostgreSQL."""
    reset_pg_schema()  # the chain runs from an empty database
    url = os.environ["SFS_TEST_PG_URL"]
    config = _cfg(url)
    command.upgrade(config, "077")
    command.downgrade(config, "077")  # a database already at 078 starts over

    def seed(conn) -> None:
        conn.execute(sa.text("DELETE FROM organizations WHERE id LIKE 'org\\_%'"))
        _seed(conn)

    _pg(url, seed)
    try:
        command.upgrade(config, "078")

        def check(conn) -> None:
            rows = conn.execute(sa.text("SELECT org_id, revision FROM org_skill_policies")).all()
            assert [tuple(r) for r in rows] == [("org_legacy", 1)]
            # Immutable revisions: one row per (org, revision).
            nested = conn.begin_nested()
            with pytest.raises(sa.exc.IntegrityError):
                conn.execute(sa.text(
                    "INSERT INTO org_skill_policies (id, org_id, revision, policy_json, policy_hash) "
                    "VALUES ('osp_dup', 'org_legacy', 1, '{}', 'sha256:x')"
                ))
            nested.rollback()
            # The report defaults render on PostgreSQL.
            conn.execute(sa.text(
                "INSERT INTO users (id, email, tier, is_active, email_verified, created_at) "
                "VALUES ('u_pg078', 'pg078@example.com', 'free', true, true, now())"
            ))
            conn.execute(sa.text(
                "INSERT INTO skill_devices (id, user_id, device_key) VALUES ('dev_pg', 'u_pg078', 'd1')"
            ))
            conn.execute(sa.text(
                "INSERT INTO device_policy_reports (id, device_id, org_id) VALUES ('dpr_pg', 'dev_pg', 'org_legacy')"
            ))
            compliant = conn.execute(sa.text("SELECT compliant FROM device_policy_reports")).scalar()
            assert compliant is False
            # One latest report per (device, org)...
            nested = conn.begin_nested()
            with pytest.raises(sa.exc.IntegrityError):
                conn.execute(sa.text(
                    "INSERT INTO device_policy_reports (id, device_id, org_id) VALUES ('dpr_pg2', 'dev_pg', 'org_legacy')"
                ))
            nested.rollback()
            # ...and one history row per (device, org, day).
            conn.execute(sa.text(
                "INSERT INTO device_policy_report_history (id, device_id, org_id, report_day) "
                "VALUES ('dph_pg', 'dev_pg', 'org_legacy', '2026-10-20')"
            ))
            nested = conn.begin_nested()
            with pytest.raises(sa.exc.IntegrityError):
                conn.execute(sa.text(
                    "INSERT INTO device_policy_report_history (id, device_id, org_id, report_day) "
                    "VALUES ('dph_pg2', 'dev_pg', 'org_legacy', '2026-10-20')"
                ))
            nested.rollback()

        _pg(url, check)
        command.downgrade(config, "077")
        command.upgrade(config, "078")
        assert _pg(url, lambda conn: conn.execute(sa.text("SELECT count(*) FROM org_skill_policies")).scalar()) == 1
    finally:
        def cleanup(conn) -> None:
            conn.execute(sa.text("DELETE FROM organizations WHERE id LIKE 'org\\_%'"))
            conn.execute(sa.text("DELETE FROM users WHERE id = 'u_pg078'"))

        _pg(url, cleanup)
