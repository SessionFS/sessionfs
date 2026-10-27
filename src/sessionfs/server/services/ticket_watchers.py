"""Ticket watcher subscriptions, notification emission and coalescing.

The route layer calls these helpers *inside* the same transaction as the
ticket mutation so subscriptions and notifications commit (or roll back)
atomically with the change they describe.

Subscriptions are best-effort: a user is subscribed once per (ticket,
user) and the first source wins. Notifications are server-generated —
``summary`` is a fixed phrase and NEVER copies a comment body — and skip
the actor so nobody is notified about their own action. Rapid bursts on
the same ticket coalesce into one unread row (``event_count`` increments
and the ``kind`` upgrades) so the inbox stays signal, not spam.

Emission is a best-effort side channel: it runs inside a SAVEPOINT and
swallows its own failures, so a broken watcher row or an access-check
error can never fail the ticket write that triggered it.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server.db.models import (
    AgentPersona,
    Project,
    Ticket,
    TicketNotification,
    TicketWatcher,
)

logger = logging.getLogger("sessionfs.ticket_watchers")

# Coalescing window: a burst of events on the same ticket within this
# window updates the existing unread row instead of creating new ones.
COALESCE_WINDOW = timedelta(minutes=15)

# Ordering for kind upgrades when events coalesce. Higher = more urgent.
_KIND_RANK = {
    "your_turn": 6,
    "review_verdict": 5,
    "status": 4,
    "assignment": 3,
    "dependency_unblocked": 2,
    "comment": 1,
}

# Human labels for the email badge and dashboard kind chip. Kept in one place
# so the email template and UI agree without duplicating the mapping.
KIND_LABELS = {
    "your_turn": "Your turn",
    "review_verdict": "Review verdict",
    "status": "Status",
    "assignment": "Assigned",
    "dependency_unblocked": "Unblocked",
    "comment": "Comment",
}


def _as_aware(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _is_recent(created_at: datetime, now: datetime) -> bool:
    return now - _as_aware(created_at) < COALESCE_WINDOW


def parse_verdict(content: str) -> str | None:
    """Return the canonical verdict when ``content`` carries a recognized
    review verdict header (``<Reviewer> R{N} review on tk_...: VERDICT``),
    else ``None``.

    This is the notification layer's "is this comment a trusted review
    verdict" seam. It delegates to the same header parser the review-state
    derivation uses (``review_state._HEADER_RE``), so a trusted reviewer's
    free-form comment — one without the structured header — is NOT treated
    as a review verdict.
    """
    from sessionfs.server.services.review_state import verdict_from_header

    return verdict_from_header(content)


async def subscribe(
    db: AsyncSession,
    *,
    ticket_id: str,
    user_id: str,
    source: str,
    persona_name: str | None = None,
    muted: bool = False,
) -> None:
    """Auto-subscribe a user to a ticket. First source wins; a no-op if
    the (ticket, user) row already exists."""
    if not user_id:
        return
    from sqlalchemy.dialects.postgresql import insert as pg_insert
    from sqlalchemy.dialects.sqlite import insert as sqlite_insert

    dialect = db.bind.dialect.name if db.bind is not None else "sqlite"
    insert_impl = pg_insert if dialect == "postgresql" else sqlite_insert
    stmt = insert_impl(TicketWatcher).values(
        ticket_id=ticket_id,
        user_id=user_id,
        persona_name=persona_name,
        source=source,
        muted=muted,
    ).on_conflict_do_nothing(index_elements=["ticket_id", "user_id"])
    await db.execute(stmt)


async def subscribe_guarded(
    db: AsyncSession,
    *,
    ticket_id: str,
    user_id: str,
    source: str,
    persona_name: str | None = None,
    muted: bool = False,
) -> None:
    """Best-effort auto-subscribe inside a SAVEPOINT.

    Auto-subscriptions in mutation routes are a side effect of the write,
    not the write itself, so a failure here must never fail (or roll back)
    the ticket mutation. This mirrors the savepoint + swallow pattern
    ``emit_notifications`` uses; the explicit watch/unwatch routes stay
    unguarded because there the subscription IS the write.
    """
    try:
        async with db.begin_nested():
            await subscribe(
                db,
                ticket_id=ticket_id,
                user_id=user_id,
                source=source,
                persona_name=persona_name,
                muted=muted,
            )
    except Exception as exc:  # pragma: no cover - defensive side channel
        logger.warning(
            "auto-subscribe failed for ticket %s user %s: %s",
            ticket_id,
            user_id,
            type(exc).__name__,
        )


async def set_muted(
    db: AsyncSession, *, ticket_id: str, user_id: str, muted: bool
) -> None:
    from sqlalchemy import update as _update

    await db.execute(
        _update(TicketWatcher)
        .where(TicketWatcher.ticket_id == ticket_id, TicketWatcher.user_id == user_id)
        .values(muted=muted)
    )


async def unsubscribe(db: AsyncSession, *, ticket_id: str, user_id: str) -> None:
    from sqlalchemy import delete

    await db.execute(
        delete(TicketWatcher).where(
            TicketWatcher.ticket_id == ticket_id, TicketWatcher.user_id == user_id
        )
    )


async def map_persona_to_human(
    db: AsyncSession, *, project_id: str, name: str, org_id: str | None
) -> tuple[str, str] | None:
    """Resolve a persona name to (user_id, persona_name) for its human
    owner, only when the mapping is unambiguous.

    Returns ``None`` when the persona doesn't exist, has no represented
    human (``represented_user_id`` is NULL), or ``org_id`` is missing for
    an org-scoped persona. ``resolve_persona`` already collapses the
    project-then-org precedence into a single canonical row, so a hit is
    by construction unambiguous.
    """
    if not name:
        return None
    from sessionfs.server.services.personas import resolve_persona

    persona: AgentPersona | None = await resolve_persona(
        db, project_id, name, org_id=org_id
    )
    if persona is None or not persona.represented_user_id:
        return None
    return persona.represented_user_id, persona.name


async def resolve_assigned_user(
    db: AsyncSession,
    *,
    project_id: str,
    ticket: Ticket,
    org_id: str | None,
) -> tuple[str, str | None] | None:
    """The human who owns the ticket's current assignment, if any.

    Direct-to-person assignment (``ticket.assigned_user_id``) wins and returns
    a None persona name; otherwise the persona name maps through
    ``map_persona_to_human``. Returns ``None`` when unassigned or unmapped.
    """
    if ticket.assigned_user_id:
        return ticket.assigned_user_id, None
    return await map_persona_to_human(
        db, project_id=project_id, name=ticket.assigned_to or "", org_id=org_id
    )


async def _coalesce(
    db: AsyncSession,
    existing: TicketNotification,
    kind: str,
    summary: str,
    now: datetime,
    *,
    actor_type: str,
    actor_user_id: str | None,
    service_key_name: str | None,
) -> None:
    """Replace an existing unread row with a fresh row carrying a NEW id.

    Coalescing must never update a row in place: the agent delta is
    ``id > cursor``, so a row upgraded to ``your_turn`` after an agent has
    already acked its id would be invisible if the id stayed the same.
    Deleting the old row and inserting a replacement gives the upgrade a new
    id (a new delta event) while the inbox keeps one unread row. The merged
    ``event_count``/``kind``/``summary`` are carried over, ``created_at`` is
    kept from the original, and ``updated_at`` becomes ``now``.

    The guarded DELETE keeps this safe against the sweep: if the row was
    already claimed (read or emailed) between load and coalesce, the DELETE
    matches nothing and we fall back to a plain fresh row so the event is
    never dropped.
    """
    incoming_rank = _KIND_RANK.get(kind, 0)
    existing_rank = _KIND_RANK.get(existing.kind or "", 0)
    merged_kind = kind if incoming_rank > existing_rank else existing.kind
    upgraded = incoming_rank > existing_rank
    merged_summary = summary if upgraded else existing.summary
    # The row is attributed to whoever caused its current kind: the incoming
    # actor when this event upgraded it, the original actor otherwise.
    merged_actor = (
        (actor_type, actor_user_id, service_key_name)
        if upgraded
        else (existing.actor_type, existing.actor_user_id, existing.service_key_name)
    )
    result = await db.execute(
        delete(TicketNotification)
        .where(
            TicketNotification.id == existing.id,
            TicketNotification.read_at.is_(None),
            TicketNotification.emailed_at.is_(None),
        )
        .execution_options(synchronize_session=False)
    )
    if result.rowcount == 0:
        # The sweep claimed the row meanwhile — emit a fresh row instead of
        # dropping the event.
        db.add(
            TicketNotification(
                user_id=existing.user_id,
                ticket_id=existing.ticket_id,
                project_id=existing.project_id,
                kind=kind,
                summary=summary,
                actor_type=actor_type,
                actor_user_id=actor_user_id,
                service_key_name=service_key_name,
                event_count=1,
                created_at=now,
                updated_at=now,
            )
        )
        return
    # The delete executed against the DB but ``synchronize_session=False``
    # left ``existing`` in the identity map. Expunge it so the replacement
    # (which on SQLite may reuse the same id) never collides with it.
    db.expunge(existing)
    db.add(
        TicketNotification(
            user_id=existing.user_id,
            ticket_id=existing.ticket_id,
            project_id=existing.project_id,
            kind=merged_kind,
            summary=merged_summary,
            actor_type=merged_actor[0],
            actor_user_id=merged_actor[1],
            service_key_name=merged_actor[2],
            event_count=existing.event_count + 1,
            created_at=existing.created_at,
            updated_at=now,
        )
    )


async def emit_notifications(
    db: AsyncSession,
    *,
    ticket: Ticket,
    kind: str,
    summary: str,
    actor_user_id: str | None,
    actor_type: str = "user",
    service_key_name: str | None = None,
    your_turn_user_id: str | None = None,
    subscribe_users: list[tuple[str, str, str | None]] | None = None,
) -> None:
    """Notify every non-muted watcher of ``ticket`` except the actor.

    ``subscribe_users`` auto-subscribes extra users (user_id, source,
    persona_name) first, so a freshly-subscribed assignee still receives
    the event that made them relevant. The ``your_turn`` recipient gets
    the ``your_turn`` kind instead of the base ``kind``.

    Watchers who can no longer access the ticket's project are skipped (so
    a member removed from an org stops receiving events even before their
    watcher rows are cleaned up), and the whole emission runs inside a
    SAVEPOINT so a failure is logged and swallowed rather than failing the
    ticket write.
    """
    now = datetime.now(timezone.utc)
    try:
        async with db.begin_nested():
            for user_id, source, persona_name in subscribe_users or []:
                await subscribe(
                    db,
                    ticket_id=ticket.id,
                    user_id=user_id,
                    source=source,
                    persona_name=persona_name,
                )

            watchers = (
                await db.execute(
                    select(TicketWatcher)
                    .where(
                        TicketWatcher.ticket_id == ticket.id,
                        TicketWatcher.muted.is_(False),
                    )
                    .order_by(TicketWatcher.user_id)
                )
            ).scalars().all()

            project = await db.get(Project, ticket.project_id)
            from sessionfs.server.auth.project_access import user_can_access_project

            access_cache: dict[str, bool] = {}
            recipients: list[TicketWatcher] = []
            for watcher in watchers:
                if watcher.user_id == actor_user_id:
                    continue
                if watcher.user_id not in access_cache:
                    access_cache[watcher.user_id] = (
                        await user_can_access_project(db, watcher.user_id, project)
                        if project is not None
                        else False
                    )
                if not access_cache[watcher.user_id]:
                    continue
                recipients.append(watcher)

            if not recipients:
                return

            # One query loads every coalesceable unread row for these
            # recipients, instead of one query per watcher.
            recipient_ids = [w.user_id for w in recipients]
            existing_rows = (
                await db.execute(
                    select(TicketNotification)
                    .where(
                        TicketNotification.ticket_id == ticket.id,
                        TicketNotification.user_id.in_(recipient_ids),
                        TicketNotification.read_at.is_(None),
                        TicketNotification.emailed_at.is_(None),
                    )
                    .order_by(TicketNotification.id.desc())
                )
            ).scalars().all()
            existing_by_user: dict[str, TicketNotification] = {}
            for row in existing_rows:
                existing_by_user.setdefault(row.user_id, row)

            for watcher in recipients:
                recipient_kind = (
                    "your_turn" if watcher.user_id == your_turn_user_id else kind
                )
                existing = existing_by_user.get(watcher.user_id)
                if existing is not None and _is_recent(existing.created_at, now):
                    await _coalesce(
                        db,
                        existing,
                        recipient_kind,
                        summary,
                        datetime.now(timezone.utc),
                        actor_type=actor_type,
                        actor_user_id=actor_user_id,
                        service_key_name=service_key_name,
                    )
                else:
                    db.add(
                        TicketNotification(
                            user_id=watcher.user_id,
                            ticket_id=ticket.id,
                            project_id=ticket.project_id,
                            kind=recipient_kind,
                            summary=summary,
                            actor_type=actor_type,
                            actor_user_id=actor_user_id,
                            service_key_name=service_key_name,
                            event_count=1,
                            # Stamp at insert time, not the database's
                            # transaction-start now(), so the delta's commit
                            # horizon measures from when the row was written.
                            created_at=datetime.now(timezone.utc),
                            updated_at=datetime.now(timezone.utc),
                        )
                    )
    except Exception as exc:  # pragma: no cover - defensive side channel
        logger.warning(
            "ticket notification emission failed for ticket %s: %s",
            ticket.id,
            type(exc).__name__,
        )
