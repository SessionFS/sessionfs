"""Operator metrics endpoint (GitHub #90).

``GET /api/v1/admin/metrics`` renders the in-process Prometheus registry as
text exposition format 0.0.4. It is authenticated: a platform-admin user key,
or a service key carrying ``admin_metrics:read`` whose owner and minter are
still active platform admins. Nothing else is exposed unauthenticated.

Metrics are per-process. Operators scrape every replica (pod annotations in the
Helm chart) and aggregate with ``sum()``. DB-derived gauges (active users,
session total, DB pool) are computed at scrape time and cached for 30 s so a
scrape storm doesn't hammer the database.
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Response
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server.auth.dependencies import AuthContext, require_admin_metrics
from sessionfs.server.db.engine import get_db
from sessionfs.server.db.models import Session, User
from sessionfs.server.metrics import (
    ACTIVE_USERS,
    CONTENT_TYPE,
    DB_POOL_CHECKED_OUT,
    DB_POOL_OVERFLOW,
    DB_POOL_SIZE,
    SESSIONS,
    render_metrics,
)

router = APIRouter(prefix="/api/v1/admin", tags=["metrics"])

# DB-derived gauges are recomputed at most once per window.
_DB_GAUGE_TTL_SECONDS = 30.0
_db_gauge_cache: dict = {"at": 0.0}

# Active-user windows: (label value, timedelta).
_ACTIVE_WINDOWS: tuple[tuple[str, timedelta], ...] = (
    ("1h", timedelta(hours=1)),
    ("1d", timedelta(days=1)),
    ("7d", timedelta(days=7)),
)


def reset_db_gauge_cache() -> None:
    """Force the next scrape to recompute DB-derived gauges (tests)."""
    _db_gauge_cache["at"] = 0.0


def _pool_gauges() -> tuple[int | None, int | None, int | None]:
    """(size, checked_out, overflow) from the async engine's sync pool.

    Returns ``None`` entries when there is no live pool (SQLite in tests, or
    the engine is not yet initialized) so the gauges stay absent rather than
    lying with zeros.
    """
    from sessionfs.server.db import engine as engine_mod

    eng = engine_mod._engine
    if eng is None:
        return None, None, None
    pool = getattr(eng.sync_engine, "pool", None)
    if pool is None:
        return None, None, None
    for attr in ("size", "checkedout", "overflow"):
        if not callable(getattr(pool, attr, None)):
            return None, None, None
    try:
        return pool.size(), pool.checkedout(), pool.overflow()
    except Exception:
        return None, None, None


async def _refresh_db_gauges(db: AsyncSession) -> None:
    now = time.monotonic()
    if now - _db_gauge_cache["at"] < _DB_GAUGE_TTL_SECONDS:
        return
    _db_gauge_cache["at"] = now

    cutoff_base = datetime.now(timezone.utc)
    for window, delta in _ACTIVE_WINDOWS:
        cutoff = cutoff_base - delta
        count = (
            await db.execute(
                select(func.count())
                .select_from(User)
                .where(User.is_active == True, User.last_sync_at >= cutoff)  # noqa: E712
            )
        ).scalar() or 0
        ACTIVE_USERS.set(float(count), window=window)

    sessions_total = (
        await db.execute(
            select(func.count()).select_from(Session).where(Session.is_deleted == False)  # noqa: E712
        )
    ).scalar() or 0
    SESSIONS.set(float(sessions_total))

    size, checked_out, overflow = _pool_gauges()
    if size is not None:
        DB_POOL_SIZE.set(float(size))
    if checked_out is not None:
        DB_POOL_CHECKED_OUT.set(float(checked_out))
    if overflow is not None:
        DB_POOL_OVERFLOW.set(float(overflow))


@router.get("/metrics")
async def get_metrics(
    auth: AuthContext = Depends(require_admin_metrics()),
    db: AsyncSession = Depends(get_db),
):
    """Render process metrics in Prometheus text format 0.0.4."""
    await _refresh_db_gauges(db)
    return Response(content=render_metrics(), media_type=CONTENT_TYPE)
