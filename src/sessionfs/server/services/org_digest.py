"""Weekly org activity digest builder (GitHub #97).

Builds the per-org 7-day activity summary that the weekly digest email
renders. Everything is org-scoped: only the org's own projects contribute;
members' personal projects and other orgs' projects are excluded. Queries
are bounded aggregates (``GROUP BY`` with a top-5 ``LIMIT`` for the session
leaderboards), never full table scans of sessions/tickets into memory.

Week boundaries are Monday 00:00 UTC to the following Monday. The digest
covers the most recently COMPLETED week relative to ``now``: ``week_start``
is the Monday that began the previous full 7 days. A Monday-morning
scheduled job therefore summarizes the prior calendar week, and a mid-week
re-run of the sweep stays idempotent because it computes the same
``week_start``.

Notes on interpretation (see the ticket's design decisions):
- ``tickets.opened`` counts ``Ticket.created_at`` in the window; the
  per-persona breakdown uses ``created_by_persona`` (nullable: human-created
  tickets are not attributed to a persona).
- ``tickets.completed`` counts ``Ticket.status == 'done'`` AND
  ``resolved_at`` in the window; the per-persona breakdown uses
  ``assigned_to``.
- ``agent_runs.severity_rollup`` rolls up ``findings_count`` by the run's
  own ``severity`` (individual findings have no guaranteed per-finding
  severity), so a run with N findings at severity "high" contributes N to
  the "high" bucket.
- ``sessions.top_users`` names only current org members; sessions from users
  who are no longer members are summed into ``sessions.former_members``.
- ``knowledge.claims_added`` counts new ``claim_class == 'claim'`` entries;
  ``claims_promoted`` counts ``promoted_at`` in the window (notes promoted
  to claims); ``claims_dismissed`` counts ``dismissed_at`` in the window.
- ``admin_actions`` is grouped from ``OrgAuditEvent.event_type`` (the
  org-scoped, append-only audit trail), NOT the platform-level
  ``AdminAction`` table which has no ``org_id``.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server.db.models import (
    AgentRun,
    Handoff,
    KnowledgeEntry,
    OrgAuditEvent,
    OrgMember,
    Project,
    Session,
    Ticket,
    User,
)

_TOP_N = 5


def monday_start(dt: datetime) -> date:
    """The Monday 00:00 UTC on or before ``dt``."""
    day = dt.astimezone(timezone.utc).date()
    return day - timedelta(days=day.weekday())


def digest_week(now: datetime) -> tuple[date, datetime, datetime]:
    """The completed week ending at ``now``: (week_start, start_dt, end_dt).

    ``week_start`` is the Monday that began the most recent full 7 days;
    the window is ``[start_dt, end_dt)``.
    """
    week_start = monday_start(now) - timedelta(days=7)
    start_dt = datetime.combine(week_start, time.min, tzinfo=timezone.utc)
    end_dt = start_dt + timedelta(days=7)
    return week_start, start_dt, end_dt


async def _org_project_ids(db: AsyncSession, org_id: str) -> list[str]:
    rows = await db.execute(select(Project.id).where(Project.org_id == org_id))
    return list(rows.scalars().all())


async def _org_member_ids(db: AsyncSession, org_id: str) -> set[str]:
    rows = await db.execute(
        select(OrgMember.user_id).where(OrgMember.org_id == org_id)
    )
    return set(rows.scalars().all())


def _empty(org_id: str, week_start: date) -> dict:
    return {
        "org_id": org_id,
        "week_start": week_start.isoformat(),
        "sessions": {
            "total": 0,
            "top_users": [],
            "top_projects": [],
            "former_members": 0,
        },
        "tickets": {
            "opened": 0,
            "completed": 0,
            "opened_by_persona": {},
            "completed_by_persona": {},
        },
        "agent_runs": {"total": 0, "by_status": {}, "severity_rollup": {}},
        "knowledge": {"claims_added": 0, "claims_promoted": 0, "claims_dismissed": 0},
        "handoffs": {"created": 0, "claimed": 0},
        "admin_actions": {"total": 0, "by_action": {}},
    }


async def build_org_digest(
    db: AsyncSession, org_id: str, week_start: date
) -> dict:
    """Build the digest payload for one org and one completed week."""
    start_dt = datetime.combine(week_start, time.min, tzinfo=timezone.utc)
    end_dt = start_dt + timedelta(days=7)

    proj_ids = await _org_project_ids(db, org_id)
    if not proj_ids:
        return _empty(org_id, week_start)

    result = _empty(org_id, week_start)

    # ── sessions: total + top users + top projects ──
    session_filters = (
        Session.project_id.in_(proj_ids),
        Session.is_deleted.is_(False),
        Session.created_at >= start_dt,
        Session.created_at < end_dt,
    )
    result["sessions"]["total"] = (
        await db.execute(select(func.count(Session.id)).where(*session_filters))
    ).scalar_one()

    # Named top users are restricted to current org members; sessions from
    # people who have since left the org are aggregated as "former members"
    # rather than being named (privacy: don't surface departed members).
    member_ids = await _org_member_ids(db, org_id)
    top_user_rows = (
        await db.execute(
            select(Session.user_id, func.count(Session.id).label("n"))
            .where(*session_filters, Session.user_id.in_(member_ids))
            .group_by(Session.user_id)
            .order_by(func.count(Session.id).desc(), Session.user_id)
            .limit(_TOP_N)
        )
    ).all()
    if top_user_rows:
        user_ids = [user_id for user_id, _ in top_user_rows]
        user_rows = await db.execute(select(User.id, User.display_name, User.email).where(User.id.in_(user_ids)))
        users = {u_id: (name, email) for u_id, name, email in user_rows.all()}
        result["sessions"]["top_users"] = [
            {"user_id": user_id, "display_name": users.get(user_id, (None, None))[0],
             "email": users.get(user_id, (None, None))[1], "sessions": n}
            for user_id, n in top_user_rows
        ]
    result["sessions"]["former_members"] = (
        await db.execute(
            select(func.count(Session.id)).where(
                *session_filters, Session.user_id.not_in(member_ids)
            )
        )
    ).scalar_one() if member_ids else result["sessions"]["total"]

    top_proj_rows = (
        await db.execute(
            select(Session.project_id, func.count(Session.id).label("n"))
            .where(*session_filters)
            .group_by(Session.project_id)
            .order_by(func.count(Session.id).desc(), Session.project_id)
            .limit(_TOP_N)
        )
    ).all()
    if top_proj_rows:
        pids = [pid for pid, _ in top_proj_rows]
        proj_rows = await db.execute(select(Project.id, Project.name).where(Project.id.in_(pids)))
        names = dict(proj_rows.all())
        result["sessions"]["top_projects"] = [
            {"project_id": pid, "name": names.get(pid), "sessions": n}
            for pid, n in top_proj_rows
        ]

    # ── tickets: opened / completed with per-persona breakdown ──
    opened_filters = (
        Ticket.project_id.in_(proj_ids),
        Ticket.created_at >= start_dt,
        Ticket.created_at < end_dt,
    )
    result["tickets"]["opened"] = (
        await db.execute(select(func.count(Ticket.id)).where(*opened_filters))
    ).scalar_one()
    opened_persona = (
        await db.execute(
            select(Ticket.created_by_persona, func.count(Ticket.id))
            .where(*opened_filters, Ticket.created_by_persona.is_not(None))
            .group_by(Ticket.created_by_persona)
        )
    ).all()
    result["tickets"]["opened_by_persona"] = dict(opened_persona)

    completed_filters = (
        Ticket.project_id.in_(proj_ids),
        Ticket.status == "done",
        Ticket.resolved_at.is_not(None),
        Ticket.resolved_at >= start_dt,
        Ticket.resolved_at < end_dt,
    )
    result["tickets"]["completed"] = (
        await db.execute(select(func.count(Ticket.id)).where(*completed_filters))
    ).scalar_one()
    completed_persona = (
        await db.execute(
            select(Ticket.assigned_to, func.count(Ticket.id))
            .where(*completed_filters, Ticket.assigned_to.is_not(None))
            .group_by(Ticket.assigned_to)
        )
    ).all()
    result["tickets"]["completed_by_persona"] = dict(completed_persona)

    # ── agent runs: total + by status + severity rollup ──
    run_filters = (
        AgentRun.project_id.in_(proj_ids),
        AgentRun.created_at >= start_dt,
        AgentRun.created_at < end_dt,
    )
    result["agent_runs"]["total"] = (
        await db.execute(select(func.count(AgentRun.id)).where(*run_filters))
    ).scalar_one()
    by_status = (
        await db.execute(
            select(AgentRun.status, func.count(AgentRun.id))
            .where(*run_filters)
            .group_by(AgentRun.status)
        )
    ).all()
    result["agent_runs"]["by_status"] = dict(by_status)
    # Group by the raw column and map NULL -> "none" in Python: coalescing
    # the literal into both SELECT and GROUP BY binds "none" twice, which
    # PostgreSQL rejects as a GROUP BY expression mismatch (GroupingError).
    severity_rollup = (
        await db.execute(
            select(
                AgentRun.severity,
                func.sum(AgentRun.findings_count),
            )
            .where(*run_filters)
            .group_by(AgentRun.severity)
        )
    ).all()
    rollup: dict[str, int] = {}
    for sev, total in severity_rollup:
        key = sev or "none"
        rollup[key] = rollup.get(key, 0) + int(total or 0)
    result["agent_runs"]["severity_rollup"] = rollup

    # ── knowledge: claims added / promoted / dismissed ──
    result["knowledge"]["claims_added"] = (
        await db.execute(
            select(func.count(KnowledgeEntry.id)).where(
                KnowledgeEntry.project_id.in_(proj_ids),
                KnowledgeEntry.claim_class == "claim",
                KnowledgeEntry.created_at >= start_dt,
                KnowledgeEntry.created_at < end_dt,
            )
        )
    ).scalar_one()
    result["knowledge"]["claims_promoted"] = (
        await db.execute(
            select(func.count(KnowledgeEntry.id)).where(
                KnowledgeEntry.project_id.in_(proj_ids),
                KnowledgeEntry.promoted_at.is_not(None),
                KnowledgeEntry.promoted_at >= start_dt,
                KnowledgeEntry.promoted_at < end_dt,
            )
        )
    ).scalar_one()
    result["knowledge"]["claims_dismissed"] = (
        await db.execute(
            select(func.count(KnowledgeEntry.id)).where(
                KnowledgeEntry.project_id.in_(proj_ids),
                KnowledgeEntry.dismissed_at.is_not(None),
                KnowledgeEntry.dismissed_at >= start_dt,
                KnowledgeEntry.dismissed_at < end_dt,
            )
        )
    ).scalar_one()

    # ── handoffs: created / claimed (attributed via source project) ──
    result["handoffs"]["created"] = (
        await db.execute(
            select(func.count(Handoff.id)).where(
                Handoff.source_project_id.in_(proj_ids),
                Handoff.created_at >= start_dt,
                Handoff.created_at < end_dt,
            )
        )
    ).scalar_one()
    result["handoffs"]["claimed"] = (
        await db.execute(
            select(func.count(Handoff.id)).where(
                Handoff.source_project_id.in_(proj_ids),
                Handoff.claimed_at.is_not(None),
                Handoff.claimed_at >= start_dt,
                Handoff.claimed_at < end_dt,
            )
        )
    ).scalar_one()

    # ── admin actions: org audit events grouped by event_type ──
    by_action = (
        await db.execute(
            select(OrgAuditEvent.event_type, func.count(OrgAuditEvent.id))
            .where(
                OrgAuditEvent.org_id == org_id,
                OrgAuditEvent.created_at >= start_dt,
                OrgAuditEvent.created_at < end_dt,
            )
            .group_by(OrgAuditEvent.event_type)
        )
    ).all()
    result["admin_actions"]["by_action"] = dict(by_action)
    result["admin_actions"]["total"] = sum(result["admin_actions"]["by_action"].values())

    return result
