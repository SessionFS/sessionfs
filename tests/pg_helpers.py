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
