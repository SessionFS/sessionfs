"""Migration 095: helper_configs + helper_usage (session-helper configuration
and the append-only token ledger).

SQLite: the real metadata minus the two new tables, stamped at 095's parent.
PostgreSQL: ``test_upgrade_on_postgres`` runs the real chain when
SFS_TEST_PG_URL is set. The parent revision is read from the script so a
release re-chain needs no test edit.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory

from sessionfs.server.db.models import Base
from tests.pg_helpers import reset_pg_schema

_MIGRATIONS = str(
    Path(__file__).resolve().parents[3] / "src" / "sessionfs" / "server" / "db" / "migrations"
)

NEW_TABLES = {"helper_configs", "helper_usage"}
CONFIG_COLUMNS = {
    "id", "scope", "org_id", "user_id", "settings", "instructions",
    "monthly_token_budget", "updated_at", "updated_by_user_id",
}
USAGE_COLUMNS = {
    "id", "org_id", "user_id", "key_source", "conversation_id",
    "input_tokens", "output_tokens", "stop_reason", "created_at",
}
CONFIG_INDEXES = {"uq_helper_configs_org", "uq_helper_configs_user"}
USAGE_INDEXES = {"idx_helper_usage_org_created", "idx_helper_usage_user_created"}


def _cfg(url: str) -> Config:
    config = Config()
    config.set_main_option("script_location", _MIGRATIONS)
    config.set_main_option("sqlalchemy.url", url)
    return config


def _prior_revision() -> str:
    revision = ScriptDirectory.from_config(_cfg("sqlite://")).get_revision("095")
    assert revision is not None and isinstance(revision.down_revision, str)
    return revision.down_revision


_SEED = [
    "INSERT INTO organizations (id, name, slug, settings) VALUES ('org_m095', 'o', 'o-m095', '{}')",
    "INSERT INTO users (id, email, display_name, tier, is_active, email_verified, created_at) "
    "VALUES ('u_m095', 'm095@example.com', 'u', 'enterprise', {true}, {true}, CURRENT_TIMESTAMP)",
    "INSERT INTO helper_conversations (id, user_id, title) VALUES ('hc_m095', 'u_m095', 't')",
]


def _seed_sql(true: str) -> list[str]:
    return [stmt.replace("{true}", true) for stmt in _SEED]


def _exercise(conn, integrity_error: type[Exception]) -> None:
    """Behaviour shared by both dialects: valid rows insert, the CHECKs and
    unique indexes refuse bad ones, and deleting a conversation keeps its
    ledger rows."""
    conn.execute(sa.text(
        "INSERT INTO helper_configs (id, scope, org_id, monthly_token_budget) "
        "VALUES ('hcfg_org', 'org', 'org_m095', 1000)"
    ))
    conn.execute(sa.text(
        "INSERT INTO helper_configs (id, scope, user_id, instructions) "
        "VALUES ('hcfg_user', 'user', 'u_m095', 'be brief')"
    ))
    bad_rows = [
        # Scope doesn't match the owner column.
        "INSERT INTO helper_configs (id, scope, user_id) VALUES ('b1', 'org', 'u_m095')",
        # Both owners set.
        "INSERT INTO helper_configs (id, scope, org_id, user_id) "
        "VALUES ('b2', 'org', 'org_m095', 'u_m095')",
        # Neither owner set.
        "INSERT INTO helper_configs (id, scope) VALUES ('b3', 'user')",
        # Budget is org-only.
        "INSERT INTO helper_configs (id, scope, user_id, monthly_token_budget) "
        "VALUES ('b4', 'user', 'u_m095', 5)",
        # One config per org.
        "INSERT INTO helper_configs (id, scope, org_id) VALUES ('b5', 'org', 'org_m095')",
        # Unknown key source on the ledger.
        "INSERT INTO helper_usage (id, user_id, key_source) VALUES ('b6', 'u_m095', 'other')",
    ]
    for stmt in bad_rows:
        nested = conn.begin_nested()
        with pytest.raises(integrity_error):
            conn.execute(sa.text(stmt))
        nested.rollback()

    conn.execute(sa.text(
        "INSERT INTO helper_usage (id, org_id, user_id, key_source, conversation_id, "
        "input_tokens, output_tokens, stop_reason) "
        "VALUES ('hu_m095', 'org_m095', 'u_m095', 'org', 'hc_m095', 10, 5, 'stop')"
    ))
    conn.execute(sa.text("DELETE FROM helper_conversations WHERE id = 'hc_m095'"))
    assert conn.execute(sa.text(
        "SELECT input_tokens + output_tokens FROM helper_usage WHERE id = 'hu_m095'"
    )).scalar() == 15


@pytest.fixture
def pre_095_db(tmp_path: Path) -> Path:
    path = tmp_path / "m095.db"
    engine = sa.create_engine(f"sqlite:///{path}")
    prior = [t for name, t in Base.metadata.tables.items() if name not in NEW_TABLES]
    assert len(prior) == len(Base.metadata.tables) - len(NEW_TABLES)
    Base.metadata.create_all(engine, tables=prior)
    with engine.begin() as conn:
        for stmt in _seed_sql("1"):
            conn.execute(sa.text(stmt))
    engine.dispose()
    command.stamp(_cfg(f"sqlite+aiosqlite:///{path}"), _prior_revision())
    return path


def _inspect(path: Path):
    engine = sa.create_engine(f"sqlite:///{path}")
    return engine, sa.inspect(engine)


def test_sqlite_upgrade_and_downgrade(pre_095_db: Path) -> None:
    engine, insp = _inspect(pre_095_db)
    assert NEW_TABLES.isdisjoint(insp.get_table_names())
    engine.dispose()

    config = _cfg(f"sqlite+aiosqlite:///{pre_095_db}")
    command.upgrade(config, "095")

    engine, insp = _inspect(pre_095_db)
    try:
        assert NEW_TABLES <= set(insp.get_table_names())
        assert CONFIG_COLUMNS <= {c["name"] for c in insp.get_columns("helper_configs")}
        assert USAGE_COLUMNS <= {c["name"] for c in insp.get_columns("helper_usage")}
        assert CONFIG_INDEXES <= {i["name"] for i in insp.get_indexes("helper_configs")}
        assert USAGE_INDEXES <= {i["name"] for i in insp.get_indexes("helper_usage")}
        # No foreign key on the ledger's conversation_id.
        fk_cols = {
            col for fk in insp.get_foreign_keys("helper_usage") for col in fk["constrained_columns"]
        }
        assert "conversation_id" not in fk_cols
        # The key rows are untouched by 095.
        assert "helper_settings" not in {c["name"] for c in insp.get_columns("org_llm_settings")}
        assert "helper_settings" not in {
            c["name"] for c in insp.get_columns("user_judge_settings")
        }
        with engine.connect() as conn:
            conn.execute(sa.text("PRAGMA foreign_keys=ON"))
            conn.commit()
            with conn.begin():
                _exercise(conn, sa.exc.IntegrityError)
    finally:
        engine.dispose()

    command.downgrade(config, _prior_revision())
    engine, insp = _inspect(pre_095_db)
    assert NEW_TABLES.isdisjoint(insp.get_table_names())
    engine.dispose()
    command.upgrade(config, "095")
    engine, insp = _inspect(pre_095_db)
    assert NEW_TABLES <= set(insp.get_table_names())
    engine.dispose()


def _pg(url: str, work) -> object:
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
    """Against the full real prior chain on PostgreSQL."""
    reset_pg_schema()
    url = os.environ["SFS_TEST_PG_URL"]
    config = _cfg(url)
    command.upgrade(config, _prior_revision())

    def seed(conn) -> None:
        for stmt in _seed_sql("true"):
            conn.execute(sa.text(stmt))

    _pg(url, seed)
    command.upgrade(config, "095")

    def check(conn) -> None:
        for table, columns, indexes in (
            ("helper_configs", CONFIG_COLUMNS, CONFIG_INDEXES),
            ("helper_usage", USAGE_COLUMNS, USAGE_INDEXES),
        ):
            present = set(conn.execute(sa.text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = 'public' AND table_name = :t"
            ), {"t": table}).scalars())
            assert columns <= present, f"{table} missing {columns - present}"
            idx = set(conn.execute(sa.text(
                "SELECT indexname FROM pg_indexes WHERE tablename = :t"
            ), {"t": table}).scalars())
            assert indexes <= idx, f"{table} missing {indexes - idx}"
        budget_type = conn.execute(sa.text(
            "SELECT data_type FROM information_schema.columns "
            "WHERE table_name = 'helper_configs' AND column_name = 'monthly_token_budget'"
        )).scalar()
        assert budget_type == "bigint"
        _exercise(conn, sa.exc.IntegrityError)

    _pg(url, check)

    command.downgrade(config, _prior_revision())

    def check_downgraded(conn) -> None:
        tables = set(conn.execute(sa.text(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'"
        )).scalars())
        assert NEW_TABLES.isdisjoint(tables)

    _pg(url, check_downgraded)
