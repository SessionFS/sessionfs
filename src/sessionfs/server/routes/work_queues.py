"""Agent work-queue management surface — WQ-P2 (tk_3481237f3b0847d6).

CRUD + lifecycle for WorkQueue (the durable, project-scoped plan for an
agent to service a set of tickets without a human dispatcher). Builds on
WQ-P1 (models WorkQueue/WorkQueueItem/WorkQueueRun, migration 054,
services/work_queues.py claim helper). Design: docs/design/agent-work-queues.md
§4, §4.6, §9.1, §11 R5/R10.

This phase is CRUD + lifecycle ONLY. The step engine
(run_work_queue_step / complete_work_queue_step), hydration/auto-adopt
materialization, the mode algorithms, and the review_until_clean stop
oracle land in WQ-P3/P4. NOT here.

Four routes under `/api/v1/projects/{project_id}/work-queues`:

    POST   /                          — create a queue (+ seed items from
                                        selector.ticket_ids)
    GET    /                          — list queues with item rollups
    GET    /{queue_id}                — detail + item summary
    POST   /{queue_id}/status         — lifecycle (server-enforced
                                        transition table; lease-fenced)

NO tier gate (design §13 packaging is a recommendation, not a commitment;
the ticket explicitly says all tiers). Access is gated on project access +
service-key scope. Every endpoint enforces BOTH project access AND, for
service keys, `assert_service_key_can_access_project` (cross-org/allowlist),
so a queue from another project surfaces as 404, never as a cross-project
data leak (design §11 R5/R10).

Status FSM (design §4.6 — the table the model's CHECK constraint allows is
active|paused|completed|cancelled; the design doc's "archived" became
"cancelled" in the WQ-P1 migration 054, so the transition table here uses
the real enum):

    active    -> paused | completed | cancelled
    paused    -> active | completed | cancelled
    completed -> active (reopen) | cancelled
    cancelled -> (terminal — no transitions)

Lifecycle is lease-fenced on the queue-level `lease_epoch` (atomic
UPDATE ... WHERE id=:id AND lease_epoch=:n, rowcount-1, 409 on stale —
same pattern as update_ticket, routes/tickets.py:1257-1287). The epoch
bumps on every successful transition.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server.auth.dependencies import (
    AuthContext,
    assert_service_key_can_access_project,
    require_scope,
)
from sessionfs.server.auth.rate_limit import SlidingWindowRateLimiter
from sessionfs.server.db.engine import get_db
from sessionfs.server.db.models import Ticket, WorkQueue, WorkQueueItem
from sessionfs.server.routes.knowledge import _get_project_for_auth
from sessionfs.server.services import work_queues as wq_engine

router = APIRouter(prefix="/api/v1/projects", tags=["work-queues"])

# ── Dedicated rate-limit class for run_work_queue_step (design §11 R6) ──
#
# run_work_queue_step is NOT a passive read — it claims work, emits directives,
# and drives ticket writes + token spend. It therefore gets its OWN app-level
# quota, separate from the global per-key request limiter, keyed by
# org/project/service_key/queue so one runaway queue can't burn the project's
# budget. The complementary Cloud Armor edge deny-429 rule (same pattern as the
# live activate/helm-validate gate, CLAUDE.md v0.11.0) is a SEPARATE infra
# follow-up owned by Forge — do NOT touch infra here.
_STEP_RATE_LIMITER = SlidingWindowRateLimiter(
    max_requests=30, window_seconds=60.0
)


def _step_rate_limit_key(
    auth: AuthContext, project_id: str, queue_id: str
) -> str:
    """Compose the per-(org|user, project, service_key, queue) quota key."""
    principal = auth.service_key_id or f"user:{auth.user.id}"
    org = auth.org_id or "noorg"
    return f"wqstep:{org}:{project_id}:{principal}:{queue_id}"


def _require_downstream_scopes(auth: AuthContext, *needed: str) -> None:
    """Act-path scope check (design §4 / §11 R5).

    run/complete require work_queues:write (enforced by require_scope on the
    route) AND the downstream write scopes the directive exercises
    (tickets:write, and agent_runs:write where a wake opens an execution
    audit). A user/admin wildcard key passes. A service key missing a
    downstream scope is rejected 403 so a queue cannot post comments / open
    runs beyond what its key is authorized for.
    """
    if "*" in auth.scopes:
        return
    have = set(auth.scopes)
    missing = [s for s in needed if s not in have]
    if missing:
        raise HTTPException(
            403,
            {
                "error": "insufficient_scope",
                "required": sorted(set(needed)),
                "current": auth.scopes,
                "message": (
                    "run/complete work-queue step requires the downstream "
                    f"write scopes the directive exercises: missing {missing}."
                ),
            },
        )


# ── Enums (validated client-side; server is the source of truth) ──

_VALID_MODES: tuple[str, ...] = (
    "review_until_clean",
    "implement_until_done",
    "triage",
)
_VALID_STOP_CONDITIONS: tuple[str, ...] = (
    "queue_empty",
    "all_clean",
    "max_tickets",
    "manual",
)
# Mirrors the migration-054 / model CheckConstraint on work_queues.status.
_VALID_STATUSES: tuple[str, ...] = (
    "active",
    "paused",
    "completed",
    "cancelled",
)
_TERMINAL_STATUSES: frozenset[str] = frozenset({"cancelled"})

# Server-side allowed-transition table (design §4.6). The SERVER, not the
# caller, enforces legality. An illegal transition → 409
# invalid_status_transition. A no-op (from == to) is also rejected so the
# lease never bumps without a real change (audit-pollution anti-pattern,
# CLAUDE.md v0.10.28).
_ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    "active": frozenset({"paused", "completed", "cancelled"}),
    "paused": frozenset({"active", "completed", "cancelled"}),
    "completed": frozenset({"active", "cancelled"}),
    "cancelled": frozenset(),  # terminal
}

# §9.1 runaway/budget caps enforced at create.
_CADENCE_FLOOR_SECONDS = 120
_CADENCE_DEFAULT_SECONDS = 300
_MAX_TICKETS_PER_RUN_DEFAULT = 1
_MAX_TICKETS_PER_RUN_HARD_CAP = 5
_MAX_ATTEMPTS_PER_ITEM_DEFAULT = 3
_MAX_ADOPT_PER_WAKE_DEFAULT = 5


# ── Pydantic models ──


class WorkQueueCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    mode: str
    # JSON filter dict OR {"ticket_ids": [...]} OR both. Stored as text.
    selector: dict = Field(default_factory=dict)
    assigned_persona: str | None = Field(None, max_length=50)
    auto_adopt: bool = False
    max_adopt_per_wake: int = Field(
        _MAX_ADOPT_PER_WAKE_DEFAULT, ge=1, le=100
    )
    stop_condition: str = "queue_empty"
    # §9.1 — floor 120s / default 300s. None → default.
    cadence_seconds: int | None = None
    # §9.1 — default 1 / hard cap 5. None → default.
    max_tickets_per_run: int | None = None
    max_attempts_per_item: int | None = None
    # Provenance (optional — created_by_user_id comes from AuthContext).
    created_by_session_id: str | None = Field(None, max_length=64)
    created_by_persona: str | None = Field(None, max_length=50)

    @field_validator("name")
    @classmethod
    def _name_shape(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("name is required")
        return v

    @field_validator("mode")
    @classmethod
    def _mode_shape(cls, v: str) -> str:
        v = (v or "").strip()
        if v not in _VALID_MODES:
            raise ValueError(f"mode must be one of: {list(_VALID_MODES)}")
        return v

    @field_validator("stop_condition")
    @classmethod
    def _stop_condition_shape(cls, v: str) -> str:
        v = (v or "queue_empty").strip()
        if v not in _VALID_STOP_CONDITIONS:
            raise ValueError(
                f"stop_condition must be one of: {list(_VALID_STOP_CONDITIONS)}"
            )
        return v

    @field_validator("cadence_seconds")
    @classmethod
    def _cadence_shape(cls, v: int | None) -> int | None:
        if v is None:
            return None
        # §9.1 — reject below the floor (cleaner than silently clamping).
        if v < _CADENCE_FLOOR_SECONDS:
            raise ValueError(
                f"cadence_seconds must be >= {_CADENCE_FLOOR_SECONDS} "
                f"(the §9.1 runaway floor)."
            )
        return v

    @field_validator("max_tickets_per_run")
    @classmethod
    def _max_tickets_shape(cls, v: int | None) -> int | None:
        if v is None:
            return None
        if v < 1:
            raise ValueError("max_tickets_per_run must be >= 1")
        if v > _MAX_TICKETS_PER_RUN_HARD_CAP:
            raise ValueError(
                f"max_tickets_per_run must be <= "
                f"{_MAX_TICKETS_PER_RUN_HARD_CAP} (the §9.1 hard cap)."
            )
        return v

    @field_validator("max_attempts_per_item")
    @classmethod
    def _max_attempts_shape(cls, v: int | None) -> int | None:
        if v is None:
            return None
        if v < 1:
            raise ValueError("max_attempts_per_item must be >= 1")
        return v


class WorkQueueStatusUpdate(BaseModel):
    status: str
    lease_epoch: int | None = None

    @field_validator("status")
    @classmethod
    def _status_shape(cls, v: str) -> str:
        v = (v or "").strip()
        if v not in _VALID_STATUSES:
            raise ValueError(f"status must be one of: {list(_VALID_STATUSES)}")
        return v


class WorkQueueStepRequest(BaseModel):
    wake_source: str = Field("manual", max_length=30)
    wake_ref: str | None = Field(None, max_length=200)
    max_tickets: int | None = Field(None, ge=1, le=_MAX_TICKETS_PER_RUN_HARD_CAP)


class WorkQueueStepCompleteRequest(BaseModel):
    item_id: str = Field(..., max_length=64)
    directive_id: str = Field(..., max_length=64)
    ticket_id: str = Field(..., max_length=64)
    outcome: str = Field(..., max_length=30)
    comment_id: str | None = Field(None, max_length=64)
    agent_run_id: str | None = Field(None, max_length=64)
    ticket_lease_epoch: int | None = None
    failed: bool = False
    summary: str | None = Field(None, max_length=2000)


class WorkQueueItemSummary(BaseModel):
    id: str
    ticket_id: str
    item_status: str


class WorkQueueProgress(BaseModel):
    pending: int = 0
    active: int = 0
    waiting: int = 0
    done: int = 0
    failed: int = 0


class WorkQueueResponse(BaseModel):
    id: str
    project_id: str
    name: str
    mode: str
    assigned_persona: str | None
    selector: dict
    auto_adopt: bool
    max_adopt_per_wake: int
    stop_condition: str
    cadence_seconds: int
    max_tickets_per_run: int
    max_attempts_per_item: int
    status: str
    lease_epoch: int
    created_by_user_id: str
    created_by_session_id: str | None
    created_by_persona: str | None
    created_at: datetime
    updated_at: datetime
    progress: WorkQueueProgress
    items: list[WorkQueueItemSummary] | None = None


class WorkQueueCreateResponse(BaseModel):
    work_queue: WorkQueueResponse
    items: list[WorkQueueItemSummary]


# ── Helpers ──


def _new_queue_id() -> str:
    return f"wq_{uuid.uuid4().hex[:16]}"


def _new_item_id() -> str:
    return f"wqi_{uuid.uuid4().hex[:16]}"


def _parse_selector(raw: str | None) -> dict:
    if not raw:
        return {}
    try:
        loaded = json.loads(raw)
        return loaded if isinstance(loaded, dict) else {}
    except (TypeError, ValueError):
        return {}


def _empty_progress() -> WorkQueueProgress:
    return WorkQueueProgress()


def _row_to_response(
    queue: WorkQueue,
    progress: WorkQueueProgress,
    items: list[WorkQueueItem] | None = None,
) -> WorkQueueResponse:
    return WorkQueueResponse(
        id=queue.id,
        project_id=queue.project_id,
        name=queue.name,
        mode=queue.mode,
        assigned_persona=queue.assigned_persona,
        selector=_parse_selector(queue.selector),
        auto_adopt=queue.auto_adopt,
        max_adopt_per_wake=queue.max_adopt_per_wake,
        stop_condition=queue.stop_condition,
        cadence_seconds=queue.cadence_seconds,
        max_tickets_per_run=queue.max_tickets_per_run,
        max_attempts_per_item=queue.max_attempts_per_item,
        status=queue.status,
        lease_epoch=queue.lease_epoch,
        created_by_user_id=queue.created_by_user_id,
        created_by_session_id=queue.created_by_session_id,
        created_by_persona=queue.created_by_persona,
        created_at=queue.created_at,
        updated_at=queue.updated_at,
        progress=progress,
        items=(
            [
                WorkQueueItemSummary(
                    id=i.id, ticket_id=i.ticket_id, item_status=i.item_status
                )
                for i in items
            ]
            if items is not None
            else None
        ),
    )


async def _progress_for_queue(
    queue_id: str, db: AsyncSession
) -> WorkQueueProgress:
    """Roll up item_status counts for one queue (single grouped query)."""
    rows = (
        await db.execute(
            select(
                WorkQueueItem.item_status,
                func.count(WorkQueueItem.id),
            )
            .where(WorkQueueItem.work_queue_id == queue_id)
            .group_by(WorkQueueItem.item_status)
        )
    ).all()
    progress = WorkQueueProgress()
    for status, count in rows:
        if hasattr(progress, status):
            setattr(progress, status, int(count))
    return progress


async def _get_queue_or_404(
    project_id: str, queue_id: str, db: AsyncSession
) -> WorkQueue:
    """Project-scoped fetch. Cross-project queue_id surfaces as 404."""
    queue = (
        await db.execute(
            select(WorkQueue).where(
                WorkQueue.id == queue_id,
                WorkQueue.project_id == project_id,
            )
        )
    ).scalar_one_or_none()
    if queue is None:
        raise HTTPException(404, "Work queue not found")
    return queue


def _extract_ticket_ids(selector: dict) -> list[str]:
    """Pull a deduped list of explicit ticket ids from the selector.

    Only `selector.ticket_ids` seeds items at create. Filter-only selectors
    materialize lazily at run-step time (WQ-P3 hydration) — not here.
    """
    raw = selector.get("ticket_ids")
    if not isinstance(raw, list):
        return []
    seen: set[str] = set()
    out: list[str] = []
    for t in raw:
        if isinstance(t, str) and t.strip() and t not in seen:
            seen.add(t)
            out.append(t.strip())
    return out


# ── Routes ──


@router.post(
    "/{project_id}/work-queues",
    response_model=WorkQueueCreateResponse,
    status_code=201,
)
async def create_work_queue(
    project_id: str,
    body: WorkQueueCreate,
    auth: AuthContext = Depends(require_scope("work_queues:write")),
    db: AsyncSession = Depends(get_db),
) -> WorkQueueCreateResponse:
    """Create a work queue. Seeds items from selector.ticket_ids only.

    NO tier gate (all tiers). §9.1 caps enforced at create:
    cadence_seconds floor 120 / default 300; max_tickets_per_run default 1
    / hard cap 5; max_attempts_per_item default 3. Provenance triple
    stamped from AuthContext (+ optional session/persona).
    """
    project = await _get_project_for_auth(project_id, db, auth)
    # Cross-org / allowlist boundary BEFORE any side effect.
    await assert_service_key_can_access_project(db, auth, project)

    # Seed-ticket ids must belong to THIS project (no cross-project leak,
    # design §11 R10).
    ticket_ids = _extract_ticket_ids(body.selector)
    if ticket_ids:
        found = (
            await db.execute(
                select(Ticket.id).where(
                    Ticket.id.in_(ticket_ids),
                    Ticket.project_id == project_id,
                )
            )
        ).scalars().all()
        missing = set(ticket_ids) - set(found)
        if missing:
            raise HTTPException(
                422,
                {
                    "error": "cross_project_ticket",
                    "message": (
                        "selector.ticket_ids contains tickets not in this "
                        "project (cross-project references are rejected)."
                    ),
                    "missing_ticket_ids": sorted(missing),
                },
            )

    now = datetime.now(timezone.utc)
    cadence = (
        body.cadence_seconds
        if body.cadence_seconds is not None
        else _CADENCE_DEFAULT_SECONDS
    )
    max_tickets = (
        body.max_tickets_per_run
        if body.max_tickets_per_run is not None
        else _MAX_TICKETS_PER_RUN_DEFAULT
    )
    max_attempts = (
        body.max_attempts_per_item
        if body.max_attempts_per_item is not None
        else _MAX_ATTEMPTS_PER_ITEM_DEFAULT
    )

    queue = WorkQueue(
        id=_new_queue_id(),
        project_id=project_id,
        name=body.name,
        mode=body.mode,
        assigned_persona=body.assigned_persona,
        selector=json.dumps(body.selector),
        auto_adopt=body.auto_adopt,
        max_adopt_per_wake=body.max_adopt_per_wake,
        stop_condition=body.stop_condition,
        cadence_seconds=cadence,
        max_tickets_per_run=max_tickets,
        max_attempts_per_item=max_attempts,
        status="active",
        lease_epoch=0,
        created_by_user_id=auth.user.id,
        created_by_session_id=body.created_by_session_id,
        created_by_persona=body.created_by_persona,
        created_at=now,
        updated_at=now,
    )
    db.add(queue)

    items: list[WorkQueueItem] = []
    for tid in ticket_ids:
        item = WorkQueueItem(
            id=_new_item_id(),
            work_queue_id=queue.id,
            ticket_id=tid,
            item_status="pending",
            attempts=0,
            created_at=now,
            updated_at=now,
        )
        db.add(item)
        items.append(item)

    try:
        await db.commit()
    except Exception:
        await db.rollback()
        # uq_work_queue_project_name (project_id, name) collision.
        raise HTTPException(
            409,
            {
                "error": "work_queue_name_conflict",
                "message": (
                    f"A work queue named {body.name!r} already exists in this "
                    "project."
                ),
            },
        )
    await db.refresh(queue)

    summaries = [
        WorkQueueItemSummary(
            id=i.id, ticket_id=i.ticket_id, item_status=i.item_status
        )
        for i in items
    ]
    progress = await _progress_for_queue(queue.id, db)
    return WorkQueueCreateResponse(
        work_queue=_row_to_response(queue, progress, items),
        items=summaries,
    )


@router.get(
    "/{project_id}/work-queues",
    response_model=list[WorkQueueResponse],
)
async def list_work_queues(
    project_id: str,
    status: str | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
    auth: AuthContext = Depends(require_scope("work_queues:read")),
    db: AsyncSession = Depends(get_db),
) -> list[WorkQueueResponse]:
    """List work queues for this project with per-queue progress rollups."""
    project = await _get_project_for_auth(project_id, db, auth)
    await assert_service_key_can_access_project(db, auth, project)

    stmt = select(WorkQueue).where(WorkQueue.project_id == project_id)
    if status:
        stmt = stmt.where(WorkQueue.status == status)
    stmt = stmt.order_by(WorkQueue.created_at.desc()).limit(limit)
    queues = (await db.execute(stmt)).scalars().all()

    out: list[WorkQueueResponse] = []
    for q in queues:
        progress = await _progress_for_queue(q.id, db)
        out.append(_row_to_response(q, progress))
    return out


@router.get(
    "/{project_id}/work-queues/{queue_id}",
    response_model=WorkQueueResponse,
)
async def get_work_queue(
    project_id: str,
    queue_id: str,
    include_items: bool = Query(True),
    auth: AuthContext = Depends(require_scope("work_queues:read")),
    db: AsyncSession = Depends(get_db),
) -> WorkQueueResponse:
    """Inspect one queue + its item summary + progress rollup."""
    project = await _get_project_for_auth(project_id, db, auth)
    await assert_service_key_can_access_project(db, auth, project)

    queue = await _get_queue_or_404(project_id, queue_id, db)
    items: list[WorkQueueItem] | None = None
    if include_items:
        items = list(
            (
                await db.execute(
                    select(WorkQueueItem)
                    .where(WorkQueueItem.work_queue_id == queue_id)
                    .order_by(WorkQueueItem.created_at.asc())
                )
            ).scalars().all()
        )
    progress = await _progress_for_queue(queue_id, db)
    return _row_to_response(queue, progress, items)


@router.post(
    "/{project_id}/work-queues/{queue_id}/status",
    response_model=WorkQueueResponse,
)
async def set_work_queue_status(
    project_id: str,
    queue_id: str,
    body: WorkQueueStatusUpdate,
    auth: AuthContext = Depends(require_scope("work_queues:write")),
    db: AsyncSession = Depends(get_db),
) -> WorkQueueResponse:
    """Lifecycle verb. Server-enforced transition table; lease-fenced.

    Transition table (design §4.6):
        active    -> paused | completed | cancelled
        paused    -> active | completed | cancelled
        completed -> active (reopen) | cancelled
        cancelled -> (terminal)

    An illegal transition → 409 invalid_status_transition. A no-op
    (from == to) is rejected (no lease bump without a real change). The
    queue-level lease_epoch is fenced via an atomic UPDATE ... WHERE
    id=:id AND lease_epoch=:n (rowcount-1, 409 on stale) and bumps on
    success.
    """
    project = await _get_project_for_auth(project_id, db, auth)
    await assert_service_key_can_access_project(db, auth, project)

    queue = await _get_queue_or_404(project_id, queue_id, db)
    current = queue.status
    target = body.status

    if target == current:
        raise HTTPException(
            409,
            {
                "error": "invalid_status_transition",
                "message": f"Queue is already {current!r}.",
                "from_status": current,
                "to_status": target,
            },
        )
    allowed = _ALLOWED_TRANSITIONS.get(current, frozenset())
    if target not in allowed:
        raise HTTPException(
            409,
            {
                "error": "invalid_status_transition",
                "message": (
                    f"Cannot transition work queue from {current!r} to "
                    f"{target!r}. Allowed from {current!r}: "
                    f"{sorted(allowed)}."
                ),
                "from_status": current,
                "to_status": target,
                "allowed": sorted(allowed),
            },
        )

    now = datetime.now(timezone.utc)
    # Lease fence: atomic UPDATE WHERE id=:id AND lease_epoch=:n. When the
    # caller supplies lease_epoch, a concurrent mutation that already
    # bumped the epoch fails the predicate (rowcount 0) → 409. When the
    # caller omits it, fence on the epoch we just read (best-effort
    # optimistic concurrency). Either way the epoch bumps on success.
    fence_epoch = (
        body.lease_epoch if body.lease_epoch is not None else queue.lease_epoch
    )
    result = await db.execute(
        update(WorkQueue)
        .where(
            WorkQueue.id == queue_id,
            WorkQueue.project_id == project_id,
            WorkQueue.lease_epoch == fence_epoch,
            WorkQueue.status == current,
        )
        .values(
            status=target,
            lease_epoch=fence_epoch + 1,
            updated_at=now,
        )
    )
    if result.rowcount != 1:
        await db.rollback()
        current_epoch = (
            await db.execute(
                select(WorkQueue.lease_epoch).where(WorkQueue.id == queue_id)
            )
        ).scalar_one_or_none()
        raise HTTPException(
            409,
            {
                "error": "stale_lease_epoch",
                "message": (
                    "Work queue lease_epoch has advanced (or status changed) "
                    "since you read it. Re-fetch the queue and retry."
                ),
                "passed_lease_epoch": fence_epoch,
                "current_lease_epoch": current_epoch,
            },
        )
    await db.commit()

    queue = await _get_queue_or_404(project_id, queue_id, db)
    progress = await _progress_for_queue(queue_id, db)
    return _row_to_response(queue, progress)


# ── WQ-P3 (tk_3de50bf7bb73418b) — the step engine ──


@router.post("/{project_id}/work-queues/{queue_id}/step")
async def run_work_queue_step(
    project_id: str,
    queue_id: str,
    body: WorkQueueStepRequest | None = None,
    auth: AuthContext = Depends(require_scope("work_queues:write")),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """The heartbeat. Claims work, emits a BOUNDED directive, advances the
    SEEN cursor (never ACKED). Stateless from the caller's side; all loop
    state is server-side (design §4.4, §5, §6, §9.1).

    Acting requires work_queues:write AND tickets:write (the directive drives
    ticket comments). A dedicated rate-limit class (design §11 R6) guards this
    route per (org, project, service_key, queue) — Cloud Armor edge deny-429 is
    a separate Forge infra follow-up.

    review_until_clean queues return HTTP 422 not_available (the trusted-
    verdict stop oracle ships in WQ-P4).
    """
    _require_downstream_scopes(auth, "tickets:write")
    wake_source = body.wake_source if body is not None else "manual"
    wake_ref = body.wake_ref if body is not None else None
    max_tickets = body.max_tickets if body is not None else None

    project = await _get_project_for_auth(project_id, db, auth)
    await assert_service_key_can_access_project(db, auth, project)
    queue = await _get_queue_or_404(project_id, queue_id, db)

    if not _STEP_RATE_LIMITER.is_allowed(
        _step_rate_limit_key(auth, project_id, queue_id)
    ):
        raise HTTPException(
            429,
            {
                "error": "rate_limited",
                "message": (
                    "run_work_queue_step rate limit exceeded for this queue. "
                    "Respect the queue's cadence_seconds between wakes."
                ),
            },
        )

    try:
        result = await wq_engine.run_work_queue_step(
            db,
            queue=queue,
            wake_source=wake_source,
            wake_ref=wake_ref,
            max_tickets=max_tickets,
            actor_type=auth.actor_type,
            service_key_id=auth.service_key_id,
            service_key_name=auth.service_key_name,
        )
    except wq_engine.StepEngineError as exc:
        await db.rollback()
        raise HTTPException(
            exc.http_status,
            {"error": exc.code, "message": exc.message},
        )
    await db.commit()
    return result.to_dict()


@router.post("/{project_id}/work-queues/{queue_id}/step/complete")
async def complete_work_queue_step(
    project_id: str,
    queue_id: str,
    body: WorkQueueStepCompleteRequest,
    auth: AuthContext = Depends(require_scope("work_queues:write")),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Settle a directive — the SINGLE commit point of the loop (design §4.5).

    Idempotent on directive_id (a duplicate settle returns the prior outcome).
    Validates the claimed writeback landed before advancing the ACKED cursor
    (THE ONLY place it advances). On reported failure, applies the 2m→60m
    backoff and parks the item as 'failed' after max_attempts_per_item.

    Requires work_queues:write AND tickets:write; agent_runs:write is required
    only when an agent_run_id is being linked.
    """
    needed = ["tickets:write"]
    if body.agent_run_id:
        needed.append("agent_runs:write")
    _require_downstream_scopes(auth, *needed)

    project = await _get_project_for_auth(project_id, db, auth)
    await assert_service_key_can_access_project(db, auth, project)
    queue = await _get_queue_or_404(project_id, queue_id, db)

    # The settled ticket must belong to THIS project (design §11 R10).
    ticket_in_project = (
        await db.execute(
            select(Ticket.id).where(
                Ticket.id == body.ticket_id,
                Ticket.project_id == project_id,
            )
        )
    ).scalar_one_or_none()
    if ticket_in_project is None:
        raise HTTPException(
            422,
            {
                "error": "cross_project_ticket",
                "message": (
                    "ticket_id does not belong to this project "
                    "(cross-project settle rejected)."
                ),
            },
        )

    try:
        result = await wq_engine.complete_work_queue_step(
            db,
            queue=queue,
            item_id=body.item_id,
            directive_id=body.directive_id,
            ticket_id=body.ticket_id,
            outcome=body.outcome,
            comment_id=body.comment_id,
            agent_run_id=body.agent_run_id,
            failed=body.failed,
            ticket_lease_epoch=body.ticket_lease_epoch,
            agent_summary=body.summary,
        )
    except wq_engine.StepEngineError as exc:
        await db.rollback()
        raise HTTPException(
            exc.http_status,
            {"error": exc.code, "message": exc.message},
        )
    await db.commit()
    return result.to_dict()
