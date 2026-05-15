"""Server-side retrieval audit routes.

These endpoints make context-shaping MCP retrievals centrally auditable.
Local JSONL logging remains a fallback for offline MCP clients, but
enterprise SoD should rely on these server rows.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server.auth.dependencies import get_current_user
from sessionfs.server.db.engine import get_db
from sessionfs.server.db.models import (
    AgentPersona,
    OrgMember,
    Project,
    RetrievalAuditContext,
    RetrievalAuditEvent,
    Session,
    Ticket,
    User,
)
from sessionfs.server.routes.wiki import _get_project_or_404

router = APIRouter(prefix="/api/v1", tags=["retrieval-audit"])

MAX_AUDIT_JSON_BYTES = 16 * 1024


class RetrievalAuditContextCreate(BaseModel):
    ticket_id: str | None = None
    persona_name: str | None = None
    lease_epoch: int | None = None


class RetrievalAuditContextResponse(BaseModel):
    id: str
    project_id: str
    ticket_id: str | None
    persona_name: str | None
    lease_epoch: int | None
    created_by_user_id: str | None
    created_at: datetime
    closed_at: datetime | None


class RetrievalAuditEventCreate(BaseModel):
    context_id: str
    session_id: str | None = None
    tool_name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    returned_refs: dict[str, list[str]] = Field(default_factory=dict)
    source: str = "mcp"


class RetrievalAuditEventResponse(BaseModel):
    id: int
    context_id: str
    project_id: str
    session_id: str | None
    tool_name: str
    arguments: dict[str, Any]
    returned_refs: dict[str, Any]
    source: str
    caller_user_id: str | None
    created_at: datetime


class RetrievalAuditLogResponse(BaseModel):
    session_id: str | None = None
    retrieval_audit_id: str
    events: list[RetrievalAuditEventResponse]
    count: int


def _context_to_response(ctx: RetrievalAuditContext) -> RetrievalAuditContextResponse:
    return RetrievalAuditContextResponse(
        id=ctx.id,
        project_id=ctx.project_id,
        ticket_id=ctx.ticket_id,
        persona_name=ctx.persona_name,
        lease_epoch=ctx.lease_epoch,
        created_by_user_id=ctx.created_by_user_id,
        created_at=ctx.created_at,
        closed_at=ctx.closed_at,
    )


def _event_to_response(event: RetrievalAuditEvent) -> RetrievalAuditEventResponse:
    return RetrievalAuditEventResponse(
        id=event.id,
        context_id=event.context_id,
        project_id=event.project_id,
        session_id=event.session_id,
        tool_name=event.tool_name,
        arguments=_loads_dict(event.arguments),
        returned_refs=_loads_dict(event.returned_refs),
        source=event.source,
        caller_user_id=event.caller_user_id,
        created_at=event.created_at,
    )


def _loads_dict(raw: str | None) -> dict:
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _json_capped(value: dict[str, Any]) -> str:
    raw = json.dumps(value, sort_keys=True, default=str)
    if len(raw.encode("utf-8")) <= MAX_AUDIT_JSON_BYTES:
        return raw
    marker = {
        "_truncated": True,
        "_original_bytes": len(raw.encode("utf-8")),
    }
    return json.dumps(marker, sort_keys=True)


def _accessible_project_ids_subquery(user_id: str):
    org_ids = select(OrgMember.org_id).where(OrgMember.user_id == user_id)
    return select(Project.id).where(
        or_(
            Project.owner_id == user_id,
            Project.org_id.in_(org_ids),
        )
    )


async def _get_context_or_404(
    context_id: str,
    project_id: str,
    db: AsyncSession,
) -> RetrievalAuditContext:
    ctx = (
        await db.execute(
            select(RetrievalAuditContext).where(
                RetrievalAuditContext.id == context_id,
                RetrievalAuditContext.project_id == project_id,
            )
        )
    ).scalar_one_or_none()
    if ctx is None:
        raise HTTPException(404, "Retrieval audit context not found")
    return ctx


async def _assert_context_owner(ctx: RetrievalAuditContext, user: User) -> None:
    if ctx.created_by_user_id != user.id:
        raise HTTPException(403, "Retrieval audit context belongs to another user")


async def _assert_audit_context_links(
    project_id: str,
    ticket_id: str | None,
    persona_name: str | None,
    db: AsyncSession,
) -> None:
    """Reject context-create when supplied ticket_id / persona_name don't
    belong to this project. Retrieval audit is SoD/enterprise evidence;
    forged or non-existent provenance links weaken the chain. Caller
    passed these IDs explicitly — silently dropping them would be worse
    than 422, so we reject. Mirrors the sentinel-R2 _validated_retrieval
    _audit_id pattern on session upload, applied at the context-create
    entry point (Codex KB #395 Finding A).
    """
    if ticket_id is not None:
        ticket = (
            await db.execute(
                select(Ticket.id).where(
                    Ticket.id == ticket_id,
                    Ticket.project_id == project_id,
                )
            )
        ).scalar_one_or_none()
        if ticket is None:
            raise HTTPException(
                422,
                f"ticket_id {ticket_id!r} does not exist in project {project_id!r}",
            )
    if persona_name is not None:
        persona = (
            await db.execute(
                select(AgentPersona.id).where(
                    AgentPersona.name == persona_name,
                    AgentPersona.project_id == project_id,
                    AgentPersona.is_active == True,  # noqa: E712
                )
            )
        ).scalar_one_or_none()
        if persona is None:
            raise HTTPException(
                422,
                (
                    f"persona_name {persona_name!r} is not an active persona "
                    f"in project {project_id!r}"
                ),
            )


@router.post(
    "/projects/{project_id}/retrieval-audit-contexts",
    response_model=RetrievalAuditContextResponse,
    status_code=201,
)
async def create_retrieval_audit_context(
    project_id: str,
    body: RetrievalAuditContextCreate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> RetrievalAuditContextResponse:
    await _get_project_or_404(project_id, db, user.id)
    await _assert_audit_context_links(
        project_id, body.ticket_id, body.persona_name, db
    )
    ctx = RetrievalAuditContext(
        id=f"ra_{uuid.uuid4().hex[:24]}",
        project_id=project_id,
        ticket_id=body.ticket_id,
        persona_name=body.persona_name,
        lease_epoch=body.lease_epoch,
        created_by_user_id=user.id,
    )
    db.add(ctx)
    await db.commit()
    await db.refresh(ctx)
    return _context_to_response(ctx)


@router.post(
    "/projects/{project_id}/retrieval-audit-events",
    response_model=RetrievalAuditEventResponse,
    status_code=201,
)
async def create_retrieval_audit_event(
    project_id: str,
    body: RetrievalAuditEventCreate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> RetrievalAuditEventResponse:
    await _get_project_or_404(project_id, db, user.id)
    ctx = await _get_context_or_404(body.context_id, project_id, db)
    await _assert_context_owner(ctx, user)
    event = RetrievalAuditEvent(
        context_id=body.context_id,
        project_id=project_id,
        session_id=body.session_id,
        tool_name=body.tool_name[:100],
        arguments=_json_capped(body.arguments),
        returned_refs=_json_capped(body.returned_refs),
        source=body.source[:20] or "mcp",
        caller_user_id=user.id,
    )
    db.add(event)
    await db.commit()
    await db.refresh(event)
    return _event_to_response(event)


@router.get(
    "/retrieval-audit-contexts/{context_id}/events",
    response_model=RetrievalAuditLogResponse,
)
async def get_retrieval_audit_context_events(
    context_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> RetrievalAuditLogResponse:
    ctx = (
        await db.execute(
            select(RetrievalAuditContext).where(
                RetrievalAuditContext.id == context_id,
                RetrievalAuditContext.project_id.in_(
                    _accessible_project_ids_subquery(user.id)
                ),
            )
        )
    ).scalar_one_or_none()
    if ctx is None:
        raise HTTPException(404, "Retrieval audit context not found")
    events = list(
        (
            await db.execute(
                select(RetrievalAuditEvent)
                .where(RetrievalAuditEvent.context_id == context_id)
                .order_by(RetrievalAuditEvent.created_at.asc(), RetrievalAuditEvent.id.asc())
            )
        ).scalars().all()
    )
    return RetrievalAuditLogResponse(
        session_id=None,
        retrieval_audit_id=context_id,
        events=[_event_to_response(e) for e in events],
        count=len(events),
    )


@router.get(
    "/sessions/{session_id}/retrieval-log",
    response_model=RetrievalAuditLogResponse,
)
async def get_session_retrieval_log(
    session_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> RetrievalAuditLogResponse:
    session = (
        await db.execute(select(Session).where(Session.id == session_id))
    ).scalar_one_or_none()
    if session is None:
        raise HTTPException(404, "Session not found")
    if session.user_id != user.id:
        if session.project_id:
            await _get_project_or_404(session.project_id, db, user.id)
        else:
            raise HTTPException(403, "No access to this session")
    context_id = session.retrieval_audit_id
    if not context_id:
        return RetrievalAuditLogResponse(
            session_id=session_id,
            retrieval_audit_id="",
            events=[],
            count=0,
        )
    events = list(
        (
            await db.execute(
                select(RetrievalAuditEvent)
                .where(RetrievalAuditEvent.context_id == context_id)
                .order_by(RetrievalAuditEvent.created_at.asc(), RetrievalAuditEvent.id.asc())
            )
        ).scalars().all()
    )
    return RetrievalAuditLogResponse(
        session_id=session_id,
        retrieval_audit_id=context_id,
        events=[_event_to_response(e) for e in events],
        count=len(events),
    )
