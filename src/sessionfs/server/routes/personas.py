"""Agent persona CRUD routes — v0.10.1 Phase 2.

Personas are portable AI roles scoped to one project. Five routes
under `/api/v1/projects/{project_id}/personas`:

    GET    /                — list active personas
    GET    /{name}          — fetch one (includes inactive if requested)
    POST   /                — create
    PUT    /{name}          — update content/role/specializations/active
    DELETE /{name}          — soft-delete (sets is_active=false)

Soft-delete via `is_active=false` preserves history (a persona may
have been referenced by past tickets and sessions). The unique
constraint is on `(project_id, name)` regardless of is_active, so
the soft-delete keeps the name reserved — recreating would require
either reactivating or first changing the deactivated row's name.
The POST route's 409 path covers both shapes deterministically.

Tier-gated via the `agent_personas` feature flag (Pro+ tier).
Project-scoped access: caller must be the project owner OR have at
least one session in the project's git remote. Reuses
`_get_project_or_404` from wiki.py — same access shape every
project-scoped route in v0.10.x uses.
"""

from __future__ import annotations

import json
import re
import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, field_validator
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

# v0.10.1 Phase 2 Round 2 (KB 322) — explicit ASCII regex. The earlier
# `str.isalnum()` validator accepted Unicode letters/digits (so `åtlås`
# or `后端` passed), which would then propagate into ticket.assigned_to,
# CLI argv, MCP prompts, and URL path segments. Personas must stay
# CLI/MCP-friendly ASCII identifiers. The regex is anchored
# start-to-end so empty + over-long inputs both fail.
_PERSONA_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,50}$")

from sessionfs.server.auth.dependencies import get_current_user
from sessionfs.server.db.engine import get_db
from sessionfs.server.db.models import AgentPersona, Ticket, User
from sessionfs.server.routes.wiki import _get_project_or_404
from sessionfs.server.tier_gate import UserContext, check_feature, get_user_context

router = APIRouter(prefix="/api/v1/projects", tags=["personas"])


# ── Request / response models ──


class PersonaCreate(BaseModel):
    name: str
    role: str
    content: str = ""
    specializations: list[str] = []

    @field_validator("name")
    @classmethod
    def _name_shape(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("name is required")
        if not _PERSONA_NAME_RE.match(v):
            raise ValueError(
                "name must be 1-50 ASCII characters: letters, digits, "
                "dash, or underscore"
            )
        return v

    @field_validator("role")
    @classmethod
    def _role_shape(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("role is required")
        if len(v) > 100:
            raise ValueError("role must be 100 characters or fewer")
        return v


class PersonaUpdate(BaseModel):
    """Partial update — every field is optional."""

    role: str | None = None
    content: str | None = None
    specializations: list[str] | None = None
    is_active: bool | None = None

    @field_validator("role")
    @classmethod
    def _role_shape(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = v.strip()
        if not v:
            raise ValueError("role cannot be empty")
        if len(v) > 100:
            raise ValueError("role must be 100 characters or fewer")
        return v


class PersonaResponse(BaseModel):
    id: str
    project_id: str
    name: str
    role: str
    content: str
    specializations: list[str]
    is_active: bool
    version: int
    created_by: str
    created_at: datetime
    updated_at: datetime


def _to_response(p: AgentPersona) -> PersonaResponse:
    try:
        specs = json.loads(p.specializations) if p.specializations else []
        if not isinstance(specs, list):
            specs = []
    except (ValueError, TypeError):
        specs = []
    return PersonaResponse(
        id=p.id,
        project_id=p.project_id,
        name=p.name,
        role=p.role,
        content=p.content,
        specializations=specs,
        is_active=p.is_active,
        version=p.version,
        created_by=p.created_by,
        created_at=p.created_at,
        updated_at=p.updated_at,
    )


# ── Routes ──


@router.get("/{project_id}/personas", response_model=list[PersonaResponse])
async def list_personas(
    project_id: str,
    include_inactive: bool = False,
    user: User = Depends(get_current_user),
    ctx: UserContext = Depends(get_user_context),
    db: AsyncSession = Depends(get_db),
) -> list[PersonaResponse]:
    """List personas in this project. Defaults to active only."""
    check_feature(ctx, "agent_personas")
    await _get_project_or_404(project_id, db, user.id)

    stmt = select(AgentPersona).where(AgentPersona.project_id == project_id)
    if not include_inactive:
        stmt = stmt.where(AgentPersona.is_active.is_(True))
    stmt = stmt.order_by(AgentPersona.name)
    rows = (await db.execute(stmt)).scalars().all()
    return [_to_response(p) for p in rows]


@router.get(
    "/{project_id}/personas/{name}", response_model=PersonaResponse
)
async def get_persona(
    project_id: str,
    name: str,
    user: User = Depends(get_current_user),
    ctx: UserContext = Depends(get_user_context),
    db: AsyncSession = Depends(get_db),
) -> PersonaResponse:
    """Fetch a single persona. Returns 404 if not found or inactive."""
    check_feature(ctx, "agent_personas")
    await _get_project_or_404(project_id, db, user.id)

    row = (
        await db.execute(
            select(AgentPersona).where(
                AgentPersona.project_id == project_id,
                AgentPersona.name == name,
                AgentPersona.is_active.is_(True),
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(404, "Persona not found")
    return _to_response(row)


@router.post(
    "/{project_id}/personas",
    response_model=PersonaResponse,
    status_code=201,
)
async def create_persona(
    project_id: str,
    body: PersonaCreate,
    user: User = Depends(get_current_user),
    ctx: UserContext = Depends(get_user_context),
    db: AsyncSession = Depends(get_db),
) -> PersonaResponse:
    """Create a new persona. Returns 409 if a persona with this name
    already exists in this project — including a soft-deleted one (the
    UNIQUE index covers is_active=false rows too, so name reservation
    survives a delete). Caller can PUT to reactivate the existing row
    instead of recreating."""
    check_feature(ctx, "agent_personas")
    await _get_project_or_404(project_id, db, user.id)

    # v0.10.1 Phase 2 Round 2 (KB 322) — pre-check by (project_id, name)
    # so the 409 path is reachable WITHOUT relying on IntegrityError
    # interpretation. The earlier broad `except IntegrityError` mapped
    # every constraint failure to "duplicate name", which would
    # misreport unrelated failures (e.g. a concurrent project hard-
    # delete that nulled the FK target between _get_project_or_404 and
    # commit). The IntegrityError catch below is still kept as a
    # concurrency backstop for the slim race window between this
    # pre-check and the commit, but it's now narrowed to the
    # uq_persona_project_name constraint name.
    existing = (
        await db.execute(
            select(AgentPersona).where(
                AgentPersona.project_id == project_id,
                AgentPersona.name == body.name,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(
            409,
            (
                f"A persona named {body.name!r} already exists in this "
                "project ("
                + ("active" if existing.is_active else "soft-deleted")
                + ")."
            ),
        )

    persona = AgentPersona(
        id=f"per_{uuid.uuid4().hex[:16]}",
        project_id=project_id,
        name=body.name,
        role=body.role,
        content=body.content,
        specializations=json.dumps(body.specializations),
        created_by=user.id,
    )
    db.add(persona)
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        # Only translate if the failure is the persona-name UNIQUE
        # constraint. Anything else (e.g. FK violation on a
        # concurrent project delete) re-raises as a 500 with
        # original detail so we don't mask the real failure mode.
        if "uq_persona_project_name" in str(exc.orig).lower() or "uq_persona_project_name" in str(exc).lower():
            raise HTTPException(
                409,
                (
                    f"A persona named {body.name!r} already exists in this "
                    "project (active or soft-deleted) — concurrent insert race."
                ),
            ) from None
        raise
    await db.refresh(persona)
    return _to_response(persona)


@router.put(
    "/{project_id}/personas/{name}",
    response_model=PersonaResponse,
)
async def update_persona(
    project_id: str,
    name: str,
    body: PersonaUpdate,
    user: User = Depends(get_current_user),
    ctx: UserContext = Depends(get_user_context),
    db: AsyncSession = Depends(get_db),
) -> PersonaResponse:
    """Update fields on an existing persona. Looks up the row by
    (project_id, name) regardless of is_active so the caller can use
    this to reactivate a soft-deleted persona by setting
    `is_active=true`."""
    check_feature(ctx, "agent_personas")
    await _get_project_or_404(project_id, db, user.id)

    persona = (
        await db.execute(
            select(AgentPersona).where(
                AgentPersona.project_id == project_id,
                AgentPersona.name == name,
            )
        )
    ).scalar_one_or_none()
    if persona is None:
        raise HTTPException(404, "Persona not found")

    changed = False
    if body.role is not None and body.role != persona.role:
        persona.role = body.role
        changed = True
    if body.content is not None and body.content != persona.content:
        persona.content = body.content
        changed = True
    if body.specializations is not None:
        new_specs = json.dumps(body.specializations)
        if new_specs != persona.specializations:
            persona.specializations = new_specs
            changed = True
    if body.is_active is not None and body.is_active != persona.is_active:
        persona.is_active = body.is_active
        changed = True

    if changed:
        # Version is a content-evolution marker for clients caching
        # persona content (CLI/MCP). Bump only on actual mutations so
        # a no-op PUT doesn't bust caches.
        persona.version = persona.version + 1
    await db.commit()
    await db.refresh(persona)
    return _to_response(persona)


_NON_TERMINAL_TICKET_STATUSES = (
    "suggested", "open", "in_progress", "blocked", "review",
)


@router.delete(
    "/{project_id}/personas/{name}", status_code=204
)
async def delete_persona(
    project_id: str,
    name: str,
    force: bool = Query(False, description=(
        "Soft-delete even when non-terminal tickets reference this persona. "
        "Stranded tickets would otherwise need reassignment before they "
        "can be started."
    )),
    user: User = Depends(get_current_user),
    ctx: UserContext = Depends(get_user_context),
    db: AsyncSession = Depends(get_db),
) -> None:
    """Soft-delete: sets is_active=false. The row stays so historical
    tickets and sessions still resolve their persona name. The name
    remains reserved (UNIQUE is global to the project); reactivate
    via PUT or rename the deactivated row first.

    Refuses to delete (409) when non-terminal tickets reference this
    persona unless ``?force=true`` is passed (KB 339 MEDIUM). Without
    this guard a delete would silently strand open/in_progress/blocked/
    review tickets — start_ticket later refuses to load an inactive
    persona, leaving no supported workflow to recover.
    """
    check_feature(ctx, "agent_personas")
    await _get_project_or_404(project_id, db, user.id)

    persona = (
        await db.execute(
            select(AgentPersona).where(
                AgentPersona.project_id == project_id,
                AgentPersona.name == name,
                AgentPersona.is_active.is_(True),
            )
        )
    ).scalar_one_or_none()
    if persona is None:
        raise HTTPException(404, "Persona not found")

    if not force:
        active_ticket_count = (
            await db.execute(
                select(func.count()).select_from(Ticket).where(
                    Ticket.project_id == project_id,
                    Ticket.assigned_to == name,
                    Ticket.status.in_(_NON_TERMINAL_TICKET_STATUSES),
                )
            )
        ).scalar_one()
        if active_ticket_count > 0:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"Persona '{name}' is still assigned to "
                    f"{active_ticket_count} non-terminal ticket(s). "
                    "Reassign them or pass force=true to delete anyway."
                ),
            )

    persona.is_active = False
    persona.version = persona.version + 1
    await db.commit()
