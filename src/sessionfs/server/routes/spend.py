"""Agent-run spend rollup endpoints (Review Loop R3, Brief A2).

Two read-only surfaces under `/api/v1/projects/{project_id}`:

    GET  /{project_id}/spend                       — project totals, grouped
    GET  /{project_id}/tickets/{ticket_id}/spend   — ticket totals + persona/model

Dollar figures are only serialized for org owners/admins (or the owner of
a personal project) and never for service keys; token totals are always
returned to callers who can read the runs (docs/design/agent-run-spend.md
§9.1). Rollups are available on every plan (no tier gate).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server.auth.dependencies import (
    AuthContext,
    assert_service_key_can_access_project,
    require_scope,
)
from sessionfs.server.auth.project_access import (
    accessible_project_ids_subquery,
    user_can_access_project,
    user_can_view_spend_dollars,
)
from sessionfs.server.db.engine import get_db
from sessionfs.server.db.models import Project, TicketComment
from sessionfs.server.routes.knowledge import _get_project_for_auth
from sessionfs.server.routes.tickets import _get_ticket_or_404
from sessionfs.server.services.agent_spend import DEFAULT_RANGE_DAYS, GROUP_BYS, rollup
from sqlalchemy.sql import Select

router = APIRouter(prefix="/api/v1/projects", tags=["agent-spend"])


def _parse_range(
    from_raw: str | None, to_raw: str | None, *, max_days: int | None = None
) -> tuple[datetime, datetime]:
    """Resolve the ``created_at`` window for the project view.

    Defaults to the last 30 days; a lone ``from`` or ``to`` extends the
    default in that direction. Returns a half-open, timezone-aware pair.
    ``max_days`` (optional) rejects a window wider than that with a 422 so
    rollups over it (e.g. weekly series) stay bounded.
    """
    to_dt = _parse_dt(to_raw) if to_raw else datetime.now(timezone.utc)
    if from_raw:
        from_dt = _parse_dt(from_raw)
    else:
        from_dt = to_dt - timedelta(days=DEFAULT_RANGE_DAYS)
    if from_dt >= to_dt:
        raise HTTPException(422, "from must be earlier than to")
    if max_days is not None and (to_dt - from_dt) > timedelta(days=max_days):
        raise HTTPException(422, f"range must be at most {max_days} days")
    return from_dt, to_dt


def _parse_dt(raw: str) -> datetime:
    try:
        # Python 3.10's fromisoformat rejects a trailing 'Z' (3.11+ accepts
        # it) — normalize so Z-suffixed timestamps parse identically.
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        raise HTTPException(422, "invalid ISO-8601 datetime")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


async def _scope(
    project_id: str, auth: AuthContext, db: AsyncSession
) -> tuple[Project, bool, Select | None]:
    """Authorize the project and return (project, show_dollars, readable).

    User keys get a byte-identical 404 for both "unknown" and "not yours"
    (existence-oracle protection, review checklist #2); service keys keep
    their org/allowlist 403 boundary."""
    if auth.key_kind == "service":
        project = await _get_project_for_auth(project_id, db, auth)
        await assert_service_key_can_access_project(db, auth, project)
        return project, False, None

    project = (
        await db.execute(select(Project).where(Project.id == project_id))
    ).scalar_one_or_none()
    if project is None or not await user_can_access_project(
        db, auth.user.id, project
    ):
        raise HTTPException(404, "Project not found")

    readable = await accessible_project_ids_subquery(db, auth.user.id)
    show_dollars = await user_can_view_spend_dollars(db, auth.user.id, project)
    return project, show_dollars, readable


@router.get("/{project_id}/spend")
async def get_project_spend(
    project_id: str,
    group_by: str | None = Query(
        None, description="persona | model | ticket | role | day"
    ),
    from_raw: str | None = Query(None, alias="from"),
    to_raw: str | None = Query(None, alias="to"),
    work_queue_id: str | None = Query(None, max_length=64),
    auth: AuthContext = Depends(require_scope("agent_runs:read", "tickets:read")),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Project spend totals, optionally grouped by persona/model/ticket/role/day."""
    if group_by is not None and group_by not in GROUP_BYS:
        raise HTTPException(422, f"group_by must be one of: {', '.join(GROUP_BYS)}")

    project, show_dollars, readable = await _scope(project_id, auth, db)
    from_dt, to_dt = _parse_range(from_raw, to_raw)

    result = await rollup(
        db,
        group_by=group_by,
        project_id=project.id,
        ticket_id=None,
        readable_projects=readable,
        from_dt=from_dt,
        to_dt=to_dt,
        show_dollars=show_dollars,
        work_queue_id=work_queue_id,
    )
    return {
        "project_id": project.id,
        "group_by": group_by,
        "work_queue_id": work_queue_id,
        "from": from_dt.isoformat(),
        "to": to_dt.isoformat(),
        **result,
    }


@router.get("/{project_id}/tickets/{ticket_id}/spend")
async def get_ticket_spend(
    project_id: str,
    ticket_id: str,
    auth: AuthContext = Depends(require_scope("agent_runs:read", "tickets:read")),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Ticket spend: totals + per-persona + per-model + review-round count."""
    project, show_dollars, readable = await _scope(project_id, auth, db)
    ticket = await _get_ticket_or_404(project.id, ticket_id, db)

    totals = await rollup(
        db,
        group_by=None,
        project_id=project.id,
        ticket_id=ticket.id,
        readable_projects=readable,
        from_dt=None,
        to_dt=None,
        show_dollars=show_dollars,
    )
    per_persona = await rollup(
        db,
        group_by="persona",
        project_id=project.id,
        ticket_id=ticket.id,
        readable_projects=readable,
        from_dt=None,
        to_dt=None,
        show_dollars=show_dollars,
    )
    per_model = await rollup(
        db,
        group_by="model",
        project_id=project.id,
        ticket_id=ticket.id,
        readable_projects=readable,
        from_dt=None,
        to_dt=None,
        show_dollars=show_dollars,
    )
    review_rounds = await _review_rounds(db, ticket.id)

    return {
        "project_id": project.id,
        "ticket_id": ticket.id,
        "totals": totals["totals"],
        "per_persona": per_persona["groups"],
        "per_model": per_model["groups"],
        "review_rounds": review_rounds,
    }


async def _review_rounds(db: AsyncSession, ticket_id: str) -> int:
    """Number of review rounds on a ticket (0 when none were trusted)."""
    from sessionfs.server.services.review_state import compute_review_state

    rows = (
        await db.execute(
            select(TicketComment)
            .where(TicketComment.ticket_id == ticket_id)
            .order_by(TicketComment.created_at, TicketComment.id)
            .limit(500)
        )
    ).scalars().all()
    comments = [
        {
            "id": r.id,
            "author_persona": r.author_persona,
            "content": r.content,
            "created_at": r.created_at,
            "verdict_trusted": r.verdict_trusted,
        }
        for r in rows
    ]
    state = compute_review_state(comments)
    return len(state.rounds) if state else 0
