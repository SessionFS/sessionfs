"""Reminders REST API (tk_8a858d6354fc4ef3, phase R1).

- ``/api/v1/me/reminders`` — list/create the caller's reminders.
- ``/api/v1/me/reminders/{id}`` — get/patch/delete; ``/snooze``; ``/notes``.
- ``/api/v1/projects/{p}/meetings/{id}/reminders`` and
  ``/api/v1/projects/{p}/tickets/{id}/reminders`` — the reminders on a source
  that the caller may see.

A reminder is private to its owner and its creator: everyone else, and every
other miss, gets the same ``404 Reminder not found``. Reads re-check the live
source (see ``services/reminders``). Org/project/group are derived from the
source, never from the client. Title and notes are DLP-scanned (strictest
union of the caller's orgs plus the source org) before storage. State changes
(snooze, close, reschedule) are conditional UPDATEs on ``status='open'`` with
rowcount checks; snooze and reschedule clear ``notified_at`` so the due sweep
fires again. Service keys act as their minter: they can create reminders only
for the minter, and see only reminders whose live source project is inside
the key's org and allowlist. Reminders are on every plan (group-meeting
sources still need group standing on a Team+ org).
"""

from __future__ import annotations

import re
from datetime import datetime, time, timedelta, timezone
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import and_, delete, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server.auth.dependencies import AuthContext, require_scope
from sessionfs.server.auth.rate_limit import SlidingWindowRateLimiter
from sessionfs.server.db.engine import get_db
from sessionfs.server.sanitize import sanitize_archive_string
from sessionfs.server.db.models import (
    MeetingActionItem,
    MeetingParticipant,
    MeetingTranscript,
    Reminder,
    ReminderNote,
    Ticket,
    TicketNotification,
    User,
)
from sessionfs.server.services import reminders as svc
from sessionfs.server.services.reminders import (
    NOT_FOUND,
    AccessCache,
    SourceInfo,
    Visibility,
    as_aware,
)

def _clean_path_ids(request: Request) -> None:
    """An id holding a NUL or a lone surrogate can never be a real id (and
    would error at the database), so it gets the same 404 as any miss."""
    for name, value in request.path_params.items():
        if isinstance(value, str) and _clean_text(value) != value:
            if name == "reminder_id":
                raise HTTPException(404, NOT_FOUND)
            if name == "ticket_id":
                raise HTTPException(404, "Ticket not found")
            raise HTTPException(404, "Meeting not found")


router = APIRouter(
    prefix="/api/v1/me", tags=["reminders"], dependencies=[Depends(_clean_path_ids)]
)
project_router = APIRouter(
    prefix="/api/v1/projects", tags=["reminders"], dependencies=[Depends(_clean_path_ids)]
)
group_router = APIRouter(
    prefix="/api/v1/groups", tags=["reminders"], dependencies=[Depends(_clean_path_ids)]
)

REOPEN_WINDOW = timedelta(hours=1)

_write_limiter = SlidingWindowRateLimiter(
    max_requests=svc.WRITES_PER_MINUTE, window_seconds=60.0
)
_LINES_RE = re.compile(r"^\d{1,6}-\d{1,6}$")
_LIST_MAX = 200
_SOURCE_LIST_MAX = 500

_OWNER_NOT_ELIGIBLE = {
    "code": "reminder_owner_not_eligible",
    "error": "reminder_owner_not_eligible",
    "message": "A reminder can be set for an active teammate who can read its source.",
}


# ── Schemas ──


class ReminderSourceIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["meeting_item", "meeting", "ticket", "session", "knowledge_entry", "chat_item"]
    id: str = Field(min_length=1, max_length=64)
    lines: str | None = Field(default=None, max_length=32)


class ReminderCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=svc.TITLE_MAX)
    due_at: datetime
    timezone: str | None = Field(default=None, max_length=64)
    # Email or user id of the person to remind; omitted = the caller.
    owner: str | None = Field(default=None, max_length=320)
    source: ReminderSourceIn | None = None


class ItemReminderCreate(BaseModel):
    """Create a reminder from a meeting item. Every field is optional: the
    item's suggestion fills the title, the due time (09:00 on its due date,
    in ``timezone`` when given, else UTC) and the owner."""

    model_config = ConfigDict(extra="forbid")

    title: str | None = Field(default=None, min_length=1, max_length=svc.TITLE_MAX)
    due_at: datetime | None = None
    timezone: str | None = Field(default=None, max_length=64)
    owner: str | None = Field(default=None, max_length=320)


class ReminderPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str | None = Field(default=None, min_length=1, max_length=svc.TITLE_MAX)
    due_at: datetime | None = None
    timezone: str | None = Field(default=None, max_length=64)
    # "open" reopens a reminder you closed in the last hour (Undo).
    status: Literal["done", "dismissed", "open"] | None = None


class SnoozeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    preset: Literal["1h", "tomorrow", "next_week", "custom"]
    until: datetime | None = None


class NoteCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    body: str = Field(min_length=1, max_length=svc.NOTE_MAX)


class ReminderSourceOut(BaseModel):
    kind: str
    id: str
    lines: str | None = None
    title: str | None = None
    project_id: str | None = None
    group_id: str | None = None
    meeting_id: str | None = None


class ReminderResponse(BaseModel):
    id: str
    title: str
    status: str
    due_at: datetime
    original_due_at: datetime
    timezone: str | None = None
    is_due: bool
    owner_user_id: str
    created_by_user_id: str | None = None
    created_via: str
    actor_type: str = "user"
    service_key_name: str | None = None
    updated_actor_type: str = "user"
    is_owner: bool
    is_creator: bool
    source: ReminderSourceOut | None = None
    # True when the reminder has a source the caller can no longer read.
    source_hidden: bool = False
    linked_ticket_id: str | None = None
    notified_at: datetime | None = None
    snooze_count: int
    closed_at: datetime | None = None
    closed_by_user_id: str | None = None
    note_count: int = 0
    created_at: datetime
    updated_at: datetime


class ReminderListResponse(BaseModel):
    items: list[ReminderResponse]
    next_cursor: str | None = None


class SourceRemindersResponse(BaseModel):
    items: list[ReminderResponse]
    counts: dict[str, int]


class NoteResponse(BaseModel):
    id: str
    reminder_id: str
    author_user_id: str | None = None
    actor_type: str = "user"
    service_key_name: str | None = None
    body: str
    created_at: datetime


class NoteListResponse(BaseModel):
    items: list[NoteResponse]


# ── Helpers ──


def _limit_reached() -> HTTPException:
    return HTTPException(
        409,
        {
            "code": "reminder_limit_reached",
            "error": "reminder_limit_reached",
            "message": f"At most {svc.MAX_OPEN_PER_USER} open reminders per person.",
        },
    )


def _not_found() -> HTTPException:
    return HTTPException(404, NOT_FOUND)


def _rate_limit(user_id: str) -> None:
    if not _write_limiter.is_allowed(user_id):
        raise HTTPException(
            429,
            {
                "code": "rate_limited",
                "error": "rate_limited",
                "message": f"At most {svc.WRITES_PER_MINUTE} reminder writes per minute.",
            },
        )


def _clean_text(value: str) -> str:
    """Strip NULs and replace lone surrogates before any DB bind (PostgreSQL
    rejects both, which would otherwise be a 500)."""
    cleaned, _ = sanitize_archive_string(value)
    return cleaned


def _zone(name: str | None) -> ZoneInfo | None:
    if name is None:
        return None
    if _clean_text(name) != name:
        raise HTTPException(
            422, {"code": "invalid_timezone", "error": "invalid_timezone", "message": "timezone must be an IANA name."}
        )
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        raise HTTPException(
            422, {"code": "invalid_timezone", "error": "invalid_timezone", "message": "timezone must be an IANA name."}
        ) from None


def _to_utc(value: datetime, zone: ZoneInfo | None) -> datetime:
    """A naive value is local to ``zone`` (UTC when no zone was given)."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=zone or timezone.utc)
    return value.astimezone(timezone.utc)


def _check_due(due_at: datetime, now: datetime) -> None:
    if due_at < now - svc.DUE_PAST_SLACK or due_at > now + svc.DUE_FUTURE_MAX:
        raise HTTPException(
            422,
            {
                "code": "due_at_out_of_range",
                "error": "due_at_out_of_range",
                "message": "due_at must be between 1 hour ago and 2 years from now.",
            },
        )


def _clean_title(title: str) -> str:
    cleaned = " ".join(_clean_text(title).split())
    if not cleaned:
        raise HTTPException(422, {"code": "title_required", "error": "title_required", "message": "title is required"})
    return cleaned


async def _scan(db: AsyncSession, auth: AuthContext, org_id: str | None, text: str) -> str:
    """DLP: strictest union of the caller's orgs plus the scope org. Block
    mode refuses with 422 ``dlp_blocked``; other modes redact."""
    from sessionfs.server.routes.meetings import _dlp_scan_content, _upload_dlp_policy

    policy = await _upload_dlp_policy(db, auth, org_id)
    scanned, _ = _dlp_scan_content(_clean_text(text), policy)
    return scanned


def _is_service(auth: AuthContext) -> bool:
    return auth.key_kind == "service"


def _updater(auth: AuthContext) -> dict:
    """Last-writer columns for an UPDATE (a service key acts as its minter
    but is recorded as itself too)."""
    return {
        "updated_by_user_id": auth.user.id,
        "updated_actor_type": "service" if _is_service(auth) else "user",
        "updated_by_service_key_id": auth.service_key_id if _is_service(auth) else None,
    }


def _service_allows(auth: AuthContext, vis: Visibility, cache: AccessCache) -> bool:
    if vis.source is None or not vis.source.project_id:
        return False
    return svc.service_key_allows_project(auth, cache.projects.get(vis.source.project_id))


def _response(
    r: Reminder, viewer_id: str, vis: Visibility, now: datetime, note_count: int = 0
) -> ReminderResponse:
    src: ReminderSourceOut | None = None
    if vis.source_readable and vis.source is not None and r.source_kind and r.source_id:
        src = ReminderSourceOut(
            kind=r.source_kind,
            id=r.source_id,
            lines=r.source_lines,
            title=vis.source.title,
            project_id=vis.source.project_id,
            group_id=vis.source.group_id,
            meeting_id=vis.source.meeting_id,
        )
    has_source = bool(r.source_kind)
    due = as_aware(r.due_at)
    return ReminderResponse(
        id=r.id,
        title=r.title,
        status=r.status,
        due_at=due,
        original_due_at=as_aware(r.original_due_at),
        timezone=r.timezone,
        is_due=r.status == "open" and due <= now,
        owner_user_id=r.owner_user_id,
        created_by_user_id=r.created_by_user_id,
        created_via=r.created_via,
        actor_type=r.actor_type,
        service_key_name=r.service_key_name,
        updated_actor_type=r.updated_actor_type,
        is_owner=r.owner_user_id == viewer_id,
        is_creator=r.created_by_user_id == viewer_id,
        source=src,
        source_hidden=has_source and src is None,
        linked_ticket_id=r.linked_ticket_id if (src is not None or not has_source) else None,
        notified_at=as_aware(r.notified_at) if r.notified_at else None,
        snooze_count=r.snooze_count,
        closed_at=as_aware(r.closed_at) if r.closed_at else None,
        closed_by_user_id=r.closed_by_user_id,
        note_count=note_count,
        created_at=as_aware(r.created_at),
        updated_at=as_aware(r.updated_at),
    )


async def _note_counts(db: AsyncSession, ids: list[str]) -> dict[str, int]:
    if not ids:
        return {}
    rows = (
        await db.execute(
            select(ReminderNote.reminder_id, func.count(ReminderNote.id))
            .where(ReminderNote.reminder_id.in_(ids))
            .group_by(ReminderNote.reminder_id)
        )
    ).all()
    return {rid: n for rid, n in rows}


async def _visible_rows(
    db: AsyncSession, auth: AuthContext, rows: list[Reminder], cache: AccessCache
) -> list[tuple[Reminder, Visibility]]:
    vis = await svc.reminder_visibility(db, auth.user.id, rows, cache)
    out: list[tuple[Reminder, Visibility]] = []
    for r in rows:
        v = vis[r.id]
        if not v.visible:
            continue
        if _is_service(auth) and not _service_allows(auth, v, cache):
            continue
        out.append((r, v))
    return out


async def _load_visible(
    db: AsyncSession,
    auth: AuthContext,
    reminder_id: str,
    *,
    for_update: bool = False,
) -> tuple[Reminder, Visibility, AccessCache]:
    stmt = select(Reminder).where(Reminder.id == reminder_id)
    if for_update:
        stmt = stmt.with_for_update()
    r = (await db.execute(stmt)).scalar_one_or_none()
    if r is None:
        raise _not_found()
    cache = AccessCache()
    visible = await _visible_rows(db, auth, [r], cache)
    if not visible:
        raise _not_found()
    return r, visible[0][1], cache


def _forbidden(message: str) -> HTTPException:
    return HTTPException(403, {"code": "reminder_edit_forbidden", "error": "reminder_edit_forbidden", "message": message})


def _not_open() -> HTTPException:
    return HTTPException(
        409, {"code": "reminder_not_open", "error": "reminder_not_open", "message": "This reminder is already closed."}
    )


async def _resolve_owner(
    db: AsyncSession,
    auth: AuthContext,
    value: str | None,
    info: SourceInfo | None,
    cache: AccessCache,
) -> str:
    """The reminder's owner. Omitted or the caller = the caller. Anyone else
    must be an active user who can read the source; every miss (unknown,
    inactive, no source, can't read it, or a service key asking for anyone
    but its minter) is the same 422 so existence is never revealed."""
    caller: User = auth.user
    value = (value or "").strip()
    if not value or value == caller.id or (
        "@" in value and caller.email and value.lower() == caller.email.lower()
    ):
        return caller.id
    if _is_service(auth) or info is None:
        raise HTTPException(422, _OWNER_NOT_ELIGIBLE)
    if "@" in value:
        stmt = select(User).where(func.lower(User.email) == value.lower())
    else:
        stmt = select(User).where(User.id == value)
    matches = (await db.execute(stmt.limit(2))).scalars().all()
    user = matches[0] if len(matches) == 1 else None
    if user is None or not user.is_active:
        raise HTTPException(422, _OWNER_NOT_ELIGIBLE)
    if not await svc.user_can_read_source(db, user.id, info, cache):
        raise HTTPException(422, _OWNER_NOT_ELIGIBLE)
    return user.id


async def _lock_user(db: AsyncSession, user_id: str) -> None:
    """Serialize per-owner cap checks (PostgreSQL row lock; SQLite has one
    writer anyway)."""
    await db.execute(select(User.id).where(User.id == user_id).with_for_update())


# ── /me/reminders ──


@router.get("/reminders", response_model=ReminderListResponse)
async def list_reminders(
    status: Literal["open", "done", "dismissed", "all"] = "open",
    due_before: datetime | None = None,
    source: str | None = Query(None, max_length=90),
    limit: int = Query(50, ge=1, le=_LIST_MAX),
    cursor: str | None = Query(None, max_length=120),
    auth: AuthContext = Depends(require_scope("reminders:read")),
    db: AsyncSession = Depends(get_db),
) -> ReminderListResponse:
    """The caller's reminders (as owner or creator), earliest due first.

    ``source`` filters by ``kind`` or ``kind:id``. ``cursor`` is the
    ``next_cursor`` of the previous page. "Due" is computed at read time
    (``is_due``), so a late sweep never hides a due reminder here.
    """
    me = auth.user.id
    now = datetime.now(timezone.utc)
    base = select(Reminder).where(
        or_(Reminder.owner_user_id == me, Reminder.created_by_user_id == me)
    )
    if status != "all":
        base = base.where(Reminder.status == status)
    if due_before is not None:
        base = base.where(Reminder.due_at < _to_utc(due_before, None))
    if source:
        if _clean_text(source) != source:
            raise HTTPException(422, {"code": "invalid_source", "error": "invalid_source", "message": "invalid source filter"})
        kind, _, sid = source.partition(":")
        if kind not in svc.SOURCE_KINDS:
            raise HTTPException(422, {"code": "invalid_source", "error": "invalid_source", "message": "unknown source kind"})
        base = base.where(Reminder.source_kind == kind)
        if sid:
            base = base.where(Reminder.source_id == sid)
    after: tuple[datetime, str] | None = None
    if cursor:
        raw_due, _, raw_id = cursor.partition("|")
        if _clean_text(cursor) != cursor:
            raise HTTPException(422, {"code": "invalid_cursor", "error": "invalid_cursor"})
        try:
            after = (as_aware(datetime.fromisoformat(raw_due.replace("Z", "+00:00"))), raw_id)
        except ValueError:
            raise HTTPException(422, {"code": "invalid_cursor", "error": "invalid_cursor"}) from None

    cache = AccessCache()
    picked: list[tuple[Reminder, Visibility]] = []
    exhausted = False
    # Bounded scan: rows the caller can no longer see are skipped without
    # looping forever.
    for _ in range(10):
        stmt = base
        if after is not None:
            stmt = stmt.where(
                or_(
                    Reminder.due_at > after[0],
                    and_(Reminder.due_at == after[0], Reminder.id > after[1]),
                )
            )
        rows = list(
            (
                await db.execute(
                    stmt.order_by(Reminder.due_at.asc(), Reminder.id.asc()).limit(limit * 2)
                )
            ).scalars()
        )
        if not rows:
            exhausted = True
            break
        for pair in await _visible_rows(db, auth, rows, cache):
            if len(picked) < limit:
                picked.append(pair)
        if len(picked) >= limit:
            last = picked[-1][0]
            after = (as_aware(last.due_at), last.id)
            break
        last_row = rows[-1]
        after = (as_aware(last_row.due_at), last_row.id)
        if len(rows) < limit * 2:
            exhausted = True
            break
    counts = await _note_counts(db, [r.id for r, _ in picked])
    items = [_response(r, me, v, now, counts.get(r.id, 0)) for r, v in picked]
    next_cursor = None
    if not exhausted and after is not None:
        next_cursor = f"{after[0].isoformat()}|{after[1]}"
    return ReminderListResponse(items=items, next_cursor=next_cursor)


async def create_reminder_record(
    db: AsyncSession,
    auth: AuthContext,
    *,
    title: str,
    due_at: datetime,
    tz_name: str | None,
    owner: str | None = None,
    source: ReminderSourceIn | None = None,
    created_via: str | None = None,
    linked_ticket_id: str | None = None,
    commit: bool = True,
) -> tuple[Reminder, Visibility]:
    """Create one reminder with every rule the REST route enforces.

    Shared by ``POST /me/reminders``, the helper's confirmed
    ``create_reminder`` action and the meeting-item route so none of them
    can drift: rate limit, due-time bounds, source readability (creator, and
    a service key's org/allowlist), owner eligibility, DLP before storage,
    and the per-owner open cap under a row lock. ``created_via`` defaults to
    ``api`` for service keys and ``manual`` otherwise. With ``commit=False``
    the caller owns the transaction (the helper confirm commits the action
    and the reminder together).
    """
    caller = auth.user
    _rate_limit(caller.id)
    now = datetime.now(timezone.utc)
    zone = _zone(tz_name)
    due_at = _to_utc(due_at, zone)
    _check_due(due_at, now)
    title = _clean_title(title)
    if owner is not None:
        owner = _clean_text(owner)
    if source is not None:
        source = source.model_copy(
            update={
                "id": _clean_text(source.id),
                "lines": _clean_text(source.lines) if source.lines is not None else None,
            }
        )

    cache = AccessCache()
    info: SourceInfo | None = None
    if source is not None:
        if source.lines is not None and not _LINES_RE.match(source.lines):
            raise HTTPException(
                422, {"code": "invalid_source_lines", "error": "invalid_source_lines", "message": "lines must look like 12-18"}
            )
        key = (source.kind, source.id)
        info = (await svc.load_sources(db, [key], cache))[key]
        # The creator must read the source; a service key's source must also
        # sit in a project inside its org and allowlist. One 404 either way.
        if not await svc.user_can_read_source(db, caller.id, info, cache):
            raise HTTPException(404, "Source not found")
        if _is_service(auth) and not svc.service_key_allows_project(
            auth, cache.projects.get(info.project_id) if info.project_id else None
        ):
            raise HTTPException(404, "Source not found")
    elif _is_service(auth):
        # A sourceless reminder has no project, so a service key could never
        # see it again; refuse up front.
        raise HTTPException(
            422,
            {
                "code": "reminder_source_required",
                "error": "reminder_source_required",
                "message": "Service keys can only create reminders on a project source.",
            },
        )

    owner_id = await _resolve_owner(db, auth, owner, info, cache)
    title = (await _scan(db, auth, info.org_id if info else None, title))[: svc.TITLE_MAX]

    await _lock_user(db, owner_id)
    open_count = (
        await db.execute(
            select(func.count(Reminder.id)).where(
                Reminder.owner_user_id == owner_id, Reminder.status == "open"
            )
        )
    ).scalar_one()
    if open_count >= svc.MAX_OPEN_PER_USER:
        raise _limit_reached()

    if owner_id != caller.id:
        created_for_other = (
            await db.execute(
                select(func.count(Reminder.id)).where(
                    Reminder.owner_user_id == owner_id,
                    Reminder.created_by_user_id == caller.id,
                    Reminder.status == "open",
                )
            )
        ).scalar_one()
        if created_for_other >= svc.MAX_OPEN_PER_CREATOR_FOR_OTHER:
            raise _limit_reached()

    reminder = Reminder(
        id=svc.new_reminder_id(),
        owner_user_id=owner_id,
        created_by_user_id=caller.id,
        created_via=created_via or ("api" if _is_service(auth) else "manual"),
        actor_type="service" if _is_service(auth) else "user",
        service_key_id=auth.service_key_id if _is_service(auth) else None,
        service_key_name=auth.service_key_name if _is_service(auth) else None,
        updated_by_user_id=caller.id,
        updated_actor_type="service" if _is_service(auth) else "user",
        updated_by_service_key_id=auth.service_key_id if _is_service(auth) else None,
        org_id=info.org_id if info else None,
        project_id=info.project_id if info else None,
        group_id=info.group_id if info else None,
        source_kind=info.kind if info else None,
        source_id=info.id if info else None,
        source_lines=source.lines if source else None,
        linked_ticket_id=linked_ticket_id,
        title=title,
        due_at=due_at,
        original_due_at=due_at,
        timezone=tz_name,
        status="open",
        snooze_count=0,
        created_at=now,
        updated_at=now,
    )
    db.add(reminder)
    if commit:
        await db.commit()
    else:
        await db.flush()
    return reminder, Visibility(True, info is not None, info)


@router.post("/reminders", response_model=ReminderResponse, status_code=201)
async def create_reminder(
    body: ReminderCreate,
    auth: AuthContext = Depends(require_scope("reminders:write")),
    db: AsyncSession = Depends(get_db),
) -> ReminderResponse:
    reminder, vis = await create_reminder_record(
        db,
        auth,
        title=body.title,
        due_at=body.due_at,
        tz_name=body.timezone,
        owner=body.owner,
        source=body.source,
    )
    return _response(reminder, auth.user.id, vis, datetime.now(timezone.utc))


@router.get("/reminders/{reminder_id}", response_model=ReminderResponse)
async def get_reminder(
    reminder_id: str,
    auth: AuthContext = Depends(require_scope("reminders:read")),
    db: AsyncSession = Depends(get_db),
) -> ReminderResponse:
    r, vis, _ = await _load_visible(db, auth, reminder_id)
    counts = await _note_counts(db, [r.id])
    return _response(r, auth.user.id, vis, datetime.now(timezone.utc), counts.get(r.id, 0))


@router.patch("/reminders/{reminder_id}", response_model=ReminderResponse)
async def patch_reminder(
    reminder_id: str,
    body: ReminderPatch,
    auth: AuthContext = Depends(require_scope("reminders:write")),
    db: AsyncSession = Depends(get_db),
) -> ReminderResponse:
    """Edit a reminder. The creator edits the title; the owner reschedules
    (``due_at``/``timezone``); either closes it (``status`` done|dismissed).
    Rescheduling and closing only apply while it is open."""
    me = auth.user.id
    _rate_limit(me)
    fields = body.model_fields_set
    if not fields:
        raise HTTPException(422, {"code": "empty_patch", "error": "empty_patch", "message": "Nothing to change."})
    r, vis, _ = await _load_visible(db, auth, reminder_id)
    is_owner, is_creator = r.owner_user_id == me, r.created_by_user_id == me
    now = datetime.now(timezone.utc)
    values: dict = {"updated_at": now, **_updater(auth)}
    state_change = False

    if "title" in fields:
        if body.title is None:
            raise HTTPException(422, {"code": "title_required", "error": "title_required", "message": "title is required"})
        if not is_creator:
            raise _forbidden("Only the person who created this reminder can rename it.")
        org_id = vis.source.org_id if vis.source is not None else None
        values["title"] = (await _scan(db, auth, org_id, _clean_title(body.title)))[
            : svc.TITLE_MAX
        ]
    if "due_at" in fields or "timezone" in fields:
        if not is_owner:
            raise _forbidden("Only the person being reminded can reschedule it.")
        new_tz = body.timezone if "timezone" in fields else r.timezone
        zone = _zone(new_tz)
        if "timezone" in fields:
            values["timezone"] = body.timezone
        if "due_at" in fields:
            if body.due_at is None:
                raise HTTPException(422, {"code": "due_at_required", "error": "due_at_required"})
            due_at = _to_utc(body.due_at, zone)
            _check_due(due_at, now)
            values["due_at"] = due_at
            values["notified_at"] = None
            state_change = True
    if "status" in fields and body.status == "open":
        if fields != {"status"}:
            raise HTTPException(
                422, {"code": "reopen_alone", "error": "reopen_alone", "message": "Reopen on its own."}
            )
        # Reopening adds an open reminder, so the create caps apply (checked
        # under the owner lock, like create).
        await _lock_user(db, r.owner_user_id)
        open_count = (
            await db.execute(
                select(func.count(Reminder.id)).where(
                    Reminder.owner_user_id == r.owner_user_id, Reminder.status == "open"
                )
            )
        ).scalar_one()
        if open_count >= svc.MAX_OPEN_PER_USER:
            await db.rollback()
            raise _limit_reached()
        if is_creator and not is_owner:
            created_for_other = (
                await db.execute(
                    select(func.count(Reminder.id)).where(
                        Reminder.owner_user_id == r.owner_user_id,
                        Reminder.created_by_user_id == me,
                        Reminder.status == "open",
                    )
                )
            ).scalar_one()
            if created_for_other >= svc.MAX_OPEN_PER_CREATOR_FOR_OTHER:
                await db.rollback()
                raise _limit_reached()
        reopened = await db.execute(
            update(Reminder)
            .where(
                Reminder.id == r.id,
                Reminder.status.in_(("done", "dismissed")),
                Reminder.closed_by_user_id == me,
                Reminder.closed_at >= now - REOPEN_WINDOW,
            )
            .values(status="open", closed_at=None, closed_by_user_id=None,
                    closed_by_service_key_id=None, **values)
            .execution_options(synchronize_session=False)
        )
        if reopened.rowcount != 1:
            await db.rollback()
            raise HTTPException(
                409,
                {"code": "reminder_not_reopenable", "error": "reminder_not_reopenable",
                 "message": "Only a reminder you closed in the last hour can be reopened."},
            )
        await db.commit()
        r, vis, _ = await _load_visible(db, auth, reminder_id)
        await db.refresh(r)
        counts = await _note_counts(db, [r.id])
        return _response(r, me, vis, now, counts.get(r.id, 0))
    if "status" in fields:
        if body.status is None:
            raise HTTPException(
                422, {"code": "invalid_status", "error": "invalid_status", "message": "status must be done or dismissed"}
            )
        values["status"] = body.status
        values["closed_at"] = now
        values["closed_by_user_id"] = me
        values["closed_by_service_key_id"] = auth.service_key_id if _is_service(auth) else None
        state_change = True

    stmt = update(Reminder).where(Reminder.id == r.id)
    if state_change:
        stmt = stmt.where(Reminder.status == "open")
    result = await db.execute(stmt.values(**values).execution_options(synchronize_session=False))
    if result.rowcount != 1:
        await db.rollback()
        raise _not_open()
    await db.commit()
    r, vis, _ = await _load_visible(db, auth, reminder_id)
    await db.refresh(r)
    counts = await _note_counts(db, [r.id])
    return _response(r, me, vis, now, counts.get(r.id, 0))


@router.delete("/reminders/{reminder_id}", status_code=204)
async def delete_reminder(
    reminder_id: str,
    auth: AuthContext = Depends(require_scope("reminders:write")),
    db: AsyncSession = Depends(get_db),
) -> Response:
    """Delete a reminder (its creator only; an owner who didn't create it can
    dismiss it instead). Notes and pending notifications go with it."""
    me = auth.user.id
    _rate_limit(me)
    if _is_service(auth):
        raise _forbidden("Service keys cannot delete reminders.")
    r, _, _ = await _load_visible(db, auth, reminder_id)
    if r.created_by_user_id != me:
        raise _forbidden("Only the person who created this reminder can delete it.")
    await db.execute(
        delete(TicketNotification).where(
            TicketNotification.subject_kind == "reminder",
            TicketNotification.subject_id == r.id,
        )
    )
    await db.execute(delete(ReminderNote).where(ReminderNote.reminder_id == r.id))
    await db.execute(delete(Reminder).where(Reminder.id == r.id))
    await db.commit()
    return Response(status_code=204)


def _snooze_target(preset: str, until: datetime | None, tz_name: str | None, now: datetime) -> datetime:
    zone = _zone(tz_name) or ZoneInfo("UTC")
    local_now = now.astimezone(zone)
    if preset == "1h":
        return now + timedelta(hours=1)
    if preset == "tomorrow":
        day = local_now.date() + timedelta(days=1)
        return datetime.combine(day, time(9, 0), tzinfo=zone).astimezone(timezone.utc)
    if preset == "next_week":
        days = 7 - local_now.weekday()  # next Monday (a week ahead on Mondays)
        day = local_now.date() + timedelta(days=days)
        return datetime.combine(day, time(9, 0), tzinfo=zone).astimezone(timezone.utc)
    if until is None:
        raise HTTPException(
            422, {"code": "until_required", "error": "until_required", "message": "custom snooze needs until"}
        )
    target = _to_utc(until, _zone(tz_name))
    if target <= now:
        raise HTTPException(
            422, {"code": "due_at_out_of_range", "error": "due_at_out_of_range", "message": "until must be in the future."}
        )
    return target


@router.post("/reminders/{reminder_id}/snooze", response_model=ReminderResponse)
async def snooze_reminder(
    reminder_id: str,
    body: SnoozeRequest,
    auth: AuthContext = Depends(require_scope("reminders:write")),
    db: AsyncSession = Depends(get_db),
) -> ReminderResponse:
    """Push an open reminder later: ``1h``, ``tomorrow`` (09:00 in the
    reminder's timezone), ``next_week`` (Monday 09:00) or ``custom`` with
    ``until``. Owner only. Clears ``notified_at`` so it fires again."""
    me = auth.user.id
    _rate_limit(me)
    r, _, _ = await _load_visible(db, auth, reminder_id)
    if r.owner_user_id != me:
        raise _forbidden("Only the person being reminded can snooze it.")
    now = datetime.now(timezone.utc)
    target = _snooze_target(body.preset, body.until, r.timezone, now)
    _check_due(target, now)
    result = await db.execute(
        update(Reminder)
        .where(Reminder.id == r.id, Reminder.status == "open")
        .values(
            due_at=target,
            notified_at=None,
            snooze_count=Reminder.snooze_count + 1,
            updated_at=now,
            **_updater(auth),
        )
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:
        await db.rollback()
        raise _not_open()
    await db.commit()
    r, vis, _ = await _load_visible(db, auth, reminder_id)
    await db.refresh(r)
    counts = await _note_counts(db, [r.id])
    return _response(r, me, vis, now, counts.get(r.id, 0))


@router.get("/reminders/{reminder_id}/notes", response_model=NoteListResponse)
async def list_notes(
    reminder_id: str,
    auth: AuthContext = Depends(require_scope("reminders:read")),
    db: AsyncSession = Depends(get_db),
) -> NoteListResponse:
    r, _, _ = await _load_visible(db, auth, reminder_id)
    rows = (
        await db.execute(
            select(ReminderNote)
            .where(ReminderNote.reminder_id == r.id)
            .order_by(ReminderNote.created_at.asc(), ReminderNote.id.asc())
            .limit(svc.MAX_NOTES_PER_REMINDER)
        )
    ).scalars()
    return NoteListResponse(
        items=[
            NoteResponse(
                id=n.id,
                reminder_id=n.reminder_id,
                author_user_id=n.author_user_id,
                actor_type=n.actor_type,
                service_key_name=n.service_key_name,
                body=n.body,
                created_at=as_aware(n.created_at),
            )
            for n in rows
        ]
    )


@router.post("/reminders/{reminder_id}/notes", response_model=NoteResponse, status_code=201)
async def add_note(
    reminder_id: str,
    body: NoteCreate,
    auth: AuthContext = Depends(require_scope("reminders:write")),
    db: AsyncSession = Depends(get_db),
) -> NoteResponse:
    """Append a note (owner or creator). Notes are append-only."""
    me = auth.user.id
    _rate_limit(me)
    r, vis, _ = await _load_visible(db, auth, reminder_id, for_update=True)
    count = (
        await db.execute(
            select(func.count(ReminderNote.id)).where(ReminderNote.reminder_id == r.id)
        )
    ).scalar_one()
    if count >= svc.MAX_NOTES_PER_REMINDER:
        raise HTTPException(
            409,
            {
                "code": "reminder_note_limit",
                "error": "reminder_note_limit",
                "message": f"At most {svc.MAX_NOTES_PER_REMINDER} notes per reminder.",
            },
        )
    org_id = vis.source.org_id if vis.source is not None else None
    raw_body = _clean_text(body.body)
    text = (await _scan(db, auth, org_id, raw_body.strip() or raw_body))[: svc.NOTE_MAX]
    now = datetime.now(timezone.utc)
    note = ReminderNote(
        id=svc.new_note_id(),
        reminder_id=r.id,
        author_user_id=me,
        actor_type="service" if _is_service(auth) else "user",
        service_key_id=auth.service_key_id if _is_service(auth) else None,
        service_key_name=auth.service_key_name if _is_service(auth) else None,
        body=text,
        created_at=now,
    )
    db.add(note)
    await db.execute(
        update(Reminder)
        .where(Reminder.id == r.id)
        .values(updated_at=now, **_updater(auth))
        .execution_options(synchronize_session=False)
    )
    await db.commit()
    return NoteResponse(
        id=note.id,
        reminder_id=r.id,
        author_user_id=me,
        actor_type=note.actor_type,
        service_key_name=note.service_key_name,
        body=text,
        created_at=now,
    )


# ── Reminders on a source ──


async def _authorize_project(
    db: AsyncSession, auth: AuthContext, project_id: str, not_found: str
) -> None:
    from sessionfs.server.routes.meetings import _load_project_for_meetings

    try:
        await _load_project_for_meetings(
            db, project_id, auth, write=False, not_found_detail=not_found
        )
    except HTTPException as exc:
        if exc.status_code in (403, 404):
            raise HTTPException(404, not_found) from None
        raise


async def _source_listing(
    db: AsyncSession, auth: AuthContext, condition
) -> SourceRemindersResponse:
    me = auth.user.id
    rows = list(
        (
            await db.execute(
                select(Reminder)
                .where(
                    condition,
                    or_(Reminder.owner_user_id == me, Reminder.created_by_user_id == me),
                )
                .order_by(Reminder.due_at.asc(), Reminder.id.asc())
                .limit(_SOURCE_LIST_MAX)
            )
        ).scalars()
    )
    cache = AccessCache()
    picked = await _visible_rows(db, auth, rows, cache)
    counts = await _note_counts(db, [r.id for r, _ in picked])
    now = datetime.now(timezone.utc)
    status_counts = {s: 0 for s in svc.STATUSES}
    for r, _ in picked:
        status_counts[r.status] = status_counts.get(r.status, 0) + 1
    return SourceRemindersResponse(
        items=[_response(r, me, v, now, counts.get(r.id, 0)) for r, v in picked],
        counts=status_counts,
    )


@project_router.get(
    "/{project_id}/meetings/{meeting_id}/reminders", response_model=SourceRemindersResponse
)
async def meeting_reminders(
    project_id: str,
    meeting_id: str,
    auth: AuthContext = Depends(require_scope("reminders:read")),
    db: AsyncSession = Depends(get_db),
) -> SourceRemindersResponse:
    """Reminders the caller may see on a project meeting or its items."""
    await _authorize_project(db, auth, project_id, "Meeting not found")
    found = (
        await db.execute(
            select(MeetingTranscript.id).where(
                MeetingTranscript.id == meeting_id,
                MeetingTranscript.project_id == project_id,
                MeetingTranscript.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if found is None:
        raise HTTPException(404, "Meeting not found")
    items = select(MeetingActionItem.id).where(MeetingActionItem.transcript_id == meeting_id)
    return await _source_listing(
        db,
        auth,
        or_(
            and_(Reminder.source_kind == "meeting", Reminder.source_id == meeting_id),
            and_(
                Reminder.source_kind.in_(("meeting_item", "chat_item")),
                Reminder.source_id.in_(items),
            ),
        ),
    )


@project_router.get(
    "/{project_id}/tickets/{ticket_id}/reminders", response_model=SourceRemindersResponse
)
async def ticket_reminders(
    project_id: str,
    ticket_id: str,
    auth: AuthContext = Depends(require_scope("reminders:read")),
    db: AsyncSession = Depends(get_db),
) -> SourceRemindersResponse:
    """Reminders the caller may see on a ticket (sourced on it, or linked to
    it when a meeting item was promoted)."""
    await _authorize_project(db, auth, project_id, "Ticket not found")
    found = (
        await db.execute(
            select(Ticket.id).where(Ticket.id == ticket_id, Ticket.project_id == project_id)
        )
    ).scalar_one_or_none()
    if found is None:
        raise HTTPException(404, "Ticket not found")
    return await _source_listing(
        db,
        auth,
        or_(
            and_(Reminder.source_kind == "ticket", Reminder.source_id == ticket_id),
            Reminder.linked_ticket_id == ticket_id,
        ),
    )


async def _meeting_in_scope(
    db: AsyncSession,
    auth: AuthContext,
    meeting_id: str,
    *,
    project_id: str | None = None,
    group_id: str | None = None,
) -> MeetingTranscript:
    """The live meeting under this project/group the caller may read. Every
    miss (wrong scope, deleted, no standing, a service key on a group
    meeting, a group plan without groups) is the same 404."""
    from sessionfs.server.routes.meetings import _meeting_scope

    meeting = (
        await db.execute(
            select(MeetingTranscript).where(
                MeetingTranscript.id == meeting_id,
                MeetingTranscript.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if meeting is None or (
        meeting.project_id != project_id if project_id is not None else meeting.group_id != group_id
    ):
        raise HTTPException(404, "Meeting not found")
    try:
        await _meeting_scope(db, meeting, auth, write=False)
    except HTTPException as exc:
        if exc.status_code in (403, 404):
            raise HTTPException(404, "Meeting not found") from None
        raise
    return meeting


async def _create_item_reminder(
    db: AsyncSession,
    auth: AuthContext,
    meeting: MeetingTranscript,
    item_id: str,
    body: ItemReminderCreate,
) -> ReminderResponse:
    from sessionfs.server.routes.meetings import suggested_reminder

    item = (
        await db.execute(
            select(MeetingActionItem).where(
                MeetingActionItem.id == item_id,
                MeetingActionItem.transcript_id == meeting.id,
            )
        )
    ).scalar_one_or_none()
    if item is None:
        raise HTTPException(404, "Meeting not found")
    owner_user_id = None
    if item.owner_participant_id:
        owner_user_id = (
            await db.execute(
                select(MeetingParticipant.user_id).where(
                    MeetingParticipant.id == item.owner_participant_id
                )
            )
        ).scalar_one_or_none()
    suggestion = suggested_reminder(item, owner_user_id)
    fields = body.model_fields_set
    due_at = body.due_at
    if due_at is None:
        if suggestion is None:
            raise HTTPException(
                422,
                {
                    "code": "due_at_required",
                    "error": "due_at_required",
                    "message": "This item has no due date; pass due_at.",
                },
            )
        # 09:00 on the due date, in the caller's timezone when given.
        due_at = datetime.combine(suggestion.due_date, time(9, 0))
        if body.timezone is None:
            due_at = due_at.replace(tzinfo=timezone.utc)
    title = body.title or (suggestion.title if suggestion else " ".join(item.description.split())[: svc.TITLE_MAX])
    owner = body.owner if "owner" in fields else (suggestion.owner_user_id if suggestion else None)
    reminder, vis = await create_reminder_record(
        db,
        auth,
        title=title,
        due_at=due_at,
        tz_name=body.timezone,
        owner=owner,
        source=ReminderSourceIn(
            kind=svc.item_source_kind(meeting.kind),
            id=item.id,
            lines=f"{item.line_start}-{item.line_end}",
        ),
        created_via="meeting",
        linked_ticket_id=item.promoted_ticket_id,
    )
    return _response(reminder, auth.user.id, vis, datetime.now(timezone.utc))


@project_router.post(
    "/{project_id}/meetings/{meeting_id}/items/{item_id}/reminder",
    response_model=ReminderResponse,
    status_code=201,
)
async def create_project_item_reminder(
    project_id: str,
    meeting_id: str,
    item_id: str,
    body: ItemReminderCreate | None = None,
    auth: AuthContext = Depends(require_scope("reminders:write")),
    db: AsyncSession = Depends(get_db),
) -> ReminderResponse:
    """Create the reminder a project meeting item suggests (created_via
    ``meeting``, source = the item and its cited lines)."""
    meeting = await _meeting_in_scope(db, auth, meeting_id, project_id=project_id)
    return await _create_item_reminder(db, auth, meeting, item_id, body or ItemReminderCreate())


@group_router.post(
    "/{group_id}/meetings/{meeting_id}/items/{item_id}/reminder",
    response_model=ReminderResponse,
    status_code=201,
)
async def create_group_item_reminder(
    group_id: str,
    meeting_id: str,
    item_id: str,
    body: ItemReminderCreate | None = None,
    auth: AuthContext = Depends(require_scope("reminders:write")),
    db: AsyncSession = Depends(get_db),
) -> ReminderResponse:
    """The group-meeting equivalent (group standing on a Team+ org)."""
    meeting = await _meeting_in_scope(db, auth, meeting_id, group_id=group_id)
    return await _create_item_reminder(db, auth, meeting, item_id, body or ItemReminderCreate())


@group_router.get(
    "/{group_id}/meetings/{meeting_id}/reminders", response_model=SourceRemindersResponse
)
async def group_meeting_reminders(
    group_id: str,
    meeting_id: str,
    auth: AuthContext = Depends(require_scope("reminders:read")),
    db: AsyncSession = Depends(get_db),
) -> SourceRemindersResponse:
    """Reminders the caller may see on a group meeting or its items. Group
    visibility first (member or grant), then the plan gate; one 404."""
    await _meeting_in_scope(db, auth, meeting_id, group_id=group_id)
    items = select(MeetingActionItem.id).where(MeetingActionItem.transcript_id == meeting_id)
    return await _source_listing(
        db,
        auth,
        or_(
            and_(Reminder.source_kind == "meeting", Reminder.source_id == meeting_id),
            and_(
                Reminder.source_kind.in_(("meeting_item", "chat_item")),
                Reminder.source_id.in_(items),
            ),
        ),
    )


# ── Admin sweep ──
# Lives in routes/admin.py next to the other sweeps; see sweep_reminders_due.
