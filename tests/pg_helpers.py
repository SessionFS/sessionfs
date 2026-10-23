"""Helpers for tests that run against the shared SFS_TEST_PG_URL database.

Every PostgreSQL test file shares one scratch database (one xdist "postgres"
group). Migration tests need it EMPTY, because they run the chain from base,
while metadata tests `create_all` / `drop_all` the current model. `drop_all`
only drops the tables the model declares: it leaves `alembic_version` (and any
migration-only objects) behind, so a later migration test would start from a
stamped-but-empty schema. Resetting the whole `public` schema removes tables,
`alembic_version`, enum types and sequences in one step.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

PG_URL_ENV = "SFS_TEST_PG_URL"
_MIGRATIONS = str(
    Path(__file__).resolve().parents[1] / "src" / "sessionfs" / "server" / "db" / "migrations"
)


async def reset_pg_schema_async(url: str | None = None) -> None:
    """Drop and recreate the `public` schema of the scratch PostgreSQL database."""
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(url or os.environ[PG_URL_ENV])
    try:
        async with engine.begin() as conn:
            # Fail loudly instead of hanging if a leaked session still holds a lock.
            await conn.execute(text("SET LOCAL lock_timeout = '30s'"))
            await conn.execute(text("DROP SCHEMA IF EXISTS public CASCADE"))
            await conn.execute(text("CREATE SCHEMA public"))
    finally:
        await engine.dispose()


def reset_pg_schema(url: str | None = None) -> None:
    """Synchronous `reset_pg_schema_async`, for tests with no running event loop."""
    asyncio.run(reset_pg_schema_async(url))


def upgrade_pg_to_head(url: str | None = None) -> None:
    """Run the real migration chain on the scratch database up to head."""
    from alembic import command
    from alembic.config import Config

    config = Config()
    config.set_main_option("script_location", _MIGRATIONS)
    config.set_main_option("sqlalchemy.url", url or os.environ[PG_URL_ENV])
    command.upgrade(config, "head")


async def wait_for_lock_waiters(
    engine,
    count: int = 1,
    *,
    task: "asyncio.Future | None" = None,
    timeout: float = 20.0,
    interval: float = 0.02,
) -> None:
    """Block until `count` backends of the scratch database wait on a lock.

    A fixed `asyncio.sleep` before "assert the racer is still blocked" is a
    guess: under load the racer may not have reached its lock wait yet, so it
    sees the committed state instead of blocking. This polls pg_stat_activity
    (from its own, third connection) until `count` other backends report
    `wait_event_type = 'Lock'` (heavyweight and advisory lock waits), and
    fails with a clear message on timeout. If `task` finishes first it never
    blocked, which is reported immediately.
    """
    from sqlalchemy import text

    query = text(
        "SELECT count(*) FROM pg_stat_activity "
        "WHERE datname = current_database() AND pid <> pg_backend_pid() "
        "AND wait_event_type = 'Lock'"
    )
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    waiting = 0
    while True:
        async with engine.connect() as conn:
            waiting = (await conn.execute(query)).scalar_one()
        if waiting >= count:
            return
        if task is not None and task.done():
            raise AssertionError(
                f"task finished before any lock wait was observed (waiting={waiting}); "
                "it never blocked on the lock"
            )
        if loop.time() >= deadline:
            raise AssertionError(
                f"timed out after {timeout}s waiting for {count} backend(s) to wait "
                f"on a lock; only {waiting} did"
            )
        await asyncio.sleep(interval)
