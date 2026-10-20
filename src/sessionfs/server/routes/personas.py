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

GH #70 — org-level personas. A persona may instead be scoped to an
organization (`org_id` set, `project_id` NULL) and is then usable from
every project in that org. Managed under
`/api/v1/orgs/{org_id}/personas` (same five verbs); writes need org
admin standing (owners count). Project routes resolve names project
first, then org (services/personas.resolve_persona), and the project
list returns both scopes with a `scope` field.

Tier-gated via the `agent_personas` feature flag (Pro+ tier). Org
routes gate on the org's own plan.
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

from sessionfs.server.auth.dependencies import (
    AuthContext,
    assert_service_key_can_access_project,
    require_scope,
)
from sessionfs.server.db.engine import get_db
from sessionfs.server.db.models import (
    AgentPersona,
    AgentRun,
    Handoff,
    Session,
    OrgAuditEvent,
    OrgMember,
    Organization,
    Project,
    ProjectRepo,
    Ticket,
)
from sessionfs.server.roles import has_minimum_role
from sessionfs.server.routes.knowledge import _get_project_for_auth
from sessionfs.server.services.personas import (
    find_org_persona,
    find_project_persona,
    list_effective_personas,
    ACTIVE_AGENT_RUN_STATUSES,
    pending_handoffs_in_project,
    persona_scope,
    resolve_persona,
    visible_persona_org_id_for_auth,
)
from sessionfs.server.services.scope_locks import lock_project_scope_authorized, lock_scopes
from sessionfs.server.tier_gate import (
    UserContext,
    check_project_feature,
    get_user_context,
    resolve_org_plan_tier,
)
from sessionfs.server.tiers import get_features_for_tier, get_minimum_tier_for_feature

router = APIRouter(prefix="/api/v1/projects", tags=["personas"])
org_router = APIRouter(prefix="/api/v1/orgs", tags=["personas"])


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
    # GH #70 — exactly one of project_id / org_id is set; `scope` says
    # which ("project" | "org").
    project_id: str | None
    org_id: str | None = None
    scope: str = "project"
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
        org_id=p.org_id,
        scope=persona_scope(p),
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
    include_org: bool = Query(
        True,
        description=(
            "Include the personas of the project's org (scope='org'). An "
            "org persona shadowed by an active project persona of the same "
            "name is omitted."
        ),
    ),
    auth: AuthContext = Depends(require_scope("personas:read")),
    ctx: UserContext = Depends(get_user_context),
    db: AsyncSession = Depends(get_db),
) -> list[PersonaResponse]:
    """List personas usable in this project. Defaults to active only."""
    project = await _get_project_for_auth(project_id, db, auth)
    await assert_service_key_can_access_project(db, auth, project)
    await check_project_feature(ctx, "agent_personas", project, db)

    # GH #70 — org personas only for current members of the project's
    # current org (service keys: their own org). Project access alone is
    # not enough: it survives a transfer to another org.
    visible_org = (
        await visible_persona_org_id_for_auth(db, project, auth) if include_org else None
    )
    rows = await list_effective_personas(
        db,
        project,
        org_id=visible_org,
        include_inactive=include_inactive,
    )
    return [_to_response(e.persona) for e in rows]


@router.get(
    "/{project_id}/personas/{name}", response_model=PersonaResponse
)
async def get_persona(
    project_id: str,
    name: str,
    auth: AuthContext = Depends(require_scope("personas:read")),
    ctx: UserContext = Depends(get_user_context),
    db: AsyncSession = Depends(get_db),
) -> PersonaResponse:
    """Fetch a single persona. Returns 404 if not found or inactive.

    Resolves project first, then the project's org (GH #70)."""
    project = await _get_project_for_auth(project_id, db, auth)
    await assert_service_key_can_access_project(db, auth, project)
    await check_project_feature(ctx, "agent_personas", project, db)

    row = await resolve_persona(
        db,
        project_id,
        name,
        org_id=await visible_persona_org_id_for_auth(db, project, auth),
        active_only=True,
    )
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
    auth: AuthContext = Depends(require_scope("personas:write")),
    ctx: UserContext = Depends(get_user_context),
    db: AsyncSession = Depends(get_db),
) -> PersonaResponse:
    """Create a new persona. Returns 409 if a persona with this name
    already exists in this project — including a soft-deleted one (the
    UNIQUE index covers is_active=false rows too, so name reservation
    survives a delete). Caller can PUT to reactivate the existing row
    instead of recreating."""
    user = auth.user
    project = await _get_project_for_auth(project_id, db, auth)
    await assert_service_key_can_access_project(db, auth, project)
    await check_project_feature(ctx, "agent_personas", project, db)

    # GH #70 — project-scoped lock so two concurrent creates of `atlas`
    # and `Atlas` cannot both pass the case-insensitive pre-check below
    # (a phantom insert no row lock can stop), and so creates serialize
    # with the project-transfer guard.
    # GH #70 — lock, then re-authorize against the re-read project.
    await lock_project_scope_authorized(db, project, auth=auth, ctx=ctx, feature="agent_personas")

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
    # Codex v0.10.23 R1 LOW (tk_884b2321fdb74170) — reject case-
    # insensitive duplicates too. Without this, a project could hold
    # both `atlas` and `Atlas` as active personas, and the ticket
    # resolver `_resolve_persona_name` would have to choose between
    # them ambiguously. The contract is now "persona names are
    # case-insensitive-unique per project" — enforced at create so
    # the ticket-side normalization never sees two rivals.
    existing = (
        await db.execute(
            select(AgentPersona)
            .where(
                AgentPersona.project_id == project_id,
                func.lower(AgentPersona.name) == body.name.lower(),
            )
            .order_by(AgentPersona.id)
        )
    ).scalars().first()
    if existing is not None:
        same_case = existing.name == body.name
        msg = (
            f"A persona named {existing.name!r} already exists in this "
            "project ("
            + ("active" if existing.is_active else "soft-deleted")
            + ")."
        )
        if not same_case:
            msg += (
                f" Persona names are case-insensitive-unique — "
                f"{body.name!r} collides with the existing record."
            )
        raise HTTPException(409, msg)

    persona = AgentPersona(
        id=f"per_{uuid.uuid4().hex[:16]}",
        project_id=project_id,
        name=body.name,
        role=body.role,
        content=body.content,
        specializations=json.dumps(body.specializations),
        created_by=user.id,
        actor_type=auth.actor_type,
        service_key_id=auth.service_key_id,
        service_key_name=auth.service_key_name,
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
    force: bool = Query(False, description=(
        "With is_active=false: deactivate even when non-terminal tickets or "
        "queued/running agent runs still need this persona (same guard as "
        "DELETE)."
    )),
    auth: AuthContext = Depends(require_scope("personas:write")),
    ctx: UserContext = Depends(get_user_context),
    db: AsyncSession = Depends(get_db),
) -> PersonaResponse:
    """Update fields on an existing persona. Looks up the row by
    (project_id, name) regardless of is_active so the caller can use
    this to reactivate a soft-deleted persona by setting
    `is_active=true`."""
    project = await _get_project_for_auth(project_id, db, auth)
    await assert_service_key_can_access_project(db, auth, project)
    await check_project_feature(ctx, "agent_personas", project, db)

    # GH #70 — serialize with creates / the transfer guard (reactivation
    # changes the effective persona set).
    # GH #70 — lock, then re-authorize against the re-read project.
    await lock_project_scope_authorized(db, project, auth=auth, ctx=ctx, feature="agent_personas")
    # GH #70 — case-insensitive like every other persona lookup.
    persona = await find_project_persona(db, project_id, name)
    if persona is None:
        raise HTTPException(
            404, await _project_miss_detail(db, project, name, auth)
        )

    # GH #70 — deactivating via PUT is a delete: same stranding guard.
    if body.is_active is False and persona.is_active and not force:
        await _assert_project_persona_not_needed(db, project, persona.name, auth)

    _apply_update(persona, body, auth)
    await db.commit()
    await db.refresh(persona)
    return _to_response(persona)


async def _project_miss_detail(
    db: AsyncSession, project: Project, name: str, auth: AuthContext
) -> str:
    """404 detail for a project-scoped write that missed. When the name is
    an org persona the caller may see (current member of the project's
    org), say where it is managed instead of a bare 'not found'."""
    visible_org = await visible_persona_org_id_for_auth(db, project, auth)
    if visible_org:
        org_row = await find_org_persona(db, visible_org, name)
        if org_row is not None:
            return (
                f"Persona {name!r} is an org-level persona; manage it under "
                f"/api/v1/orgs/{project.org_id}/personas (org admins), or "
                "create a project persona with the same name to override it."
            )
    return "Persona not found"


def _apply_update(
    persona: AgentPersona, body: PersonaUpdate, auth: AuthContext
) -> bool:
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
        persona.actor_type = auth.actor_type
        persona.service_key_id = auth.service_key_id
        persona.service_key_name = auth.service_key_name
    return changed


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
    auth: AuthContext = Depends(require_scope("personas:write")),
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
    project = await _get_project_for_auth(project_id, db, auth)
    await assert_service_key_can_access_project(db, auth, project)
    await check_project_feature(ctx, "agent_personas", project, db)

    # GH #70 — lock, then re-authorize against the re-read project.
    await lock_project_scope_authorized(db, project, auth=auth, ctx=ctx, feature="agent_personas")
    persona = await find_project_persona(db, project_id, name, active_only=True)
    if persona is None:
        raise HTTPException(
            404, await _project_miss_detail(db, project, name, auth)
        )
    name = persona.name

    if not force:
        await _assert_project_persona_not_needed(db, project, name, auth)

    persona.is_active = False
    persona.version = persona.version + 1
    persona.actor_type = auth.actor_type
    persona.service_key_id = auth.service_key_id
    persona.service_key_name = auth.service_key_name
    await db.commit()


async def _assert_project_persona_not_needed(
    db: AsyncSession, project: Project, name: str, auth: AuthContext
) -> None:
    """Shared stranding guard for deactivating a project persona (DELETE,
    and PUT with is_active=false). 409 while non-terminal tickets or
    queued/running agent runs in the project still need it.

    GH #70 — when an active org persona of the same name exists, those
    references fall back to it, so nothing is stranded. Only an org the
    CALLER may see counts (visible_persona_org_id_for_auth): for anyone
    else the hidden org persona is treated as absent, so the response does
    not reveal whether a private org persona of that name exists.
    """
    project_id = project.id
    visible_org = await visible_persona_org_id_for_auth(db, project, auth)
    if visible_org:
        org_fallback = await find_org_persona(
            db, visible_org, name, active_only=True
        )
        if org_fallback is not None:
            return
    active_ticket_count = (
        await db.execute(
            select(func.count()).select_from(Ticket).where(
                Ticket.project_id == project_id,
                func.lower(Ticket.assigned_to) == name.lower(),
                Ticket.status.in_(_NON_TERMINAL_TICKET_STATUSES),
            )
        )
    ).scalar_one()
    # GH #70 — queued/running agent runs re-resolve their persona at
    # start, so they would be stranded too.
    active_run_count = (
        await db.execute(
            select(func.count()).select_from(AgentRun).where(
                AgentRun.project_id == project_id,
                func.lower(AgentRun.persona_name) == name.lower(),
                AgentRun.status.in_(ACTIVE_AGENT_RUN_STATUSES),
            )
        )
    ).scalar_one()
    # GH #70 — pending handoffs from this project's sessions carry the
    # persona into the recipient's bundle on claim.
    pending_handoff_count = (
        await db.execute(
            select(func.count()).select_from(Handoff).where(
                *pending_handoffs_in_project(project_id),
                func.lower(Handoff.persona_name) == name.lower(),
            )
        )
    ).scalar_one()
    if active_ticket_count > 0 or active_run_count > 0 or pending_handoff_count > 0:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Persona '{name}' is still assigned to "
                f"{active_ticket_count} non-terminal ticket(s), "
                f"{active_run_count} queued/running agent run(s) and "
                f"{pending_handoff_count} pending handoff(s). "
                "Reassign them or pass force=true to delete anyway."
            ),
        )


# ── Org-level personas (GH #70) ──
#
# /api/v1/orgs/{org_id}/personas — same five verbs as the project routes.
#
# Authorization:
#   * user keys: reads need membership of THIS org (any role); writes need
#     org admin standing via has_minimum_role(role, "admin") — owners count.
#     Membership is checked against {org_id} itself, never the caller's
#     primary org, so multi-org admins can manage each of their orgs.
#   * service keys: personas:read / personas:write scope (require_scope) and
#     the key's org must be {org_id}. A key restricted to a project
#     allowlist may read but not write: an org-wide persona reaches every
#     project in the org, beyond the key's allowlist.
#   * every miss (unknown org, non-member, other org's key) is the same 404,
#     so the routes never confirm an org exists to an outsider.
# Tier: the org's own plan must include `agent_personas` (checked after
# authorization so the plan lookup leaks nothing).


_ORG_NOT_FOUND = "Organization not found"


async def _authorize_org_personas(
    db: AsyncSession,
    auth: AuthContext,
    org_id: str,
    *,
    write: bool,
) -> tuple[Organization, str | None]:
    """Return (org, caller_role). caller_role is None for service keys."""
    org = await db.get(Organization, org_id)
    role: str | None = None
    if auth.key_kind == "service":
        if org is None or auth.org_id != org_id:
            raise HTTPException(404, _ORG_NOT_FOUND)
        if write and auth.project_ids is not None:
            raise HTTPException(
                status_code=403,
                detail={
                    "error": "service_key_project_restricted",
                    "message": (
                        "A service key restricted to specific projects cannot "
                        "write org-level personas."
                    ),
                },
            )
    else:
        membership = (
            await db.execute(
                select(OrgMember.role).where(
                    OrgMember.org_id == org_id,
                    OrgMember.user_id == auth.user.id,
                )
            )
        ).scalars().first()
        if org is None or membership is None:
            raise HTTPException(404, _ORG_NOT_FOUND)
        if write and not has_minimum_role(membership, "admin"):
            raise HTTPException(
                status_code=403,
                detail={
                    "error": "insufficient_role",
                    "current_role": membership,
                    "required_role": "admin",
                    "message": "Managing org-level personas requires the admin role.",
                },
            )
        role = membership

    plan = await resolve_org_plan_tier(org_id, db)
    if "agent_personas" not in get_features_for_tier(plan):
        required = get_minimum_tier_for_feature("agent_personas")
        raise HTTPException(
            status_code=403,
            detail={
                "error": "upgrade_required",
                "feature": "agent_personas",
                "plan_scope": "org",
                "current_tier": plan.value,
                "required_tier": required,
                "upgrade_url": "https://sessionfs.dev/pricing",
                "message": f"This feature requires the org's plan to be {required} or above.",
            },
        )
    return org, role


def _persona_snapshot(p: AgentPersona) -> dict:
    return {
        "name": p.name,
        "role": p.role,
        "is_active": bool(p.is_active) if p.is_active is not None else True,
        "version": p.version if p.version is not None else 1,
    }


def _org_audit(
    db: AsyncSession,
    *,
    org: Organization,
    auth: AuthContext,
    actor_role: str | None,
    event_type: str,
    persona: AgentPersona,
    before: dict | None,
    after: dict | None,
) -> None:
    """Append-only OrgAuditEvent for an org-persona mutation."""
    db.add(
        OrgAuditEvent(
            id=f"oae_{uuid.uuid4().hex[:24]}",
            org_id=org.id,
            org_name_snapshot=org.name,
            event_type=event_type,
            actor_user_id=auth.user.id,
            actor_email_snapshot=auth.user.email,
            actor_role_at_time=actor_role if actor_role else auth.actor_type,
            target_type="persona",
            target_id=persona.id,
            before=json.dumps(before) if before is not None else None,
            after=json.dumps(after) if after is not None else None,
        )
    )


@org_router.get("/{org_id}/personas", response_model=list[PersonaResponse])
async def list_org_personas(
    org_id: str,
    include_inactive: bool = False,
    auth: AuthContext = Depends(require_scope("personas:read")),
    db: AsyncSession = Depends(get_db),
) -> list[PersonaResponse]:
    """List the org's own personas (org scope only). Active only by default."""
    await _authorize_org_personas(db, auth, org_id, write=False)
    stmt = select(AgentPersona).where(AgentPersona.org_id == org_id)
    if not include_inactive:
        stmt = stmt.where(AgentPersona.is_active.is_(True))
    rows = (await db.execute(stmt.order_by(AgentPersona.name))).scalars().all()
    return [_to_response(p) for p in rows]


@org_router.get("/{org_id}/personas/{name}", response_model=PersonaResponse)
async def get_org_persona(
    org_id: str,
    name: str,
    auth: AuthContext = Depends(require_scope("personas:read")),
    db: AsyncSession = Depends(get_db),
) -> PersonaResponse:
    """Fetch one active org persona by exact name."""
    await _authorize_org_personas(db, auth, org_id, write=False)
    row = await find_org_persona(db, org_id, name, active_only=True)
    if row is None:
        raise HTTPException(404, "Persona not found")
    return _to_response(row)


@org_router.post(
    "/{org_id}/personas", response_model=PersonaResponse, status_code=201
)
async def create_org_persona(
    org_id: str,
    body: PersonaCreate,
    auth: AuthContext = Depends(require_scope("personas:write")),
    db: AsyncSession = Depends(get_db),
) -> PersonaResponse:
    """Create an org-level persona. 409 on a case-insensitive name clash
    with another persona of this org (active or soft-deleted). A project
    persona with the same name is NOT a clash — it overrides this one
    inside its project."""
    await _authorize_org_personas(db, auth, org_id, write=True)
    await lock_scopes(db, org_ids=[org_id])
    # Re-read standing under the org lock (role changes take it too).
    org, role = await _authorize_org_personas(db, auth, org_id, write=True)
    existing = await find_org_persona(db, org_id, body.name)
    if existing is not None:
        raise HTTPException(
            409,
            (
                f"An org persona named {existing.name!r} already exists in this "
                "org (" + ("active" if existing.is_active else "soft-deleted")
                + "). Persona names are case-insensitive-unique per org."
            ),
        )
    persona = AgentPersona(
        id=f"per_{uuid.uuid4().hex[:16]}",
        project_id=None,
        org_id=org_id,
        name=body.name,
        role=body.role,
        content=body.content,
        specializations=json.dumps(body.specializations),
        created_by=auth.user.id,
        actor_type=auth.actor_type,
        service_key_id=auth.service_key_id,
        service_key_name=auth.service_key_name,
    )
    db.add(persona)
    _org_audit(
        db, org=org, auth=auth, actor_role=role, event_type="persona_created",
        persona=persona, before=None, after=_persona_snapshot(persona),
    )
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        text_ = f"{exc.orig} {exc}".lower()
        if "uq_persona_org_lower_name" in text_ or "agent_personas.org_id" in text_:
            raise HTTPException(
                409,
                (
                    f"An org persona named {body.name!r} already exists in this "
                    "org — concurrent insert race."
                ),
            ) from None
        raise
    await db.refresh(persona)
    return _to_response(persona)


@org_router.put("/{org_id}/personas/{name}", response_model=PersonaResponse)
async def update_org_persona(
    org_id: str,
    name: str,
    body: PersonaUpdate,
    force: bool = Query(False, description=(
        "With is_active=false: deactivate even when non-terminal tickets or "
        "queued/running agent runs still need this persona (same guard as "
        "DELETE)."
    )),
    auth: AuthContext = Depends(require_scope("personas:write")),
    db: AsyncSession = Depends(get_db),
) -> PersonaResponse:
    """Update an org persona (active or soft-deleted — `is_active=true`
    reactivates). `is_active=false` runs the same stranding guard and audit
    as DELETE."""
    await _authorize_org_personas(db, auth, org_id, write=True)
    await lock_scopes(db, org_ids=[org_id])
    # Re-read standing under the org lock (role changes take it too).
    org, role = await _authorize_org_personas(db, auth, org_id, write=True)
    persona = await find_org_persona(db, org_id, name)
    if persona is None:
        raise HTTPException(404, "Persona not found")
    deactivating = body.is_active is False and bool(persona.is_active)
    if deactivating and not force:
        await _assert_org_persona_not_needed(db, org_id, persona.name)
    before = _persona_snapshot(persona)
    if _apply_update(persona, body, auth):
        _org_audit(
            db, org=org, auth=auth, actor_role=role,
            event_type="persona_deleted" if deactivating else "persona_updated",
            persona=persona, before=before, after=_persona_snapshot(persona),
        )
    await db.commit()
    await db.refresh(persona)
    return _to_response(persona)


@org_router.delete("/{org_id}/personas/{name}", status_code=204)
async def delete_org_persona(
    org_id: str,
    name: str,
    force: bool = Query(False, description=(
        "Soft-delete even when non-terminal tickets in the org's projects "
        "still resolve to this persona."
    )),
    auth: AuthContext = Depends(require_scope("personas:write")),
    db: AsyncSession = Depends(get_db),
) -> None:
    """Soft-delete an org persona. Refuses (409) while non-terminal tickets
    in any of the org's projects are assigned to this name AND that project
    has no active project persona of the same name to fall back to —
    unless ``?force=true``."""
    await _authorize_org_personas(db, auth, org_id, write=True)
    # GH #70 — serializes with ticket assignment in the org's projects
    # (which takes project then org lock) and with project transfers.
    await lock_scopes(db, org_ids=[org_id])
    # Re-read standing under the org lock (role changes take it too).
    org, role = await _authorize_org_personas(db, auth, org_id, write=True)
    persona = await find_org_persona(db, org_id, name, active_only=True)
    if persona is None:
        raise HTTPException(404, "Persona not found")

    if not force:
        await _assert_org_persona_not_needed(db, org_id, persona.name)

    before = _persona_snapshot(persona)
    persona.is_active = False
    persona.version = persona.version + 1
    persona.actor_type = auth.actor_type
    persona.service_key_id = auth.service_key_id
    persona.service_key_name = auth.service_key_name
    _org_audit(
        db, org=org, auth=auth, actor_role=role, event_type="persona_deleted",
        persona=persona, before=before, after=_persona_snapshot(persona),
    )
    await db.commit()


async def _assert_org_persona_not_needed(
    db: AsyncSession, org_id: str, persona_name: str
) -> None:
    """Shared stranding guard for deactivating an org persona (DELETE, and
    PUT with is_active=false): 409 while non-terminal tickets or
    queued/running agent runs in the org's projects need it and that
    project has no active same-named project persona to fall back to."""
    overriding_projects = select(AgentPersona.project_id).where(
        AgentPersona.project_id.is_not(None),
        func.lower(AgentPersona.name) == persona_name.lower(),
        AgentPersona.is_active.is_(True),
    )
    stranded = (
        await db.execute(
            select(func.count())
            .select_from(Ticket)
            .join(Project, Project.id == Ticket.project_id)
            .where(
                Project.org_id == org_id,
                func.lower(Ticket.assigned_to) == persona_name.lower(),
                Ticket.status.in_(_NON_TERMINAL_TICKET_STATUSES),
                Ticket.project_id.not_in(overriding_projects),
            )
        )
    ).scalar_one()
    stranded_runs = (
        await db.execute(
            select(func.count())
            .select_from(AgentRun)
            .join(Project, Project.id == AgentRun.project_id)
            .where(
                Project.org_id == org_id,
                func.lower(AgentRun.persona_name) == persona_name.lower(),
                AgentRun.status.in_(ACTIVE_AGENT_RUN_STATUSES),
                AgentRun.project_id.not_in(overriding_projects),
            )
        )
    ).scalar_one()
    # Pending handoffs from sessions of the org's projects (same fallback
    # rule: a project with an active same-named project persona absorbs it).
    # The durable anchor (source_project_id, migration 072) attributes
    # handoffs whose source session has project_id NULL (legacy sessions);
    # rows without an anchor fall back to the session's project_id, then to
    # the legacy remote resolution (project_repos first, then
    # projects.git_remote_normalized) that pending_handoffs_in_project uses —
    # so a legacy remote-only session (both project links NULL) still
    # strands the persona instead of being silently dropped at claim time.
    repo_project = (
        select(Project.id)
        .join(ProjectRepo, ProjectRepo.project_id == Project.id)
        .where(
            ProjectRepo.git_remote_normalized == Session.git_remote_normalized,
            Project.org_id == org_id,
        )
        .limit(1)
        # Correlate only `Session` (the outer joined row); keep Project and
        # ProjectRepo as this subquery's own FROM so auto-correlation doesn't
        # strip them and leave the subquery with no FROM clauses.
        .correlate(Session)
        .scalar_subquery()
    )
    direct_project = (
        select(Project.id)
        .where(
            Project.git_remote_normalized == Session.git_remote_normalized,
            Project.org_id == org_id,
        )
        .limit(1)
        .correlate(Session)
        .scalar_subquery()
    )
    handoff_project = func.coalesce(
        Handoff.source_project_id,
        Session.project_id,
        repo_project,
        direct_project,
    )
    stranded_handoffs = (
        await db.execute(
            select(func.count())
            .select_from(Handoff)
            .join(Session, Session.id == Handoff.session_id)
            .join(Project, Project.id == handoff_project)
            .where(
                Project.org_id == org_id,
                Handoff.status == "pending",
                func.lower(Handoff.persona_name) == persona_name.lower(),
                handoff_project.not_in(overriding_projects),
            )
        )
    ).scalar_one()
    if stranded > 0 or stranded_runs > 0 or stranded_handoffs > 0:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Org persona '{persona_name}' is still assigned to "
                f"{stranded} non-terminal ticket(s), {stranded_runs} "
                f"queued/running agent run(s) and {stranded_handoffs} pending "
                "handoff(s) across the org's projects. "
                "Reassign them or pass force=true to delete anyway."
            ),
        )
