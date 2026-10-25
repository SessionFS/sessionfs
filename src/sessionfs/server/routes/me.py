"""Me-scoped ticket notification routes.

The human inbox (``/ticket-notifications*``) and the agent delta
(``/ticket-updates``) share one guarantee: every read re-checks tenant
isolation against the ticket's *current* project, so a user who leaves a
project (or an org) stops seeing its notifications immediately. Notification
``summary`` fields are server-written and never contain comment bodies.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server.auth.dependencies import (
    AuthContext,
    get_current_user,
    require_scope,
)
from sessionfs.server.db.engine import get_db
from sessionfs.server.db.models import (
    Project,
    Ticket,
    TicketNotification,
    TicketWatchCursor,
    User,
    UserNotificationSettings,
)

router = APIRouter(prefix="/api/v1/me", tags=["me"])

_EMAIL_MODES = {"off", "your_turn", "all"}
_NOTIFICATION_LIMIT_DEFAULT = 20
_NOTIFICATION_LIMIT_MAX = 100
_DELTA_LIMIT = 100
_READ_IDS_MAX = 500
_PERSONA_MAX = 50
# Delta rows are only surfaced once they are older than this horizon. A row's
# id is allocated by the sequence before its ticket transaction commits, so a
# still-open transaction can hold a small id while a later transaction's larger
# id commits first; stopping at the first too-new row keeps the cursor from
# advancing past the (not-yet-visible) smaller id.
_DELTA_HORIZON = timedelta(seconds=5)


class TicketNotificationItem(BaseModel):
    id: int
    ticket_id: str
    project_id: str
    ticket_title: str | None = None
    kind: str
    summary: str
    actor_type: str
    actor_user_id: str | None = None
    service_key_name: str | None = None
    event_count: int
    created_at: datetime
    updated_at: datetime
    read_at: datetime | None = None


class NotificationListResponse(BaseModel):
    items: list[TicketNotificationItem]
    next_before: int | None = None


class TicketUpdatesResponse(BaseModel):
    items: list[TicketNotificationItem]
    next_cursor: int


class ReadNotificationsRequest(BaseModel):
    ids: list[int] | None = None
    all: bool = False


class AckUpdatesRequest(BaseModel):
    cursor: int
    persona: str = ""


class NotificationSettingsResponse(BaseModel):
    ticket_email: str


class NotificationSettingsRequest(BaseModel):
    ticket_email: str


def _as_aware(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _to_item(notification: TicketNotification, title: str | None) -> TicketNotificationItem:
    return TicketNotificationItem(
        id=notification.id,
        ticket_id=notification.ticket_id,
        project_id=notification.project_id,
        ticket_title=title,
        kind=notification.kind,
        summary=notification.summary,
        actor_type=notification.actor_type,
        actor_user_id=notification.actor_user_id,
        service_key_name=notification.service_key_name,
        event_count=notification.event_count,
        created_at=notification.created_at,
        updated_at=notification.updated_at,
        read_at=notification.read_at,
    )


async def _accessible_project_ids(
    db: AsyncSession, user_id: str, project_ids: set[str]
) -> set[str]:
    """The subset of ``project_ids`` the user can still access."""
    from sessionfs.server.auth.project_access import user_can_access_project

    if not project_ids:
        return set()
    projects = (
        await db.execute(select(Project).where(Project.id.in_(list(project_ids))))
    ).scalars().all()
    accessible: set[str] = set()
    for project in projects:
        if await user_can_access_project(db, user_id, project):
            accessible.add(project.id)
    return accessible


async def _service_accessible_project_ids(
    db: AsyncSession, auth: AuthContext, project_ids: set[str]
) -> set[str]:
    """The subset of ``project_ids`` inside a service key's org scope.

    A service key shares its minter's ``user_id``, so a plain user-scoped
    filter would leak every notification the minter can see. Service keys are
    instead restricted to tickets whose project is in the key's org and, when
    the key carries a project allowlist, in that allowlist — mirroring
    ``assert_service_key_can_access_project``.
    """
    if not project_ids or auth.org_id is None:
        return set()
    projects = (
        await db.execute(select(Project).where(Project.id.in_(list(project_ids))))
    ).scalars().all()
    accessible: set[str] = set()
    for project in projects:
        if project.org_id != auth.org_id:
            continue
        if auth.project_ids is not None and project.id not in auth.project_ids:
            continue
        accessible.add(project.id)
    return accessible


def _cursor_persona(auth: AuthContext, persona: str) -> str:
    """Cursor namespace: service keys get their own, keyed off the key id, so
    they never share (or collide with) their minter's human cursor."""
    if auth.key_kind == "service":
        return ("svc:" + (auth.service_key_id or ""))[:_PERSONA_MAX]
    if persona.startswith("svc:"):
        # The ``svc:`` namespace is reserved for service-key cursors. A user
        # key must not be able to read or ack a service key's read position.
        raise HTTPException(422, "persona may not use the reserved svc: prefix")
    return persona


@router.get("/ticket-notifications", response_model=NotificationListResponse)
async def list_ticket_notifications(
    unread: bool = False,
    limit: int = Query(_NOTIFICATION_LIMIT_DEFAULT, ge=1, le=_NOTIFICATION_LIMIT_MAX),
    before: int | None = Query(None),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> NotificationListResponse:
    """List the caller's notifications, newest first.

    ``before`` is the id of the last row from the previous page (exclusive).
    Rows whose ticket now lives in a project the caller can no longer access
    are hidden, so losing access to a project hides its notifications at once.
    """
    items: list[TicketNotificationItem] = []
    seen: set[int] = set()
    cursor = before
    # Bounded scan: a long run of rows the caller lost access to can't loop
    # forever — at most 10 pages of ``limit * 2`` rows are scanned.
    for _ in range(10):
        if len(items) >= limit:
            break
        stmt = (
            select(TicketNotification, Ticket.title, Ticket.project_id)
            .join(Ticket, Ticket.id == TicketNotification.ticket_id)
            .where(TicketNotification.user_id == user.id)
            .order_by(TicketNotification.id.desc())
            .limit(limit * 2)
        )
        if unread:
            stmt = stmt.where(TicketNotification.read_at.is_(None))
        if cursor is not None:
            stmt = stmt.where(TicketNotification.id < cursor)
        rows = (await db.execute(stmt)).all()
        if not rows:
            break
        cursor = rows[-1][0].id
        accessible = await _accessible_project_ids(db, user.id, {r[2] for r in rows})
        for notification, title, project_id in rows:
            if project_id not in accessible or notification.id in seen:
                continue
            seen.add(notification.id)
            if len(items) >= limit:
                break
            items.append(_to_item(notification, title))
    return NotificationListResponse(
        items=items,
        next_before=items[-1].id if items else None,
    )


@router.get("/ticket-notifications/unread-count")
async def ticket_notifications_unread_count(
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Response:
    """Unread count for the inbox badge, with an ETag for 60s polling.

    The count only includes tickets whose current project the caller can
    still access. The ETag folds in the newest unread id so it also changes
    when a notification is marked read without changing the total.
    """
    # Aggregate in SQL (COUNT + MAX grouped by project) instead of loading
    # every unread row, then check access once per distinct project id.
    rows = (
        await db.execute(
            select(
                func.count(TicketNotification.id),
                func.max(TicketNotification.id),
                Ticket.project_id,
            )
            .join(Ticket, Ticket.id == TicketNotification.ticket_id)
            .where(
                TicketNotification.user_id == user.id,
                TicketNotification.read_at.is_(None),
            )
            .group_by(Ticket.project_id)
        )
    ).all()
    accessible = await _accessible_project_ids(db, user.id, {r[2] for r in rows})
    count = sum(r[0] for r in rows if r[2] in accessible)
    max_id = max((r[1] for r in rows if r[2] in accessible), default=0)
    etag = f'"{count}-{max_id}"'
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304)
    return JSONResponse({"count": count}, headers={"ETag": etag})


@router.post("/ticket-notifications/read")
async def mark_ticket_notifications_read(
    body: ReadNotificationsRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Mark the caller's notifications read by id, or all of them."""
    if not body.all and not body.ids:
        raise HTTPException(400, "Pass ids or all=true")
    if body.ids is not None and len(body.ids) > _READ_IDS_MAX:
        raise HTTPException(422, f"at most {_READ_IDS_MAX} ids per request")
    now = datetime.now(timezone.utc)
    stmt = (
        update(TicketNotification)
        .where(
            TicketNotification.user_id == user.id,
            TicketNotification.read_at.is_(None),
        )
        .values(read_at=now)
    )
    if not body.all:
        stmt = stmt.where(TicketNotification.id.in_(body.ids))
    result = await db.execute(stmt)
    await db.commit()
    return {"updated": result.rowcount}


@router.get("/ticket-updates", response_model=TicketUpdatesResponse)
async def get_ticket_updates(
    persona: str = Query(""),
    auth: AuthContext = Depends(require_scope("tickets:read")),
    db: AsyncSession = Depends(get_db),
) -> TicketUpdatesResponse:
    """Agent delta: notifications with id greater than the caller's stored
    cursor for ``persona`` (the empty string is the human's own cursor).

    Returns at most 100 rows and a ``next_cursor`` to hand to the ack
    endpoint. ``summary`` is server-generated and never a comment body.
    """
    user = auth.user
    is_service = auth.key_kind == "service"
    cursor_persona = _cursor_persona(auth, persona)
    stored = (
        await db.execute(
            select(TicketWatchCursor.last_seen_notification_id).where(
                TicketWatchCursor.user_id == user.id,
                TicketWatchCursor.persona_name == cursor_persona,
            )
        )
    ).scalar_one_or_none()
    cursor_id = stored if stored is not None else 0

    now = datetime.now(timezone.utc)
    horizon = now - _DELTA_HORIZON
    items: list[TicketNotificationItem] = []
    seen: set[int] = set()
    scanned_cursor = cursor_id
    # Bounded scan: a run of rows the caller lost access to can't wedge the
    # cursor forever — keep advancing ``scanned_cursor`` up to 10 pages until
    # we find a visible item or the feed is exhausted. ``next_cursor`` is the
    # last row *scanned* (not the last visible row), so invisible rows are
    # skipped once instead of re-read every poll.
    for _ in range(10):
        rows = (
            await db.execute(
                select(TicketNotification, Ticket.title, Ticket.project_id)
                .join(Ticket, Ticket.id == TicketNotification.ticket_id)
                .where(
                    TicketNotification.user_id == user.id,
                    TicketNotification.id > scanned_cursor,
                )
                .order_by(TicketNotification.id.asc())
                .limit(_DELTA_LIMIT)
            )
        ).all()
        if not rows:
            break
        # Stop at the first row still inside the commit horizon: its id may
        # have been allocated before a still-uncommitted row, so the cursor
        # must never advance past it (which would skip that row once it lands).
        original_len = len(rows)
        stop_at = original_len
        for i, (notification, _, _) in enumerate(rows):
            if _as_aware(notification.updated_at) > horizon:
                stop_at = i
                break
        rows = rows[:stop_at]
        if not rows:
            # Every row is inside the horizon — nothing to return yet, and the
            # cursor stays put so these rows are re-read once they age out.
            break
        scanned_cursor = rows[-1][0].id
        project_ids = {r[2] for r in rows}
        if is_service:
            accessible = await _service_accessible_project_ids(db, auth, project_ids)
        else:
            accessible = await _accessible_project_ids(db, user.id, project_ids)
        for notification, title, project_id in rows:
            if project_id not in accessible or notification.id in seen:
                continue
            seen.add(notification.id)
            items.append(_to_item(notification, title))
        if stop_at < original_len:
            # A too-new row ended this page — don't scan further pages past it.
            break
        if items:
            break
    return TicketUpdatesResponse(
        items=items[: _DELTA_LIMIT],
        next_cursor=scanned_cursor,
    )


@router.post("/ticket-updates/ack")
async def ack_ticket_updates(
    body: AckUpdatesRequest,
    auth: AuthContext = Depends(require_scope("tickets:read")),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Advance the caller's cursor for ``persona``. Idempotent: acking a
    cursor that is not newer than the stored one leaves it unchanged."""
    user = auth.user
    if len(body.persona) > _PERSONA_MAX:
        raise HTTPException(
            422, f"persona must be at most {_PERSONA_MAX} characters"
        )
    cursor_persona = _cursor_persona(auth, body.persona)
    now = datetime.now(timezone.utc)

    # Upsert with GREATEST (PG) / MAX (SQLite) so two concurrent first acks
    # can't resurrect an older cursor after a newer one won — the column can
    # only move forward.
    dialect = db.bind.dialect.name if db.bind is not None else "sqlite"
    values = {
        "user_id": user.id,
        "persona_name": cursor_persona,
        "last_seen_notification_id": body.cursor,
        "updated_at": now,
    }
    if dialect == "postgresql":
        from sqlalchemy.dialects.postgresql import insert as pg_insert

        stmt = pg_insert(TicketWatchCursor).values(**values)
        stmt = stmt.on_conflict_do_update(
            index_elements=["user_id", "persona_name"],
            set_={
                "last_seen_notification_id": func.greatest(
                    TicketWatchCursor.last_seen_notification_id, body.cursor
                ),
                "updated_at": now,
            },
        )
    else:
        from sqlalchemy.dialects.sqlite import insert as sqlite_insert

        stmt = sqlite_insert(TicketWatchCursor).values(**values)
        stmt = stmt.on_conflict_do_update(
            index_elements=["user_id", "persona_name"],
            set_={
                "last_seen_notification_id": func.max(
                    TicketWatchCursor.last_seen_notification_id, body.cursor
                ),
                "updated_at": now,
            },
        )
    await db.execute(stmt)
    await db.commit()

    stored = (
        await db.execute(
            select(TicketWatchCursor.last_seen_notification_id).where(
                TicketWatchCursor.user_id == user.id,
                TicketWatchCursor.persona_name == cursor_persona,
            )
        )
    ).scalar_one_or_none()
    return {"cursor": stored if stored is not None else body.cursor}


@router.get("/notification-settings", response_model=NotificationSettingsResponse)
async def get_notification_settings(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> NotificationSettingsResponse:
    """Read the caller's ticket email preference (default your_turn)."""
    row = (
        await db.execute(
            select(UserNotificationSettings).where(
                UserNotificationSettings.user_id == user.id
            )
        )
    ).scalar_one_or_none()
    return NotificationSettingsResponse(
        ticket_email=row.ticket_email if row is not None else "your_turn"
    )


@router.put("/notification-settings", response_model=NotificationSettingsResponse)
async def put_notification_settings(
    body: NotificationSettingsRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> NotificationSettingsResponse:
    """Store the caller's ticket email preference."""
    if body.ticket_email not in _EMAIL_MODES:
        raise HTTPException(
            400, f"ticket_email must be one of {sorted(_EMAIL_MODES)}"
        )
    row = (
        await db.execute(
            select(UserNotificationSettings).where(
                UserNotificationSettings.user_id == user.id
            )
        )
    ).scalar_one_or_none()
    if row is None:
        row = UserNotificationSettings(user_id=user.id, ticket_email=body.ticket_email)
        db.add(row)
    else:
        row.ticket_email = body.ticket_email
        row.updated_at = datetime.now(timezone.utc)
    await db.commit()
    return NotificationSettingsResponse(ticket_email=row.ticket_email)
