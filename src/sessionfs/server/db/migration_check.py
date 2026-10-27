"""Defense-in-depth check that the DB schema matches the code's head revision.

The Helm chart runs migrations as a hook before the API pods start, but if that
hook is skipped (ArgoCD without the hook annotation, or an out-of-band
`alembic upgrade head` that was never followed by a restart) the API can come up
against an out-of-date schema and fail authenticated routes with "column X does
not exist". This module lets startup detect that state and log a single
actionable error without blocking the process — the hosted service health-checks
/health, so startup must never fail or stall on this check.
"""

from __future__ import annotations

import asyncio
import functools
import logging
from pathlib import Path

logger = logging.getLogger("sessionfs.api.migrations")

_MIGRATIONS_DIR = Path(__file__).parent / "migrations"

# Readiness DB probe budget; must stay well below the Helm readinessProbe
# timeoutSeconds (5) so the handler always answers before Kubernetes gives up.
READINESS_DB_TIMEOUT_SECONDS = 1.0


@functools.lru_cache(maxsize=1)
def code_head_revision() -> str | None:
    """Return the single head revision bundled with this code, or None.

    Linear migration history means there is exactly one head; any other shape
    (zero or multiple heads) is a packaging bug we cannot reason about, so we
    report None and stay quiet rather than guess.
    """
    try:
        from alembic.script import ScriptDirectory
    except ImportError:  # pragma: no cover - alembic is a server dependency
        return None
    try:
        heads = ScriptDirectory(str(_MIGRATIONS_DIR)).get_heads()
    except Exception:
        # A dangling down_revision (e.g. a branch whose parent revision is not
        # merged yet) makes alembic unable to walk the chain. Report None
        # rather than raise — this module must never fail startup or readiness.
        return None
    if len(heads) != 1:
        return None
    return heads[0]


def describe_migration_gap(
    code_head: str,
    db_revision: str | None,
    known_revisions: frozenset[str] | set[str] | None = None,
) -> str | None:
    """Return an operator-facing message when the DB does not match code, else None.

    Conservative by design: if either side is unknown we return None so a fresh
    or unreachable database never trips a false alarm. ``known_revisions`` is
    every revision bundled with this code; a DB revision outside it means the
    database is AHEAD (e.g. the image was rolled back), which needs a different fix.
    """
    if not code_head or not db_revision:
        return None
    if db_revision == code_head:
        return None
    if known_revisions is not None and db_revision not in known_revisions:
        return (
            f"database schema is ahead of the running code: DB revision {db_revision!r} "
            f"is not known to this release (code head {code_head!r}); deploy the matching "
            "or a newer api image"
        )
    return (
        f"database schema is behind the running code: DB revision {db_revision!r} "
        f"vs code head {code_head!r}; run `alembic upgrade head`, then restart the api"
    )


@functools.lru_cache(maxsize=1)
def known_revisions() -> frozenset[str] | None:
    """Every revision id bundled with this code, or None if unavailable."""
    try:
        from alembic.script import ScriptDirectory
    except ImportError:  # pragma: no cover - alembic is a server dependency
        return None
    try:
        script = ScriptDirectory(str(_MIGRATIONS_DIR))
        return frozenset(rev.revision for rev in script.walk_revisions())
    except Exception:
        # Same dangling-down_revision case as ``code_head_revision``.
        return None


async def _read_db_revision(engine) -> str | None:
    """Read the current alembic revision from the database, or None."""
    from sqlalchemy import text

    async with engine.connect() as conn:
        result = await conn.execute(text("SELECT version_num FROM alembic_version"))
        row = result.first()
    return row[0] if row is not None else None


async def log_migration_gap_if_behind() -> None:
    """Best-effort startup check: log ONE error if the DB does not match code.

    Never raises and never blocks startup: a slow or unreachable database (or a
    DB that has not been migrated at all) must not prevent the API from serving
    /health. The database probe is bounded by a short timeout.
    """
    try:
        code_head = code_head_revision()
        if not code_head:
            return

        from sessionfs.server.db.engine import _engine

        if _engine is None:
            return

        try:
            db_revision = await asyncio.wait_for(_read_db_revision(_engine), timeout=5.0)
        except Exception:
            # Unreachable DB or missing alembic_version table — not a mismatch
            # we can assert, so stay quiet.
            return

        message = describe_migration_gap(code_head, db_revision, known_revisions())
    except Exception:
        logger.debug("migration revision check skipped", exc_info=True)
        return
    if message:
        logger.error(message)


async def schema_state() -> str:
    """Classify the database schema against this code, for readiness.

    Returns one of:
      - "current": DB is at the code's head.
      - "behind": DB is at an older revision this code knows. Not ready: the
        new code expects columns that do not exist yet.
      - "missing": the database has no alembic_version table (never migrated).
        Not ready.
      - "ahead": DB is at a revision this code does not know (e.g. the image was
        rolled back after a migration). Treated as ready, since migrations are
        additive and blocking it would make rollbacks impossible.
      - "unknown": the check could not run (DB unreachable, no engine). The
        readiness route treats it as not ready until the pod has confirmed the
        schema once, then as ready, so a brief blip does not drain every pod.
    """
    try:
        code_head = code_head_revision()
        if not code_head:
            return "unknown"

        from sqlalchemy.exc import DBAPIError

        from sessionfs.server.db.engine import _engine

        if _engine is None:
            return "unknown"
        try:
            # Well under the chart's 5 s probe timeout, so a hung database
            # reports "unknown" (ready) rather than timing the probe out.
            db_revision = await asyncio.wait_for(
                _read_db_revision(_engine), timeout=READINESS_DB_TIMEOUT_SECONDS
            )
        except DBAPIError as exc:
            # A missing table is a definite "never migrated"; anything else
            # (connection refused, timeout) is unknown.
            if "alembic_version" in str(exc.orig if exc.orig is not None else exc):
                return "missing"
            return "unknown"
        except Exception:
            return "unknown"
        if db_revision is None:
            return "missing"
        if db_revision == code_head:
            return "current"
        revisions = known_revisions()
        if revisions is not None and db_revision not in revisions:
            return "ahead"
        return "behind"
    except Exception:
        logger.debug("schema state check failed", exc_info=True)
        return "unknown"
