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

from sessionfs.server.db.models import (
    ContextCompilation,
    KnowledgeEntry,
    Project,
    ProjectGroup,
    ProjectGroupAccess,
    User,
)
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


# A group claimed by a sweep is left alone by other sweeps for this long, so
# overlapping runs never compile it twice; a skip or failure also waits this
# long before the group is retried.
GROUP_CLAIM_LEASE = timedelta(minutes=10)


def _group_due_condition(now: datetime):
    """An opted-in group is due when it was never compiled, or when any
    member project has a ``ContextCompilation`` newer than the group's last
    compile — whether that project compiled in this sweep, by hand, or
    through the API — and no sweep claimed it within ``GROUP_CLAIM_LEASE``."""
    newer_member_compile = (
        select(ContextCompilation.id)
        .join(
            ProjectGroupAccess,
            ProjectGroupAccess.project_id == ContextCompilation.project_id,
        )
        .where(
            ProjectGroupAccess.group_id == ProjectGroup.id,
            ContextCompilation.compiled_at > ProjectGroup.context_compiled_at,
        )
        .correlate(ProjectGroup)
        .exists()
    )
    return and_(
        ProjectGroup.compile_enabled.is_(True),
        ProjectGroup.deleted_at.is_(None),
        or_(ProjectGroup.context_compiled_at.is_(None), newer_member_compile),
        _claimable(now),
    )


def _claimable(now: datetime):
    return or_(
        ProjectGroup.compile_attempted_at.is_(None),
        ProjectGroup.compile_attempted_at <= now - GROUP_CLAIM_LEASE,
    )


async def select_due_groups(
    db: AsyncSession, *, limit: int, now: datetime | None = None
) -> list[str]:
    """Ids of up to ``limit`` × ``SELECT_OVERFETCH_FACTOR`` due groups, those
    never attempted first, then the least recently attempted. Every attempt
    (success, skip or failure) stamps ``compile_attempted_at``, so a group
    that keeps being skipped or failing rotates behind healthy ones instead of
    starving them."""
    now = now or datetime.now(timezone.utc)
    return list(
        (
            await db.execute(
                select(ProjectGroup.id)
                .where(_group_due_condition(now))
                .order_by(
                    ProjectGroup.compile_attempted_at.is_(None).desc(),
                    ProjectGroup.compile_attempted_at.asc(),
                    ProjectGroup.id.asc(),
                )
                .limit(limit * SELECT_OVERFETCH_FACTOR)
            )
        ).scalars().all()
    )


async def claim_group(db: AsyncSession, group_id: str, now: datetime) -> bool:
    """Claim ``group_id`` for this sweep by stamping ``compile_attempted_at``
    with a conditional UPDATE, committed at once so the stamp survives a
    failed compile. Returns False when another sweep claimed it within the
    lease (on PostgreSQL the second UPDATE waits on the row lock, then
    re-checks the condition and matches nothing) or it is no longer enabled."""
    claimed = await db.execute(
        update(ProjectGroup)
        .where(
            ProjectGroup.id == group_id,
            ProjectGroup.compile_enabled.is_(True),
            ProjectGroup.deleted_at.is_(None),
            _claimable(now),
        )
        .values(compile_attempted_at=now)
        .execution_options(synchronize_session=False)
    )
    await db.commit()
    return claimed.rowcount == 1


async def _group_skip_reason(db: AsyncSession, group: ProjectGroup) -> str | None:
    """Why a scheduled recompile of ``group`` can't run, or None.

    The recompile is attributed to (and scoped by the read access of) the
    group's creator, so the creator must still be an active user with
    org-admin or group-admin standing, and the org's plan must still include
    ``project_groups``."""
    from sessionfs.server.auth.project_access import GROUP_ROLE_ADMIN, user_group_role
    from sessionfs.server.tier_gate import resolve_org_plan_tier

    creator_id = group.created_by_user_id
    if creator_id is None:
        return "group has no creator"
    creator = await db.get(User, creator_id)
    if creator is None or not creator.is_active:
        return "creator is inactive"
    if await user_group_role(db, creator_id, group) != GROUP_ROLE_ADMIN:
        return "creator lacks admin standing"
    plan = await resolve_org_plan_tier(group.org_id, db)
    if "project_groups" not in get_features_for_tier(plan):
        return "org plan lacks project_groups"
    return None


async def _recompile_due_groups(
    db: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    *,
    limit: int,
    now: datetime,
) -> dict:
    """Recompile up to ``limit`` opted-in groups whose context is stale.

    See ``_group_due_condition`` for when a group is stale. The recompile is
    attributed to the group's creator; a group whose creator lost standing
    (or whose org lost the feature) is skipped with a log line."""
    from sessionfs.server.services import group_compiler

    result: dict = {
        "groups_selected": 0,
        "groups_compiled": 0,
        "groups_skipped": 0,
        "groups_failed": 0,
        "groups_claimed_elsewhere": 0,
    }
    # Wall clock, never earlier than the caller's `now`: the claim lease and
    # the group's compile stamp must order after member compiles recorded
    # during this sweep.
    stamp = max(now, datetime.now(timezone.utc))
    group_ids = await select_due_groups(db, limit=limit, now=stamp)
    result["groups_selected"] = len(group_ids)
    # Release the read transaction before the loop (see the project sweep);
    # only the plain ids survive past the rollback.
    await db.rollback()

    for group_id in group_ids:
        if result["groups_compiled"] >= limit:
            break
        try:
            async with session_factory() as group_db:
                if not await claim_group(group_db, group_id, stamp):
                    result["groups_claimed_elsewhere"] += 1
                    continue
                loaded = await group_db.get(ProjectGroup, group_id)
                if loaded is None:
                    continue
                reason = await _group_skip_reason(group_db, loaded)
                if reason is not None:
                    logger.info("Skipping group %s recompile: %s", loaded.id, reason)
                    result["groups_skipped"] += 1
                    continue
                await group_compiler.compile_group_context(
                    group_db, loaded, loaded.created_by_user_id, now=stamp
                )
                loaded.updated_at = stamp
                await group_db.commit()
                result["groups_compiled"] += 1
        except Exception as exc:  # noqa: BLE001 - one failure must not stop the batch
            logger.warning(
                "Scheduled group recompile failed for group %s: %s",
                group_id,
                exc,
                exc_info=True,
            )
            result["groups_failed"] += 1
    return result


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
    sweep. After the project pass, up to ``limit`` opted-in groups whose
    context is stale (never compiled, or a member project compiled since)
    are recompiled (GitHub #101 phase 2).
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

    group_result = await _recompile_due_groups(
        db,
        session_factory,
        limit=limit,
        now=now,
    )
    result.update(group_result)
    return result
