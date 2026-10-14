"""Health check endpoint."""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from sessionfs import __version__

router = APIRouter()


@router.get("/health")
@router.get("/api/v1/health")
async def health():
    """Health check.

    Exposed at both /health (the canonical path that pods inside the cluster
    hit directly) and /api/v1/health (the path the AWS ALB / nginx ingress
    rewrites incoming probes to). Both routes intentionally serve the
    identical payload so external monitoring can use either.
    """
    return {"status": "healthy", "version": __version__, "service": "sessionfs-api"}


# Set once this process has seen the schema at (or ahead of) its head.
_schema_confirmed = False


@router.get("/health/ready")
async def readiness():
    """Readiness: is the database schema migrated for this code?

    Kubernetes readiness probes use this (the Helm chart points its
    readinessProbe here) so a pod running new code against an unmigrated
    database is kept out of rotation instead of serving 500s. /health stays a
    pure liveness check. A database AHEAD of this code (a rollback) reports
    ready; an unreachable database reports ready only after this pod has
    confirmed the schema once. See migration_check.schema_state.
    """
    global _schema_confirmed
    from sessionfs.server.db.migration_check import schema_state

    state = await schema_state()
    if state in ("current", "ahead"):
        _schema_confirmed = True
    # "unknown" (database unreachable or slow) only counts as ready once this
    # pod has confirmed the schema at least once: a fresh pod must not take
    # traffic before the database is up, but a brief outage later should not
    # pull every pod out of service at once.
    ready = state in ("current", "ahead") or (state == "unknown" and _schema_confirmed)
    return JSONResponse(
        status_code=200 if ready else 503,
        content={"status": "ready" if ready else "not_ready", "schema": state},
    )


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
