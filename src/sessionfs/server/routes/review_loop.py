"""Review Loop rollup endpoints (Review Loop R3, Brief E2).

Three read-only surfaces under `/api/v1/projects/{project_id}`:

    GET  /{project_id}/tickets/{ticket_id}/loop-report   — per-round rollup
    GET  /{project_id}/loop-insights?from=&to=           — caught / cost / learning
    GET  /{project_id}/tickets/{ticket_id}/story         — ordered event stream

Dollar figures follow the same visibility rule as spend: serialized only for
org owners/admins (or the owner of a personal project), never for service
keys — the ``cost_usd`` key is ABSENT (not null, not zero) for everyone else.
Rollups are available on every plan (no tier gate).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server.auth.dependencies import AuthContext, require_scope
from sessionfs.server.db.engine import get_db
from sessionfs.server.routes.spend import _parse_range, _scope
from sessionfs.server.routes.tickets import _get_ticket_or_404
from sessionfs.server.services import review_loop

router = APIRouter(prefix="/api/v1/projects", tags=["review-loop"])


@router.get("/{project_id}/tickets/{ticket_id}/loop-report")
async def get_loop_report(
    project_id: str,
    ticket_id: str,
    auth: AuthContext = Depends(require_scope("loops:read")),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Per-round rollup: findings by harm, class/design counts, tokens, dollars."""
    project, show_dollars, _ = await _scope(project_id, auth, db)
    ticket = await _get_ticket_or_404(project.id, ticket_id, db)
    result = await review_loop.loop_report(
        db, project_id=project.id, ticket_id=ticket.id, show_dollars=show_dollars
    )
    return {"project_id": project.id, "ticket_id": ticket.id, **result}


@router.get("/{project_id}/loop-insights")
async def get_loop_insights(
    project_id: str,
    from_raw: str | None = Query(None, alias="from"),
    to_raw: str | None = Query(None, alias="to"),
    auth: AuthContext = Depends(require_scope("loops:read")),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Project insights: caught findings, cost per accepted change, learning.

    The ``learning`` list maps each checklist class to ``title`` and a
    ``joined_at`` date string (nullable — active items never accepted, or a
    schema without the checklist table, report ``null``) plus a zero-filled
    weekly ``series``.
    """
    project, show_dollars, _ = await _scope(project_id, auth, db)
    from_dt, to_dt = _parse_range(from_raw, to_raw, max_days=366)
    result = await review_loop.loop_insights(
        db,
        project_id=project.id,
        from_dt=from_dt,
        to_dt=to_dt,
        show_dollars=show_dollars,
    )
    return {
        "project_id": project.id,
        "from": from_dt.isoformat(),
        "to": to_dt.isoformat(),
        **result,
    }


@router.get("/{project_id}/tickets/{ticket_id}/story")
async def get_ticket_story(
    project_id: str,
    ticket_id: str,
    auth: AuthContext = Depends(require_scope("loops:read")),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Ordered event stream for a ticket's loop history."""
    project, show_dollars, _ = await _scope(project_id, auth, db)
    ticket = await _get_ticket_or_404(project.id, ticket_id, db)
    return await review_loop.ticket_story(
        db, project_id=project.id, ticket=ticket, show_dollars=show_dollars
    )
