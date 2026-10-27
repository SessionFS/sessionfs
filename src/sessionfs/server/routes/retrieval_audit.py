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
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server.auth.dependencies import get_current_user
from sessionfs.server.db.engine import get_db
from sessionfs.server.db.models import (
    RetrievalAuditContext,
    RetrievalAuditEvent,
    Session,
    Ticket,
    User,
)
from sessionfs.server.services.personas import (
    resolve_persona,
    visible_persona_org_id,
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
    # Nullable (090): helper turns may open a context without a single project.
    project_id: str | None
    ticket_id: str | None
    persona_name: str | None
    lease_epoch: int | None
    created_by_user_id: str | None
    created_at: datetime
    closed_at: datetime | None


_MAX_REF_ELEMENTS = 500
_MAX_REF_FIELDS = 8
_MAX_REF_TEXT = 256


class RetrievalAuditEventCreate(BaseModel):
    context_id: str
    session_id: str | None = None
    tool_name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    # Plain id refs (`kb_entry_ids: ["42"]`) and structured refs such as the
    # marketplace `skill_packages` entries ({package_id, version_id,
    # content_hash}); anything else is rejected so the event is never
    # silently dropped by a shape the local JSONL writer produces.
    returned_refs: dict[str, list[str | dict[str, str]]] = Field(default_factory=dict)
    source: str = "mcp"

    @field_validator("returned_refs")
    @classmethod
    def _validate_refs(cls, value: dict[str, list[str | dict[str, str]]]) -> dict[str, list[str | dict[str, str]]]:
        for key, items in value.items():
            if not isinstance(key, str) or not key or len(key) > 64:
                raise ValueError("returned_refs keys must be short strings")
            if len(items) > _MAX_REF_ELEMENTS:
                raise ValueError(f"returned_refs.{key} has too many elements")
            for item in items:
                if isinstance(item, dict):
                    if not item or len(item) > _MAX_REF_FIELDS:
                        raise ValueError(f"returned_refs.{key} structured ref has an invalid field count")
                    for field_name, field_value in item.items():
                        if not isinstance(field_name, str) or not field_name or len(field_name) > 64:
                            raise ValueError(f"returned_refs.{key} structured ref has an invalid field name")
                        if not isinstance(field_value, str) or len(field_value) > _MAX_REF_TEXT:
                            raise ValueError(f"returned_refs.{key}.{field_name} must be a short string")
                elif not isinstance(item, str):
                    raise ValueError(f"returned_refs.{key} elements must be strings or objects")
        return value


class RetrievalAuditEventResponse(BaseModel):
    id: int
    context_id: str
    # Nullable (090): helper events may carry no project.
    project_id: str | None
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


async def _accessible_project_ids_subquery(db: AsyncSession, user_id: str):
    """Projects whose retrieval audit the user may read: owner or org
    member — restriction-aware (GitHub #101: in an org that restricts
    visibility, org admins/owners and group grantees only). Captured-session
    readers were never admitted here and still are not."""
    from sessionfs.server.auth.project_access import accessible_project_ids_subquery

    return await accessible_project_ids_subquery(db, user_id, include_captured=False)


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
    *,
    persona_org_id: str | None = None,
) -> str | None:
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
        # GH #70 — project persona first, then the project's org.
        persona = await resolve_persona(
            db, project_id, persona_name, org_id=persona_org_id, active_only=True
        )
        if persona is None:
            raise HTTPException(
                422,
                (
                    f"persona_name {persona_name!r} is not an active persona "
                    f"in project {project_id!r}"
                ),
            )
        # GH #70 — store the canonical stored spelling.
        return persona.name
    return None


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
    project = await _get_project_or_404(project_id, db, user.id)
    # GH #70 — scope lock before validating links / inserting (merge
    # relocates audit contexts; refuses a merged-away project).
    from sessionfs.server.services.scope_locks import (
        lock_project_scope_authorized,
    )

    # GH #70 — lock, then re-authorize against the re-read project.
    await lock_project_scope_authorized(db, project, user_id=user.id)
    canonical_persona = await _assert_audit_context_links(
        project_id,
        body.ticket_id,
        body.persona_name,
        db,
        persona_org_id=await visible_persona_org_id(db, project, user_id=user.id),
    )
    ctx = RetrievalAuditContext(
        id=f"ra_{uuid.uuid4().hex[:24]}",
        project_id=project_id,
        ticket_id=body.ticket_id,
        persona_name=canonical_persona,
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
    project = await _get_project_or_404(project_id, db, user.id)
    # GH #70 — scope lock before the insert (merge relocates events).
    from sessionfs.server.services.scope_locks import (
        lock_project_scope_authorized,
    )

    # GH #70 — lock, then re-authorize against the re-read project.
    await lock_project_scope_authorized(db, project, user_id=user.id)
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
                    await _accessible_project_ids_subquery(db, user.id)
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
