"""Merge-check trigger endpoint (Review Loop R3, track F2a).

``POST /api/v1/projects/{project_id}/tickets/{ticket_id}/merge-check`` recomputes
merge readiness for the ticket's head commit and posts (or updates) the
SessionFS review-loop check run. Access mirrors the loop surface: an editor, or
a service key with ``loops:write`` on the project. Outsiders get the
byte-identical 404 (review checklist #2).
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server.auth.dependencies import AuthContext, require_scope
from sessionfs.server.db.engine import get_db
from sessionfs.server.routes.loop_checklist import _write_scope
from sessionfs.server.services import merge_checks

router = APIRouter(prefix="/api/v1/projects", tags=["merge-checks"])


@router.post("/{project_id}/tickets/{ticket_id}/merge-check")
async def post_ticket_merge_check(
    project_id: str,
    ticket_id: str,
    auth: AuthContext = Depends(require_scope("loops:write")),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Recompute and post a ticket's merge check. Returns the posted outcome."""
    project = await _write_scope(project_id, auth, db)
    return await merge_checks.refresh_merge_check(
        db, ticket_id=ticket_id, project_id=project.id
    )
