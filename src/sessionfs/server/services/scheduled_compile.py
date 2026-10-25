"""Scheduled knowledge-base compile sweep (GitHub #93).

The operator endpoint ``POST /api/v1/admin/sweeps/kb-compile`` selects the
projects that are due a compile and compiles each one deterministically —
never through the LLM path — in its own transaction, so one failure cannot
roll back another project's work.

A project is due when it has at least one pending work item (a pending claim
or auto-promotable evidence) AND either:

* its schedule has elapsed since ``last_scheduled_compile_at``
  ('nightly' = 24h, 'weekly' = 7d; a project never compiled before is
  immediately due), or
* it has reached ``compile_after_claims`` pending work items (this fires
  regardless of schedule, so 'off' still compiles on threshold).

Projects with no pending work, and projects whose plan does not include the
``project_context`` feature (Free/Starter), are never selected.
"""

from __future__ import annotations

from typing import cast

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from sessionfs.server.db.models import KnowledgeEntry, Project
from sessionfs.server.services.knowledge import compile_work_condition
from sessionfs.server.tier_gate import resolve_project_plan_tiers
from sessionfs.server.tiers import get_features_for_tier

logger = logging.getLogger("sessionfs.scheduled_compile")

NIGHTLY_INTERVAL = timedelta(hours=24)
WEEKLY_INTERVAL = timedelta(days=7)

# Over-select the due set before the Python tier filter: Free/Starter
# projects are dropped after the plan lookup, so fetch a multiple of the
# requested limit to avoid under-filling the batch when many candidates are
# gated (GitHub #93 review round 2 LOW).
SELECT_OVERFETCH_FACTOR = 5


@dataclass(frozen=True)
class _DueProject:
    """The lightweight project fields the sweep selection + tier gate need.

    Selecting only these columns (instead of full ``Project`` rows) keeps the
    due-set query cheap for a large batch (GitHub #93 review round 2 LOW).
    """

    id: str
    org_id: str | None
    owner_id: str
    last_scheduled_compile_at: datetime | None


def _compile_work_subquery():
    """Count a project's pending work: claims PLUS auto-promotable evidence.

    Both will be folded into the context on the next compile (evidence is
    promoted to a claim in the compiler's housekeeping phase), so the sweep
    and compile-status treat them as one number (GitHub #93 review round 1
    MEDIUM).
    """
    return (
        select(
            KnowledgeEntry.project_id.label("project_id"),
            func.count(KnowledgeEntry.id).label("pending"),
        )
        .where(compile_work_condition())
        .group_by(KnowledgeEntry.project_id)
        .subquery()
    )


async def select_due_projects(
    db: AsyncSession,
    *,
    now: datetime,
    limit: int,
    ineligible_out: list[str] | None = None,
) -> list[tuple[str, int, str]]:
    """Return ``(project_id, pending_count, owner_id)`` for due projects.

    The inner join on the pending-count subquery means a project with zero
    pending work is never returned. Projects whose plan does not include
    ``project_context`` are filtered out (the sweep must not compile
    Free/Starter projects, and their owners cannot change the schedule).
    Selection is ordered by ``last_scheduled_compile_at`` NULLS FIRST then id
    so a project that keeps failing cannot starve the batch.

    Due projects dropped by the tier gate are appended to ``ineligible_out``
    so the caller can stamp them to the back of the queue; otherwise a pile of
    downgraded projects would sit ahead of every eligible one forever.
    """
    pending = _compile_work_subquery()
    nightly_cutoff = now - NIGHTLY_INTERVAL
    weekly_cutoff = now - WEEKLY_INTERVAL
    # Select only the columns the tier gate + ordering need, and over-fetch by
    # SELECT_OVERFETCH_FACTOR so the Python tier filter can drop Free/Starter
    # projects without under-filling the batch (GitHub #93 review round 2 LOW).
    stmt = (
        select(
            Project.id,
            Project.org_id,
            Project.owner_id,
            Project.last_scheduled_compile_at,
            pending.c.pending,
        )
        .join(pending, pending.c.project_id == Project.id)
        .where(
            or_(
                and_(
                    Project.compile_schedule == "nightly",
                    or_(
                        Project.last_scheduled_compile_at.is_(None),
                        Project.last_scheduled_compile_at <= nightly_cutoff,
                    ),
                ),
                and_(
                    Project.compile_schedule == "weekly",
                    or_(
                        Project.last_scheduled_compile_at.is_(None),
                        Project.last_scheduled_compile_at <= weekly_cutoff,
                    ),
                ),
                and_(
                    Project.compile_after_claims.is_not(None),
                    pending.c.pending >= Project.compile_after_claims,
                ),
            )
        )
        # NULLS FIRST, then oldest, then id — mirrors the Python sort below so
        # the SQL LIMIT keeps the highest-priority projects (a failing project
        # falls to the back and cannot starve the batch).
        .order_by(
            Project.last_scheduled_compile_at.is_(None).desc(),
            Project.last_scheduled_compile_at.asc(),
            Project.id.asc(),
        )
        .limit(limit * SELECT_OVERFETCH_FACTOR)
    )
    rows = (await db.execute(stmt)).all()
    candidates = [
        (
            _DueProject(
                id=project_id,
                org_id=org_id,
                owner_id=owner_id,
                last_scheduled_compile_at=last_scheduled_compile_at,
            ),
            cast(int, pending_count),
        )
        for project_id, org_id, owner_id, last_scheduled_compile_at, pending_count in rows
    ]

    # Tier gate — resolve every candidate's plan in bulk and keep only the
    # projects whose plan pays for project_context.
    if candidates:
        tiers = await resolve_project_plan_tiers(
            [project for project, _ in candidates], db
        )
    else:
        tiers = {}
    allowed: list[tuple[_DueProject, int]] = []
    for project, pending_count in candidates:
        if "project_context" in get_features_for_tier(tiers[project.id]):
            allowed.append((project, pending_count))
        elif ineligible_out is not None:
            ineligible_out.append(project.id)

    # NULLS FIRST, then oldest, then id — re-sort after the tier filter so the
    # final batch still follows the same priority as the SQL LIMIT above.
    def _order_key(item: tuple[_DueProject, int]) -> tuple:
        project, _pending = item
        last = project.last_scheduled_compile_at
        return (0 if last is None else 1, last, project.id)

    allowed.sort(key=_order_key)

    return [
        (project.id, pending_count, project.owner_id)
        for project, pending_count in allowed[:limit]
    ]


async def run_kb_compile_sweep(
    db: AsyncSession,
    *,
    limit: int,
    session_factory: async_sessionmaker[AsyncSession],
    now: datetime | None = None,
) -> dict:
    """Compile every due project, one transaction per project.

    ``db`` is used only for the read-only selection; each compile runs in a
    fresh session from ``session_factory`` so its commit cannot drag another
    project's work in or out. ``api_key=None`` forces the deterministic
    compiler — the sweep must never call the LLM. Each compilation is
    attributed to the project's owner, not the platform admin that ran the
    sweep.
    """
    from sessionfs.server.services.compiler import compile_project_context

    now = now or datetime.now(timezone.utc)
    ineligible: list[str] = []
    due = await select_due_projects(db, now=now, limit=limit, ineligible_out=ineligible)
    # Release the read transaction before the loop: the selection is already
    # materialized, and on file-backed SQLite a held read lock would block
    # each fresh session's commit (single writer).
    await db.rollback()

    result: dict = {
        "selected": len(due),
        "compiled": 0,
        "skipped_no_pending": 0,
        "failed": 0,
        "skipped_ineligible": len(ineligible),
        "errors": [],
    }
    if ineligible:
        # Rotate projects whose plan no longer includes project_context to the
        # back of the queue so they cannot crowd out eligible ones.
        async with session_factory() as stamp_db:
            for start in range(0, len(ineligible), 1000):
                await stamp_db.execute(
                    update(Project)
                    .where(Project.id.in_(ineligible[start : start + 1000]))
                    .values(last_scheduled_compile_at=now)
                )
            await stamp_db.commit()
    for project_id, _pending_count, owner_id in due:
        try:
            async with session_factory() as project_db:
                compilation = await compile_project_context(
                    project_id=project_id,
                    user_id=owner_id,
                    db=project_db,
                    api_key=None,
                    trigger="scheduled",
                    skip_access_check=True,
                )
            if compilation is None:
                # The batch drained since selection (e.g. a manual compile).
                result["skipped_no_pending"] += 1
            else:
                result["compiled"] += 1
        except Exception as exc:  # noqa: BLE001 - one failure must not stop the batch
            logger.warning(
                "Scheduled compile failed for project %s: %s",
                project_id,
                exc,
                exc_info=True,
            )
            # Record the failure timestamp so the failing project drops to
            # the back of the next selection and cannot starve the batch
            # (GitHub #93 review round 1 LOW).
            try:
                async with session_factory() as fail_db:
                    await fail_db.execute(
                        update(Project)
                        .where(Project.id == project_id)
                        .values(last_scheduled_compile_at=datetime.now(timezone.utc))
                    )
                    await fail_db.commit()
            except Exception:  # noqa: BLE001 - bookkeeping must not mask the failure
                logger.exception(
                    "Failed to record sweep failure timestamp for %s", project_id
                )
            result["failed"] += 1
            result["errors"].append({"project_id": project_id, "error": str(exc)[:200]})
    return result
