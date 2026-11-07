"""For You: the caller's ranked attention feed (tk_dc118ffd6e804a9a).

- ``GET  /api/v1/me/for-you``                       ranked items (ETag / 304)
- ``GET  /api/v1/me/for-you/count``                 per-band counts
- ``POST /api/v1/me/for-you/hide``                  hide until it changes
- ``GET  /api/v1/me/for-you/items/{key}/context``   "Before you start"
- ``POST /api/v1/me/for-you/collaboration/remind``  remind the reviewer or
  blocking-ticket owner a "Working with others" row is waiting on

Reads take ``attention:read``: user keys see their own feed; a service key
sees its minter's feed limited to the key's org and project allowlist. Hide
is a write to the person's own state and takes a user key. Every miss on the
context route is the same 404; a hide of an unknown key is the same 204 as a
real one.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server.auth.dependencies import AuthContext, get_current_user, require_scope
from sessionfs.server.db.engine import get_db
from sessionfs.server.db.models import User
from sessionfs.server.services import attention as svc

router = APIRouter(prefix="/api/v1/me/for-you", tags=["attention"])

_NOT_FOUND = {"code": "not_found", "error": "not_found", "message": "Item not found"}


class AttentionSource(BaseModel):
    type: str
    id: str


class AttentionItem(BaseModel):
    item_key: str
    kind: str
    band: Literal["now", "your_turn", "upcoming", "lately"]
    involvement: str
    occurred_at: str | None = None
    due_at: str | None = None
    title: str
    why: str
    link: str
    project_id: str | None = None
    actions: list[str]
    source: AttentionSource
    detail: dict[str, Any] | None = None
    group: str
    context: dict[str, Any] = Field(default_factory=dict)
    rank: list[dict[str, Any]] = Field(default_factory=list)


class AttentionCounts(BaseModel):
    now: int
    your_turn: int
    upcoming: int
    lately: int
    total: int
    # Items in the On your radar / Waiting on you / Your team is asking
    # sections: the sidebar badge.
    actionable: int


class AttentionSection(BaseModel):
    count: int
    error: str | None = None


class ChyronEntry(BaseModel):
    item_key: str
    category: str
    text: str
    ref: str


class AttentionFeedResponse(BaseModel):
    items: list[AttentionItem]
    counts: AttentionCounts
    groups: dict[str, int]
    sections: dict[str, AttentionSection]
    truncated: bool
    chyron: list[ChyronEntry]
    collaboration: dict[str, Any] | None = None


class CollaborationRemindRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    flow_key: str = Field(..., min_length=1, max_length=200)
    message: str = Field(..., min_length=1, max_length=4000)
    when: Literal["now", "tomorrow"] = "now"
    timezone: str | None = Field(default=None, max_length=64)


class CollaborationRemindResponse(BaseModel):
    reminder_id: str
    due_at: str
    to: str


class HideRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    item_key: str = Field(..., min_length=1, max_length=200)
    until: datetime | None = None


def _etag_matches(header: str | None, etag: str) -> bool:
    if not header:
        return False
    for candidate in header.split(","):
        c = candidate.strip()
        if c == "*" or c.removeprefix("W/") == etag:
            return True
    return False


@router.get("", response_model=AttentionFeedResponse, response_model_exclude_none=False)
async def get_for_you(
    response: Response,
    band: Literal["now", "your_turn", "upcoming", "lately"] | None = None,
    limit: int = Query(svc.ITEM_LIMIT, ge=1, le=svc.ITEM_LIMIT),
    if_none_match: str | None = Header(None, alias="If-None-Match"),
    auth: AuthContext = Depends(require_scope("attention:read")),
    db: AsyncSession = Depends(get_db),
):
    """The ranked feed: Now, Your turn, Upcoming, Lately. At most ``limit``
    (<= 100) items; ``counts`` always holds the full per-band totals and
    ``groups`` the per-section totals. ``chyron`` is the "Right now" ticker
    and ``collaboration`` the "Working with others" block. Poll with
    ``If-None-Match`` — an unchanged feed answers 304."""
    scope = svc.AttentionScope.from_auth(auth)
    feed = await svc.build_feed(db, scope, collaboration=True)
    body = svc.feed_response(feed, band=band, limit=limit)
    if band is None:
        svc.remember_counts(scope, body["counts"])
    etag = svc.etag_for(body)
    headers = {"ETag": etag, "Cache-Control": "private, no-cache"}
    if _etag_matches(if_none_match, etag):
        return Response(status_code=304, headers=headers)
    for k, v in headers.items():
        response.headers[k] = v
    return body


@router.get("/count", response_model=AttentionCounts)
async def get_for_you_count(
    auth: AuthContext = Depends(require_scope("attention:read")),
    db: AsyncSession = Depends(get_db),
) -> dict[str, int]:
    """Full per-band counts (for the sidebar badge). Answers from the counts
    the last feed build left (30 s), else builds the feed without the
    collaboration block."""
    return await svc.feed_counts(db, svc.AttentionScope.from_auth(auth))


@router.post("/hide", status_code=204)
async def hide_for_you_item(
    body: HideRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Response:
    """Hide an item until its source changes (or until ``until``, a snooze).
    Idempotent. Unknown or invisible keys are a silent no-op (same 204)."""
    until = body.until
    if until is not None:
        if until.tzinfo is None:
            until = until.replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        if until <= now or until - now > svc.HIDE_MAX_FUTURE:
            raise HTTPException(
                422,
                {
                    "code": "invalid_until",
                    "error": "invalid_until",
                    "message": "until must be in the future and within a year",
                },
            )
    await svc.hide_item(db, svc.AttentionScope(user_id=user.id), body.item_key, until=until)
    return Response(status_code=204)


class UnhideRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    item_key: str = Field(..., min_length=1, max_length=200)


@router.post("/unhide", status_code=204)
async def unhide_for_you_item(
    body: UnhideRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Response:
    """Undo a hide or snooze. Idempotent; unknown keys are the same 204."""
    await svc.unhide_item(db, svc.AttentionScope(user_id=user.id), body.item_key)
    return Response(status_code=204)


@router.get("/items/{item_key}/context")
async def get_for_you_item_context(
    item_key: str,
    auth: AuthContext = Depends(require_scope("attention:read")),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Related knowledge and your past sessions for an assigned-ticket card.
    Fetched when the card is opened; access to the ticket is re-checked."""
    if len(item_key) > 200:
        raise HTTPException(404, _NOT_FOUND)
    result = await svc.item_context(db, svc.AttentionScope.from_auth(auth), item_key)
    if result is None:
        raise HTTPException(404, _NOT_FOUND)
    return result


REMIND_LOOKUP_TIMEOUT_S = 4.0
REMIND_COOLDOWN = timedelta(hours=1)

_FLOW_NOT_FOUND = {"code": "not_found", "error": "not_found", "message": "Nothing to remind about"}


@router.post(
    "/collaboration/remind", response_model=CollaborationRemindResponse, status_code=201
)
async def remind_collaborator(
    body: CollaborationRemindRequest,
    auth: AuthContext = Depends(require_scope("reminders:write")),
    db: AsyncSession = Depends(get_db),
) -> CollaborationRemindResponse:
    """Send a reminder to the person a "Working with others" row waits on
    (the reviewer, or the blocking ticket's owner) with an editable message,
    now or at 09:00 tomorrow in ``timezone``.

    The recipient and the source ticket are resolved server-side from the
    caller's live feed: an unknown, stale or unremindable ``flow_key`` is
    one 404. The reminder goes through the reminders service, so a reminder
    for someone else still needs both people to read the source ticket
    (``reminder_owner_not_eligible`` otherwise), DLP runs on the title and
    the message (stored as the reminder's first note), and the write limits
    apply. User keys only."""
    from sessionfs.server.db.models import ReminderNote
    from sessionfs.server.routes import reminders as rroutes
    from sessionfs.server.services import attention_collab as collab
    from sessionfs.server.services import reminders as rsvc

    if auth.key_kind == "service":
        raise HTTPException(
            403,
            {
                "code": "service_key_not_allowed",
                "error": "service_key_not_allowed",
                "message": "Reminding a teammate needs a user key.",
            },
        )
    raw = rroutes._clean_text(body.message).strip()
    if not raw:
        raise HTTPException(
            422, {"code": "message_required", "error": "message_required", "message": "Write a message first."}
        )
    scope = svc.AttentionScope(user_id=auth.user.id)

    async def resolve() -> collab.Flow | None:
        ctx = svc._Ctx(
            db=db,
            scope=scope,
            now=datetime.now(timezone.utc),
            projects=await svc._readable_projects(db, scope),
        )
        await ctx.feature_project_ids("agent_tickets")
        await ctx.personas()
        flows = await collab.collaboration_flows(ctx)
        return next((f for f in flows if f.key == body.flow_key), None)

    async def timed_resolve() -> collab.Flow | None:
        try:
            return await asyncio.wait_for(resolve(), REMIND_LOOKUP_TIMEOUT_S)
        except asyncio.TimeoutError:
            await db.rollback()
            raise HTTPException(
                503,
                {"code": "busy", "error": "busy", "message": "Couldn't check that right now. Try again."},
            ) from None

    flow = await timed_resolve()
    if flow is None or flow.recipient_user_id is None:
        raise HTTPException(404, _FLOW_NOT_FOUND)

    # Serialise: lock the waiting ticket and the source ticket (a concurrent
    # reassignment or a double click waits here), then re-derive the flow
    # from what is now committed and refuse if it moved.
    from sqlalchemy import or_, select

    from sessionfs.server.db.models import Reminder, Ticket

    waiter_id = flow.body["ticket"]["id"]
    await db.execute(
        select(Ticket.id)
        .where(Ticket.id.in_(sorted({waiter_id, flow.source_ticket_id})))
        .order_by(Ticket.id)
        .with_for_update()
    )
    first = flow
    # The flow queries select plain columns, so they read what is committed
    # now (no stale identity-map rows).
    flow = await timed_resolve()
    if (
        flow is None
        or flow.recipient_user_id != first.recipient_user_id
        or flow.source_ticket_id != first.source_ticket_id
    ):
        await db.rollback()
        raise HTTPException(
            409,
            {
                "code": "flow_changed",
                "error": "flow_changed",
                "message": "That changed while you were writing. Refresh and try again.",
            },
        )
    recipient = flow.recipient_user_id
    assert recipient is not None
    await rroutes._lock_user(db, recipient)

    # One open reminder per (you, them, ticket), and at most one an hour,
    # checked under the locks above.
    recent = (
        await db.execute(
            select(Reminder.status, Reminder.created_at).where(
                Reminder.owner_user_id == recipient,
                Reminder.created_by_user_id == auth.user.id,
                Reminder.source_kind == "ticket",
                Reminder.source_id == flow.source_ticket_id,
                or_(
                    Reminder.status == "open",
                    Reminder.created_at >= datetime.now(timezone.utc) - REMIND_COOLDOWN,
                ),
            )
        )
    ).all()
    if any(r.status == "open" for r in recent):
        await db.rollback()
        raise HTTPException(
            409,
            {
                "code": "reminder_exists",
                "error": "reminder_exists",
                "message": "You already have an open reminder to them about this ticket.",
            },
        )
    if recent:
        await db.rollback()
        raise HTTPException(
            429,
            {
                "code": "reminder_cooldown",
                "error": "reminder_cooldown",
                "message": "You reminded them about this in the last hour.",
            },
        )

    now = datetime.now(timezone.utc)
    zone = rroutes._zone(body.timezone)
    due_at = now if body.when == "now" else rroutes._snooze_target("tomorrow", None, body.timezone, now)
    # The title never copies the ticket's title: it outlives access to the
    # ticket. The live source title shows only while it can be read.
    sender = auth.user.display_name or "A teammate"
    what = "waiting for your review" if flow.body["state"] == "review" else "waiting on a ticket"
    title = f"Reminder from {sender}: {what}"
    reminder, vis = await rroutes.create_reminder_record(
        db,
        auth,
        title=title[: rsvc.TITLE_MAX],
        due_at=due_at,
        tz_name=body.timezone if zone is not None else None,
        owner=recipient,
        source=rroutes.ReminderSourceIn(kind="ticket", id=flow.source_ticket_id),
        commit=False,
    )
    org_id = vis.source.org_id if vis.source is not None else None
    text = (await rroutes._scan(db, auth, org_id, raw))[: rsvc.NOTE_MAX]
    db.add(
        ReminderNote(
            id=rsvc.new_note_id(),
            reminder_id=reminder.id,
            author_user_id=auth.user.id,
            actor_type="user",
            service_key_id=None,
            service_key_name=None,
            body=text,
            created_at=now,
        )
    )
    await db.commit()
    to = (flow.body.get("to") or {}).get("name") or "your teammate"
    return CollaborationRemindResponse(
        reminder_id=reminder.id, due_at=svc._iso(reminder.due_at) or "", to=to
    )
