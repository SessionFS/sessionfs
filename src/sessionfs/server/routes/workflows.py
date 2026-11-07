"""Record-only workflow templates and workflow loops (GitHub #120, W1).

Templates (immutable versions; an edit is a new version):

- ``GET/POST /api/v1/projects/{p}/workflow-templates``
- ``GET/PUT/DELETE /api/v1/projects/{p}/workflow-templates/{id}``
- ``GET/POST /api/v1/orgs/{org}/workflow-templates``
- ``GET/PUT/DELETE /api/v1/orgs/{org}/workflow-templates/{id}``

Workflows (a workflow run is a loop):

- ``POST /api/v1/projects/{p}/workflows``            start from a template
- ``GET  /api/v1/projects/{p}/workflows/{loop}/plan`` steps, states, briefs

Project templates are read by project readers and written by project
editors; org templates are read by org members and written by org admins
(people only). Service keys use ``loops:read`` / ``loops:write`` inside their
org and project allowlist. Every miss is one 404 body; visibility is checked
before the plan gate. SessionFS never starts an agent: the caller's
orchestrator runs the steps and reports through the loop worker routes.
"""

from __future__ import annotations

import math
import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server.auth.dependencies import (
    AuthContext,
    assert_service_key_can_access_project,
    require_scope,
)
from sessionfs.server.auth.project_access import (
    user_can_access_project,
    user_can_edit_project,
)
from sessionfs.server.db.engine import get_db
from sessionfs.server.db.models import (
    Organization,
    OrgMember,
    Project,
    Ticket,
    WorkflowTemplate,
)
from sessionfs.server.roles import has_minimum_role
from sessionfs.server.routes.loop_checklist import _sanitize_text
from sessionfs.server.routes.loops import (
    _LOOP_NOT_FOUND,
    _active_checklist_items,
    _load_loop_config,
    _readable_project,
    _require_dollar_budget,
    _require_loop_downstream_scopes,
    _reviewer_is_trusted,
)
from sessionfs.server.services import workflows as wf
from sessionfs.server.services.personas import visible_persona_org_id_for_auth
from sessionfs.server.services.scope_locks import lock_project_scope_authorized
from sessionfs.server.tier_gate import (
    UserContext,
    check_project_feature,
    get_features_for_tier,
    get_user_context,
    raise_project_upgrade_required,
    resolve_org_plan_tier,
)

project_router = APIRouter(prefix="/api/v1/projects", tags=["workflows"])
org_router = APIRouter(prefix="/api/v1/orgs", tags=["workflows"])

_TEMPLATE_NOT_FOUND = "Workflow template not found"
# Active templates per scope (a project, or an org). Lists return every
# active template, so this cap is also what keeps a list complete: a project
# list is at most its own cap plus its org's.
MAX_ACTIVE_TEMPLATES = 100
_LIST_LIMIT = 2 * MAX_ACTIVE_TEMPLATES


def _nf() -> HTTPException:
    return HTTPException(404, _TEMPLATE_NOT_FOUND)


# ── Bodies ──


class TemplateWrite(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=100)
    definition: dict[str, Any]

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        v = _sanitize_text(v)[:100]
        if not v:
            raise ValueError("name is required")
        return v


class WorkflowStart(BaseModel):
    model_config = ConfigDict(extra="forbid")

    template_id: str = Field(min_length=1, max_length=64)
    ticket_id: str | None = Field(default=None, max_length=64)
    title: str = Field(default="", max_length=200)
    description: str = Field(default="", max_length=20000)
    reviewer_persona: str | None = Field(default=None, max_length=50)
    token_budget: StrictInt | None = Field(None, ge=1)
    cost_budget_usd: float | None = Field(None, gt=0)
    max_review_rounds: StrictInt | None = Field(None, ge=1)

    @field_validator("cost_budget_usd", mode="before")
    @classmethod
    def _no_bool(cls, v: Any) -> Any:
        if isinstance(v, bool):
            raise ValueError("cost_budget_usd must be a finite number, not a boolean")
        return v

    @field_validator("cost_budget_usd")
    @classmethod
    def _finite(cls, v: float | None) -> float | None:
        if v is not None and not math.isfinite(v):
            raise ValueError("cost_budget_usd must be a finite number")
        return v

    @field_validator("reviewer_persona")
    @classmethod
    def _persona(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = v.strip()
        return v or None


# ── Shared helpers ──


def _definition_or_422(raw: dict[str, Any]) -> wf.WorkflowDefinition:
    try:
        return wf.parse_definition(raw)
    except wf.WorkflowDefinitionError as exc:
        raise HTTPException(
            422, {"error": "invalid_workflow_definition", "message": str(exc)}
        )


async def _clean_template(
    db: AsyncSession,
    auth: AuthContext,
    body: TemplateWrite,
    *,
    scope_org_id: str | None,
) -> tuple[str, wf.WorkflowDefinition]:
    """Validate the definition, apply the scope's DLP policy (strictest
    union of the caller's orgs and the scope org, as meetings do) to the
    name, step titles and briefs, and check approval-policy gates belong to
    the scope. Fails closed: a scan error is a 422, never unscanned text."""
    from sessionfs.server.routes.meetings import _dlp_scan_content, _upload_dlp_policy

    definition = _definition_or_422(body.definition)
    try:
        policy = await _upload_dlp_policy(db, auth, scope_org_id)

        def scan(value: str) -> str:
            return _dlp_scan_content(value, policy)[0] if value else value

        name = scan(body.name)
        raw = definition.model_dump(mode="json")
        for step in raw["steps"]:
            step["title"] = scan(step["title"])
            step["brief_template"] = scan(step["brief_template"])
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(
            422,
            {
                "error": "dlp_unavailable",
                "message": "The template could not be scanned for sensitive data, so it was not saved.",
            },
        )
    definition = _definition_or_422(raw)
    try:
        await wf.validate_policy_scope(db, definition, org_id=scope_org_id)
    except wf.WorkflowDefinitionError as exc:
        raise HTTPException(
            422, {"error": "invalid_workflow_definition", "message": str(exc)}
        )
    name = _sanitize_text(name)[:100] or body.name
    return name, definition


async def _check_template_cap(db: AsyncSession, scope) -> None:
    """409 ``template_limit`` once a scope has ``MAX_ACTIVE_TEMPLATES``
    active templates (the caller holds the scope's row lock)."""
    from sqlalchemy import func

    count = (
        await db.execute(
            select(func.count())
            .select_from(WorkflowTemplate)
            .where(scope, WorkflowTemplate.active.is_(True))
        )
    ).scalar_one()
    if count >= MAX_ACTIVE_TEMPLATES:
        raise HTTPException(
            409,
            {
                "error": "template_limit",
                "limit": MAX_ACTIVE_TEMPLATES,
                "message": (
                    f"This scope already has {MAX_ACTIVE_TEMPLATES} active workflow "
                    "templates; disable one before creating another."
                ),
            },
        )


async def _list_rows(db: AsyncSession, scope, response: Response) -> list[WorkflowTemplate]:
    """Every active template in ``scope``, newest first. The per-scope cap
    keeps this complete; should rows ever exceed the bound anyway, the
    response says so (``X-Result-Truncated: true``) instead of dropping
    them silently."""
    rows = list(
        (
            await db.execute(
                select(WorkflowTemplate)
                .where(scope, WorkflowTemplate.active.is_(True))
                .order_by(WorkflowTemplate.created_at.desc(), WorkflowTemplate.id.desc())
                .limit(_LIST_LIMIT + 1)
            )
        ).scalars().all()
    )
    truncated = len(rows) > _LIST_LIMIT
    response.headers["X-Result-Truncated"] = "true" if truncated else "false"
    return rows[:_LIST_LIMIT]


def _template_view(row: WorkflowTemplate, *, with_definition: bool = True) -> dict[str, Any]:
    out: dict[str, Any] = {
        "id": row.id,
        "lineage_id": row.lineage_id,
        "version": row.version,
        "name": row.name,
        "scope": "org" if row.org_id else "project",
        "org_id": row.org_id,
        "project_id": row.project_id,
        "active": bool(row.active),
        "supersedes_id": row.supersedes_id,
        "created_by_user_id": row.created_by_user_id,
        "actor_type": row.actor_type,
        "service_key_name": row.service_key_name,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }
    if with_definition:
        try:
            out["definition"] = wf.parse_definition(row.definition).model_dump(mode="json")
        except wf.WorkflowDefinitionError:
            out["definition"] = None
    return out


def _new_row(
    auth: AuthContext,
    *,
    name: str,
    definition: wf.WorkflowDefinition,
    org_id: str | None,
    project_id: str | None,
    lineage_id: str | None = None,
    version: int = 1,
    supersedes_id: str | None = None,
) -> WorkflowTemplate:
    return WorkflowTemplate(
        id=f"wft_{uuid.uuid4().hex[:16]}",
        org_id=org_id,
        project_id=project_id,
        name=name,
        lineage_id=lineage_id or f"wfl_{uuid.uuid4().hex[:16]}",
        version=version,
        definition=wf.dump_definition(definition),
        active=True,
        supersedes_id=supersedes_id,
        created_by_user_id=auth.user.id,
        actor_type=auth.actor_type,
        service_key_id=auth.service_key_id,
        service_key_name=auth.service_key_name,
        created_at=datetime.now(timezone.utc),
    )


async def _new_version(
    db: AsyncSession,
    auth: AuthContext,
    current: WorkflowTemplate,
    *,
    name: str,
    definition: wf.WorkflowDefinition,
) -> dict[str, Any]:
    """Deactivate ``current`` (guarded) and insert version + 1."""
    res = await db.execute(
        update(WorkflowTemplate)
        .where(WorkflowTemplate.id == current.id, WorkflowTemplate.active.is_(True))
        .values(active=False)
    )
    if res.rowcount != 1:
        await db.rollback()
        raise HTTPException(
            409,
            {
                "error": "workflow_template_superseded",
                "message": "Only the active version of a template can be edited.",
            },
        )
    row = _new_row(
        auth,
        name=name,
        definition=definition,
        org_id=current.org_id,
        project_id=current.project_id,
        lineage_id=current.lineage_id,
        version=current.version + 1,
        supersedes_id=current.id,
    )
    db.add(row)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(
            409,
            {
                "error": "workflow_template_superseded",
                "message": "Another edit created this version first.",
            },
        )
    await db.refresh(row)
    return _template_view(row)


async def _deactivate(db: AsyncSession, row: WorkflowTemplate) -> Response:
    """Guarded: only the still-active version is deactivated; a version that
    was already disabled or superseded is ``409 template_stale``.

    Deliberately NOT plan-gated: deactivating is revoke-like, and revoke
    paths stay open after a plan downgrade (0.29.4 convention)."""
    res = await db.execute(
        update(WorkflowTemplate)
        .where(WorkflowTemplate.id == row.id, WorkflowTemplate.active.is_(True))
        .values(active=False)
    )
    if res.rowcount != 1:
        await db.rollback()
        raise HTTPException(
            409,
            {
                "error": "template_stale",
                "message": "This template version is no longer active.",
            },
        )
    await db.commit()
    return Response(status_code=204)


# ── Project scope ──


async def _project_for_read(
    project_id: str, db: AsyncSession, auth: AuthContext, detail: str
) -> Project:
    project = (
        await db.execute(select(Project).where(Project.id == project_id))
    ).scalar_one_or_none()
    if project is None:
        raise HTTPException(404, detail)
    if auth.key_kind == "service":
        await assert_service_key_can_access_project(
            db, auth, project, opaque_404_detail=detail
        )
    elif not await user_can_access_project(db, auth.user.id, project):
        raise HTTPException(404, detail)
    return project


async def _project_for_write(
    project_id: str, db: AsyncSession, auth: AuthContext, detail: str
) -> Project:
    """Visibility first (one 404 body), then editor standing (403)."""
    project = await _project_for_read(project_id, db, auth, detail)
    if auth.key_kind != "service" and not await user_can_edit_project(
        db, auth.user.id, project
    ):
        from sessionfs.server.auth.project_access import project_edit_forbidden

        raise project_edit_forbidden()
    return project


async def _project_template(
    db: AsyncSession, project: Project, template_id: str
) -> WorkflowTemplate:
    row = await db.get(WorkflowTemplate, template_id)
    if row is None or row.project_id != project.id:
        raise _nf()
    return row


async def _project_write_gated(
    project_id: str, db: AsyncSession, auth: AuthContext, ctx: UserContext
) -> Project:
    """Template writes are Review Loop writes: visibility (one 404 body),
    editor standing, the ``agent_tickets`` plan gate on the project's plan,
    then the project scope lock (re-authorized) so the active-template cap
    is checked serially."""
    project = await _project_for_write(project_id, db, auth, _TEMPLATE_NOT_FOUND)
    await check_project_feature(ctx, "agent_tickets", project, db)
    await lock_project_scope_authorized(
        db, project, auth=auth, ctx=ctx, feature="agent_tickets"
    )
    return project


@project_router.get("/{project_id}/workflow-templates")
async def list_project_templates(
    project_id: str,
    response: Response,
    auth: AuthContext = Depends(require_scope("loops:read")),
    db: AsyncSession = Depends(get_db),
) -> list[dict]:
    """Every active template usable in this project: its own plus the org's
    (when the caller can see the org's), newest first. Complete by
    construction (each scope holds at most ``MAX_ACTIVE_TEMPLATES``)."""
    project = await _project_for_read(project_id, db, auth, _TEMPLATE_NOT_FOUND)
    org_id = await visible_persona_org_id_for_auth(db, project, auth)
    scope = WorkflowTemplate.project_id == project.id
    if org_id:
        scope = scope | (WorkflowTemplate.org_id == org_id)
    rows = await _list_rows(db, scope, response)
    return [_template_view(r) for r in rows]


@project_router.post("/{project_id}/workflow-templates", status_code=201)
async def create_project_template(
    project_id: str,
    body: TemplateWrite,
    auth: AuthContext = Depends(require_scope("loops:write")),
    ctx: UserContext = Depends(get_user_context),
    db: AsyncSession = Depends(get_db),
) -> dict:
    project = await _project_write_gated(project_id, db, auth, ctx)
    name, definition = await _clean_template(db, auth, body, scope_org_id=project.org_id)
    await _check_template_cap(db, WorkflowTemplate.project_id == project.id)
    row = _new_row(
        auth, name=name, definition=definition, org_id=None, project_id=project.id
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return _template_view(row)


@project_router.get("/{project_id}/workflow-templates/{template_id}")
async def get_project_template(
    project_id: str,
    template_id: str,
    auth: AuthContext = Depends(require_scope("loops:read")),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Any version by id: the project's own, or its org's when visible."""
    project = await _project_for_read(project_id, db, auth, _TEMPLATE_NOT_FOUND)
    row = await _usable_template(db, auth, project, template_id)
    return _template_view(row)


@project_router.put("/{project_id}/workflow-templates/{template_id}")
async def update_project_template(
    project_id: str,
    template_id: str,
    body: TemplateWrite,
    auth: AuthContext = Depends(require_scope("loops:write")),
    ctx: UserContext = Depends(get_user_context),
    db: AsyncSession = Depends(get_db),
) -> dict:
    project = await _project_write_gated(project_id, db, auth, ctx)
    current = await _project_template(db, project, template_id)
    name, definition = await _clean_template(db, auth, body, scope_org_id=project.org_id)
    return await _new_version(db, auth, current, name=name, definition=definition)


@project_router.delete("/{project_id}/workflow-templates/{template_id}", status_code=204)
async def deactivate_project_template(
    project_id: str,
    template_id: str,
    auth: AuthContext = Depends(require_scope("loops:write")),
    db: AsyncSession = Depends(get_db),
) -> Response:
    project = await _project_for_write(project_id, db, auth, _TEMPLATE_NOT_FOUND)
    row = await _project_template(db, project, template_id)
    return await _deactivate(db, row)


# ── Org scope ──


async def _org_role(
    db: AsyncSession, auth: AuthContext, org_id: str
) -> str | None:
    """The caller's role in ``org_id`` (404 when unknown or not visible).

    A service key sees only its own org and has no role (read-only)."""
    org = await db.get(Organization, org_id)
    if org is None:
        raise _nf()
    if auth.key_kind == "service":
        # A key limited to some projects reads org templates only through
        # those projects' routes, never org-wide.
        if auth.org_id != org_id or auth.project_ids:
            raise _nf()
        return None
    role = (
        await db.execute(
            select(OrgMember.role).where(
                OrgMember.org_id == org_id, OrgMember.user_id == auth.user.id
            )
        )
    ).scalars().first()
    if role is None:
        raise _nf()
    return role


async def _org_admin(db: AsyncSession, auth: AuthContext, org_id: str) -> None:
    role = await _org_role(db, auth, org_id)
    if auth.key_kind == "service":
        raise HTTPException(
            403,
            {
                "error": "service_key_not_allowed",
                "message": "Org workflow templates are managed by org admins, not service keys.",
            },
        )
    if role is None or not has_minimum_role(role, "admin"):
        raise HTTPException(
            403,
            {
                "error": "insufficient_role",
                "current_role": role,
                "required_role": "admin",
                "message": "Managing org workflow templates requires the admin or owner role.",
            },
        )


async def _org_write_gated(db: AsyncSession, auth: AuthContext, org_id: str) -> None:
    """Org admin standing, then the org's own plan must include
    ``agent_tickets``, then the org row lock for the active-template cap."""
    await _org_admin(db, auth, org_id)
    plan = await resolve_org_plan_tier(org_id, db)
    if "agent_tickets" not in get_features_for_tier(plan):
        raise_project_upgrade_required("agent_tickets", plan, None)
    await db.execute(
        select(Organization.id).where(Organization.id == org_id).with_for_update()
    )


async def _org_template(db: AsyncSession, org_id: str, template_id: str) -> WorkflowTemplate:
    row = await db.get(WorkflowTemplate, template_id)
    if row is None or row.org_id != org_id:
        raise _nf()
    return row


@org_router.get("/{org_id}/workflow-templates")
async def list_org_templates(
    org_id: str,
    response: Response,
    auth: AuthContext = Depends(require_scope("loops:read")),
    db: AsyncSession = Depends(get_db),
) -> list[dict]:
    await _org_role(db, auth, org_id)
    rows = await _list_rows(db, WorkflowTemplate.org_id == org_id, response)
    return [_template_view(r) for r in rows]


@org_router.post("/{org_id}/workflow-templates", status_code=201)
async def create_org_template(
    org_id: str,
    body: TemplateWrite,
    auth: AuthContext = Depends(require_scope("loops:write")),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await _org_write_gated(db, auth, org_id)
    name, definition = await _clean_template(db, auth, body, scope_org_id=org_id)
    await _check_template_cap(db, WorkflowTemplate.org_id == org_id)
    row = _new_row(auth, name=name, definition=definition, org_id=org_id, project_id=None)
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return _template_view(row)


@org_router.get("/{org_id}/workflow-templates/{template_id}")
async def get_org_template(
    org_id: str,
    template_id: str,
    auth: AuthContext = Depends(require_scope("loops:read")),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await _org_role(db, auth, org_id)
    return _template_view(await _org_template(db, org_id, template_id))


@org_router.put("/{org_id}/workflow-templates/{template_id}")
async def update_org_template(
    org_id: str,
    template_id: str,
    body: TemplateWrite,
    auth: AuthContext = Depends(require_scope("loops:write")),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await _org_write_gated(db, auth, org_id)
    current = await _org_template(db, org_id, template_id)
    name, definition = await _clean_template(db, auth, body, scope_org_id=org_id)
    return await _new_version(db, auth, current, name=name, definition=definition)


@org_router.delete("/{org_id}/workflow-templates/{template_id}", status_code=204)
async def deactivate_org_template(
    org_id: str,
    template_id: str,
    auth: AuthContext = Depends(require_scope("loops:write")),
    db: AsyncSession = Depends(get_db),
) -> Response:
    await _org_admin(db, auth, org_id)
    row = await _org_template(db, org_id, template_id)
    return await _deactivate(db, row)


# ── Workflows ──


def _is_step_slot_conflict(exc: IntegrityError) -> bool:
    """The unique step-slot / one-slot-per-ticket constraints only (another
    start won the race); every other integrity error is a real failure."""
    raw = str(getattr(exc, "orig", exc))
    return (
        "uq_workflow_step_children_slot" in raw
        or "uq_workflow_step_children_ticket" in raw
        or "UNIQUE constraint failed: workflow_step_children." in raw
    )


async def _usable_template(
    db: AsyncSession, auth: AuthContext, project: Project, template_id: str
) -> WorkflowTemplate:
    """A template this project may use: its own, or its org's when the
    caller can see the org's (same rule as org personas)."""
    row = await db.get(WorkflowTemplate, template_id)
    if row is None:
        raise _nf()
    if row.project_id is not None:
        if row.project_id != project.id:
            raise _nf()
        return row
    org_id = await visible_persona_org_id_for_auth(db, project, auth)
    if not org_id or row.org_id != org_id:
        raise _nf()
    return row


@project_router.post("/{project_id}/workflows", status_code=201)
async def start_workflow(
    project_id: str,
    body: WorkflowStart,
    auth: AuthContext = Depends(require_scope("loops:write")),
    ctx: UserContext = Depends(get_user_context),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Start a workflow: a loop pinned to the template version, one child
    ticket per step slot, briefs rendered from the template + checklist.

    A loop write: same standing, scopes, plan gate (Team+, ``agent_tickets``)
    and scope lock as ``start_review_loop``. ``ticket_id`` adopts an existing
    issue; otherwise ``title`` names a new one. Nothing is started.
    """
    project = await _project_for_write(project_id, db, auth, _TEMPLATE_NOT_FOUND)
    template = await _usable_template(db, auth, project, body.template_id)
    _require_loop_downstream_scopes(auth)
    await check_project_feature(ctx, "agent_tickets", project, db)
    await lock_project_scope_authorized(
        db, project, auth=auth, ctx=ctx, feature="agent_tickets"
    )
    # Re-read the template under the lock: a concurrent edit or disable
    # since the visibility check must not start a superseded version.
    template = (
        await db.execute(
            select(WorkflowTemplate)
            .where(WorkflowTemplate.id == template.id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if template is None:
        raise _nf()
    superseded = (
        await db.execute(
            select(WorkflowTemplate.id)
            .where(WorkflowTemplate.supersedes_id == template.id)
            .limit(1)
        )
    ).scalar_one_or_none()
    if not template.active or superseded is not None:
        raise HTTPException(
            409,
            {
                "error": "workflow_template_inactive",
                "message": "This template version is not active; start from the active version.",
            },
        )
    if body.cost_budget_usd is not None:
        await _require_dollar_budget(db, auth, project)
    try:
        definition = wf.parse_definition(template.definition)
        await wf.validate_policy_scope(db, definition, org_id=project.org_id)
    except wf.WorkflowDefinitionError as exc:
        raise HTTPException(
            422, {"error": "invalid_workflow_definition", "message": str(exc)}
        )

    loop_extra: dict[str, Any] = {}
    for key in ("token_budget", "cost_budget_usd", "max_review_rounds"):
        value = getattr(body, key)
        if value is not None:
            loop_extra[key] = value
    try:
        parent, placed, queue = await wf.start_workflow(
            db,
            project=project,
            auth=auth,
            template=template,
            definition=definition,
            from_ticket_id=body.ticket_id,
            title=_sanitize_text(body.title),
            description=_sanitize_text(body.description),
            reviewer_persona=body.reviewer_persona,
            loop_extra=loop_extra,
            checklist_items=await _active_checklist_items(db, project_id),
        )
    except wf.WorkflowAgentUnknown as exc:
        await db.rollback()
        raise HTTPException(
            422,
            {
                "error": "workflow_agent_unknown",
                "agent": exc.agent,
                "message": (
                    f"No active agent named {exc.agent!r} is visible in this project."
                ),
            },
        )
    except wf.WorkflowDefinitionError as exc:
        await db.rollback()
        raise HTTPException(
            422, {"error": "invalid_workflow_definition", "message": str(exc)}
        )
    except IntegrityError as exc:
        await db.rollback()
        if not _is_step_slot_conflict(exc):
            raise
        raise HTTPException(409, "This workflow was already started.")
    reviewer_persona = queue.assigned_persona
    await db.commit()

    trusted = await _reviewer_is_trusted(db, project, reviewer_persona)
    loop = await db.get(Ticket, parent.id)
    assert loop is not None
    plan = await wf.compute_plan(
        db,
        loop,
        _load_loop_config(loop.loop_config),
        checklist_items=await _active_checklist_items(db, project_id),
    )
    steps: dict[str, list[dict[str, Any]]] = {s.id: [] for s in definition.steps}
    for step_id, index, child in placed:
        steps[step_id].append(
            {
                "id": child.id,
                "index": index,
                "title": child.title,
                "agent": child.assigned_to,
                "brief_markdown": child.description,
            }
        )
    return {
        "loop_id": parent.id,
        "queue_id": queue.id,
        "workflow": {
            "template_id": template.id,
            "lineage_id": template.lineage_id,
            "version": template.version,
            "name": template.name,
        },
        "steps": [{"step_id": sid, "tickets": tks} for sid, tks in steps.items()],
        "reviewer": {"persona": reviewer_persona, "trusted": trusted},
        "plan": wf.plan_view(loop, plan) if plan is not None else None,
    }


@project_router.get("/{project_id}/workflows/{loop_id}/plan")
async def get_workflow_plan(
    project_id: str,
    loop_id: str,
    auth: AuthContext = Depends(require_scope("loops:read")),
    ctx: UserContext = Depends(get_user_context),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Per step: state (blocked|ready|running|needs_human|done), the ready
    tickets with their briefs, and why a step is not ready. Computed from
    stored rows on every call."""
    project = await _readable_project(project_id, db, auth)
    loop = (
        await db.execute(
            select(Ticket).where(
                Ticket.id == loop_id,
                Ticket.project_id == project_id,
                Ticket.loop_config.isnot(None),
            )
        )
    ).scalar_one_or_none()
    config = _load_loop_config(loop.loop_config) if loop is not None else {}
    if loop is None or wf.workflow_pin(config) is None:
        raise HTTPException(404, _LOOP_NOT_FOUND)
    await check_project_feature(ctx, "agent_tickets", project, db)
    plan = await wf.compute_plan(
        db, loop, config, checklist_items=await _active_checklist_items(db, project_id)
    )
    if plan is None:
        raise HTTPException(
            409,
            {
                "error": "workflow_template_unavailable",
                "message": "This loop's pinned template version can no longer be read.",
            },
        )
    show_template = plan.template.org_id is None or (
        await visible_persona_org_id_for_auth(db, project, auth)
    ) == plan.template.org_id
    return wf.plan_view(loop, plan, show_template=show_template)


__all__ = ["org_router", "project_router"]
