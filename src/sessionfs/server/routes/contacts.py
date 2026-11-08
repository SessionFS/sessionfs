"""External contacts directory (GitHub #125, docs/design/external-contacts.md).

Under ``/api/v1/orgs/{org_id}/contacts``:

- list / get / references: active org members, and service keys of the org
  with ``contacts:read``.
- create / patch / aliases: any active member (decision 3), service keys with
  ``contacts:write``.
- merge, delete, export (incl. ``format=csv``) and the "To confirm"
  suggestions queue: org owners and admins (suggestions: any member), user
  keys only.

Every miss — unknown org, not a member, unknown contact, contact in another
org — is the same ``404 "Contact not found"``. Email is shown to owners,
admins and the contact's creator, masked for other members and never sent to
a service key (decision 4). Name, aliases, role and tags go through the
strictest-union DLP policy before they are stored.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server.auth.dependencies import AuthContext, get_current_user, require_scope
from sessionfs.server.db.engine import get_db
from sessionfs.server.db.models import (
    AttentionHide,
    ExternalContact,
    ExternalContactAlias,
    MeetingParticipant,
    MeetingTranscript,
    OrgAuditEvent,
    Organization,
    Project,
    ProjectGroup,
    User,
)
from sessionfs.server.services import contacts as svc
from sessionfs.server.services.ticket_events import record_ticket_event

router = APIRouter(prefix="/api/v1/orgs", tags=["contacts"])

_LIST_LIMIT_DEFAULT = 50
_LIST_LIMIT_MAX = 200
_CSV_MAX_ROWS = 10_000
_CSV_COLUMNS = (
    "id", "display_name", "email", "role", "tags", "aliases", "status",
    "linked_user_id", "created_at", "updated_at",
)
_SUGGESTION_DAYS = 90
_SUGGESTION_ROWS = 500
_SUGGESTION_GROUPS = 50
_SUGGESTION_SOURCES = 3


# ── Schemas ──


class ContactCreate(BaseModel):
    display_name: str = Field(..., min_length=1, max_length=svc.NAME_MAX)
    email: str | None = Field(default=None, max_length=svc.EMAIL_MAX)
    role: str | None = Field(default=None, max_length=svc.ROLE_MAX)
    tags: list[str] = Field(default_factory=list, max_length=svc.TAGS_MAX)
    aliases: list[str] = Field(default_factory=list, max_length=svc.ALIASES_MAX)


class ContactUpdate(BaseModel):
    display_name: str | None = Field(default=None, min_length=1, max_length=svc.NAME_MAX)
    email: str | None = Field(default=None, max_length=svc.EMAIL_MAX)
    role: str | None = Field(default=None, max_length=svc.ROLE_MAX)
    tags: list[str] | None = Field(default=None, max_length=svc.TAGS_MAX)
    status: Literal["active", "archived"] | None = None


class AliasCreate(BaseModel):
    alias: str = Field(..., min_length=1, max_length=svc.NAME_MAX)


class DuplicateHint(BaseModel):
    id: str
    display_name: str


class ContactResponse(BaseModel):
    id: str
    org_id: str
    display_name: str
    role: str | None = None
    tags: list[str] = []
    aliases: list[str] = []
    status: str
    # Owners, admins and the creator see the address; other members a masked
    # form (``email_masked`` true); service keys get null.
    email: str | None = None
    email_masked: bool = False
    has_email: bool = False
    linked_user_id: str | None = None
    linked_at: datetime | None = None
    created_by_user_id: str | None = None
    created_at: datetime
    updated_at: datetime
    # Set when the requested id was merged into this contact.
    redirected_from: str | None = None
    # Create only: contacts whose name or alias exactly matches this one.
    possible_duplicates: list[DuplicateHint] = []


class ContactListResponse(BaseModel):
    items: list[ContactResponse]
    next_before: str | None = None


class SuggestionSource(BaseModel):
    meeting_id: str
    kind: str
    title: str


class SuggestionCandidate(BaseModel):
    id: str
    display_name: str
    hint: str


class SuggestionGroup(BaseModel):
    name: str
    name_norm: str
    mention_count: int
    latest_mention_at: datetime | None = None
    sources: list[SuggestionSource] = []
    candidates: list[SuggestionCandidate] = []


class SuggestionsResponse(BaseModel):
    groups: list[SuggestionGroup]
    truncated: bool = False


class SuggestionApply(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    contact_id: str = Field(..., min_length=1, max_length=64)


class SuggestionApplyResponse(BaseModel):
    applied: int
    skipped: int


class SuggestionDismiss(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)


class MergeResponse(BaseModel):
    confirmed: bool
    target: ContactResponse
    source_id: str
    participants_moved: int
    tickets_moved: int
    aliases_added: list[str]


class DeleteResponse(BaseModel):
    deleted: bool
    contact_id: str
    participants_reset: int
    tickets_unassigned: int


# ── Helpers ──


def _not_found() -> HTTPException:
    return HTTPException(404, svc.CONTACT_NOT_FOUND)


def _input_error(exc: svc.ContactInputError) -> HTTPException:
    return HTTPException(422, {"error": exc.code, "message": exc.message})


def _email_conflict(existing: ExternalContact | None, *, role: str, auth: AuthContext) -> HTTPException:
    """409 for an email already held by a contact or a member. Only owners and
    admins (with a user key) learn which: everyone else, who sees contact
    emails masked, gets one generic conflict so a guessed address can't be
    matched to a contact or a member."""
    if auth.key_kind == "service" or role not in ("owner", "admin"):
        return HTTPException(409, {"error": "email_conflict",
                                   "message": "This email is already used in this organization."})
    if existing is not None:
        return HTTPException(409, {"error": "contact_exists", "contact_id": existing.id,
                                   "message": "Another contact already has this email."})
    return HTTPException(409, {"error": "email_is_member",
                               "message": "That email belongs to a member: assign the user instead."})


async def _standing(db: AsyncSession, auth: AuthContext, org_id: str) -> str:
    """``"service"`` for a service key of this org, else the caller's active
    org role. Every other caller gets the contact 404."""
    if not org_id or len(org_id) > 64:
        raise _not_found()
    if auth.key_kind == "service":
        if auth.org_id != org_id or await db.get(Organization, org_id) is None:
            raise _not_found()
        return "service"
    role = await svc.org_role(db, org_id, auth.user.id)
    if role is None:
        raise _not_found()
    return role


def _require_admin(role: str) -> None:
    if role not in ("owner", "admin"):
        raise HTTPException(
            403,
            {
                "error": "insufficient_role",
                "required_role": "admin",
                "message": "This action needs an org owner or admin.",
            },
        )


async def _load(db: AsyncSession, org_id: str, contact_id: str, *, follow_merge: bool = False,
                lock: bool = False) -> tuple[ExternalContact, str | None]:
    """The contact in ``org_id`` (following a merge when asked)."""
    if not svc.valid_contact_id(contact_id):
        raise _not_found()
    stmt = select(ExternalContact).where(
        ExternalContact.id == contact_id, ExternalContact.org_id == org_id
    )
    if lock:
        stmt = stmt.with_for_update().execution_options(populate_existing=True)
    contact = (await db.execute(stmt)).scalar_one_or_none()
    if contact is None:
        raise _not_found()
    redirected: str | None = None
    hops = 0
    while contact.status == "merged" and contact.merged_into_id:
        if not follow_merge or hops > 10:
            raise _not_found()
        redirected = redirected or contact.id
        nxt = await db.get(ExternalContact, contact.merged_into_id)
        if nxt is None or nxt.org_id != org_id:
            raise _not_found()
        contact = nxt
        hops += 1
    if contact.status == "merged":
        raise _not_found()
    return contact, redirected


async def _aliases(db: AsyncSession, contact_ids: list[str]) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {cid: [] for cid in contact_ids}
    if not contact_ids:
        return out
    for cid, alias in (
        await db.execute(
            select(ExternalContactAlias.contact_id, ExternalContactAlias.alias)
            .where(ExternalContactAlias.contact_id.in_(contact_ids))
            .order_by(ExternalContactAlias.created_at.asc(), ExternalContactAlias.alias_norm.asc())
        )
    ).all():
        out.setdefault(cid, []).append(alias)
    return out


def _response(contact: ExternalContact, aliases: list[str], *, role: str,
              auth: AuthContext, redirected_from: str | None = None) -> ContactResponse:
    from sessionfs.server.email import mask_address

    service = auth.key_kind == "service"
    visible = svc.email_visible(contact, role=role, user_id=auth.user.id, service_key=service)
    email: str | None
    if not contact.email or service:
        email = None
    elif visible:
        email = contact.email
    else:
        email = mask_address(contact.email)
    return ContactResponse(
        id=contact.id,
        org_id=contact.org_id,
        display_name=contact.display_name,
        role=contact.role,
        tags=svc.load_tags(contact.tags),
        aliases=aliases,
        status=contact.status,
        email=email,
        email_masked=bool(contact.email) and not visible and not service,
        has_email=bool(contact.email),
        linked_user_id=contact.linked_user_id,
        linked_at=contact.linked_at,
        created_by_user_id=contact.created_by_user_id,
        created_at=contact.created_at,
        updated_at=contact.updated_at,
        redirected_from=redirected_from,
    )


async def _one(db: AsyncSession, contact: ExternalContact, *, role: str, auth: AuthContext,
               redirected_from: str | None = None) -> ContactResponse:
    aliases = await _aliases(db, [contact.id])
    return _response(contact, aliases[contact.id], role=role, auth=auth,
                     redirected_from=redirected_from)


async def _dlp(db: AsyncSession, auth: AuthContext, org_id: str, values: list[str | None]
               ) -> list[str | None]:
    """Each value through the strictest-union policy (BLOCK refuses 422,
    otherwise the redacted text is what gets stored)."""
    from sessionfs.server.routes.meetings import _dlp_scan_content, _upload_dlp_policy

    policy = await _upload_dlp_policy(db, auth, org_id)
    out: list[str | None] = []
    for value in values:
        if value is None:
            out.append(None)
            continue
        scanned, _ = _dlp_scan_content(value, policy)
        out.append(scanned)
    return out


# The built-in detector for email addresses. The email field is an address
# by definition, so only this one is skipped there; secrets, phone and every
# other PHI pattern (and the org's custom patterns) still apply.
_EMAIL_DETECTORS = frozenset({"medical_email"})


async def _clean_contact_email(db: AsyncSession, auth: AuthContext, org_id: str,
                               value: str | None) -> str | None:
    """Validate an email and scan it under the strictest-union policy.

    The raw value is scanned before it is lowercased so case-sensitive
    secret patterns still match. BLOCK refuses 422 ``dlp_blocked``; in other
    modes a finding is redacted like any other field, and a redacted value is
    no longer an address, so it is refused 422 ``invalid_email`` rather than
    stored."""
    from sessionfs.server.routes.meetings import _dlp_scan_content, _upload_dlp_policy

    if value is None or not value.strip():
        return None
    raw = value.strip()
    if len(raw) > svc.EMAIL_MAX:
        raise _input_error(svc.ContactInputError("invalid_email", "email is not a valid address"))
    policy = await _upload_dlp_policy(db, auth, org_id)
    scanned, redactions = _dlp_scan_content(raw, policy, exclude_patterns=_EMAIL_DETECTORS)
    if redactions:
        raise HTTPException(422, {"error": "invalid_email",
                                  "message": "email contains content the DLP policy redacts"})
    try:
        return svc.clean_email(scanned)
    except svc.ContactInputError as exc:
        raise _input_error(exc) from None


async def _clean_name(db: AsyncSession, auth: AuthContext, org_id: str, value: str,
                      field: str) -> tuple[str, str]:
    try:
        cleaned = svc.clean_text(value, field=field, max_len=svc.NAME_MAX, required=True)
    except svc.ContactInputError as exc:
        raise _input_error(exc) from None
    assert cleaned is not None
    scanned = (await _dlp(db, auth, org_id, [cleaned]))[0] or ""
    scanned = scanned[: svc.NAME_MAX]
    key = svc.norm_name(scanned)[: svc.NAME_MAX]
    if not key:
        raise HTTPException(422, {"error": "invalid_contact", "message": f"{field} must not be blank"})
    return scanned, key


async def _email_owner(db: AsyncSession, org_id: str, email: str,
                       exclude_id: str | None = None) -> tuple[ExternalContact | None, User | None]:
    """The active/archived contact already holding ``email`` in the org, and
    the active member with that email (if any)."""
    stmt = select(ExternalContact).where(
        ExternalContact.org_id == org_id,
        ExternalContact.email == email,
        ExternalContact.status != "merged",
    )
    if exclude_id:
        stmt = stmt.where(ExternalContact.id != exclude_id)
    contact = (await db.execute(stmt.limit(1))).scalars().first()
    from sessionfs.server.db.models import OrgMember

    member = (
        await db.execute(
            select(User)
            .join(OrgMember, OrgMember.user_id == User.id)
            .where(
                OrgMember.org_id == org_id,
                func.lower(User.email) == email,
                User.is_active.is_(True),
            )
            .limit(1)
        )
    ).scalars().first()
    return contact, member


def _audit(org: Organization | None, *, org_id: str, event_type: str, auth: AuthContext,
           role: str, contact_id: str, before: dict | None = None,
           after: dict | None = None) -> OrgAuditEvent:
    import uuid

    return OrgAuditEvent(
        id=f"oae_{uuid.uuid4().hex[:24]}",
        org_id=org_id,
        org_name_snapshot=org.name if org is not None else "",
        event_type=event_type,
        actor_user_id=auth.user.id,
        actor_email_snapshot=auth.user.email if auth.key_kind == "user" else None,
        actor_role_at_time=role,
        target_type="external_contact",
        target_id=contact_id,
        before=json.dumps(before) if before is not None else None,
        after=json.dumps(after) if after is not None else None,
    )


async def _readable_scopes(db: AsyncSession, auth: AuthContext, org_id: str
                           ) -> tuple[set[str], set[str]]:
    """Projects and groups in ``org_id`` whose meetings/tickets the caller can
    read (a service key: its minter's, inside the key's org and allowlist)."""
    from sessionfs.server.helper.tools import meeting_scope_ids

    scope = SimpleNamespace(project_id=None, group_id=None, session_ids=None)
    project_ids, group_ids = await meeting_scope_ids(db, auth.user.id, scope)
    if project_ids:
        project_ids = set(
            (
                await db.execute(
                    select(Project.id).where(
                        Project.id.in_(sorted(project_ids)), Project.org_id == org_id
                    )
                )
            ).scalars().all()
        )
    if auth.key_kind == "service" and auth.project_ids is not None:
        project_ids &= set(auth.project_ids)
    if group_ids:
        group_ids = set(
            (
                await db.execute(
                    select(ProjectGroup.id).where(
                        ProjectGroup.id.in_(sorted(group_ids)), ProjectGroup.org_id == org_id
                    )
                )
            ).scalars().all()
        )
    if auth.key_kind == "service" and auth.project_ids is not None:
        group_ids = set()
    return project_ids, group_ids


def _csv_cell(value: Any) -> str:
    from sessionfs.server.routes.approvals import csv_safe

    return csv_safe(value)


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.isoformat()


def _parse_before(before: str | None) -> tuple[datetime, str] | None:
    if not before:
        return None
    ts, sep, cid = before.rpartition("|")
    if not sep or not svc.valid_contact_id(cid):
        raise HTTPException(422, {"error": "invalid_cursor", "message": "before must be a next_before value"})
    try:
        when = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        raise HTTPException(422, {"error": "invalid_cursor", "message": "before must be a next_before value"}) from None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return when, cid


def _escape_like(q: str) -> str:
    return q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


# ── Directory ──


@router.get("/{org_id}/contacts", response_model=ContactListResponse)
async def list_contacts(
    org_id: str,
    q: str | None = Query(default=None, max_length=120),
    status: Literal["active", "archived", "all"] = Query(default="active"),
    tag: str | None = Query(default=None, max_length=svc.TAG_MAX),
    before: str | None = Query(default=None, max_length=200),
    limit: int = Query(default=_LIST_LIMIT_DEFAULT, ge=1, le=_LIST_LIMIT_MAX),
    format: Literal["json", "csv"] = Query(default="json"),
    auth: AuthContext = Depends(require_scope("contacts:read")),
    db: AsyncSession = Depends(get_db),
):
    """The org's contacts, newest first. ``format=csv`` (owners/admins with a
    user key) exports up to 10,000 rows with formula-like cells defused."""
    role = await _standing(db, auth, org_id)
    if format == "csv":
        if auth.key_kind == "service":
            raise HTTPException(403, {"error": "service_key_not_allowed",
                                      "message": "Contact export needs a user key."})
        _require_admin(role)
    stmt = select(ExternalContact).where(
        ExternalContact.org_id == org_id, ExternalContact.status != "merged"
    )
    if status != "all":
        stmt = stmt.where(ExternalContact.status == status)
    if tag:
        stmt = stmt.where(ExternalContact.tags.like(f'%"{_escape_like(tag.strip().lower())}"%', escape="\\"))
    if q and q.strip():
        needle = f"%{_escape_like(svc.norm_name(q))}%"
        alias_ids = select(ExternalContactAlias.contact_id).where(
            ExternalContactAlias.org_id == org_id,
            ExternalContactAlias.alias_norm.like(needle, escape="\\"),
        )
        conds = [
            ExternalContact.name_norm.like(needle, escape="\\"),
            ExternalContact.id.in_(alias_ids),
        ]
        if role in ("owner", "admin"):
            conds.append(ExternalContact.email.like(f"%{_escape_like(q.strip().lower())}%", escape="\\"))
        stmt = stmt.where(or_(*conds))
    stmt = stmt.order_by(ExternalContact.created_at.desc(), ExternalContact.id.desc())

    def _page(s: Any, cur: tuple[datetime, str] | None) -> Any:
        if cur is None:
            return s
        ts, cid = cur
        return s.where(
            (ExternalContact.created_at < ts)
            | ((ExternalContact.created_at == ts) & (ExternalContact.id < cid))
        )

    cursor = _parse_before(before)
    if format == "csv":
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(_CSV_COLUMNS)
        written = 0
        cur = cursor
        while written < _CSV_MAX_ROWS:
            rows = (await db.execute(_page(stmt, cur).limit(_LIST_LIMIT_MAX))).scalars().all()
            if not rows:
                break
            aliases = await _aliases(db, [c.id for c in rows])
            for c in rows:
                writer.writerow([
                    _csv_cell(v) for v in (
                        c.id, c.display_name, c.email, c.role, ";".join(svc.load_tags(c.tags)),
                        ";".join(aliases.get(c.id, [])), c.status, c.linked_user_id,
                        _iso(c.created_at), _iso(c.updated_at),
                    )
                ])
                written += 1
                if written >= _CSV_MAX_ROWS:
                    break
            last = rows[-1]
            cur = (last.created_at if last.created_at.tzinfo else last.created_at.replace(tzinfo=timezone.utc), last.id)
            if len(rows) < _LIST_LIMIT_MAX:
                break
        headers = {
            "Content-Disposition": f'attachment; filename="contacts-{org_id}.csv"',
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        }
        if written >= _CSV_MAX_ROWS:
            headers["X-Truncated"] = "true"
        return Response(buf.getvalue(), media_type="text/csv; charset=utf-8", headers=headers)

    rows = (await db.execute(_page(stmt, cursor).limit(limit + 1))).scalars().all()
    more = len(rows) > limit
    rows = rows[:limit]
    aliases = await _aliases(db, [c.id for c in rows])
    items = [_response(c, aliases.get(c.id, []), role=role, auth=auth) for c in rows]
    next_before = f"{_iso(rows[-1].created_at)}|{rows[-1].id}" if more and rows else None
    return ContactListResponse(items=items, next_before=next_before)


@router.post("/{org_id}/contacts", response_model=ContactResponse, status_code=201)
async def create_contact(
    org_id: str,
    body: ContactCreate,
    auth: AuthContext = Depends(require_scope("contacts:write")),
    db: AsyncSession = Depends(get_db),
):
    role = await _standing(db, auth, org_id)
    display_name, name_norm = await _clean_name(db, auth, org_id, body.display_name, "display_name")
    try:
        email = svc.clean_email(body.email)
    except svc.ContactInputError as exc:
        raise _input_error(exc) from None
    email = await _clean_contact_email(db, auth, org_id, body.email)
    try:
        role_text = svc.clean_text(body.role, field="role", max_len=svc.ROLE_MAX, required=False)
        tags = svc.clean_tags(body.tags)
    except svc.ContactInputError as exc:
        raise _input_error(exc) from None
    role_text = (await _dlp(db, auth, org_id, [role_text]))[0]
    tags = [t for t in (await _dlp(db, auth, org_id, list(tags))) if t]  # type: ignore[misc]
    aliases: list[tuple[str, str]] = []
    for raw in body.aliases:
        alias, alias_norm = await _clean_name(db, auth, org_id, raw, "alias")
        if alias_norm != name_norm and alias_norm not in {a[1] for a in aliases}:
            aliases.append((alias, alias_norm))

    if email:
        existing, member = await _email_owner(db, org_id, email)
        if existing is not None or member is not None:
            raise _email_conflict(existing, role=role, auth=auth)

    duplicates: dict[str, ExternalContact] = {}
    for key in [name_norm, *[a[1] for a in aliases]]:
        for match in await svc.active_contacts_by_norm(db, org_id, key, limit=5):
            duplicates.setdefault(match.id, match)

    now = datetime.now(timezone.utc)
    contact = ExternalContact(
        id=svc.new_contact_id(),
        org_id=org_id,
        display_name=display_name,
        name_norm=name_norm,
        email=email,
        role=role_text,
        tags=json.dumps(tags),
        status="active",
        created_by_user_id=auth.user.id,
        actor_type=auth.actor_type,
        service_key_id=auth.service_key_id,
        service_key_name=auth.service_key_name,
        created_at=now,
        updated_at=now,
    )
    db.add(contact)
    # Aliases have no relationship(): flush the parent first (FK order).
    try:
        await db.flush()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(409, {"error": "contact_exists",
                                  "message": "Another contact already has this email."}) from None
    for alias, alias_norm in aliases:
        db.add(ExternalContactAlias(contact_id=contact.id, alias=alias, alias_norm=alias_norm,
                                    org_id=org_id, created_at=now))
    org = await db.get(Organization, org_id)
    db.add(_audit(org, org_id=org_id, event_type="contact_created", auth=auth,
                  role=role, contact_id=contact.id))
    await db.commit()
    await db.refresh(contact)
    out = await _one(db, contact, role=role, auth=auth)
    out.possible_duplicates = [
        DuplicateHint(id=c.id, display_name=c.display_name) for c in duplicates.values()
    ]
    return out


@router.get("/{org_id}/contacts/suggestions", response_model=SuggestionsResponse)
async def contact_suggestions(
    org_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """"People to confirm": unresolved names in the org's meetings and chats
    the caller can read (last 90 days), grouped by normalised name. Nothing
    is stored; a person decides."""
    auth = AuthContext(user=user, api_key_id="", key_kind="user")
    await _standing(db, auth, org_id)
    project_ids, group_ids = await _readable_scopes(db, auth, org_id)
    conds = []
    if project_ids:
        conds.append(MeetingTranscript.project_id.in_(sorted(project_ids)))
    if group_ids:
        conds.append(MeetingTranscript.group_id.in_(sorted(group_ids)))
    if not conds:
        return SuggestionsResponse(groups=[])
    since = datetime.now(timezone.utc) - timedelta(days=_SUGGESTION_DAYS)
    rows = (
        await db.execute(
            select(MeetingParticipant.name_raw, MeetingTranscript)
            .join(MeetingTranscript, MeetingTranscript.id == MeetingParticipant.transcript_id)
            .where(
                MeetingParticipant.resolution_method == "unresolved",
                MeetingParticipant.contact_id.is_(None),
                MeetingTranscript.deleted_at.is_(None),
                MeetingTranscript.created_at >= since,
                or_(*conds),
            )
            .order_by(MeetingTranscript.created_at.desc(), MeetingParticipant.id.asc())
            .limit(_SUGGESTION_ROWS + 1)
        )
    ).all()
    truncated = len(rows) > _SUGGESTION_ROWS
    rows = rows[:_SUGGESTION_ROWS]
    hidden = set(
        (
            await db.execute(
                select(AttentionHide.item_key).where(
                    AttentionHide.user_id == user.id,
                    AttentionHide.item_key.like("contact_suggestion:%"),
                )
            )
        ).scalars().all()
    )
    groups: dict[str, SuggestionGroup] = {}
    seen_sources: dict[str, set[str]] = {}
    for name_raw, meeting in rows:
        key = svc.norm_name(name_raw)
        if not key or _hide_key(org_id, key) in hidden:
            continue
        group = groups.get(key)
        if group is None:
            if len(groups) >= _SUGGESTION_GROUPS:
                truncated = True
                continue
            group = SuggestionGroup(name=name_raw, name_norm=key, mention_count=0,
                                    latest_mention_at=meeting.created_at)
            groups[key] = group
            seen_sources[key] = set()
        group.mention_count += 1
        if meeting.id not in seen_sources[key] and len(group.sources) < _SUGGESTION_SOURCES:
            seen_sources[key].add(meeting.id)
            group.sources.append(SuggestionSource(meeting_id=meeting.id,
                                                  kind=meeting.kind or "meeting",
                                                  title=meeting.title))
    if groups:
        active = (
            await db.execute(
                select(ExternalContact).where(
                    ExternalContact.org_id == org_id, ExternalContact.status == "active"
                ).limit(2000)
            )
        ).scalars().all()
        for key, group in groups.items():
            group.candidates = _candidates(key, active)
    return SuggestionsResponse(groups=list(groups.values()), truncated=truncated)


def _candidates(key: str, contacts: list[ExternalContact]) -> list[SuggestionCandidate]:
    """Deterministic hints only (they never resolve on their own): an exact
    name match, a first-token match, or a prefix + initial match."""
    tokens = key.split()
    out: list[SuggestionCandidate] = []
    for c in contacts:
        ctoks = c.name_norm.split()
        hint: str | None = None
        if c.name_norm == key:
            hint = "same name"
        elif tokens and ctoks and ctoks[0] == tokens[0]:
            hint = "same first name"
        elif tokens and ctoks and ctoks[0].startswith(tokens[0]) and len(tokens[0]) >= 3:
            hint = "name starts the same"
        if hint:
            out.append(SuggestionCandidate(id=c.id, display_name=c.display_name, hint=hint))
        if len(out) >= 5:
            break
    return out


def _hide_key(org_id: str, key: str) -> str:
    return "contact_suggestion:" + hashlib.sha256(f"{org_id}\x00{key}".encode()).hexdigest()


@router.post("/{org_id}/contacts/suggestions/apply", response_model=SuggestionApplyResponse)
async def apply_suggestion(
    org_id: str,
    body: SuggestionApply,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Resolve every unresolved participant with this name (in sources the
    caller can edit) to the contact. Others stay unresolved."""
    from sessionfs.server.routes.meetings import _meeting_scope

    auth = AuthContext(user=user, api_key_id="", key_kind="user")
    await _standing(db, auth, org_id)
    contact, _ = await _load(db, org_id, body.contact_id)
    if contact.status != "active":
        raise HTTPException(422, {"error": "participant_not_eligible",
                                  "message": "That contact can't be used."})
    key = svc.norm_name(body.name)
    project_ids, group_ids = await _readable_scopes(db, auth, org_id)
    conds = []
    if project_ids:
        conds.append(MeetingTranscript.project_id.in_(sorted(project_ids)))
    if group_ids:
        conds.append(MeetingTranscript.group_id.in_(sorted(group_ids)))
    if not conds or not key:
        return SuggestionApplyResponse(applied=0, skipped=0)
    since = datetime.now(timezone.utc) - timedelta(days=_SUGGESTION_DAYS)
    rows = (
        await db.execute(
            select(MeetingParticipant, MeetingTranscript)
            .join(MeetingTranscript, MeetingTranscript.id == MeetingParticipant.transcript_id)
            .where(
                MeetingParticipant.resolution_method == "unresolved",
                MeetingParticipant.contact_id.is_(None),
                MeetingTranscript.deleted_at.is_(None),
                MeetingTranscript.created_at >= since,
                or_(*conds),
            )
            .limit(_SUGGESTION_ROWS)
        )
    ).all()
    editable: dict[str, bool] = {}
    applied = skipped = 0
    for participant, meeting in rows:
        if svc.norm_name(participant.name_raw) != key:
            continue
        if meeting.id not in editable:
            try:
                await _meeting_scope(db, meeting, auth, write=True)
                editable[meeting.id] = True
            except HTTPException:
                editable[meeting.id] = False
        if not editable[meeting.id]:
            skipped += 1
            continue
        result = await db.execute(
            update(MeetingParticipant)
            .where(
                MeetingParticipant.id == participant.id,
                MeetingParticipant.resolution_method == "unresolved",
            )
            .values(contact_id=contact.id, resolution_method="manual",
                    resolved_by_user_id=user.id)
            .execution_options(synchronize_session=False)
        )
        applied += result.rowcount or 0
    await db.commit()
    return SuggestionApplyResponse(applied=applied, skipped=skipped)


@router.post("/{org_id}/contacts/suggestions/dismiss", status_code=204)
async def dismiss_suggestion(
    org_id: str,
    body: SuggestionDismiss,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Hide one name from this user's queue (a per-user attention hide)."""
    auth = AuthContext(user=user, api_key_id="", key_kind="user")
    await _standing(db, auth, org_id)
    item_key = _hide_key(org_id, svc.norm_name(body.name))
    existing = await db.get(AttentionHide, (user.id, item_key))
    if existing is None:
        db.add(AttentionHide(user_id=user.id, item_key=item_key, version_key="1"))
        await db.commit()
    return Response(status_code=204)


@router.get("/{org_id}/contacts/{contact_id}", response_model=ContactResponse)
async def get_contact(
    org_id: str,
    contact_id: str,
    auth: AuthContext = Depends(require_scope("contacts:read")),
    db: AsyncSession = Depends(get_db),
):
    """One contact; a merged id returns its target with ``redirected_from``."""
    role = await _standing(db, auth, org_id)
    contact, redirected = await _load(db, org_id, contact_id, follow_merge=True)
    return await _one(db, contact, role=role, auth=auth, redirected_from=redirected)


@router.get("/{org_id}/contacts/{contact_id}/references")
async def contact_references(
    org_id: str,
    contact_id: str,
    auth: AuthContext = Depends(require_scope("contacts:read")),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Meetings, chats, action items and tickets naming the contact — only
    those the caller can read, each filtered by its own source predicate."""
    await _standing(db, auth, org_id)
    contact, _ = await _load(db, org_id, contact_id, follow_merge=True)
    project_ids, group_ids = await _readable_scopes(db, auth, org_id)
    refs = await svc.reference_rows(db, contact.id, project_ids=project_ids, group_ids=group_ids)
    if auth.key_kind == "service":
        # contacts:read alone never reaches meeting or ticket content: each
        # section needs its own source's read scope (as For You does).
        scopes = set(auth.scopes or [])
        if "*" not in scopes:
            if "meetings:read" not in scopes:
                refs["conversations"] = []
                refs["action_items"] = []
            if "tickets:read" not in scopes:
                refs["tickets"] = []
    return {"contact_id": contact.id, **refs}


@router.patch("/{org_id}/contacts/{contact_id}", response_model=ContactResponse)
async def update_contact(
    org_id: str,
    contact_id: str,
    body: ContactUpdate,
    auth: AuthContext = Depends(require_scope("contacts:write")),
    db: AsyncSession = Depends(get_db),
):
    """Edit name, role, tags, email or status (active ↔ archived). An explicit
    null clears role or email. An owner/admin setting an email that is a
    member's verified address links the contact to that member."""
    role = await _standing(db, auth, org_id)
    contact, _ = await _load(db, org_id, contact_id, lock=True)
    fields = body.model_fields_set
    before = {"status": contact.status, "has_email": bool(contact.email)}
    link_member: User | None = None

    if "display_name" in fields and body.display_name is not None:
        name, key = await _clean_name(db, auth, org_id, body.display_name, "display_name")
        contact.display_name, contact.name_norm = name, key
    if "role" in fields:
        try:
            text = svc.clean_text(body.role, field="role", max_len=svc.ROLE_MAX, required=False)
        except svc.ContactInputError as exc:
            raise _input_error(exc) from None
        contact.role = (await _dlp(db, auth, org_id, [text]))[0]
    if "tags" in fields:
        try:
            tags = svc.clean_tags(body.tags or [])
        except svc.ContactInputError as exc:
            raise _input_error(exc) from None
        tags = [t for t in (await _dlp(db, auth, org_id, list(tags))) if t]  # type: ignore[misc]
        contact.tags = json.dumps(tags)
    if "email" in fields:
        try:
            email = svc.clean_email(body.email)
        except svc.ContactInputError as exc:
            raise _input_error(exc) from None
        email = await _clean_contact_email(db, auth, org_id, body.email)
        if email and email != contact.email:
            existing, member = await _email_owner(db, org_id, email, exclude_id=contact.id)
            if existing is not None:
                raise _email_conflict(existing, role=role, auth=auth)
            if member is not None:
                if role in ("owner", "admin") and member.email_verified and contact.linked_user_id is None:
                    link_member = member
                else:
                    raise _email_conflict(None, role=role, auth=auth)
        contact.email = email
    if "status" in fields and body.status is not None and body.status != contact.status:
        result = await db.execute(
            update(ExternalContact)
            .where(ExternalContact.id == contact.id, ExternalContact.status == contact.status)
            .values(status=body.status)
            .execution_options(synchronize_session=False)
        )
        if result.rowcount != 1:
            await db.rollback()
            raise HTTPException(409, {"error": "contact_changed",
                                      "message": "The contact changed; reload and try again."})
        contact.status = body.status
    contact.updated_at = datetime.now(timezone.utc)
    try:
        await db.flush()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(409, {"error": "contact_exists",
                                  "message": "Another contact already has this email."}) from None
    if link_member is not None:
        await svc.link_contacts_for_user(db, link_member, org_id=org_id)
    org = await db.get(Organization, org_id)
    db.add(_audit(org, org_id=org_id, event_type="contact_updated", auth=auth, role=role,
                  contact_id=contact.id, before=before,
                  after={"status": contact.status, "has_email": bool(contact.email),
                         "fields": sorted(fields)}))
    await db.commit()
    await db.refresh(contact)
    return await _one(db, contact, role=role, auth=auth)


@router.post("/{org_id}/contacts/{contact_id}/aliases", response_model=ContactResponse)
async def add_alias(
    org_id: str,
    contact_id: str,
    body: AliasCreate,
    auth: AuthContext = Depends(require_scope("contacts:write")),
    db: AsyncSession = Depends(get_db),
):
    role = await _standing(db, auth, org_id)
    contact, _ = await _load(db, org_id, contact_id, lock=True)
    alias, key = await _clean_name(db, auth, org_id, body.alias, "alias")
    if key != contact.name_norm and await db.get(ExternalContactAlias, (contact.id, key)) is None:
        count = (
            await db.execute(
                select(func.count()).select_from(ExternalContactAlias).where(
                    ExternalContactAlias.contact_id == contact.id
                )
            )
        ).scalar_one()
        if count >= svc.ALIASES_MAX:
            raise HTTPException(422, {"error": "too_many_aliases",
                                      "message": f"A contact has at most {svc.ALIASES_MAX} aliases."})
        db.add(ExternalContactAlias(contact_id=contact.id, alias=alias, alias_norm=key,
                                    org_id=org_id, created_at=datetime.now(timezone.utc)))
        contact.updated_at = datetime.now(timezone.utc)
        await db.commit()
        await db.refresh(contact)
    return await _one(db, contact, role=role, auth=auth)


@router.delete("/{org_id}/contacts/{contact_id}/aliases", response_model=ContactResponse)
async def remove_alias(
    org_id: str,
    contact_id: str,
    alias: str = Query(..., min_length=1, max_length=200),
    auth: AuthContext = Depends(require_scope("contacts:write")),
    db: AsyncSession = Depends(get_db),
):
    role = await _standing(db, auth, org_id)
    contact, _ = await _load(db, org_id, contact_id, lock=True)
    row = await db.get(ExternalContactAlias, (contact.id, svc.norm_name(alias)))
    if row is not None:
        await db.delete(row)
        contact.updated_at = datetime.now(timezone.utc)
        await db.commit()
        await db.refresh(contact)
    return await _one(db, contact, role=role, auth=auth)


# ── Admin: merge, delete, export ──


@router.post("/{org_id}/contacts/{contact_id}/merge-from/{source_id}", response_model=MergeResponse)
async def merge_contacts(
    org_id: str,
    contact_id: str,
    source_id: str,
    confirm: bool = Query(default=False),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Merge ``source`` into ``contact``: repoint participants and tickets,
    union aliases (the source's name becomes one), fill empty fields. A
    preview unless ``confirm=true``."""
    auth = AuthContext(user=user, api_key_id="", key_kind="user")
    role = await _standing(db, auth, org_id)
    _require_admin(role)
    if contact_id == source_id:
        raise HTTPException(422, {"error": "invalid_merge", "message": "A contact can't merge with itself."})
    # Lock both rows in id order, then re-check both are active and in-org.
    for cid in sorted((contact_id, source_id)):
        await _load(db, org_id, cid, lock=True)
    target, _ = await _load(db, org_id, contact_id)
    source, _ = await _load(db, org_id, source_id)
    if target.status != "active" or source.status != "active":
        raise HTTPException(409, {"error": "contact_changed",
                                  "message": "Both contacts must be active to merge."})
    if target.email and source.email and target.email != source.email:
        raise HTTPException(409, {"error": "email_conflict",
                                  "message": "The two contacts have different emails."})
    existing = set((await _aliases(db, [target.id]))[target.id])
    existing_norms = {svc.norm_name(a) for a in existing} | {target.name_norm}
    source_aliases = (await _aliases(db, [source.id]))[source.id]
    new_aliases: list[str] = []
    for name in [source.display_name, *source_aliases]:
        key = svc.norm_name(name)
        if key and key not in existing_norms:
            existing_norms.add(key)
            new_aliases.append(name)
    participants = (
        await db.execute(
            select(func.count()).select_from(MeetingParticipant).where(
                MeetingParticipant.contact_id == source.id
            )
        )
    ).scalar_one()
    # Both contact rows are locked; now the tickets of BOTH contacts, in one
    # id-ordered read (contacts by id, then tickets by id).
    locked = await svc.lock_contact_tickets(
        db, contact_ids=sorted((source.id, target.id))
    )
    tickets = [t for t in locked if t.assigned_contact_id == source.id]
    target_tickets = [t for t in locked if t.assigned_contact_id == target.id]
    room = svc.ALIASES_MAX - len(existing)
    new_aliases = new_aliases[: max(room, 0)]
    if not confirm:
        preview = await _one(db, target, role=role, auth=auth)
        preview_source_id = source.id
        await db.rollback()
        return MergeResponse(confirmed=False, target=preview, source_id=preview_source_id,
                             participants_moved=participants, tickets_moved=len(tickets),
                             aliases_added=new_aliases)

    before = {"target": target.id, "source": source.id}
    await db.execute(
        update(MeetingParticipant)
        .where(MeetingParticipant.contact_id == source.id)
        .values(contact_id=target.id)
        .execution_options(synchronize_session=False)
    )
    # The merged contact keeps (or inherits) a link. When that link is an
    # active member, a linked contact is never an assignee: open tickets in
    # projects the member can read move to them, as on link-on-signup; the
    # rest repoint to the target contact.
    link_user_id = target.linked_user_id or source.linked_user_id
    link_member: User | None = None
    if link_user_id and await svc.org_role(db, org_id, link_user_id) is not None:
        link_member = await db.get(User, link_user_id)
    remaining = list(tickets)
    if link_member is not None:
        remaining = await svc.move_tickets_to_user(
            db, remaining, contact_id=source.id, user=link_member,
            actor_type="user", actor_user_id=user.id,
        )
        # The target's own open tickets move too: once linked, the target
        # contact is never an assignee. The rest stay on the target.
        await svc.move_tickets_to_user(
            db, target_tickets, contact_id=target.id, user=link_member,
            actor_type="user", actor_user_id=user.id,
        )
    for ticket in remaining:
        if not await svc.reassign_contact_ticket(
            db, ticket, from_contact_id=source.id, assigned_contact_id=target.id
        ):
            continue
        await record_ticket_event(
            db, ticket, "assigned", field="assigned_contact_id",
            before={"contact_id": source.id}, after={"contact_id": target.id},
            actor_type="user", actor_user_id=user.id,
        )
    now = datetime.now(timezone.utc)
    for name in new_aliases:
        key = svc.norm_name(name)[: svc.NAME_MAX]
        db.add(ExternalContactAlias(contact_id=target.id, alias=name[: svc.NAME_MAX],
                                    alias_norm=key, org_id=org_id, created_at=now))
    from sqlalchemy import delete as sa_delete

    await db.execute(sa_delete(ExternalContactAlias).where(ExternalContactAlias.contact_id == source.id))
    source_email = source.email
    if not target.role and source.role:
        target.role = source.role
    target_tags = svc.load_tags(target.tags)
    if not target_tags and svc.load_tags(source.tags):
        target.tags = source.tags
    target.updated_at = now
    result = await db.execute(
        update(ExternalContact)
        .where(ExternalContact.id == source.id, ExternalContact.status == "active")
        .values(status="merged", merged_into_id=target.id, updated_at=now)
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:
        await db.rollback()
        raise HTTPException(409, {"error": "contact_changed",
                                  "message": "The contact changed; reload and try again."})
    # The source is merged now, so its email no longer holds the unique slot.
    if not target.email and source_email:
        target.email = source_email
    if target.linked_user_id is None and source.linked_user_id is not None:
        target.linked_user_id = source.linked_user_id
        target.linked_at = source.linked_at
    org = await db.get(Organization, org_id)
    db.add(_audit(org, org_id=org_id, event_type="contact_merged", auth=auth, role=role,
                  contact_id=target.id, before=before,
                  after={"merged_into": target.id, "participants_moved": participants,
                         "tickets_moved": len(tickets), "aliases_added": len(new_aliases)}))
    await db.commit()
    await db.refresh(target)
    return MergeResponse(confirmed=True, target=await _one(db, target, role=role, auth=auth),
                         source_id=source.id, participants_moved=participants,
                         tickets_moved=len(tickets), aliases_added=new_aliases)


@router.delete("/{org_id}/contacts/{contact_id}", response_model=DeleteResponse)
async def delete_contact(
    org_id: str,
    contact_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Erase a contact (owners/admins): the row and aliases go, participants
    fall back to unresolved, tickets become unassigned with an event that
    never names the contact."""
    auth = AuthContext(user=user, api_key_id="", key_kind="user")
    role = await _standing(db, auth, org_id)
    _require_admin(role)
    contact, _ = await _load(db, org_id, contact_id, lock=True)
    try:
        result = await svc.erase_contact(db, contact, actor_user_id=user.id)
    except svc.ContactChanged:
        await db.rollback()
        raise HTTPException(409, {"error": "contact_changed",
                                  "message": "The contact changed; reload and try again."}) from None
    org = await db.get(Organization, org_id)
    db.add(_audit(org, org_id=org_id, event_type="contact_deleted", auth=auth, role=role,
                  contact_id=contact_id,
                  after={"participants_reset": result.participants_reset,
                         "tickets_unassigned": result.tickets_unassigned}))
    await db.commit()
    return DeleteResponse(deleted=True, contact_id=contact_id,
                          participants_reset=result.participants_reset,
                          tickets_unassigned=result.tickets_unassigned)


@router.get("/{org_id}/contacts/{contact_id}/export")
async def export_contact(
    org_id: str,
    contact_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Response:
    """Everything stored about one contact, for a data request (owners and
    admins): the row with its full email, aliases, and every reference as ids
    and kinds."""
    auth = AuthContext(user=user, api_key_id="", key_kind="user")
    role = await _standing(db, auth, org_id)
    _require_admin(role)
    contact, _ = await _load(db, org_id, contact_id, follow_merge=True)
    refs = await svc.reference_rows(db, contact.id, project_ids=None, group_ids=None, limit=10_000)
    merged_from = list(
        (
            await db.execute(
                select(ExternalContact.id).where(ExternalContact.merged_into_id == contact.id)
            )
        ).scalars().all()
    )
    payload = {
        "contact": {
            "id": contact.id,
            "org_id": contact.org_id,
            "display_name": contact.display_name,
            "email": contact.email,
            "role": contact.role,
            "tags": svc.load_tags(contact.tags),
            "status": contact.status,
            "linked_user_id": contact.linked_user_id,
            "linked_at": _iso(contact.linked_at),
            "created_by_user_id": contact.created_by_user_id,
            "created_at": _iso(contact.created_at),
            "updated_at": _iso(contact.updated_at),
            "merged_from": merged_from,
        },
        "aliases": (await _aliases(db, [contact.id]))[contact.id],
        "references": {
            "conversations": [
                {"kind": r["kind"], "meeting_id": r["meeting_id"], "participant_id": r["participant_id"]}
                for r in refs["conversations"]
            ],
            "action_items": [
                {"kind": r["kind"], "item_id": r["item_id"], "meeting_id": r["meeting_id"]}
                for r in refs["action_items"]
            ],
            "tickets": [{"id": r["id"], "project_id": r["project_id"]} for r in refs["tickets"]],
        },
        "exported_at": _iso(datetime.now(timezone.utc)),
    }
    return Response(
        json.dumps(payload),
        media_type="application/json",
        headers={
            "Content-Disposition": f'attachment; filename="contact-{contact.id}.json"',
            "Cache-Control": "no-store",
        },
    )
