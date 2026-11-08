"""External contacts (GitHub #125, docs/design/external-contacts.md).

An external contact is a person the org attributes work to who is not a
SessionFS user. People create them; extraction never does. Everything here
is commit-free: callers own the transaction.

- Normalisation is the meeting extraction ``_norm`` (NFKC + casefold +
  whitespace fold), so a contact name matches a transcript name exactly when
  the extraction evidence matcher would.
- Resolution (``resolve_contact``) uses an exact email, then exactly one
  active contact whose ``name_norm`` or alias matches. Nothing fuzzy.
- Linking to a user happens only by VERIFIED email of an active member of the
  contact's org (``link_contacts_for_user``); it moves the contact's open
  tickets to that user in the same transaction, with an event each.
- Erasure (``erase_contact``) hard-deletes the row and its aliases, puts
  participants back to unresolved and unassigns tickets with an event that
  stores only ``{"contact_removed": true}``.
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import delete, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import set_committed_value

from sessionfs.server.db.models import (
    ExternalContact,
    ExternalContactAlias,
    MeetingParticipant,
    OrgMember,
    Project,
    Ticket,
    User,
)
from sessionfs.server.services.ticket_events import record_ticket_event

logger = logging.getLogger("sessionfs.contacts")

NAME_MAX = 120
ROLE_MAX = 80
EMAIL_MAX = 320
TAG_MAX = 32
TAGS_MAX = 10
ALIASES_MAX = 20
OPEN_TICKET_EXCLUDED = ("done", "closed", "cancelled")

CONTACT_NOT_FOUND = "Contact not found"

_EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+'-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,63}$")
_TAG_RE = re.compile(r"^[a-z0-9][a-z0-9_.:-]{0,31}$")
_ID_RE = re.compile(r"^ec_[0-9a-f]{12}$")


def new_contact_id() -> str:
    return f"ec_{uuid.uuid4().hex[:12]}"


def valid_contact_id(value: str | None) -> bool:
    return bool(value) and bool(_ID_RE.match(value or ""))


def norm_name(text: str | None) -> str:
    """The extraction normaliser (one definition for both sides of a match)."""
    from sessionfs.server.services.meeting_extraction import _norm

    return _norm(text or "")


class ContactInputError(ValueError):
    """A field failed validation; ``code`` is the 422 error code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def clean_text(value: str | None, *, field: str, max_len: int, required: bool) -> str | None:
    """Strip NULs / lone surrogates, fold whitespace, enforce length."""
    from sessionfs.server.sanitize import sanitize_archive_string

    if value is None:
        if required:
            raise ContactInputError("invalid_contact", f"{field} is required")
        return None
    cleaned, _ = sanitize_archive_string(value)
    cleaned = " ".join(str(cleaned).split())
    if not cleaned:
        if required:
            raise ContactInputError("invalid_contact", f"{field} must not be blank")
        return None
    if len(cleaned) > max_len:
        raise ContactInputError(
            "invalid_contact", f"{field} must be at most {max_len} characters"
        )
    return cleaned


def clean_email(value: str | None) -> str | None:
    if value is None:
        return None
    email = value.strip().lower()
    if not email:
        return None
    if len(email) > EMAIL_MAX or not _EMAIL_RE.match(email):
        raise ContactInputError("invalid_email", "email is not a valid address")
    return email


def clean_tags(values: list[str] | None) -> list[str]:
    if not values:
        return []
    out: list[str] = []
    for raw in values:
        tag = (raw or "").strip().lower()
        if not tag:
            continue
        if not _TAG_RE.match(tag):
            raise ContactInputError(
                "invalid_tag",
                f"tags are lowercase letters, digits, '.', '_', ':' or '-' "
                f"(at most {TAG_MAX} characters)",
            )
        if tag not in out:
            out.append(tag)
    if len(out) > TAGS_MAX:
        raise ContactInputError("invalid_tag", f"at most {TAGS_MAX} tags")
    return out


def load_tags(raw: str | None) -> list[str]:
    try:
        value = json.loads(raw or "[]")
    except (ValueError, TypeError):
        return []
    return [t for t in value if isinstance(t, str)] if isinstance(value, list) else []


# ── Visibility ──


async def org_role(db: AsyncSession, org_id: str, user_id: str) -> str | None:
    """The user's role in ``org_id`` when they are an active member."""
    return (
        await db.execute(
            select(OrgMember.role)
            .join(User, User.id == OrgMember.user_id)
            .where(
                OrgMember.org_id == org_id,
                OrgMember.user_id == user_id,
                User.is_active.is_(True),
            )
        )
    ).scalars().first()


def email_visible(contact: ExternalContact, *, role: str | None, user_id: str | None,
                  service_key: bool) -> bool:
    """Decision 4: owners, admins and the creator see the email; service keys
    never do."""
    if service_key:
        return False
    if role in ("owner", "admin"):
        return True
    return bool(user_id) and contact.created_by_user_id == user_id


def contact_ref(contact: ExternalContact) -> dict[str, Any]:
    """What an artifact (meeting, chat, ticket) renders inline for a contact:
    name and role, never the email."""
    return {
        "id": contact.id,
        "display_name": contact.display_name,
        "role": contact.role,
        "status": contact.status,
        "linked_user_id": contact.linked_user_id,
        "external": True,
    }


async def contacts_in_org(
    db: AsyncSession, contact_ids: set[str], org_id: str | None
) -> dict[str, ExternalContact]:
    """The contacts among ``contact_ids`` that belong to ``org_id`` (the
    artifact's live scope org). Any other contact is not rendered."""
    if not contact_ids or not org_id:
        return {}
    rows = (
        await db.execute(
            select(ExternalContact).where(
                ExternalContact.id.in_(sorted(contact_ids)),
                ExternalContact.org_id == org_id,
            )
        )
    ).scalars().all()
    return {c.id: c for c in rows}


# ── Resolution ──


async def active_contact_by_email(
    db: AsyncSession, org_id: str, email: str
) -> ExternalContact | None:
    rows = (
        await db.execute(
            select(ExternalContact)
            .where(
                ExternalContact.org_id == org_id,
                ExternalContact.status == "active",
                ExternalContact.email == email.strip().lower(),
            )
            .limit(2)
        )
    ).scalars().all()
    return rows[0] if len(rows) == 1 else None


async def active_contacts_by_norm(
    db: AsyncSession, org_id: str, key: str, *, limit: int = 2
) -> list[ExternalContact]:
    """Active contacts whose display name or an alias normalises to ``key``."""
    if not key:
        return []
    alias_ids = (
        select(ExternalContactAlias.contact_id)
        .where(
            ExternalContactAlias.org_id == org_id,
            ExternalContactAlias.alias_norm == key,
        )
        .scalar_subquery()
    )
    return list(
        (
            await db.execute(
                select(ExternalContact)
                .where(
                    ExternalContact.org_id == org_id,
                    ExternalContact.status == "active",
                    or_(
                        ExternalContact.name_norm == key,
                        ExternalContact.id.in_(alias_ids),
                    ),
                )
                .order_by(ExternalContact.id.asc())
                .limit(limit)
            )
        ).scalars().all()
    )


async def resolve_contact(
    db: AsyncSession, org_id: str | None, name: str, email: str | None
) -> ExternalContact | None:
    """Steps 4–5 of the resolution order: exact email of an active contact,
    then exactly one active contact by name or alias. ``None`` otherwise."""
    if not org_id:
        return None
    if email:
        contact = await active_contact_by_email(db, org_id, email)
        if contact is not None:
            return contact
    matches = await active_contacts_by_norm(db, org_id, norm_name(name))
    return matches[0] if len(matches) == 1 else None


async def linked_member_id(db: AsyncSession, contact: ExternalContact) -> str | None:
    """The linked user when they are still an active member of the contact's
    org; otherwise None (the contact resolves as itself)."""
    if not contact.linked_user_id:
        return None
    role = await org_role(db, contact.org_id, contact.linked_user_id)
    return contact.linked_user_id if role is not None else None


async def eligible_assignee_contact(
    db: AsyncSession,
    project: Any,
    contact_id: str,
    *,
    lock: bool = False,
    reject_linked_member: bool = False,
) -> ExternalContact | None:
    """An active contact in ``project``'s live org (anything with an
    ``org_id``: a project, or a meeting's scope), else None.

    ``lock`` reads the row ``FOR UPDATE`` (refreshing any copy already in the
    session) so the caller's write serialises with link, merge, archive and
    delete, which all lock the contact row first. Callers take the project
    scope lock before this row lock.

    ``reject_linked_member`` refuses a contact linked to a user who is still
    an active member of its org: that person is assigned as the user, not
    through their old contact record (§4, §7). A link whose member has left
    resolves as the contact again, as resolution and promote drafting do.
    """
    if not project.org_id or not valid_contact_id(contact_id):
        return None
    if lock:
        contact = (
            await db.execute(
                select(ExternalContact)
                .where(ExternalContact.id == contact_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
    else:
        contact = await db.get(ExternalContact, contact_id)
    if contact is None or contact.org_id != project.org_id or contact.status != "active":
        return None
    if contact.merged_into_id is not None:
        return None
    if reject_linked_member and await linked_member_id(db, contact) is not None:
        return None
    return contact


# ── Linking to a user (verified email only) ──


async def link_contacts_for_user(
    db: AsyncSession, user: User, *, org_id: str | None = None
) -> list[str]:
    """Link the user to every contact in their orgs whose email equals their
    VERIFIED email, and move each contact's open tickets to them.

    Runs inside the caller's transaction (membership creation, email
    verification, an admin setting a contact's email). No-op unless the user
    is active with a verified email. Returns the linked contact ids.

    Each candidate is locked and re-read before linking (a merge may have
    committed since the candidate list was read), and the link itself is a
    guarded UPDATE that refuses a merged row. Lock order: contact rows in id
    order, then their tickets in id order.
    """
    if not user.is_active or not user.email_verified or not user.email:
        return []
    email = user.email.strip().lower()
    stmt = (
        select(ExternalContact.id)
        .where(
            ExternalContact.email == email,
            ExternalContact.status != "merged",
            ExternalContact.linked_user_id.is_(None),
        )
        .order_by(ExternalContact.id.asc())
    )
    if org_id is not None:
        stmt = stmt.where(ExternalContact.org_id == org_id)
    linked: list[str] = []
    for contact_id in list((await db.execute(stmt)).scalars().all()):
        contact = await lock_contact(db, contact_id)
        if (
            contact is None
            or contact.status == "merged"
            or contact.merged_into_id is not None
            or contact.linked_user_id is not None
            or contact.email != email
            or (org_id is not None and contact.org_id != org_id)
        ):
            continue
        if await org_role(db, contact.org_id, user.id) is None:
            continue
        now = datetime.now(timezone.utc)
        result = await db.execute(
            update(ExternalContact)
            .where(
                ExternalContact.id == contact.id,
                ExternalContact.linked_user_id.is_(None),
                ExternalContact.status != "merged",
                ExternalContact.merged_into_id.is_(None),
            )
            .values(linked_user_id=user.id, linked_at=now, updated_at=now)
            .execution_options(synchronize_session=False)
        )
        if result.rowcount != 1:
            continue
        set_committed_value(contact, "linked_user_id", user.id)
        set_committed_value(contact, "linked_at", now)
        set_committed_value(contact, "updated_at", now)
        linked.append(contact.id)
        tickets = await lock_contact_tickets(db, contact.id, open_only=True)
        await move_tickets_to_user(
            db, tickets, contact_id=contact.id, user=user,
            actor_type="system", actor_label="contact linked to member",
        )
    return linked


async def lock_contact_tickets(
    db: AsyncSession,
    contact_id: str | None = None,
    *,
    project_id: str | None = None,
    open_only: bool = False,
    contact_ids: list[str] | None = None,
) -> list[Ticket]:
    """The tickets assigned to a contact (or to any contact, in a project),
    read ``FOR UPDATE`` in id order with every copy in the session refreshed.

    Callers lock the contact row(s) first (and the project scope before
    that, where they take it), then call this: one lock order everywhere.
    """
    stmt = select(Ticket)
    if contact_ids is not None:
        stmt = stmt.where(Ticket.assigned_contact_id.in_(contact_ids))
    elif contact_id is not None:
        stmt = stmt.where(Ticket.assigned_contact_id == contact_id)
    else:
        stmt = stmt.where(Ticket.assigned_contact_id.is_not(None))
    if project_id is not None:
        stmt = stmt.where(Ticket.project_id == project_id)
    if open_only:
        stmt = stmt.where(Ticket.status.notin_(OPEN_TICKET_EXCLUDED))
    return list(
        (
            await db.execute(
                stmt.order_by(Ticket.id.asc())
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalars().all()
    )


async def reassign_contact_ticket(
    db: AsyncSession, ticket: Ticket, *, from_contact_id: str, **values: Any
) -> bool:
    """Set ``values`` on ``ticket`` only while it is still assigned to
    ``from_contact_id`` (a guarded UPDATE, rowcount 1). False when another
    write moved the ticket first; the caller then leaves it alone."""
    values.setdefault("updated_at", datetime.now(timezone.utc))
    result = await db.execute(
        update(Ticket)
        .where(Ticket.id == ticket.id, Ticket.assigned_contact_id == from_contact_id)
        .values(**values)
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:
        return False
    for key, value in values.items():
        set_committed_value(ticket, key, value)
    return True


async def move_tickets_to_user(
    db: AsyncSession,
    tickets: list[Ticket],
    *,
    contact_id: str,
    user: User,
    actor_type: str,
    actor_user_id: str | None = None,
    actor_label: str | None = None,
) -> list[Ticket]:
    """Move the (locked) open tickets of ``contact_id`` whose project ``user``
    can read to ``user``, each with an event and an assignee subscription.
    Returns the tickets that were NOT moved (closed, unreadable project, or
    reassigned by another write)."""
    from sessionfs.server.auth.project_access import user_has_read_standing
    from sessionfs.server.services import ticket_watchers

    projects: dict[str, bool] = {}
    left: list[Ticket] = []
    for ticket in tickets:
        if ticket.assigned_contact_id != contact_id or ticket.status in OPEN_TICKET_EXCLUDED:
            left.append(ticket)
            continue
        if ticket.project_id not in projects:
            project = await db.get(Project, ticket.project_id)
            projects[ticket.project_id] = project is not None and await user_has_read_standing(
                db, user.id, project
            )
        if not projects[ticket.project_id]:
            left.append(ticket)
            continue
        if not await reassign_contact_ticket(
            db, ticket, from_contact_id=contact_id,
            assigned_contact_id=None, assigned_user_id=user.id,
        ):
            continue
        await record_ticket_event(
            db,
            ticket,
            "assigned",
            field="assigned_user_id",
            before={"contact_id": contact_id},
            after={"user_id": user.id},
            actor_type=actor_type,
            actor_user_id=actor_user_id,
            actor_label=actor_label,
        )
        await ticket_watchers.subscribe_guarded(
            db, ticket_id=ticket.id, user_id=user.id, source="assignee"
        )
    return left


async def link_contacts_best_effort(db: AsyncSession, user: User, *, org_id: str | None = None) -> None:
    """``link_contacts_for_user`` inside a SAVEPOINT: a failure here never
    fails the membership or verification write it rides on."""
    try:
        async with db.begin_nested():
            await link_contacts_for_user(db, user, org_id=org_id)
    except Exception:  # noqa: BLE001 — best effort by design
        logger.warning("contact link failed for user %s", user.id, exc_info=True)


# ── Erasure / transfer ──


@dataclass
class EraseResult:
    participants_reset: int = 0
    tickets_unassigned: int = 0


async def unassign_contact_tickets(
    db: AsyncSession,
    *,
    contact_id: str | None = None,
    project_id: str | None = None,
    actor_user_id: str | None,
    reason: str,
) -> int:
    """Unassign tickets from a contact (by contact or by project), with an
    event that stores only a flag, never the name."""
    count = 0
    for ticket in await lock_contact_tickets(db, contact_id, project_id=project_id):
        held = ticket.assigned_contact_id
        if held is None or (contact_id is not None and held != contact_id):
            continue
        if not await reassign_contact_ticket(
            db, ticket, from_contact_id=held, assigned_contact_id=None
        ):
            continue
        await record_ticket_event(
            db,
            ticket,
            "unassigned",
            field="assigned_contact_id",
            before={reason: True},
            actor_type="user" if actor_user_id else "system",
            actor_user_id=actor_user_id,
        )
        count += 1
    return count


async def clear_project_contact_assignments(
    db: AsyncSession, project_id: str, *, actor_user_id: str | None
) -> int:
    """A project moving to another org leaves its contacts behind."""
    return await unassign_contact_tickets(
        db, project_id=project_id, actor_user_id=actor_user_id, reason="contact_left_org"
    )


class ContactChanged(RuntimeError):
    """The contact row no longer matched what the eraser locked and checked
    (deleted, merged or its status changed); nothing may be committed."""


async def lock_contact(db: AsyncSession, contact_id: str) -> ExternalContact | None:
    """Re-read one contact ``FOR UPDATE`` (refreshing any stale copy in the
    session). None when the row is gone."""
    return (
        await db.execute(
            select(ExternalContact)
            .where(ExternalContact.id == contact_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()


async def contact_is_referenced(db: AsyncSession, contact_id: str) -> bool:
    """Any participant or ticket still pointing at the contact."""
    if (
        await db.execute(
            select(MeetingParticipant.id).where(MeetingParticipant.contact_id == contact_id).limit(1)
        )
    ).first() is not None:
        return True
    return (
        await db.execute(select(Ticket.id).where(Ticket.assigned_contact_id == contact_id).limit(1))
    ).first() is not None


async def erase_contact(
    db: AsyncSession, contact: ExternalContact, *, actor_user_id: str | None
) -> EraseResult:
    """Erase ``contact`` (which the caller has locked and re-checked).

    The final delete is guarded on the status the caller saw and that the
    row is not merged, and must hit exactly one row; otherwise
    ``ContactChanged`` is raised and the caller rolls the whole erasure back.
    """
    expected_status = contact.status
    result = EraseResult()
    reset = await db.execute(
        update(MeetingParticipant)
        .where(
            MeetingParticipant.contact_id == contact.id,
            MeetingParticipant.user_id.is_(None),
            MeetingParticipant.persona_name.is_(None),
        )
        .values(contact_id=None, resolution_method="unresolved", resolved_by_user_id=None)
        .execution_options(synchronize_session=False)
    )
    result.participants_reset = reset.rowcount or 0
    await db.execute(
        update(MeetingParticipant)
        .where(MeetingParticipant.contact_id == contact.id)
        .values(contact_id=None)
        .execution_options(synchronize_session=False)
    )
    result.tickets_unassigned = await unassign_contact_tickets(
        db, contact_id=contact.id, actor_user_id=actor_user_id, reason="contact_removed"
    )
    # Rows merged into this one are the same person: erased with it.
    merged_ids = list(
        (
            await db.execute(
                select(ExternalContact.id).where(ExternalContact.merged_into_id == contact.id)
            )
        ).scalars().all()
    )
    if merged_ids:
        await db.execute(
            delete(ExternalContactAlias).where(ExternalContactAlias.contact_id.in_(merged_ids))
        )
        await db.execute(delete(ExternalContact).where(ExternalContact.id.in_(merged_ids)))
    await db.execute(
        delete(ExternalContactAlias).where(ExternalContactAlias.contact_id == contact.id)
    )
    deleted = await db.execute(
        delete(ExternalContact)
        .where(
            ExternalContact.id == contact.id,
            ExternalContact.status == expected_status,
            ExternalContact.merged_into_id.is_(None),
        )
        .execution_options(synchronize_session=False)
    )
    if deleted.rowcount != 1:
        raise ContactChanged(contact.id)
    return result


# ── References ──


async def reference_rows(
    db: AsyncSession,
    contact_id: str,
    *,
    project_ids: set[str] | None,
    group_ids: set[str] | None,
    limit: int = 200,
) -> dict[str, list[dict[str, Any]]]:
    """Where a contact appears: participants (meetings/chats) and tickets.

    ``project_ids`` / ``group_ids`` are the caller's readable scopes; ``None``
    means unfiltered (the admin export, which lists ids and kinds only).
    """
    from sessionfs.server.db.models import MeetingActionItem, MeetingTranscript

    conversations: list[dict[str, Any]] = []
    stmt = (
        select(MeetingParticipant, MeetingTranscript)
        .join(MeetingTranscript, MeetingTranscript.id == MeetingParticipant.transcript_id)
        .where(
            MeetingParticipant.contact_id == contact_id,
            MeetingTranscript.deleted_at.is_(None),
        )
        .order_by(MeetingTranscript.created_at.desc(), MeetingTranscript.id.desc())
        .limit(limit)
    )
    readable = True
    if project_ids is not None or group_ids is not None:
        conds = []
        if project_ids:
            conds.append(MeetingTranscript.project_id.in_(sorted(project_ids)))
        if group_ids:
            conds.append(MeetingTranscript.group_id.in_(sorted(group_ids)))
        readable = bool(conds)
        if conds:
            stmt = stmt.where(or_(*conds))
    participant_ids: list[str] = []
    if readable:
        for participant, meeting in (await db.execute(stmt)).all():
            participant_ids.append(participant.id)
            conversations.append(
                {
                    "kind": meeting.kind or "meeting",
                    "meeting_id": meeting.id,
                    "participant_id": participant.id,
                    "title": meeting.title,
                    "project_id": meeting.project_id,
                    "group_id": meeting.group_id,
                    "meeting_at": meeting.meeting_at,
                    "created_at": meeting.created_at,
                }
            )
    action_items: list[dict[str, Any]] = []
    if participant_ids:
        for item in (
            await db.execute(
                select(MeetingActionItem)
                .where(MeetingActionItem.owner_participant_id.in_(participant_ids))
                .order_by(MeetingActionItem.extracted_at.desc(), MeetingActionItem.id.desc())
                .limit(limit)
            )
        ).scalars().all():
            action_items.append(
                {
                    "item_id": item.id,
                    "meeting_id": item.transcript_id,
                    "kind": item.kind,
                    "status": item.status,
                    "description": (item.description or "")[:200],
                    "due_date": item.due_date,
                }
            )
    tickets: list[dict[str, Any]] = []
    tstmt = (
        select(Ticket)
        .where(Ticket.assigned_contact_id == contact_id)
        .order_by(Ticket.updated_at.desc(), Ticket.id.desc())
        .limit(limit)
    )
    if project_ids is not None:
        tstmt = tstmt.where(Ticket.project_id.in_(sorted(project_ids) or [""]))
    for ticket in (await db.execute(tstmt)).scalars().all():
        tickets.append(
            {
                "id": ticket.id,
                "project_id": ticket.project_id,
                "title": ticket.title,
                "status": ticket.status,
                "updated_at": ticket.updated_at,
            }
        )
    return {"conversations": conversations, "action_items": action_items, "tickets": tickets}
