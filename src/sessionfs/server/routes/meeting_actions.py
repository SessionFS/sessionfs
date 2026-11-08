"""Meeting action-item extraction and promote (GitHub #116, M1b §5, §7).

REST surface for the things a stored transcript can do after upload: run
(or re-run) extraction, promote an extracted action item to a ticket
(through a confirmable preview), dismiss/restore, and (0.29.11) tie an item
to an EXISTING ticket: link (preview + confirm), unlink, and dismiss the
extraction's suggestion. Suggest, never auto-link: only these routes, called
by a person or their key, link an item.

Access follows §3: every meeting-level miss is the same ``"Meeting not found"``
404 (the shared ``_get_meeting`` / ``_meeting_scope`` helpers keep existence and
visibility indistinguishable), and writes reuse the ``meetings:write`` scope so
project meetings admit service keys while group meetings stay user-key only.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timezone
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server.auth.dependencies import (
    AuthContext,
    get_db,
    require_scope,
)
from sessionfs.server.db.models import (
    ConversationMessageMeta,
    MeetingActionItem,
    MeetingParticipant,
    MeetingTranscript,
    Organization,
    Project,
    ProjectGroup,
    ProjectGroupAccess,
    Reminder,
)
from sessionfs.server.routes.meetings import (
    ItemTicketRef,
    MeetingActionItemResponse,
    MeetingDetailResponse,
    RowKind,
    _action_item_response,
    _dlp_scan_content,
    _get_meeting,
    _meeting_audit_event,
    _meeting_detail,
    _meeting_scope,
    _meeting_scope_org_id,
    _ticket_ref,
    _upload_dlp_policy,
    _user_org_role,
    item_tickets,
)
from sessionfs.server.integrations.trackers.promote import ContentTooLarge
from sessionfs.server.routes.tickets import TicketCreate, authorize_ticket_comment
from sessionfs.server.db.models import Ticket
from sessionfs.server.services.ticket_events import record_ticket_event
from sessionfs.server.services.ticket_match import visible_tickets
from sessionfs.server.services.ticket_writes import insert_ticket_comment
from sessionfs.server.services.helper_ticket_actions import (
    create_ticket_action,
    display_name_for,
    jira_publish_preflight,
    publish_ticket_to_jira,
)
from sessionfs.server.services.ticket_writes import check_assignee_conflict
from sessionfs.server.services.meeting_extraction import run_meeting_extraction
from sessionfs.server.tier_gate import build_user_context

router = APIRouter(prefix="/api/v1", tags=["meetings"])


class ExtractionResponse(MeetingDetailResponse):
    dropped_items: int


class ProjectOption(BaseModel):
    id: str
    name: str


class PromotePreviewResponse(BaseModel):
    meeting_id: str
    item_id: str
    title: str
    description: str
    priority: str
    kind: str
    assigned_to: str | None
    assigned_user_id: str | None
    # External contact owner (GitHub #125) when the owner has no user/persona.
    assigned_contact_id: str | None = None
    due_date: date | None
    context_refs: list[str]
    target_project_id: str | None
    target_project_options: list[ProjectOption] | None = None
    # True only when publishing this ticket to Jira would pass every
    # database-side gate (plan, caller standing, connection, mapping).
    jira_publish_available: bool = False
    # Chat threads: "matched by exporter email" when the owner was matched to
    # a member only through the email the chat export recorded for them.
    owner_match_note: str | None = None


class PromoteRequest(BaseModel):
    title: str = Field(..., min_length=1, max_length=200)
    description: str = Field(..., min_length=1)
    priority: Literal["critical", "high", "medium", "low"] = "medium"
    kind: Literal["task", "issue"] = "task"
    assigned_to: str | None = None
    assigned_user_id: str | None = Field(None, max_length=320)
    assigned_contact_id: str | None = Field(default=None, max_length=64)
    due_date: date | None = None
    target_project_id: str | None = None
    publish_to_jira: bool = False


class PromoteResponse(BaseModel):
    ticket_id: str
    item_id: str
    status: Literal["promoted"]
    published_to_jira: bool = False
    warnings: list[str] = []


# ── Draft building (§7) ──


def _first_sentence(text: str) -> str:
    """The first sentence of ``text``, capped at the promote title limit."""
    text = text.strip()
    match = re.search(r"[.!?](\s|$)", text)
    if match:
        text = text[: match.start() + 1]
    return text[:120].strip()


def _promote_description(meeting: MeetingTranscript, item: MeetingActionItem) -> str:
    """The ticket description: item text + provenance header + quoted lines."""
    lines = meeting.content.split("\n")
    start = max(item.line_start, 1) - 1
    end = min(item.line_end, len(lines))
    quoted = "\n".join(f"> {line}" for line in lines[start:end][:40])

    if meeting.kind == "chat":
        header = f"From chat thread *{meeting.title}*"
        if meeting.channel_name and meeting.channel_name != meeting.title:
            header += f" in {meeting.channel_name}"
        if meeting.meeting_at is not None:
            header += f" ({meeting.meeting_at.date().isoformat()})"
        header += f", messages {item.line_start}-{item.line_end}:"
    else:
        header = f"From meeting *{meeting.title}*"
        if meeting.meeting_at is not None:
            header += f" ({meeting.meeting_at.date().isoformat()})"
        header += f", lines {item.line_start}-{item.line_end}:"
    return f"{item.description}\n\n{header}\n{quoted}"


async def _draft_assignee(
    db: AsyncSession, item: MeetingActionItem, scope_org_id: str | None = None
) -> tuple[str | None, str | None, str | None]:
    """``(assigned_to, assigned_user_id, assigned_contact_id)`` from the
    resolved owner.

    A persona assignment wins over the represented person, matching §7
    ("persona if any, else person"); an external contact (GitHub #125) comes
    last, and a contact linked to a still-active member drafts that member.
    """
    if item.owner_participant_id is None:
        return None, None, None
    participant = await db.get(MeetingParticipant, item.owner_participant_id)
    if participant is None:
        return None, None, None
    if participant.persona_name:
        return participant.persona_name, None, None
    if participant.user_id:
        return None, participant.user_id, None
    if participant.contact_id and scope_org_id:
        from sessionfs.server.db.models import ExternalContact
        from sessionfs.server.services.contacts import linked_member_id

        contact = await db.get(ExternalContact, participant.contact_id)
        if contact is not None and contact.org_id == scope_org_id and contact.status == "active":
            member_id = await linked_member_id(db, contact)
            if member_id is not None:
                return None, member_id, None
            return None, None, contact.id
    return None, None, None


EXPORTER_EMAIL_NOTE = "matched by exporter email"


async def _owner_match_note(
    db: AsyncSession, meeting: MeetingTranscript, item: MeetingActionItem
) -> str | None:
    """``matched by exporter email`` when a chat item's owner resolved to a
    member by an email that only the export's users.json supplied (it isn't
    written anywhere in the thread itself)."""
    if meeting.kind != "chat" or item.owner_participant_id is None:
        return None
    participant = await db.get(MeetingParticipant, item.owner_participant_id)
    if (
        participant is None
        or participant.resolution_method != "exact_email"
        or not participant.email_raw
    ):
        return None
    email = participant.email_raw.casefold()
    if email in meeting.content.casefold():
        return None
    exporter_emails = (
        await db.execute(
            select(ConversationMessageMeta.email_raw).where(
                ConversationMessageMeta.source_id == meeting.id,
                ConversationMessageMeta.email_raw.is_not(None),
            )
        )
    ).scalars().all()
    if any(e and e.casefold() == email for e in exporter_emails):
        return EXPORTER_EMAIL_NOTE
    return None


# ── Target project resolution (§7 group meetings) ──


async def _group_editable_projects(
    db: AsyncSession, group: ProjectGroup, auth: AuthContext
) -> list[Project]:
    from sessionfs.server.auth.project_access import user_can_edit_project

    project_ids = (
        (
            await db.execute(
                select(ProjectGroupAccess.project_id).where(
                    ProjectGroupAccess.group_id == group.id
                )
            )
        )
        .scalars()
        .all()
    )
    projects: list[Project] = []
    for project_id in project_ids:
        project = await db.get(Project, project_id)
        if project is None or project.merged_into_project_id is not None:
            continue
        if await user_can_edit_project(db, auth.user.id, project):
            projects.append(project)
    return projects


async def _resolve_target_project(
    db: AsyncSession,
    meeting: MeetingTranscript,
    scope: Project | ProjectGroup,
    auth: AuthContext,
    requested: str | None,
) -> Project:
    if meeting.project_id is not None:
        assert isinstance(scope, Project)
        return scope
    assert isinstance(scope, ProjectGroup)
    if not requested:
        raise HTTPException(
            422,
            {
                "code": "target_project_required",
                "message": "Choose a target project for this group meeting.",
            },
        )
    editable = await _group_editable_projects(db, scope, auth)
    target = next((p for p in editable if p.id == requested), None)
    if target is None:
        raise HTTPException(
            422,
            {
                "code": "target_project_not_editable",
                "message": "The target project is not editable from this group.",
            },
        )
    return target


# ── Item lookup ──


async def _get_action_item(
    db: AsyncSession, meeting_id: str, item_id: str
) -> MeetingActionItem:
    item = (
        await db.execute(
            select(MeetingActionItem).where(
                MeetingActionItem.id == item_id,
                MeetingActionItem.transcript_id == meeting_id,
            )
        )
    ).scalar_one_or_none()
    if item is None:
        raise HTTPException(404, "Action item not found")
    return item


def _require_ticket_scope(auth: AuthContext) -> None:
    """Promote creates tickets (and may publish to Jira): a service key needs
    ``tickets:write`` as well, exactly as POST /projects/{p}/tickets does."""
    if auth.key_kind != "service":
        return
    if "*" in auth.scopes or "tickets:write" in auth.scopes:
        return
    raise HTTPException(
        status_code=403,
        detail={
            "error": "insufficient_scope",
            "required": ["tickets:write"],
            "current": auth.scopes,
            "message": (
                "Scope required: one of ['tickets:write']. "
                f"Key has: {auth.scopes}."
            ),
        },
    )


async def _lock_meeting_live(db: AsyncSession, meeting_id: str) -> MeetingTranscript:
    """Re-read the meeting FOR UPDATE; a concurrent soft-delete (an UPDATE of
    the same row) then blocks until this promote commits, and a meeting already
    deleted is refused with the uniform 404."""
    locked = (
        await db.execute(
            select(MeetingTranscript)
            .where(MeetingTranscript.id == meeting_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if locked is None or locked.deleted_at is not None:
        raise HTTPException(404, "Meeting not found")
    return locked


async def _lock_and_recheck_group_promote(
    db: AsyncSession,
    meeting_id: str,
    item_id: str,
    target: Project,
    auth: AuthContext,
) -> None:
    """Re-authorize a group promote under the target's scope lock.

    Scope locks come first (project, then its org), then the group row, the
    caller's grant and membership rows and the target's group membership are
    locked FOR UPDATE, so a grant revocation, group deletion or removal of the
    target from the group serialises against this promote. Under those locks
    the live meeting + item, the caller's group edit standing and the target's
    group membership are checked again. Every refusal about the meeting or
    group is the uniform 404; a target that left the group is the same 422 as
    an uneditable target.
    """
    from sessionfs.server.db.models import OrgMember, ProjectGroupMember
    from sessionfs.server.services.scope_locks import lock_project_scope

    await lock_project_scope(db, target)
    current = (
        await db.execute(
            select(MeetingTranscript)
            .where(MeetingTranscript.id == meeting_id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if current is None or current.deleted_at is not None or current.group_id is None:
        raise HTTPException(404, "Meeting not found")
    group_id = current.group_id
    group = (
        await db.execute(
            select(ProjectGroup)
            .where(ProjectGroup.id == group_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if group is None:
        raise HTTPException(404, "Meeting not found")
    await db.execute(
        select(ProjectGroupMember)
        .where(
            ProjectGroupMember.group_id == group_id,
            ProjectGroupMember.user_id == auth.user.id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    await db.execute(
        select(OrgMember)
        .where(OrgMember.org_id == group.org_id, OrgMember.user_id == auth.user.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    await _lock_meeting_live(db, meeting_id)
    access = (
        await db.execute(
            select(ProjectGroupAccess)
            .where(
                ProjectGroupAccess.group_id == group_id,
                ProjectGroupAccess.project_id == target.id,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()

    # Group edit standing, against the live group.
    await _meeting_scope(db, current, auth, write=True)
    if access is None:
        raise HTTPException(
            422,
            {
                "code": "target_project_not_editable",
                "message": "The target project is not editable from this group.",
            },
        )
    item = (
        await db.execute(
            select(MeetingActionItem)
            .where(
                MeetingActionItem.id == item_id,
                MeetingActionItem.transcript_id == meeting_id,
            )
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if item is None:
        raise HTTPException(404, "Action item not found")
    if item.status != "pending":
        raise _item_not_pending()


def _item_not_pending() -> HTTPException:
    return HTTPException(
        409,
        {
            "code": "item_not_pending",
            "message": "This action item is no longer pending.",
        },
    )


# ── Routes ──


@router.post("/meetings/{meeting_id}/extract", response_model=ExtractionResponse)
async def extract_meeting(
    meeting_id: str,
    kind: RowKind | None = Query(default=None),
    auth: AuthContext = Depends(require_scope("meetings:write")),
    db: AsyncSession = Depends(get_db),
):
    meeting = await _get_meeting(db, meeting_id, kind)
    if meeting.deleted_at is not None:
        raise HTTPException(404, "Meeting not found")
    await _meeting_scope(db, meeting, auth, write=True)

    dropped_items, _ = await run_meeting_extraction(db, meeting=meeting, auth=auth)
    detail = await _meeting_detail(db, meeting, auth=auth)
    return ExtractionResponse(**detail.model_dump(), dropped_items=dropped_items)


@router.post(
    "/meetings/{meeting_id}/action-items/{item_id}/promote/preview",
    response_model=PromotePreviewResponse,
)
async def preview_promote(
    meeting_id: str,
    item_id: str,
    target_project_id: str | None = None,
    kind: RowKind | None = Query(default=None),
    auth: AuthContext = Depends(require_scope("meetings:write")),
    db: AsyncSession = Depends(get_db),
):
    meeting = await _get_meeting(db, meeting_id, kind)
    if meeting.deleted_at is not None:
        raise HTTPException(404, "Meeting not found")
    scope = await _meeting_scope(db, meeting, auth, write=True)
    _require_ticket_scope(auth)

    item = await _get_action_item(db, meeting_id, item_id)
    if item.kind != "action":
        raise HTTPException(422, "Only action items can be promoted to tickets")
    if item.status != "pending":
        raise _item_not_pending()

    assigned_to, assigned_user_id, assigned_contact_id = await _draft_assignee(
        db, item, await _meeting_scope_org_id(db, meeting)
    )

    resolved_target_id: str | None = None
    target_project_options: list[ProjectOption] | None = None
    if meeting.project_id is not None:
        resolved_target_id = meeting.project_id
    else:
        assert isinstance(scope, ProjectGroup)
        projects = await _group_editable_projects(db, scope, auth)
        target_project_options = [
            ProjectOption(id=p.id, name=p.name) for p in projects
        ]
        # A validated editable ?target_project_id= is the draft's target, so
        # confirming the returned draft needs no second choice.
        if any(p.id == target_project_id for p in projects):
            resolved_target_id = target_project_id

    # Reuse the publish preflight's DB-only gates (no outbound request) so
    # this flag and the real publish cannot drift. A group meeting is judged
    # for the chosen target only.
    jira_target_id = resolved_target_id
    jira_publish_available = False
    if jira_target_id is not None:
        try:
            await jira_publish_preflight(
                db,
                user_id=auth.user.id,
                ctx=await build_user_context(auth.user, db),
                project_id=jira_target_id,
                auth=auth,
            )
            jira_publish_available = True
        except HTTPException:
            jira_publish_available = False

    return PromotePreviewResponse(
        meeting_id=meeting.id,
        item_id=item.id,
        title=_first_sentence(item.description),
        description=_promote_description(meeting, item),
        priority="medium",
        kind="task",
        assigned_to=assigned_to,
        assigned_user_id=assigned_user_id,
        assigned_contact_id=assigned_contact_id,
        due_date=item.due_date,
        context_refs=[f"meeting:{meeting.id}#{item.id}"],
        target_project_id=resolved_target_id,
        target_project_options=target_project_options,
        jira_publish_available=jira_publish_available,
        owner_match_note=await _owner_match_note(db, meeting, item),
    )


@router.post(
    "/meetings/{meeting_id}/action-items/{item_id}/promote",
    response_model=PromoteResponse,
)
async def promote_meeting_item(
    meeting_id: str,
    item_id: str,
    body: PromoteRequest,
    kind: RowKind | None = Query(default=None),
    auth: AuthContext = Depends(require_scope("meetings:write")),
    db: AsyncSession = Depends(get_db),
):
    meeting = await _get_meeting(db, meeting_id, kind)
    if meeting.deleted_at is not None:
        raise HTTPException(404, "Meeting not found")
    scope = await _meeting_scope(db, meeting, auth, write=True)
    _require_ticket_scope(auth)

    item = await _get_action_item(db, meeting_id, item_id)
    if item.kind != "action":
        raise HTTPException(422, "Only action items can be promoted to tickets")
    if item.status != "pending":
        raise _item_not_pending()

    target = await _resolve_target_project(db, meeting, scope, auth, body.target_project_id)
    if meeting.group_id is not None:
        try:
            await _lock_and_recheck_group_promote(
                db, meeting_id, item_id, target, auth
            )
        except HTTPException:
            await db.rollback()
            raise
    else:
        from sessionfs.server.services.scope_locks import lock_project_scope

        try:
            await lock_project_scope(db, target)
            await _lock_meeting_live(db, meeting_id)
        except HTTPException:
            await db.rollback()
            raise
    ctx = await build_user_context(auth.user, db)
    scope_org_id = await _meeting_scope_org_id(db, meeting)

    # The same draft validation POST /projects/{p}/tickets applies, with the
    # same 422 bodies (field errors in the framework's envelope, one error
    # for a persona + person assignment together).
    check_assignee_conflict(
        body.assigned_to,
        body.assigned_user_id,
        None,
        assigned_contact_id=body.assigned_contact_id,
    )
    try:
        draft = TicketCreate(
            title=body.title,
            description=body.description,
            priority=body.priority,
            kind=body.kind,
            assigned_to=body.assigned_to,
            assigned_user_id=body.assigned_user_id,
            assigned_contact_id=body.assigned_contact_id,
            due_date=body.due_date,
            context_refs=[f"meeting:{meeting.id}#{item.id}"],
        )
    except ValidationError as exc:
        errors = [
            {**err, "loc": ("body", *err["loc"])}
            for err in exc.errors(include_url=False, include_context=False)
        ]
        raise RequestValidationError(errors) from None

    if body.publish_to_jira:
        # The Jira gates run BEFORE anything is created, so a refusal leaves
        # the item pending and no ticket behind.
        await jira_publish_preflight(
            db, user_id=auth.user.id, ctx=ctx, project_id=target.id, auth=auth
        )

    ticket_id = await create_ticket_action(
        db,
        user_id=auth.user.id,
        ctx=ctx,
        project_id=target.id,
        title=draft.title,
        description=draft.description,
        priority=draft.priority,
        kind=draft.kind,
        assigned_to=draft.assigned_to,
        assigned_user_id=draft.assigned_user_id,
        acceptance_criteria=[],
        conversation_id=meeting.id,
        display_name=display_name_for(auth.user),
        due_date=draft.due_date,
        context_refs=[f"meeting:{meeting.id}#{item.id}"],
        append_provenance=False,
        actor_type=auth.actor_type,
        service_key_id=auth.service_key_id,
        service_key_name=auth.service_key_name,
        auth=auth,
        require_editor=True,
        assigned_contact_id=draft.assigned_contact_id,
    )

    # Rowcount-1 guard: only a still-pending item flips. A concurrent promote
    # created its own ticket above but this update matches zero rows, so the
    # 409 rolls the whole transaction back and that ticket is never committed.
    result = await db.execute(
        update(MeetingActionItem)
        .where(
            MeetingActionItem.id == item_id,
            MeetingActionItem.transcript_id == meeting_id,
            MeetingActionItem.status == "pending",
        )
        .values(
            status="promoted",
            promoted_ticket_id=ticket_id,
            promoted_by_user_id=auth.user.id,
        )
    )
    if result.rowcount != 1:
        raise _item_not_pending()
    # Reminders on the promoted item follow it to the new ticket (they then
    # also list on the ticket's reminders strip).
    await db.execute(
        update(Reminder)
        .where(
            Reminder.source_kind.in_(("meeting_item", "chat_item")),
            Reminder.source_id == item_id,
        )
        .values(linked_ticket_id=ticket_id)
        .execution_options(synchronize_session=False)
    )

    if scope_org_id:
        org = await db.get(Organization, scope_org_id)
        actor_role = (
            await _user_org_role(db, auth.user.id, scope_org_id)
            if auth.key_kind == "user"
            else "service_key"
        )
        event = _meeting_audit_event(
            meeting=meeting,
            org=org,
            event_type="meeting_action_promoted",
            actor_user_id=auth.user.id,
            actor_email=auth.user.email if auth.key_kind == "user" else None,
            actor_role=actor_role,
            after={
                "meeting_id": meeting.id,
                "action_item_id": item.id,
                "ticket_id": ticket_id,
            },
        )
        if event is not None:
            db.add(event)

    await db.commit()

    published_to_jira = False
    warnings: list[str] = []
    if body.publish_to_jira:
        # The ticket and the promotion are committed: a publish failure must
        # not turn into an error (a retry would hit item_not_pending), so it
        # is reported as a warning and the ticket can be published later.
        try:
            await publish_ticket_to_jira(
                db,
                project_id=target.id,
                ticket_id=ticket_id,
                actor_user_id=auth.user.id,
                auth=auth,
            )
            published_to_jira = True
        except ContentTooLarge:
            warnings.append("jira_publish_failed: content_too_large")
        except HTTPException as exc:
            detail = exc.detail
            code = (
                (detail.get("error") or detail.get("code"))
                if isinstance(detail, dict)
                else None
            ) or f"http_{exc.status_code}"
            warnings.append(f"jira_publish_failed: {code}")

    return PromoteResponse(
        ticket_id=ticket_id,
        item_id=item_id,
        status="promoted",
        published_to_jira=published_to_jira,
        warnings=warnings,
    )


async def _item_out(
    db: AsyncSession,
    meeting: MeetingTranscript,
    auth: AuthContext,
    item: MeetingActionItem,
) -> MeetingActionItemResponse:
    """One item's response, with the tickets the caller may see."""
    return _action_item_response(
        item,
        await _item_owner_user_id(db, item),
        await item_tickets(db, meeting, auth, [item]),
    )


async def _item_owner_user_id(db: AsyncSession, item: MeetingActionItem) -> str | None:
    if not item.owner_participant_id:
        return None
    return (
        await db.execute(
            select(MeetingParticipant.user_id).where(
                MeetingParticipant.id == item.owner_participant_id
            )
        )
    ).scalar_one_or_none()


@router.post(
    "/meetings/{meeting_id}/action-items/{item_id}/dismiss",
    response_model=MeetingActionItemResponse,
)
async def dismiss_meeting_item(
    meeting_id: str,
    item_id: str,
    kind: RowKind | None = Query(default=None),
    auth: AuthContext = Depends(require_scope("meetings:write")),
    db: AsyncSession = Depends(get_db),
):
    meeting = await _get_meeting(db, meeting_id, kind)
    if meeting.deleted_at is not None:
        raise HTTPException(404, "Meeting not found")
    await _meeting_scope(db, meeting, auth, write=True)

    item = await _get_action_item(db, meeting_id, item_id)
    result = await db.execute(
        update(MeetingActionItem)
        .where(
            MeetingActionItem.id == item_id,
            MeetingActionItem.transcript_id == meeting_id,
            MeetingActionItem.status == "pending",
        )
        .values(status="dismissed")
    )
    if result.rowcount != 1:
        raise _item_not_pending()
    await db.commit()
    await db.refresh(item)
    return await _item_out(db, meeting, auth, item)


@router.post(
    "/meetings/{meeting_id}/action-items/{item_id}/restore",
    response_model=MeetingActionItemResponse,
)
async def restore_meeting_item(
    meeting_id: str,
    item_id: str,
    kind: RowKind | None = Query(default=None),
    auth: AuthContext = Depends(require_scope("meetings:write")),
    db: AsyncSession = Depends(get_db),
):
    meeting = await _get_meeting(db, meeting_id, kind)
    if meeting.deleted_at is not None:
        raise HTTPException(404, "Meeting not found")
    await _meeting_scope(db, meeting, auth, write=True)

    item = await _get_action_item(db, meeting_id, item_id)
    result = await db.execute(
        update(MeetingActionItem)
        .where(
            MeetingActionItem.id == item_id,
            MeetingActionItem.transcript_id == meeting_id,
            MeetingActionItem.status == "dismissed",
        )
        .values(status="pending")
    )
    if result.rowcount != 1:
        raise HTTPException(
            409,
            {
                "code": "item_not_dismissed",
                "message": "This action item is not dismissed.",
            },
        )
    await db.commit()
    await db.refresh(item)
    return await _item_out(db, meeting, auth, item)


# ── Link an item to an existing ticket (0.29.11) ──


class LinkRequest(BaseModel):
    # Omitted: the item's current (visible, not dismissed) suggestion.
    ticket_id: str | None = Field(default=None, min_length=1, max_length=64)


class LinkPreviewResponse(BaseModel):
    meeting_id: str
    item_id: str
    ticket: ItemTicketRef
    # True when the ticket is the extraction's suggestion for this item.
    suggested: bool
    # The comment Link will add on the ticket (DLP-redacted).
    comment: str


class LinkResponse(BaseModel):
    ticket_id: str
    item_id: str
    status: Literal["linked"]
    comment_id: str
    item: MeetingActionItemResponse


def _not_linkable() -> HTTPException:
    """One refusal for every ticket that can't be linked: missing, outside
    the conversation's scope, unreadable, or not open to the caller's
    comments. Never says which."""
    return HTTPException(
        422,
        {
            "code": "ticket_not_linkable",
            "message": "That ticket can't be linked to this item.",
        },
    )


def _link_ticket_id(item: MeetingActionItem, requested: str | None) -> str:
    if requested and requested.strip():
        return requested.strip()
    if item.suggested_ticket_id and item.suggestion_dismissed_at is None:
        return item.suggested_ticket_id
    raise HTTPException(
        422,
        {
            "code": "ticket_required",
            "message": "Choose the ticket to link this item to.",
        },
    )


async def _linkable_ticket(
    db: AsyncSession,
    meeting: MeetingTranscript,
    auth: AuthContext,
    ticket_id: str,
) -> tuple[Ticket, Project]:
    """The ticket, if the caller may link this conversation's item to it:
    in scope and readable (``visible_tickets``), and the caller passes the
    ticket-comment authorization (the link writes a comment)."""
    ticket = (await visible_tickets(db, meeting, auth, {ticket_id})).get(ticket_id)
    if ticket is None:
        raise _not_linkable()
    try:
        project = await authorize_ticket_comment(
            db, auth, ticket.project_id, await build_user_context(auth.user, db)
        )
    except HTTPException:
        raise _not_linkable() from None
    return ticket, project


def _range_label(meeting: MeetingTranscript, item: MeetingActionItem) -> str:
    letter = "M" if meeting.kind == "chat" else "L"
    if item.line_end != item.line_start:
        return f"{letter}{item.line_start}–{item.line_end}"
    return f"{letter}{item.line_start}"


def link_comment_text(meeting: MeetingTranscript, item: MeetingActionItem) -> str:
    """``Raised again in meeting "<title>" on <date>, lines L<a>–<b>: <text>``
    (``chat thread`` / ``messages M<a>–<b>`` for a chat), before DLP."""
    is_chat = meeting.kind == "chat"
    noun = "chat thread" if is_chat else "meeting"
    unit = "messages" if is_chat else "lines"
    title = " ".join((meeting.title or "").split())
    when = meeting.meeting_at or meeting.created_at
    on = f" on {when.date().isoformat()}" if when is not None else ""
    text = " ".join((item.description or "").split())
    return f'Raised again in {noun} "{title}"{on}, {unit} {_range_label(meeting, item)}: {text}'


async def _link_comment(
    db: AsyncSession, meeting: MeetingTranscript, item: MeetingActionItem, auth: AuthContext
) -> str:
    """The comment body, redacted under the strictest-union policy the
    meeting routes use (never blocks: the parts are already-stored text)."""
    policy = await _upload_dlp_policy(db, auth, await _meeting_scope_org_id(db, meeting))
    if policy is not None:
        policy = dict(policy)
        policy["mode"] = "redact"
    redacted, _ = _dlp_scan_content(link_comment_text(meeting, item), policy)
    return redacted


async def _load_item_for_link(
    db: AsyncSession, meeting_id: str, item_id: str, kind: str | None, auth: AuthContext
) -> tuple[MeetingTranscript, MeetingActionItem]:
    meeting = await _get_meeting(db, meeting_id, kind)
    if meeting.deleted_at is not None:
        raise HTTPException(404, "Meeting not found")
    await _meeting_scope(db, meeting, auth, write=True)
    _require_ticket_scope(auth)
    item = await _get_action_item(db, meeting_id, item_id)
    return meeting, item


def _event_actor(auth: AuthContext) -> dict:
    if auth.key_kind == "service":
        return {
            "actor_type": "service",
            "actor_user_id": None,
            "actor_label": auth.service_key_name,
        }
    return {"actor_type": "user", "actor_user_id": auth.user.id, "actor_label": None}


async def _audit(
    db: AsyncSession,
    meeting: MeetingTranscript,
    auth: AuthContext,
    event_type: str,
    after: dict,
) -> None:
    scope_org_id = await _meeting_scope_org_id(db, meeting)
    if not scope_org_id:
        return
    org = await db.get(Organization, scope_org_id)
    actor_role = (
        await _user_org_role(db, auth.user.id, scope_org_id)
        if auth.key_kind == "user"
        else "service_key"
    )
    event = _meeting_audit_event(
        meeting=meeting,
        org=org,
        event_type=event_type,
        actor_user_id=auth.user.id,
        actor_email=auth.user.email if auth.key_kind == "user" else None,
        actor_role=actor_role,
        after=after,
    )
    if event is not None:
        db.add(event)


def _conversation_ref(meeting: MeetingTranscript, item: MeetingActionItem) -> dict:
    return {
        "kind": meeting.kind or "meeting",
        "conversation_id": meeting.id,
        "item_id": item.id,
    }


@router.post(
    "/meetings/{meeting_id}/action-items/{item_id}/link/preview",
    response_model=LinkPreviewResponse,
)
async def preview_link(
    meeting_id: str,
    item_id: str,
    body: LinkRequest | None = None,
    kind: RowKind | None = Query(default=None),
    auth: AuthContext = Depends(require_scope("meetings:write")),
    db: AsyncSession = Depends(get_db),
):
    """What Link would do (the ticket + the comment it adds); writes nothing."""
    meeting, item = await _load_item_for_link(db, meeting_id, item_id, kind, auth)
    if item.status != "pending":
        raise _item_not_pending()
    ticket_id = _link_ticket_id(item, body.ticket_id if body else None)
    ticket, _ = await _linkable_ticket(db, meeting, auth, ticket_id)
    ref = _ticket_ref(ticket)
    assert ref is not None
    return LinkPreviewResponse(
        meeting_id=meeting.id,
        item_id=item.id,
        ticket=ref,
        suggested=(
            item.suggested_ticket_id == ticket.id and item.suggestion_dismissed_at is None
        ),
        comment=await _link_comment(db, meeting, item, auth),
    )


@router.post(
    "/meetings/{meeting_id}/action-items/{item_id}/link",
    response_model=LinkResponse,
)
async def link_meeting_item(
    meeting_id: str,
    item_id: str,
    body: LinkRequest | None = None,
    kind: RowKind | None = Query(default=None),
    auth: AuthContext = Depends(require_scope("meetings:write")),
    db: AsyncSession = Depends(get_db),
):
    """Tie a pending item to an existing ticket: the item becomes ``linked``
    and the ticket gets a "raised again" comment plus a timeline event, all
    in one transaction."""
    meeting, item = await _load_item_for_link(db, meeting_id, item_id, kind, auth)
    if item.status != "pending":
        raise _item_not_pending()
    ticket_id = _link_ticket_id(item, body.ticket_id if body else None)
    _, project = await _linkable_ticket(db, meeting, auth, ticket_id)

    try:
        # The same locks as promote; then the meeting, the caller's standing
        # and the ticket are checked again against the live rows.
        if meeting.group_id is not None:
            try:
                await _lock_and_recheck_group_promote(
                    db, meeting_id, item_id, project, auth
                )
            except HTTPException as exc:
                if exc.status_code == 422:
                    raise _not_linkable() from None
                raise
        else:
            from sessionfs.server.services.scope_locks import lock_project_scope

            await lock_project_scope(db, project)
            await _lock_meeting_live(db, meeting_id)
            await _meeting_scope(db, meeting, auth, write=True)
        ticket, project = await _linkable_ticket(db, meeting, auth, ticket_id)

        now = datetime.now(timezone.utc)
        # Rowcount-1 guard: only a still-pending item links, so two racing
        # links (or a link racing a promote) can't both land.
        result = await db.execute(
            update(MeetingActionItem)
            .where(
                MeetingActionItem.id == item_id,
                MeetingActionItem.transcript_id == meeting_id,
                MeetingActionItem.status == "pending",
            )
            .values(
                status="linked",
                linked_ticket_id=ticket.id,
                linked_at=now,
                linked_by_user_id=auth.user.id,
            )
        )
        if result.rowcount != 1:
            raise _item_not_pending()

        comment_id = await insert_ticket_comment(
            db,
            project=project,
            ticket_id=ticket.id,
            user_id=auth.user.id,
            content=await _link_comment(db, meeting, item, auth),
            author_persona=None,
            session_id=None,
            lease_epoch=None,
            verdict_trusted=False,
            auth=auth,
        )
        await record_ticket_event(
            db,
            ticket,
            "conversation_linked",
            after=_conversation_ref(meeting, item),
            **_event_actor(auth),
        )
        # Reminders on the item now also list on the ticket.
        await db.execute(
            update(Reminder)
            .where(
                Reminder.source_kind.in_(("meeting_item", "chat_item")),
                Reminder.source_id == item_id,
            )
            .values(linked_ticket_id=ticket.id)
            .execution_options(synchronize_session=False)
        )
        await _audit(
            db,
            meeting,
            auth,
            "meeting_action_linked",
            {"meeting_id": meeting.id, "action_item_id": item.id, "ticket_id": ticket.id},
        )
    except HTTPException:
        await db.rollback()
        raise
    await db.commit()
    await db.refresh(item)
    return LinkResponse(
        ticket_id=ticket_id,
        item_id=item_id,
        status="linked",
        comment_id=comment_id,
        item=await _item_out(db, meeting, auth, item),
    )


@router.post(
    "/meetings/{meeting_id}/action-items/{item_id}/unlink",
    response_model=MeetingActionItemResponse,
)
async def unlink_meeting_item(
    meeting_id: str,
    item_id: str,
    kind: RowKind | None = Query(default=None),
    auth: AuthContext = Depends(require_scope("meetings:write")),
    db: AsyncSession = Depends(get_db),
):
    """Back to pending. The ticket's "raised again" comment stays (comments
    are append-only); the ticket's timeline records the unlink when the
    caller can still read that ticket.

    Takes the same locks as link (the linked ticket's project scope, the
    group row for a group conversation, then the meeting FOR UPDATE) and
    re-checks the meeting, the caller's standing and the item under them.
    """
    meeting, item = await _load_item_for_link(db, meeting_id, item_id, kind, auth)
    try:
        from sessionfs.server.services.scope_locks import lock_project_scope

        linked = await db.get(Ticket, item.linked_ticket_id) if item.linked_ticket_id else None
        project = (
            await db.get(Project, linked.project_id) if linked is not None else None
        )
        if project is not None:
            # A merged project must not block the unlink itself.
            await lock_project_scope(db, project, require_live=False)
        if meeting.group_id is not None:
            await db.execute(
                select(ProjectGroup)
                .where(ProjectGroup.id == meeting.group_id)
                .with_for_update()
            )
        meeting = await _lock_meeting_live(db, meeting_id)
        await _meeting_scope(db, meeting, auth, write=True)
        item = await _get_action_item(db, meeting_id, item_id)
        await db.refresh(item)
        previous = item.linked_ticket_id

        result = await db.execute(
            update(MeetingActionItem)
            .where(
                MeetingActionItem.id == item_id,
                MeetingActionItem.transcript_id == meeting_id,
                MeetingActionItem.status == "linked",
            )
            .values(
                status="pending",
                linked_ticket_id=None,
                linked_at=None,
                linked_by_user_id=None,
            )
        )
        if result.rowcount != 1:
            raise HTTPException(
                409,
                {"code": "item_not_linked", "message": "This action item is not linked."},
            )
        if previous:
            # Only a ticket the caller can still read (in this conversation's
            # scope and readable) gets the timeline event; the unlink itself
            # goes through either way.
            ticket = (await visible_tickets(db, meeting, auth, {previous})).get(previous)
            if ticket is not None:
                await record_ticket_event(
                    db,
                    ticket,
                    "conversation_unlinked",
                    before=_conversation_ref(meeting, item),
                    **_event_actor(auth),
                )
            await db.execute(
                update(Reminder)
                .where(
                    Reminder.source_kind.in_(("meeting_item", "chat_item")),
                    Reminder.source_id == item_id,
                    Reminder.linked_ticket_id == previous,
                )
                .values(linked_ticket_id=None)
                .execution_options(synchronize_session=False)
            )
        await _audit(
            db,
            meeting,
            auth,
            "meeting_action_unlinked",
            {"meeting_id": meeting.id, "action_item_id": item.id, "ticket_id": previous},
        )
    except HTTPException:
        await db.rollback()
        raise
    await db.commit()
    await db.refresh(item)
    return await _item_out(db, meeting, auth, item)


@router.post(
    "/meetings/{meeting_id}/action-items/{item_id}/dismiss-suggestion",
    response_model=MeetingActionItemResponse,
)
async def dismiss_item_suggestion(
    meeting_id: str,
    item_id: str,
    kind: RowKind | None = Query(default=None),
    auth: AuthContext = Depends(require_scope("meetings:write")),
    db: AsyncSession = Depends(get_db),
):
    """"Not this one": hide the suggestion; the item stays pending. Idempotent,
    and the same answer whether or not the item had a (visible) suggestion."""
    meeting = await _get_meeting(db, meeting_id, kind)
    if meeting.deleted_at is not None:
        raise HTTPException(404, "Meeting not found")
    await _meeting_scope(db, meeting, auth, write=True)
    # Suggestions point at tickets: the same ticket scope as link, preview
    # and the linkable-tickets picker.
    _require_ticket_scope(auth)
    item = await _get_action_item(db, meeting_id, item_id)
    result = await db.execute(
        update(MeetingActionItem)
        .where(
            MeetingActionItem.id == item_id,
            MeetingActionItem.transcript_id == meeting_id,
            MeetingActionItem.status == "pending",
        )
        .values(
            suggestion_dismissed_at=func.coalesce(
                MeetingActionItem.suggestion_dismissed_at, datetime.now(timezone.utc)
            )
        )
    )
    if result.rowcount != 1:
        raise _item_not_pending()
    await db.commit()
    await db.refresh(item)
    return await _item_out(db, meeting, auth, item)


class LinkableTicketsResponse(BaseModel):
    tickets: list[ItemTicketRef]


_LINKABLE_LIMIT_MAX = 50


@router.get(
    "/meetings/{meeting_id}/linkable-tickets",
    response_model=LinkableTicketsResponse,
)
async def list_linkable_tickets(
    meeting_id: str,
    q: str | None = Query(default=None, max_length=200),
    limit: int = Query(default=20, ge=1, le=_LINKABLE_LIMIT_MAX),
    kind: RowKind | None = Query(default=None),
    auth: AuthContext = Depends(require_scope("meetings:write")),
    db: AsyncSession = Depends(get_db),
):
    """Tickets an item of this conversation can be linked to (the "Link to
    ticket…" picker): the meeting's project, or the group's member projects
    the caller can read; cancelled tickets left out; newest first. ``q``
    matches the title (substring) or an id prefix."""
    from sessionfs.server.routes.meetings import _escape_like
    from sessionfs.server.services.ticket_match import scope_project_ids

    meeting = await _get_meeting(db, meeting_id, kind)
    if meeting.deleted_at is not None:
        raise HTTPException(404, "Meeting not found")
    await _meeting_scope(db, meeting, auth, write=True)
    _require_ticket_scope(auth)
    project_ids = await scope_project_ids(db, meeting, auth)
    if not project_ids:
        return LinkableTicketsResponse(tickets=[])
    stmt = select(Ticket).where(
        Ticket.project_id.in_(project_ids), Ticket.status != "cancelled"
    )
    text = " ".join((q or "").split())
    if text:
        pattern = _escape_like(text.casefold())
        stmt = stmt.where(
            or_(
                func.lower(Ticket.title).like(f"%{pattern}%", escape="\\"),
                func.lower(Ticket.id).like(f"{pattern}%", escape="\\"),
            )
        )
    rows = (
        await db.execute(
            stmt.order_by(Ticket.updated_at.desc(), Ticket.id.desc()).limit(limit)
        )
    ).scalars().all()
    return LinkableTicketsResponse(
        tickets=[ref for ref in (_ticket_ref(t) for t in rows) if ref is not None]
    )
