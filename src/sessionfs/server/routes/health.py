"""Health check endpoint."""

from __future__ import annotations

from fastapi import APIRouter

from sessionfs import __version__

router = APIRouter()


@router.get("/health")
async def health():
    return {"status": "healthy", "version": __version__, "service": "sessionfs-api"}


@router.get("/health/pool")
async def pool_health():
    """Connection pool utilization metrics."""
    from sessionfs.server.db.engine import _engine

    if not _engine:
        return {"status": "not_initialized"}
    pool = _engine.pool
    size = pool.size()
    checked_out = pool.checkedout()
    overflow = pool.overflow()
    max_overflow = getattr(pool, "_max_overflow", 0)
    total_capacity = size + max_overflow
    utilization = round(checked_out / total_capacity * 100, 1) if total_capacity > 0 else 0
    return {
        "pool_size": size,
        "checked_in": pool.checkedin(),
        "checked_out": checked_out,
        "overflow": overflow,
        "max_overflow": max_overflow,
        "utilization_pct": utilization,
        "status": "healthy" if utilization < 80 else "warning" if utilization < 95 else "critical",
    }
