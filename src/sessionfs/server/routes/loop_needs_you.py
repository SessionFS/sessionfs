"""Me-scoped "Needs you" inbox (Review Loop R3, Brief C3).

User keys only — ``Depends(get_current_user)`` rejects service keys with the
same 403 as every other ``/me`` route. The feed spans every project the
caller can read, so it is a plain listing with no per-project tier gate and
no dollar fields; ``can_act`` marks the items the caller has editor standing
on.
"""

from __future__ import annotations

import time

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server.auth.dependencies import get_current_user
from sessionfs.server.db.engine import get_db
from sessionfs.server.db.models import User
from sessionfs.server.services.loop_needs_you import needs_you

router = APIRouter(prefix="/api/v1/me/loops", tags=["loops"])

# Per-process sidebar-badge cache: user id -> (monotonic time, count).
_COUNT_TTL = 30.0
_COUNT_CACHE_MAX = 10_000
_count_cache: dict[str, tuple[float, int]] = {}


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
    # True when more loops exist than one request evaluates (newest first).
    truncated: bool = False


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
    result = await needs_you(db, user_id=user.id)
    await db.commit()  # keep approval requests raised for ready loop children
    return result


@router.get("/needs-you/count", response_model=NeedsYouCountResponse)
async def get_needs_you_count(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> NeedsYouCountResponse:
    """Total inbox count for the sidebar badge (not capped at 100).

    The badge is polled, so the count is served from a short per-user cache
    (``_COUNT_TTL`` seconds) instead of recomputing the whole feed each poll.
    """
    now = time.monotonic()
    cached = _count_cache.get(user.id)
    if cached is not None and now - cached[0] < _COUNT_TTL:
        return NeedsYouCountResponse(count=cached[1])
    result = await needs_you(db, user_id=user.id)
    await db.commit()  # keep approval requests raised for ready loop children
    count = result["counts"]["total"]
    if len(_count_cache) >= _COUNT_CACHE_MAX:
        _count_cache.clear()
    _count_cache[user.id] = (now, count)
    return NeedsYouCountResponse(count=count)
