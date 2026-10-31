"""Me-scoped "Needs you" inbox (Review Loop R3, Brief C3).

User keys only — ``Depends(get_current_user)`` rejects service keys with the
same 403 as every other ``/me`` route. The feed spans every project the
caller can read, so it is a plain listing with no per-project tier gate and
no dollar fields; ``can_act`` marks the items the caller has editor standing
on.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server.auth.dependencies import get_current_user
from sessionfs.server.db.engine import get_db
from sessionfs.server.db.models import User
from sessionfs.server.services.loop_needs_you import needs_you

router = APIRouter(prefix="/api/v1/me/loops", tags=["loops"])


class NeedsYouItem(BaseModel):
    kind: str
    project_id: str
    project_name: str
    loop_id: str | None = None
    ticket_id: str | None = None
    ticket_title: str | None = None
    checklist_item_id: str | None = None
    title: str
    reason: str
    can_act: bool


class NeedsYouCounts(BaseModel):
    ready: int
    needs_human: int
    proposed_checklist: int
    total: int


class NeedsYouResponse(BaseModel):
    items: list[NeedsYouItem]
    counts: NeedsYouCounts


class NeedsYouCountResponse(BaseModel):
    count: int


@router.get("/needs-you", response_model=NeedsYouResponse)
async def get_needs_you(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> NeedsYouResponse:
    """The caller's org-wide review-loop inbox, newest first (max 100 items)."""
    return await needs_you(db, user_id=user.id)


@router.get("/needs-you/count", response_model=NeedsYouCountResponse)
async def get_needs_you_count(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> NeedsYouCountResponse:
    """Total inbox count for the sidebar badge (not capped at 100)."""
    result = await needs_you(db, user_id=user.id)
    return NeedsYouCountResponse(count=result["counts"]["total"])
