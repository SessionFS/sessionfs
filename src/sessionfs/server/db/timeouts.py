"""Statement timeouts for heavy read routes (PostgreSQL only).

``bound_heavy_reads(db)`` puts ``SET LOCAL statement_timeout`` on the
session's current transaction and on every transaction the session begins
afterwards (an ``after_begin`` listener), so a route that commits part-way
keeps the bound. ``SET LOCAL`` reverts at commit/rollback, so the pooled
connection goes back with the server default. On SQLite it is a no-op.

A statement cancelled by the timeout raises SQLSTATE 57014; the global
``DBAPIError`` handler (server/errors.py) answers it with 503
``query_timeout`` instead of a 500.

The bound comes from ``SFS_HEAVY_READ_TIMEOUT_MS`` (default 15000; 0 turns
it off).
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger("sessionfs.db.timeouts")

DEFAULT_HEAVY_READ_TIMEOUT_MS = 15_000
_heavy_read_timeout_ms = DEFAULT_HEAVY_READ_TIMEOUT_MS
_INFO_KEY = "sfs_statement_timeout_ms"


def configure_heavy_read_timeout(ms: int) -> None:
    global _heavy_read_timeout_ms
    _heavy_read_timeout_ms = max(int(ms), 0)


def heavy_read_timeout_ms() -> int:
    return _heavy_read_timeout_ms


def _is_postgres(db: AsyncSession) -> bool:
    try:
        bind = db.get_bind()
    except Exception:  # noqa: BLE001 — unbound session: nothing to bound
        return False
    return getattr(getattr(bind, "dialect", None), "name", None) == "postgresql"


def _statement(ms: int) -> str:
    # An int we computed, never caller text.
    return f"SET LOCAL statement_timeout = {int(ms)}"


async def bound_heavy_reads(db: AsyncSession, ms: int | None = None) -> None:
    """Bound every statement this session runs from now on (PostgreSQL)."""
    if _heavy_read_timeout_ms <= 0:
        return  # turned off for this deployment
    value = _heavy_read_timeout_ms if ms is None else max(int(ms), 0)
    if value <= 0 or not _is_postgres(db):
        return
    sync_session = db.sync_session
    if _INFO_KEY not in sync_session.info:

        def _after_begin(session: Any, transaction: Any, connection: Any) -> None:
            current = session.info.get(_INFO_KEY)
            if current:
                connection.exec_driver_sql(_statement(current))

        event.listen(sync_session, "after_begin", _after_begin)
    sync_session.info[_INFO_KEY] = value
    # The listener covers transactions begun from here on; the one already
    # open (auth commits, then reads autobegin a new one) needs it now.
    if db.in_transaction():
        await db.execute(text(_statement(value)))
