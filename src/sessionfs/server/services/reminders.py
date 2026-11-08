"""Reminders: source resolution, visibility, due notifications and the sweep.

A reminder is private to its owner (the person reminded) and its creator.
Everyone else gets the same 404 as for an id that does not exist.

Every read re-checks the reminder's source against the LIVE source row:

- the creator always sees the reminder;
- an owner who is also the creator always sees it;
- an owner who did not create it sees it only while they can read the source.

The source link itself (``source`` in responses) is shown only while the
viewer can read the source. The stored ``org_id``/``project_id``/``group_id``
are hints written from the source at create time; access never trusts them.

Source predicates (for a user id):

- ``ticket`` / ``knowledge_entry``: read access to the row's project.
- ``meeting`` / ``meeting_item``: a live (not deleted) meeting whose project
  the user can read, or whose group the user has standing on (org admin or an
  explicit grant) while the group's org plan includes project groups.
  ``meeting`` is the whole conversation (a meeting or a chat thread).
- ``chat_item``: an item of a live chat thread (``kind='chat'``), with the
  same readability rules. A ``meeting_item`` id that belongs to a chat thread
  (or a ``chat_item`` id on a meeting) does not exist as far as callers see.
- ``session``: the user owns the (not deleted) session.

Visibility is computed in bulk: sources are loaded with one query per kind,
projects and groups with one query each, and access is cached per
(user, project) / (user, group), so a list never issues a query per row.
"""

from __future__ import annotations

import logging
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Iterable

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server.db.models import (
    KnowledgeEntry,
    MeetingActionItem,
    MeetingTranscript,
    OrgMember,
    Project,
    ProjectGroup,
    ProjectGroupMember,
    Reminder,
    ReminderNote,
    Session,
    Ticket,
    TicketNotification,
    User,
)

logger = logging.getLogger("sessionfs.reminders")

SOURCE_KINDS = ("meeting_item", "meeting", "ticket", "session", "knowledge_entry", "chat_item")
READABLE_SOURCE_KINDS = frozenset(SOURCE_KINDS)
# Item source kind -> the conversation kind its transcript must have.
ITEM_SOURCE_KINDS = {"meeting_item": "meeting", "chat_item": "chat"}


def item_source_kind(conversation_kind: str | None) -> str:
    """The reminder source kind for an item of a conversation of this kind."""
    return "chat_item" if conversation_kind == "chat" else "meeting_item"
STATUSES = ("open", "done", "dismissed")

MAX_OPEN_PER_USER = 500
# One creator may keep at most this many open reminders for any one other person.
MAX_OPEN_PER_CREATOR_FOR_OTHER = 20
MAX_NOTES_PER_REMINDER = 200
WRITES_PER_MINUTE = 60
TITLE_MAX = 200
NOTE_MAX = 4000
DUE_PAST_SLACK = timedelta(hours=1)
DUE_FUTURE_MAX = timedelta(days=730)

NOT_FOUND = "Reminder not found"
REMINDER_DUE_KIND = "reminder_due"
REMINDER_DUE_SUMMARY = "Reminder due"

SWEEP_BATCH = 500
SWEEP_MAX_BATCHES = 20


def new_reminder_id() -> str:
    return "rem_" + secrets.token_hex(12)


def new_note_id() -> str:
    return "rnote_" + secrets.token_hex(12)


def as_aware(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


# ── Sources ──


@dataclass
class SourceInfo:
    """The live state of a reminder's source, or ``exists=False``."""

    kind: str
    id: str
    exists: bool = False
    project_id: str | None = None
    group_id: str | None = None
    org_id: str | None = None
    title: str | None = None
    session_owner_id: str | None = None
    meeting_id: str | None = None


SourceKey = tuple[str, str]


@dataclass
class AccessCache:
    """Per-request memo for project/group rows and access verdicts."""

    projects: dict[str, Project | None] = field(default_factory=dict)
    groups: dict[str, ProjectGroup | None] = field(default_factory=dict)
    project_access: dict[tuple[str, str], bool] = field(default_factory=dict)
    group_access: dict[tuple[str, str], bool] = field(default_factory=dict)
    group_plan_ok: dict[str, bool] = field(default_factory=dict)
    sources: dict[SourceKey, SourceInfo] = field(default_factory=dict)


def _short(text: str | None, limit: int = 200) -> str | None:
    if text is None:
        return None
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


async def load_sources(
    db: AsyncSession, refs: Iterable[SourceKey], cache: AccessCache | None = None
) -> dict[SourceKey, SourceInfo]:
    """Load the live state of every ``(kind, id)`` in ``refs`` in bulk."""
    cache = cache if cache is not None else AccessCache()
    wanted = {r for r in refs if r not in cache.sources}
    by_kind: dict[str, set[str]] = {}
    for kind, sid in wanted:
        by_kind.setdefault(kind, set()).add(sid)
        cache.sources[(kind, sid)] = SourceInfo(kind=kind, id=sid)

    meeting_ids: set[str] = set(by_kind.get("meeting", set()))
    item_rows: list[tuple[str, str, str, str]] = []  # (source kind, id, transcript, text)
    for item_kind in ITEM_SOURCE_KINDS:
        if not by_kind.get(item_kind):
            continue
        for item_id, transcript_id, description in (
            await db.execute(
                select(
                    MeetingActionItem.id,
                    MeetingActionItem.transcript_id,
                    MeetingActionItem.description,
                ).where(MeetingActionItem.id.in_(list(by_kind[item_kind])))
            )
        ).all():
            item_rows.append((item_kind, item_id, transcript_id, description))
            meeting_ids.add(transcript_id)
    meetings: dict[str, tuple] = {}
    if meeting_ids:
        for row in (
            await db.execute(
                select(
                    MeetingTranscript.id,
                    MeetingTranscript.project_id,
                    MeetingTranscript.group_id,
                    MeetingTranscript.title,
                    MeetingTranscript.kind,
                ).where(
                    MeetingTranscript.id.in_(list(meeting_ids)),
                    MeetingTranscript.deleted_at.is_(None),
                )
            )
        ).all():
            meetings[row[0]] = row
    for mid in by_kind.get("meeting", set()):
        m = meetings.get(mid)
        if m is not None:
            info = cache.sources[("meeting", mid)]
            info.exists = True
            info.project_id, info.group_id, info.title = m[1], m[2], _short(m[3])
            info.meeting_id = mid
    for item_kind, item_id, transcript_id, description in item_rows:
        m = meetings.get(transcript_id)
        # A meeting_item id on a chat thread (or the reverse) stays missing:
        # the same opaque "not found" as an id that doesn't exist.
        if m is not None and (m[4] or "meeting") == ITEM_SOURCE_KINDS[item_kind]:
            info = cache.sources[(item_kind, item_id)]
            info.exists = True
            info.project_id, info.group_id = m[1], m[2]
            info.title = _short(description)
            info.meeting_id = transcript_id

    if by_kind.get("ticket"):
        for tid, pid, title in (
            await db.execute(
                select(Ticket.id, Ticket.project_id, Ticket.title).where(
                    Ticket.id.in_(list(by_kind["ticket"]))
                )
            )
        ).all():
            info = cache.sources[("ticket", tid)]
            info.exists, info.project_id, info.title = True, pid, _short(title)

    if by_kind.get("session"):
        for sid, owner, pid, title in (
            await db.execute(
                select(Session.id, Session.user_id, Session.project_id, Session.title).where(
                    Session.id.in_(list(by_kind["session"])),
                    Session.is_deleted == False,  # noqa: E712
                )
            )
        ).all():
            info = cache.sources[("session", sid)]
            info.exists, info.session_owner_id = True, owner
            info.project_id, info.title = pid, _short(title)

    if by_kind.get("knowledge_entry"):
        int_ids = {int(x) for x in by_kind["knowledge_entry"] if x.isascii() and x.isdigit() and len(x) < 18}
        if int_ids:
            for kid, pid, content in (
                await db.execute(
                    select(
                        KnowledgeEntry.id, KnowledgeEntry.project_id, KnowledgeEntry.content
                    ).where(KnowledgeEntry.id.in_(list(int_ids)))
                )
            ).all():
                info = cache.sources[("knowledge_entry", str(kid))]
                info.exists, info.project_id, info.title = True, pid, _short(content, 120)

    # Live org from the live scope (never the stored hint).
    project_ids = {
        i.project_id for i in cache.sources.values() if i.exists and i.project_id
    }
    group_ids = {i.group_id for i in cache.sources.values() if i.exists and i.group_id}
    await _load_projects(db, project_ids, cache)
    await _load_groups(db, group_ids, cache)
    for key in wanted:
        info = cache.sources[key]
        if not info.exists:
            continue
        if info.project_id:
            project = cache.projects.get(info.project_id)
            info.org_id = project.org_id if project is not None else None
        elif info.group_id:
            group = cache.groups.get(info.group_id)
            info.org_id = group.org_id if group is not None else None
    return {r: cache.sources[r] for r in refs}


async def _load_projects(db: AsyncSession, ids: set[str], cache: AccessCache) -> None:
    missing = [i for i in ids if i not in cache.projects]
    if not missing:
        return
    found = {
        p.id: p
        for p in (
            await db.execute(select(Project).where(Project.id.in_(missing)))
        ).scalars()
    }
    for pid in missing:
        cache.projects[pid] = found.get(pid)


async def _load_groups(db: AsyncSession, ids: set[str], cache: AccessCache) -> None:
    missing = [i for i in ids if i not in cache.groups]
    if not missing:
        return
    found = {
        g.id: g
        for g in (
            await db.execute(
                select(ProjectGroup).where(
                    ProjectGroup.id.in_(missing), ProjectGroup.deleted_at.is_(None)
                )
            )
        ).scalars()
    }
    for gid in missing:
        cache.groups[gid] = found.get(gid)


async def _user_reads_project(
    db: AsyncSession, user_id: str, project_id: str, cache: AccessCache
) -> bool:
    from sessionfs.server.auth.project_access import user_can_access_project

    key = (user_id, project_id)
    if key not in cache.project_access:
        await _load_projects(db, {project_id}, cache)
        project = cache.projects.get(project_id)
        cache.project_access[key] = project is not None and await user_can_access_project(
            db, user_id, project
        )
    return cache.project_access[key]


async def _user_reads_group(
    db: AsyncSession, user_id: str, group_id: str, cache: AccessCache
) -> bool:
    """Group standing (org admin or explicit grant) AND the org plan includes
    project groups — visibility is decided before the plan, but both must hold
    for the source to count as readable."""
    from sessionfs.server.roles import ADMIN_ROLES
    from sessionfs.server.tier_gate import resolve_org_plan_tier
    from sessionfs.server.tiers import get_features_for_tier

    key = (user_id, group_id)
    if key in cache.group_access:
        return cache.group_access[key]
    await _load_groups(db, {group_id}, cache)
    group = cache.groups.get(group_id)
    ok = False
    if group is not None:
        role = (
            await db.execute(
                select(OrgMember.role).where(
                    OrgMember.org_id == group.org_id, OrgMember.user_id == user_id
                )
            )
        ).scalar_one_or_none()
        if role is not None:
            if role in ADMIN_ROLES:
                ok = True
            else:
                ok = (
                    await db.execute(
                        select(ProjectGroupMember.group_id).where(
                            ProjectGroupMember.group_id == group_id,
                            ProjectGroupMember.user_id == user_id,
                        )
                    )
                ).first() is not None
        if ok:
            if group.org_id not in cache.group_plan_ok:
                plan = await resolve_org_plan_tier(group.org_id, db)
                cache.group_plan_ok[group.org_id] = "project_groups" in get_features_for_tier(
                    plan
                )
            ok = cache.group_plan_ok[group.org_id]
    cache.group_access[key] = ok
    return ok


async def user_can_read_source(
    db: AsyncSession, user_id: str, info: SourceInfo | None, cache: AccessCache
) -> bool:
    if info is None or not info.exists or info.kind not in READABLE_SOURCE_KINDS:
        return False
    if info.kind == "session":
        return info.session_owner_id == user_id
    if info.project_id:
        return await _user_reads_project(db, user_id, info.project_id, cache)
    if info.group_id:
        return await _user_reads_group(db, user_id, info.group_id, cache)
    return False


# ── Visibility ──


@dataclass
class Visibility:
    visible: bool
    source_readable: bool
    source: SourceInfo | None


async def reminder_visibility(
    db: AsyncSession,
    viewer_id: str,
    reminders: Iterable[Reminder],
    cache: AccessCache | None = None,
) -> dict[str, Visibility]:
    """Visibility of each reminder for ``viewer_id`` (see the module doc)."""
    cache = cache if cache is not None else AccessCache()
    reminders = list(reminders)
    refs = {
        (r.source_kind, r.source_id)
        for r in reminders
        if r.source_kind and r.source_id
    }
    sources = await load_sources(db, refs, cache) if refs else {}
    out: dict[str, Visibility] = {}
    for r in reminders:
        is_owner = r.owner_user_id == viewer_id
        is_creator = r.created_by_user_id == viewer_id
        if not (is_owner or is_creator):
            out[r.id] = Visibility(False, False, None)
            continue
        if not r.source_kind or not r.source_id:
            out[r.id] = Visibility(True, False, None)
            continue
        info = sources.get((r.source_kind, r.source_id))
        readable = await user_can_read_source(db, viewer_id, info, cache)
        visible = is_creator or readable
        out[r.id] = Visibility(visible, readable, info if readable else None)
    return out


def service_key_allows_project(auth, project: Project | None) -> bool:
    """A service key sees only reminders whose live project is inside the
    key's org and (when set) its project allowlist."""
    if project is None or auth.org_id is None:
        return False
    if project.org_id != auth.org_id:
        return False
    if auth.project_ids is not None and project.id not in auth.project_ids:
        return False
    return True


# ── Notifications + sweep ──


def reminder_notification(reminder: Reminder, now: datetime) -> TicketNotification:
    """A ``reminder_due`` row for the owner. ``project_id``/``ticket_id`` stay
    NULL: access is re-derived from the reminder (and its source) on read."""
    return TicketNotification(
        user_id=reminder.owner_user_id,
        subject_kind="reminder",
        subject_id=reminder.id,
        ticket_id=None,
        project_id=None,
        kind=REMINDER_DUE_KIND,
        summary=REMINDER_DUE_SUMMARY,
        actor_type="user",
        actor_user_id=None,
        service_key_name=None,
        event_count=1,
        created_at=now,
        updated_at=now,
    )


async def clear_unread_due_notifications(db: AsyncSession, reminder_id: str) -> int:
    """Drop the reminder's unread ``reminder_due`` rows.

    Called after a reschedule or snooze (in the same transaction, AFTER the
    guarded UPDATE so a sweep that already claimed the row has committed its
    notification first): the reminder is no longer due, so the old "Reminder
    due" entry leaves the inbox and the digest. Read rows stay as history; the
    sweep writes a fresh row when the new time comes.
    """
    result = await db.execute(
        delete(TicketNotification)
        .where(
            TicketNotification.subject_kind == "reminder",
            TicketNotification.subject_id == reminder_id,
            TicketNotification.kind == REMINDER_DUE_KIND,
            TicketNotification.read_at.is_(None),
        )
        .execution_options(synchronize_session=False)
    )
    return result.rowcount or 0


async def delete_reminder_rows(db: AsyncSession, reminder_id: str) -> None:
    """Delete a reminder with its notes and notifications (no commit).

    The reminder row is locked FIRST. The due sweep holds that row lock from
    its claim until it commits the ``reminder_due`` row, so deleting the
    notifications only after taking the lock always sees a notification the
    sweep wrote; without it a delete racing the sweep could leave an orphan.
    A sweep that comes second finds the row gone and claims nothing.
    """
    await db.execute(
        select(Reminder.id).where(Reminder.id == reminder_id).with_for_update()
    )
    await db.execute(
        delete(TicketNotification).where(
            TicketNotification.subject_kind == "reminder",
            TicketNotification.subject_id == reminder_id,
        )
    )
    await db.execute(delete(ReminderNote).where(ReminderNote.reminder_id == reminder_id))
    await db.execute(delete(Reminder).where(Reminder.id == reminder_id))


async def claim_due_reminder(db: AsyncSession, reminder_id: str, now: datetime) -> bool:
    """The guarded claim: only one sweep can stamp ``notified_at``."""
    result = await db.execute(
        update(Reminder)
        .where(
            Reminder.id == reminder_id,
            Reminder.status == "open",
            Reminder.notified_at.is_(None),
            Reminder.due_at <= now,
        )
        .values(notified_at=now)
        .execution_options(synchronize_session=False)
    )
    return result.rowcount == 1


async def sweep_due_reminders(
    db: AsyncSession,
    *,
    now: datetime | None = None,
    batch_size: int = SWEEP_BATCH,
    max_batches: int = SWEEP_MAX_BATCHES,
) -> dict:
    """Notify the owner of every open, due, un-notified reminder once.

    Keyset batches by id. Each row is claimed with a rowcount-1 guarded UPDATE
    (two concurrent sweeps can't both notify), then the owner's access is
    re-checked before the notification is written; a claimed row whose owner
    can no longer see the reminder (or is inactive) is consumed silently.
    Each batch commits on its own.
    """
    now = now or datetime.now(timezone.utc)
    notified = skipped_access = lost_race = 0
    last_id = ""
    batches = 0
    exhausted = False
    while batches < max_batches:
        batches += 1
        ids = list(
            (
                await db.execute(
                    select(Reminder.id)
                    .where(
                        Reminder.status == "open",
                        Reminder.notified_at.is_(None),
                        Reminder.due_at <= now,
                        Reminder.id > last_id,
                    )
                    .order_by(Reminder.id.asc())
                    .limit(batch_size)
                )
            ).scalars()
        )
        if not ids:
            exhausted = True
            break
        last_id = ids[-1]
        claimed: list[str] = []
        for rid in ids:
            if await claim_due_reminder(db, rid, now):
                claimed.append(rid)
            else:
                lost_race += 1
        if claimed:
            rows = list(
                (
                    await db.execute(select(Reminder).where(Reminder.id.in_(claimed)))
                ).scalars()
            )
            owners = {
                u.id: u
                for u in (
                    await db.execute(
                        select(User).where(
                            User.id.in_(list({r.owner_user_id for r in rows}))
                        )
                    )
                ).scalars()
            }
            cache = AccessCache()
            # One visibility pass per owner (sources are loaded once for the
            # whole group), not one per row.
            by_owner: dict[str, list[Reminder]] = {}
            for r in rows:
                owner = owners.get(r.owner_user_id)
                if owner is None or not owner.is_active:
                    skipped_access += 1
                    continue
                by_owner.setdefault(owner.id, []).append(r)
            visibility: dict[str, Visibility] = {}
            for owner_id, owner_rows in by_owner.items():
                visibility.update(
                    await reminder_visibility(db, owner_id, owner_rows, cache)
                )
            for r in rows:
                if r.owner_user_id not in by_owner:
                    continue
                vis = visibility[r.id]
                if not vis.visible:
                    skipped_access += 1
                    continue
                db.add(reminder_notification(r, now))
                notified += 1
        await db.commit()
        if len(ids) < batch_size:
            exhausted = True
            break
    return {
        "reminders_notified": notified,
        "reminders_skipped_access": skipped_access,
        "reminders_already_claimed": lost_race,
        "batches": batches,
        "exhausted": exhausted,
    }
