"""Review Loop R3 — loop start + status (Brief D1).

``POST /api/v1/projects/{p}/loops`` turns an issue into (or creates) a loop
parent and files one child task per brief under it, then stands up a
``review_until_clean`` work queue for the reviewer persona. ``GET …/loops``
and ``GET …/loops/{id}`` roll the stored records up into the shape
``dashboard/src/api/reviewLoop.ts`` expects (children with status, branch,
head, rounds, harm, tokens/dollars, budget, needs_human, readiness).

Design: docs/design/review-loop-r3.md §4–§6.
"""

from __future__ import annotations

import json
import math
import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, StrictInt, field_validator
from sqlalchemy import and_, inspect, or_, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server.auth.dependencies import (
    AuthContext,
    assert_service_key_can_access_project,
    get_current_user,
    require_scope,
)
from sessionfs.server.auth.project_access import (
    load_project_for_editor,
    user_can_access_project,
)
from sessionfs.server.db.engine import get_db
from sessionfs.server.db.models import (
    Project,
    Ticket,
    TrustedReviewer,
    User,
    WorkQueue,
    WorkQueueItem,
    WorkQueueRun,
)
from sessionfs.server.routes.knowledge import _get_project_for_editor
from sessionfs.server.routes.loop_checklist import _sanitize_text
from sessionfs.server.routes.tickets import _record_create_events
from sessionfs.server.services import ticket_watchers
from sessionfs.server.services.loops import load_loops
from sessionfs.server.services.scope_locks import lock_project_scope_authorized
from sessionfs.server.tier_gate import UserContext, check_project_feature, get_user_context

router = APIRouter(prefix="/api/v1/projects", tags=["loops"])

_LOOP_CHECKLIST_TABLE = "loop_checklist_items"

# review_until_clean queues stop when every item has a clean verdict.
_STOP_ALL_CLEAN = "all_clean"

# Loop routes share one 404 body for every miss (unknown project, loop,
# child, or queue item) so ids can't be probed (review checklist #2).
_LOOP_NOT_FOUND = "Loop not found"


class LoopTicketSpec(BaseModel):
    title: str
    description: str = ""
    acceptance_criteria: list[str] = []

    @field_validator("title")
    @classmethod
    def _title_shape(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("title is required")
        if len(v) > 200:
            raise ValueError("title must be 200 characters or fewer")
        return v

    @field_validator("description")
    @classmethod
    def _description_shape(cls, v: str) -> str:
        if len(v) > 20000:
            raise ValueError("description must be 20000 characters or fewer")
        return v

    @field_validator("acceptance_criteria")
    @classmethod
    def _criteria_shape(cls, v: list[str]) -> list[str]:
        if len(v) > 50:
            raise ValueError("acceptance_criteria must have 50 items or fewer")
        for criterion in v:
            if len(criterion) > 1000:
                raise ValueError(
                    "each acceptance criterion must be 1000 characters or fewer"
                )
        return v


class LoopCreate(BaseModel):
    title: str = ""
    description: str = ""
    from_ticket_id: str | None = None
    tickets: list[LoopTicketSpec] = Field(min_length=1, max_length=50)
    reviewer_persona: str
    token_budget: StrictInt | None = Field(None, ge=1)
    cost_budget_usd: float | None = Field(None, gt=0)
    # No default — the caller must opt in to capping review rounds.
    max_review_rounds: StrictInt | None = Field(None, ge=1)

    @field_validator("reviewer_persona")
    @classmethod
    def _persona_shape(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("reviewer_persona is required")
        if len(v) > 50:
            raise ValueError("reviewer_persona must be 50 characters or fewer")
        return v

    @field_validator("title")
    @classmethod
    def _title_shape(cls, v: str) -> str:
        v = v.strip()
        if len(v) > 200:
            raise ValueError("title must be 200 characters or fewer")
        return v

    @field_validator("description")
    @classmethod
    def _description_shape(cls, v: str) -> str:
        if len(v) > 20000:
            raise ValueError("description must be 20000 characters or fewer")
        return v

    @field_validator("cost_budget_usd", mode="before")
    @classmethod
    def _reject_bool_cost(cls, v: Any) -> Any:
        # ``bool`` is a subclass of ``int`` in Python; a ``true`` would
        # otherwise coerce to 1.0 and slip past ``gt=0``.
        if isinstance(v, bool):
            raise ValueError("cost_budget_usd must be a finite number, not a boolean")
        return v

    @field_validator("cost_budget_usd")
    @classmethod
    def _finite_cost(cls, v: float | None) -> float | None:
        if v is not None and not math.isfinite(v):
            raise ValueError("cost_budget_usd must be a finite number")
        return v


class LoopUpdate(BaseModel):
    """Patch body for ``PATCH …/loops/{id}``.

    Only fields present in the request are changed. An explicit ``null`` clears
    that budget cap; a field that is absent is left untouched.
    """

    token_budget: StrictInt | None = Field(None, ge=1)
    max_review_rounds: StrictInt | None = Field(None, ge=1)
    cost_budget_usd: float | None = Field(None, gt=0)

    @field_validator("cost_budget_usd", mode="before")
    @classmethod
    def _reject_bool_cost(cls, v: Any) -> Any:
        # ``bool`` is a subclass of ``int`` in Python; a ``true`` would
        # otherwise coerce to 1.0 and slip past ``gt=0``.
        if isinstance(v, bool):
            raise ValueError("cost_budget_usd must be a finite number, not a boolean")
        return v

    @field_validator("cost_budget_usd")
    @classmethod
    def _finite_cost(cls, v: float | None) -> float | None:
        if v is not None and not math.isfinite(v):
            raise ValueError("cost_budget_usd must be a finite number")
        return v


def _load_loop_config(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


async def _table_exists(db: AsyncSession, name: str) -> bool:
    conn = await db.connection()

    def _has(sync_conn) -> bool:
        return inspect(sync_conn).has_table(name)

    return await conn.run_sync(_has)


async def _active_checklist_items(
    db: AsyncSession, project_id: str
) -> list[dict[str, str]]:
    """Active ``loop_checklist_items`` for the briefs' "Known traps" section.

    The table is added by a later track; until it lands, this returns []
    (guard via the SQLAlchemy inspector so a project on an older schema
    still starts loops).
    """
    if not await _table_exists(db, _LOOP_CHECKLIST_TABLE):
        return []
    rows = (
        await db.execute(
            text(
                'SELECT title, "check" AS how_to_check '
                f"FROM {_LOOP_CHECKLIST_TABLE} "
                "WHERE project_id = :pid AND status = :status "
                "ORDER BY slug"
            ),
            {"pid": project_id, "status": "active"},
        )
    ).mappings().all()
    return [{"title": r["title"], "check": r["how_to_check"]} for r in rows]


def _render_brief(
    *,
    description: str,
    acceptance_criteria: list[str],
    checklist_items: list[dict[str, str]],
) -> str:
    sections: list[str] = []
    if description.strip():
        sections.append(description.strip())
    if acceptance_criteria:
        sections.append(
            "## Acceptance criteria\n" + "\n".join(f"- {c}" for c in acceptance_criteria)
        )
    if checklist_items:
        sections.append(
            "## Known traps\n"
            + "\n".join(
                f"- **{item['title']}** — {item['check']}" for item in checklist_items
            )
        )
    return "\n\n".join(sections)


async def _reviewer_is_trusted(
    db: AsyncSession, project: Project, reviewer_persona: str
) -> bool:
    """True iff an ACTIVE trusted_reviewers row exists for ``reviewer_persona``
    scoped to the project OR org-wide for the project's org (identity-agnostic
    — this answers "is anyone registered to review as this persona here")."""
    scope_clause = or_(
        TrustedReviewer.project_id == project.id,
        and_(
            TrustedReviewer.project_id.is_(None),
            TrustedReviewer.org_id == project.org_id,
        )
        if project.org_id is not None
        else False,
    )
    stmt = (
        select(TrustedReviewer.id)
        .where(
            TrustedReviewer.is_active.is_(True),
            TrustedReviewer.revoked_at.is_(None),
            TrustedReviewer.reviewer_persona == reviewer_persona,
            scope_clause,
        )
        .limit(1)
    )
    return (await db.execute(stmt)).scalar_one_or_none() is not None


def _require_loop_downstream_scopes(auth: AuthContext) -> None:
    """A service key that starts a loop files child tickets and stands up a
    work queue, so it needs those write scopes in addition to ``loops:write``
    (same downstream-scope rule as the work-queue act paths). User keys carry
    ``*`` and pass."""
    if "*" in auth.scopes:
        return
    needed = ("tickets:write", "work_queues:write")
    missing = [s for s in needed if s not in auth.scopes]
    if missing:
        raise HTTPException(
            403,
            {
                "error": "insufficient_scope",
                "required": sorted(needed),
                "current": auth.scopes,
                "message": (
                    "starting a review loop requires the downstream write "
                    f"scopes it exercises: missing {missing}."
                ),
            },
        )


async def _require_dollar_budget(db: AsyncSession, auth: AuthContext, project: Project) -> None:
    """``cost_budget_usd`` is org-admin/owner only; service keys never see
    dollars (same rule as the spend rollups)."""
    if auth.key_kind == "service":
        raise HTTPException(
            403,
            "cost_budget_usd requires org owner/admin standing; service keys "
            "cannot set a dollar budget.",
        )
    from sessionfs.server.auth.project_access import user_can_view_spend_dollars

    if not await user_can_view_spend_dollars(db, auth.user.id, project):
        raise HTTPException(
            403,
            "cost_budget_usd requires org owner/admin standing (or personal-"
            "project ownership).",
        )


async def _load_loop_ticket(db: AsyncSession, project_id: str, ticket_id: str) -> Ticket:
    ticket = (
        await db.execute(
            select(Ticket).where(
                Ticket.id == ticket_id,
                Ticket.project_id == project_id,
            )
        )
    ).scalar_one_or_none()
    if ticket is None:
        raise HTTPException(404, "Ticket not found")
    return ticket


@router.post("/{project_id}/loops", status_code=201)
async def start_review_loop(
    project_id: str,
    body: LoopCreate,
    auth: AuthContext = Depends(require_scope("loops:write")),
    ctx: UserContext = Depends(get_user_context),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Start (or adopt) a review loop.

    Editor-gated: user keys need editor standing; service keys need the
    ``loops:write`` scope and their org/allowlist boundary (enforced below).
    ``from_ticket_id`` adopts an existing ISSUE as the loop parent; otherwise
    a new issue is created from ``title``.
    """
    project = await _get_project_for_editor(project_id, db, auth)
    await assert_service_key_can_access_project(db, auth, project)
    _require_loop_downstream_scopes(auth)
    await check_project_feature(ctx, "agent_tickets", project, db)
    # GH #70 — serialize with project merge / persona-delete guards, then
    # re-authorize against the re-read project (same as ticket create).
    await lock_project_scope_authorized(
        db, project, auth=auth, ctx=ctx, feature="agent_tickets"
    )

    if body.cost_budget_usd is not None:
        await _require_dollar_budget(db, auth, project)

    # Text hygiene (review checklist #7): strip NULs/lone surrogates and
    # redact secrets before anything is stored or rendered into a brief.
    body.title = _sanitize_text(body.title)
    body.description = _sanitize_text(body.description)

    if body.from_ticket_id is not None:
        parent = await _load_loop_ticket(db, project_id, body.from_ticket_id)
        if parent.kind != "issue":
            raise HTTPException(
                422,
                "from_ticket_id must reference an issue ticket (a loop parent "
                "is an issue that rolls up its child tasks).",
            )
    else:
        if not body.title.strip():
            raise HTTPException(
                422, "title is required when from_ticket_id is not supplied"
            )
        parent = Ticket(
            id=f"tk_{uuid.uuid4().hex[:16]}",
            project_id=project_id,
            title=body.title.strip(),
            description=body.description,
            priority="medium",
            kind="issue",
            status="open",
            created_by_user_id=auth.user.id,
            actor_type=auth.actor_type,
            service_key_id=auth.service_key_id,
            service_key_name=auth.service_key_name,
        )
        db.add(parent)
        await db.flush()
        await _record_create_events(
            db, parent, auth=auth, assigned_to=None, assigned_user_id=None
        )
        # Same creator auto-subscribe as the standalone create route: the
        # creator follows the loop parent; a service key's minter is not.
        if auth.key_kind != "service":
            await ticket_watchers.subscribe_guarded(
                db, ticket_id=parent.id, user_id=auth.user.id, source="creator"
            )

    # Claim the parent atomically BEFORE any child/queue rows are created
    # (review checklist #11): the queue id is pre-assigned so loop_config can
    # be written now. The conditional UPDATE only lands while loop_config is
    # still NULL, so two concurrent adopters can't both stand up children and
    # a queue — the loser sees rowcount 0 and aborts with 409.
    queue_id = f"wq_{uuid.uuid4().hex[:16]}"
    loop_config = {
        "queue_id": queue_id,
        "reviewer_persona": body.reviewer_persona,
    }
    if body.token_budget is not None:
        loop_config["token_budget"] = body.token_budget
    if body.cost_budget_usd is not None:
        loop_config["cost_budget_usd"] = body.cost_budget_usd
    if body.max_review_rounds is not None:
        loop_config["max_review_rounds"] = body.max_review_rounds
    claimed = await db.execute(
        update(Ticket)
        .where(Ticket.id == parent.id, Ticket.loop_config.is_(None))
        .values(loop_config=json.dumps(loop_config))
    )
    if claimed.rowcount != 1:
        raise HTTPException(
            409, f"Ticket {parent.id!r} is already a review loop."
        )

    # Child tasks + their briefs (one pass; checklist fetched once).
    checklist_items = await _active_checklist_items(db, project_id)
    children: list[Ticket] = []
    briefs: list[dict[str, Any]] = []
    for spec in body.tickets:
        spec.title = _sanitize_text(spec.title)
        spec.description = _sanitize_text(spec.description)
        spec.acceptance_criteria = [_sanitize_text(c) for c in spec.acceptance_criteria]
        brief_markdown = _render_brief(
            description=spec.description,
            acceptance_criteria=spec.acceptance_criteria,
            checklist_items=checklist_items,
        )
        child = Ticket(
            id=f"tk_{uuid.uuid4().hex[:16]}",
            project_id=project_id,
            title=spec.title,
            description=brief_markdown,
            priority="medium",
            kind="task",
            parent_ticket_id=parent.id,
            acceptance_criteria=json.dumps(spec.acceptance_criteria),
            status="open",
            created_by_user_id=auth.user.id,
            actor_type=auth.actor_type,
            service_key_id=auth.service_key_id,
            service_key_name=auth.service_key_name,
        )
        db.add(child)
        children.append(child)
        briefs.append(
            {"id": child.id, "title": child.title, "brief_markdown": brief_markdown}
        )
        # Child tickets get the same create event and creator auto-subscribe
        # as the parent (and as the standalone ticket-create route): the
        # event shares the loop's transaction, and a service key's minter is
        # NOT auto-subscribed (the key acts on the minter's behalf).
        await _record_create_events(
            db, child, auth=auth, assigned_to=None, assigned_user_id=None
        )
        if auth.key_kind != "service":
            await ticket_watchers.subscribe_guarded(
                db, ticket_id=child.id, user_id=auth.user.id, source="creator"
            )

    # review_until_clean queue for the reviewer persona over the children.
    # Reuses the queue_id recorded in loop_config at claim time.
    queue = WorkQueue(
        id=queue_id,
        project_id=project_id,
        name=f"Review loop {parent.id}",
        mode="review_until_clean",
        assigned_persona=body.reviewer_persona,
        selector=json.dumps({"ticket_ids": [c.id for c in children]}),
        auto_adopt=True,
        max_adopt_per_wake=5,
        stop_condition=_STOP_ALL_CLEAN,
        cadence_seconds=300,
        max_tickets_per_run=1,
        max_attempts_per_item=3,
        status="active",
        lease_epoch=0,
        created_by_user_id=auth.user.id,
        actor_type=auth.actor_type,
        service_key_id=auth.service_key_id,
        service_key_name=auth.service_key_name,
    )
    db.add(queue)
    await db.flush()
    for child in children:
        db.add(
            WorkQueueItem(
                id=f"wqi_{uuid.uuid4().hex[:16]}",
                work_queue_id=queue.id,
                ticket_id=child.id,
                item_status="pending",
                attempts=0,
            )
        )

    await db.commit()

    trusted = await _reviewer_is_trusted(db, project, body.reviewer_persona)
    return {
        "loop_id": parent.id,
        "tickets": briefs,
        "queue_id": queue.id,
        "reviewer": {
            "persona": body.reviewer_persona,
            "trusted": trusted,
            "setup_hint": (
                None
                if trusted
                else (
                    f"No active trusted reviewer is registered for persona "
                    f"{body.reviewer_persona!r} in this project (or org-wide). "
                    "Register one before the loop can self-close."
                )
            ),
        },
        "env": {
            "implementer": {"SESSIONFS_PROFILE": "implementer"},
            "reviewer": {"SESSIONFS_PROFILE": "reviewer"},
        },
    }


async def _show_dollars(db: AsyncSession, auth: AuthContext, project: Project) -> bool:
    if auth.key_kind == "service":
        return False
    from sessionfs.server.auth.project_access import user_can_view_spend_dollars

    return await user_can_view_spend_dollars(db, auth.user.id, project)


def _fold_project_miss(exc: HTTPException) -> HTTPException:
    """Fold a project-level 404 into the loop routes' single miss body.

    ``_get_project_for_editor`` / ``load_project_for_editor`` report a missing
    project as ``Project not found``; the loop routes must be byte-identical
    with a missing loop (review checklist #2), so the project miss is
    rewritten to ``Loop not found``. No-access 403s pass through untouched
    (write routes keep the 403 oracle).
    """
    if exc.status_code == 404:
        return HTTPException(404, _LOOP_NOT_FOUND)
    return exc


async def _readable_project(
    project_id: str, db: AsyncSession, auth: AuthContext
) -> Project:
    """Project boundary for the read loop routes, folded into the loop 404.

    No plan gate here: callers gate after the loop lookup so a tier error
    never leaks the org's plan ahead of the existence oracle (checklist #2).
    """
    if auth.key_kind == "service":
        project = (
            await db.execute(select(Project).where(Project.id == project_id))
        ).scalar_one_or_none()
        if project is None:
            raise HTTPException(404, _LOOP_NOT_FOUND)
        # Existence oracle (review checklist #2): a cross-org or unallowlisted
        # project is byte-identical to one that does not exist, never a
        # cross_org_denied / project_not_in_allowlist probe.
        await assert_service_key_can_access_project(
            db, auth, project, opaque_404_detail=_LOOP_NOT_FOUND
        )
    else:
        # Existence-oracle protection (review checklist #2): user keys get a
        # byte-identical 404 for "unknown" and "not yours" instead of a 403.
        project = (
            await db.execute(select(Project).where(Project.id == project_id))
        ).scalar_one_or_none()
        if project is None or not await user_can_access_project(
            db, auth.user.id, project
        ):
            raise HTTPException(404, _LOOP_NOT_FOUND)
    return project


@router.get("/{project_id}/loops")
async def list_loops(
    project_id: str,
    auth: AuthContext = Depends(require_scope("loops:read")),
    ctx: UserContext = Depends(get_user_context),
    db: AsyncSession = Depends(get_db),
) -> list[dict]:
    """Every loop in the project (parent tickets with a ``loop_config``)."""
    project = await _readable_project(project_id, db, auth)
    await check_project_feature(ctx, "agent_tickets", project, db)
    loops = (
        await db.execute(
            select(Ticket)
            .where(
                Ticket.project_id == project_id,
                Ticket.loop_config.isnot(None),
            )
            .order_by(Ticket.created_at.desc(), Ticket.id.desc())
        )
    ).scalars().all()
    return await load_loops(
        db, loop_tickets=list(loops), show_dollars=await _show_dollars(db, auth, project)
    )


@router.get("/{project_id}/loops/{loop_id}")
async def get_loop(
    project_id: str,
    loop_id: str,
    auth: AuthContext = Depends(require_scope("loops:read")),
    ctx: UserContext = Depends(get_user_context),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """One loop by id (404 unless it is a loop in this project)."""
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
    if loop is None:
        raise HTTPException(404, _LOOP_NOT_FOUND)
    await check_project_feature(ctx, "agent_tickets", project, db)
    results = await load_loops(
        db, loop_tickets=[loop], show_dollars=await _show_dollars(db, auth, project)
    )
    return results[0]


async def _load_loop_for_editor(
    db: AsyncSession, project_id: str, loop_id: str
) -> Ticket:
    loop = (
        await db.execute(
            select(Ticket).where(
                Ticket.id == loop_id,
                Ticket.project_id == project_id,
                Ticket.loop_config.isnot(None),
            )
        )
    ).scalar_one_or_none()
    if loop is None:
        raise HTTPException(404, _LOOP_NOT_FOUND)
    return loop


@router.patch("/{project_id}/loops/{loop_id}")
async def update_loop(
    project_id: str,
    loop_id: str,
    body: LoopUpdate,
    auth: AuthContext = Depends(require_scope("loops:write")),
    ctx: UserContext = Depends(get_user_context),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Raise or clear a loop's budget caps.

    Only the fields present in the body change; an explicit ``null`` clears a
    cap, an absent field is left as-is. ``cost_budget_usd`` follows the same
    org-owner/admin-only rule as loop create (service keys may never touch
    dollars); ``token_budget``/``max_review_rounds`` are editor-gated.
    """
    try:
        project = await _get_project_for_editor(project_id, db, auth)
    except HTTPException as exc:
        raise _fold_project_miss(exc)
    await assert_service_key_can_access_project(db, auth, project)

    # Resolve loop visibility before any plan/authorization gate (checklist
    # #2): a missing loop 404s first, so the plan and dollar standing never
    # leak ahead of the loop oracle.
    loop = await _load_loop_for_editor(db, project_id, loop_id)
    await check_project_feature(ctx, "agent_tickets", project, db)

    fields = body.model_fields_set
    if "cost_budget_usd" in fields:
        await _require_dollar_budget(db, auth, project)

    config = _load_loop_config(loop.loop_config)

    for key in ("token_budget", "max_review_rounds"):
        if key in fields:
            value = getattr(body, key)
            if value is None:
                config.pop(key, None)
            else:
                config[key] = value
    if "cost_budget_usd" in fields:
        if body.cost_budget_usd is None:
            config.pop("cost_budget_usd", None)
        else:
            config["cost_budget_usd"] = body.cost_budget_usd

    loop.loop_config = json.dumps(config)
    await db.commit()

    results = await load_loops(
        db, loop_tickets=[loop], show_dollars=await _show_dollars(db, auth, project)
    )
    return results[0]


@router.post("/{project_id}/loops/{loop_id}/children/{ticket_id}/close")
async def close_loop_item(
    project_id: str,
    loop_id: str,
    ticket_id: str,
    user: User = Depends(get_current_user),
    ctx: UserContext = Depends(get_user_context),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Settle one ``needs_human`` child as done-by-human.

    People only — a human closes a loop item the agent handed back. Service
    keys are rejected by ``get_current_user`` with the standard 403 before
    any side effect. Parks the queue cursor in ``done`` so the loop can stop
    waiting, WITHOUT touching ``Ticket.status`` (the ticket keeps its real
    state). Only a currently-``needs_human`` item may be closed this way.
    """
    try:
        project = await load_project_for_editor(project_id, db, user.id)
    except HTTPException as exc:
        raise _fold_project_miss(exc)

    # Resolve every level of visibility (loop, child, queue item) before the
    # plan gate (checklist #2): a gated project still 404s on a missing loop,
    # child, or queue item instead of leaking its plan.
    loop = await _load_loop_for_editor(db, project_id, loop_id)
    config = _load_loop_config(loop.loop_config)
    queue_id = config.get("queue_id")
    if not queue_id:
        raise HTTPException(409, "Loop has no work queue")

    child = (
        await db.execute(
            select(Ticket).where(
                Ticket.id == ticket_id,
                Ticket.project_id == project_id,
                Ticket.parent_ticket_id == loop.id,
            )
        )
    ).scalar_one_or_none()
    if child is None:
        raise HTTPException(404, _LOOP_NOT_FOUND)

    item = (
        await db.execute(
            select(WorkQueueItem).where(
                WorkQueueItem.work_queue_id == queue_id,
                WorkQueueItem.ticket_id == ticket_id,
            )
        )
    ).scalar_one_or_none()
    if item is None:
        raise HTTPException(404, _LOOP_NOT_FOUND)

    await check_project_feature(ctx, "agent_tickets", project, db)

    # Guarded settle (checklist #11): the UPDATE only lands while the item is
    # still needs_human, so two concurrent closes cannot both append a run.
    now = datetime.now(timezone.utc)
    result = await db.execute(
        update(WorkQueueItem)
        .where(
            WorkQueueItem.id == item.id,
            WorkQueueItem.item_status == "needs_human",
        )
        .values(
            item_status="done",
            closed_by_user_id=user.id,
            closed_by_service_key_id=None,
            auto_close_review_kind="human",
            next_eligible_at=None,
            updated_at=now,
        )
    )
    if result.rowcount != 1:
        raise HTTPException(
            409, "Only a needs_human loop item can be closed by hand"
        )

    db.add(
        WorkQueueRun(
            id=f"wqr_{uuid.uuid4().hex[:16]}",
            work_queue_id=queue_id,
            work_queue_item_id=item.id,
            outcome="closed_by_human",
        )
    )
    await db.commit()

    return {
        "ticket_id": ticket_id,
        "loop_id": loop.id,
        "item_status": "done",
        "closed_by": {"kind": "user", "user_id": user.id},
    }
