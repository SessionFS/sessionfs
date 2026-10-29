"""Meeting action-item extraction and promote (GitHub #116, M1b §5, §7).

REST surface for the two things a stored transcript can do after upload:
run (or re-run) extraction, and promote an extracted action item to a ticket
(through a confirmable preview), plus dismiss/restore.

Access follows §3: every meeting-level miss is the same ``"Meeting not found"``
404 (the shared ``_get_meeting`` / ``_meeting_scope`` helpers keep existence and
visibility indistinguishable), and writes reuse the ``meetings:write`` scope so
project meetings admit service keys while group meetings stay user-key only.
"""

from __future__ import annotations

import re
from datetime import date
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server.auth.dependencies import (
    AuthContext,
    get_db,
    require_scope,
)
from sessionfs.server.db.models import (
    MeetingActionItem,
    MeetingParticipant,
    MeetingTranscript,
    Organization,
    Project,
    ProjectGroup,
    ProjectGroupAccess,
)
from sessionfs.server.routes.meetings import (
    MeetingActionItemResponse,
    MeetingDetailResponse,
    _action_item_response,
    _get_meeting,
    _meeting_audit_event,
    _meeting_detail,
    _meeting_scope,
    _meeting_scope_org_id,
    _user_org_role,
)
from sessionfs.server.integrations.trackers.promote import ContentTooLarge
from sessionfs.server.routes.tickets import TicketCreate
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
    due_date: date | None
    context_refs: list[str]
    target_project_id: str | None
    target_project_options: list[ProjectOption] | None = None
    # True only when publishing this ticket to Jira would pass every
    # database-side gate (plan, caller standing, connection, mapping).
    jira_publish_available: bool = False


class PromoteRequest(BaseModel):
    title: str = Field(..., min_length=1, max_length=200)
    description: str = Field(..., min_length=1)
    priority: Literal["critical", "high", "medium", "low"] = "medium"
    kind: Literal["task", "issue"] = "task"
    assigned_to: str | None = None
    assigned_user_id: str | None = None
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

    header = f"From meeting *{meeting.title}*"
    if meeting.meeting_at is not None:
        header += f" ({meeting.meeting_at.date().isoformat()})"
    header += f", lines {item.line_start}-{item.line_end}:"
    return f"{item.description}\n\n{header}\n{quoted}"


async def _draft_assignee(
    db: AsyncSession, item: MeetingActionItem
) -> tuple[str | None, str | None]:
    """``(assigned_to, assigned_user_id)`` from the resolved owner.

    A persona assignment wins over the represented person, matching §7
    ("persona if any, else person").
    """
    if item.owner_participant_id is None:
        return None, None
    participant = await db.get(MeetingParticipant, item.owner_participant_id)
    if participant is None:
        return None, None
    if participant.persona_name:
        return participant.persona_name, None
    if participant.user_id:
        return None, participant.user_id
    return None, None


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
    auth: AuthContext = Depends(require_scope("meetings:write")),
    db: AsyncSession = Depends(get_db),
):
    meeting = await _get_meeting(db, meeting_id)
    if meeting.deleted_at is not None:
        raise HTTPException(404, "Meeting not found")
    await _meeting_scope(db, meeting, auth, write=True)

    dropped_items, _ = await run_meeting_extraction(db, meeting=meeting, auth=auth)
    detail = await _meeting_detail(db, meeting)
    return ExtractionResponse(**detail.model_dump(), dropped_items=dropped_items)


@router.post(
    "/meetings/{meeting_id}/action-items/{item_id}/promote/preview",
    response_model=PromotePreviewResponse,
)
async def preview_promote(
    meeting_id: str,
    item_id: str,
    target_project_id: str | None = None,
    auth: AuthContext = Depends(require_scope("meetings:write")),
    db: AsyncSession = Depends(get_db),
):
    meeting = await _get_meeting(db, meeting_id)
    if meeting.deleted_at is not None:
        raise HTTPException(404, "Meeting not found")
    scope = await _meeting_scope(db, meeting, auth, write=True)
    _require_ticket_scope(auth)

    item = await _get_action_item(db, meeting_id, item_id)
    if item.kind != "action":
        raise HTTPException(422, "Only action items can be promoted to tickets")
    if item.status != "pending":
        raise _item_not_pending()

    assigned_to, assigned_user_id = await _draft_assignee(db, item)

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
        due_date=item.due_date,
        context_refs=[f"meeting:{meeting.id}#{item.id}"],
        target_project_id=resolved_target_id,
        target_project_options=target_project_options,
        jira_publish_available=jira_publish_available,
    )


@router.post(
    "/meetings/{meeting_id}/action-items/{item_id}/promote",
    response_model=PromoteResponse,
)
async def promote_meeting_item(
    meeting_id: str,
    item_id: str,
    body: PromoteRequest,
    auth: AuthContext = Depends(require_scope("meetings:write")),
    db: AsyncSession = Depends(get_db),
):
    meeting = await _get_meeting(db, meeting_id)
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
    check_assignee_conflict(body.assigned_to, body.assigned_user_id, None)
    try:
        draft = TicketCreate(
            title=body.title,
            description=body.description,
            priority=body.priority,
            kind=body.kind,
            assigned_to=body.assigned_to,
            assigned_user_id=body.assigned_user_id,
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


@router.post(
    "/meetings/{meeting_id}/action-items/{item_id}/dismiss",
    response_model=MeetingActionItemResponse,
)
async def dismiss_meeting_item(
    meeting_id: str,
    item_id: str,
    auth: AuthContext = Depends(require_scope("meetings:write")),
    db: AsyncSession = Depends(get_db),
):
    meeting = await _get_meeting(db, meeting_id)
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
    return _action_item_response(item)


@router.post(
    "/meetings/{meeting_id}/action-items/{item_id}/restore",
    response_model=MeetingActionItemResponse,
)
async def restore_meeting_item(
    meeting_id: str,
    item_id: str,
    auth: AuthContext = Depends(require_scope("meetings:write")),
    db: AsyncSession = Depends(get_db),
):
    meeting = await _get_meeting(db, meeting_id)
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
    return _action_item_response(item)
