"""Smoke test that `alembic upgrade head` succeeds on a fresh SQLite DB.

Catches the class of SQLite-incompat migration bugs Codex flagged on
migration 040 (UNIQUE constraint via separate ALTER TABLE step fails
on SQLite). 040 was fixed in place to define the constraint inline
at create_table time after Codex R7 caught that a follow-up migration
couldn't repair the chain — Alembic halts at the failed 040 revision
on fresh SQLite before reaching any repair migration.

DEFERRED — broader SQLite chain is also broken:

Running `alembic upgrade head` on SQLite currently fails at migration
003 (`CREATE INDEX idx_sessions_search ON sessions USING GIN(search_vector)`
— PostgreSQL-specific syntax). Migration 003 has never been
SQLite-compatible. Fixing 040's specific issue (now done in place)
doesn't make the full chain runnable on SQLite. The broader fix tracks
as `tk_7dc9e8764a5a4297`.

The smoke tests below are marked `xfail strict=True` — they flip to
`xpass` (loud failure) once the broader SQLite-compat issue is
resolved, prompting us to remove the xfail markers.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config


@pytest.fixture
def alembic_config(tmp_path: Path) -> Config:
    """Point Alembic at a fresh on-disk SQLite DB.

    Note: alembic's env.py calls `fileConfig(alembic.ini)` which defaults
    to `disable_existing_loggers=True` — that mutates the global logger
    state and breaks any later test that uses `caplog` against a
    non-root logger (e.g. `sfs.writeback`). We don't pass the ini path
    to Config so env.py skips fileConfig; the migration logic itself
    doesn't need the logger setup."""
    db_path = tmp_path / "migration_smoke.db"
    cfg = Config()
    cfg.set_main_option(
        "script_location", "src/sessionfs/server/db/migrations"
    )
    cfg.set_main_option(
        "sqlalchemy.url", f"sqlite+aiosqlite:///{db_path}"
    )
    return cfg


@pytest.mark.xfail(
    reason=(
        "Migration 003 uses PostgreSQL-specific 'CREATE INDEX ... USING GIN' "
        "syntax that SQLite can't parse. Broader fix tracked in "
        "tk_7dc9e8764a5a4297. Migration 040's wiki_page_revisions fix is "
        "correct in isolation but can't be reached on SQLite until 003+ are "
        "made cross-DB compatible."
    ),
    strict=True,
)
def test_upgrade_head_on_fresh_sqlite_succeeds(alembic_config: Config):
    """v0.10.7 R2-R7 HIGH (migration 040 SQLite-incompat, fixed in
    place after R7).

    Will flip to xpass once the broader SQLite migration chain is
    fixed (tk_7dc9e8764a5a4297) — prompting us to remove this xfail
    marker."""
    command.upgrade(alembic_config, "head")


@pytest.mark.xfail(
    reason="Blocked by same root cause as upgrade test — migration 003 PG-only syntax.",
    strict=True,
)
def test_downgrade_one_step_succeeds(alembic_config: Config):
    command.upgrade(alembic_config, "head")
    command.downgrade(alembic_config, "-1")
