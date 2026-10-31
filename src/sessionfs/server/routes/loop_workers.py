"""Review Loop R3 — worker runs and review rounds (Brief D3).

Three endpoints that wrap the existing ticket-start, agent-run usage, budget
and work-queue verdict code so a worker (or a reviewer) can drive a loop with
one call each:

- ``POST …/agent-runs/start-worker``     take the ticket lease + open an
  implementer run linked to the loop's queue item.
- ``POST …/agent-runs/{run_id}/finish``  close the run, record usage
  additively, run the budget check. Idempotent on (run id, head_sha).
- ``POST …/loops/{loop_id}/rounds``      record one review round: a reviewer
  run with validated findings plus the verdict comment, posted through the
  work-queue verdict helper so trust comes only from the credential.

Every miss the caller cannot see is the same ``Loop not found`` 404 body;
dollars are present only for org owners/admins (or the personal-project
owner) and absent otherwise.

Design: docs/design/review-loop-r3.md §4, §5, §6.4, §6.5, §7.
"""

from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server.auth.dependencies import AuthContext, require_scope
from sessionfs.server.auth.project_access import require_project_editor
from sessionfs.server.db.engine import get_db
from sessionfs.server.sanitize import sanitize_archive_string
from sessionfs.server.db.models import (
    AgentRun,
    Project,
    Ticket,
    TicketComment,
    WorkQueue,
    WorkQueueItem,
)
from sessionfs.server.routes.agent_runs import (
    _MAX_COST_USD,
    _VALID_COST_BASIS,
    _VALID_EXIT_REASONS,
    _AgentRunUsage,
    _additive_usage,
    _usage_within_caps,
    _validate_finding,
)
from sessionfs.server.routes.loops import (
    _LOOP_NOT_FOUND,
    _load_loop_config,
    _load_loop_for_editor,
    _readable_project,
    _show_dollars,
)
from sessionfs.server.routes.loop_checklist import _sanitize_text
from sessionfs.server.services.loop_budget import (
    BudgetStatus,
    budget_status,
    park_item_needs_human,
)
from sessionfs.server.services.review_state import compute_review_state
from sessionfs.server.services.scope_locks import lock_project_scope_authorized
from sessionfs.server.services.ticket_transitions import TransitionRefused, start_ticket
from sessionfs.server.services.work_queues import (
    StepEngineError,
    _review_comments_for_oracle,
    post_server_verdict,
)
from sessionfs.server.tier_gate import UserContext, check_project_feature, get_user_context

router = APIRouter(prefix="/api/v1/projects", tags=["loop-workers"])

_COMMIT_RE = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_SHORT_SHA_RE = re.compile(r"^[0-9a-f]{7,64}$")
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")

_MAX_FINDINGS = 200
_MAX_TITLE = 300
_MAX_SUMMARY = 4000
_MAX_ROUND = 1000

_OPEN_ITEM_STATUSES = ("pending", "active", "waiting")
_EXIT_TO_STATUS = {"finished": "passed", "out_of_turns": "failed", "crashed": "errored"}
_REVIEW_ROLES = ("reviewer", "design-reviewer")


# ── Shared helpers ──


def _require_scopes(auth: AuthContext, needed: tuple[str, ...]) -> None:
    """A service key needs the write scopes of every surface a call touches
    (same downstream-scope rule as loop start). User keys carry ``*``."""
    if "*" in auth.scopes:
        return
    missing = [s for s in needed if s not in auth.scopes]
    if missing:
        raise HTTPException(
            403,
            {
                "error": "insufficient_scope",
                "required": sorted(needed),
                "current": auth.scopes,
                "message": f"this call exercises downstream write scopes: missing {missing}.",
            },
        )


async def _editor_project(
    project_id: str, db: AsyncSession, auth: AuthContext
) -> Project:
    """Visibility first (single 404 body, opaque for cross-org keys), then
    editor standing for user keys."""
    project = await _readable_project(project_id, db, auth)
    if auth.key_kind != "service":
        await require_project_editor(db, auth.user.id, project)
    return project


def _nf() -> HTTPException:
    return HTTPException(404, _LOOP_NOT_FOUND)


def _needs_human(detail: str) -> HTTPException:
    return HTTPException(409, {"error": "loop_needs_human", "message": detail})


async def _loop_for_ticket(
    db: AsyncSession, ticket: Ticket
) -> tuple[Ticket | None, dict[str, Any], WorkQueue | None, WorkQueueItem | None]:
    """(loop parent, config, queue, item) for a loop child, else Nones."""
    if not ticket.parent_ticket_id:
        return None, {}, None, None
    parent = await db.get(Ticket, ticket.parent_ticket_id)
    if (
        parent is None
        or parent.project_id != ticket.project_id
        or not parent.loop_config
    ):
        return None, {}, None, None
    config = _load_loop_config(parent.loop_config)
    queue_id = config.get("queue_id")
    queue = await db.get(WorkQueue, queue_id) if queue_id else None
    if queue is not None and queue.project_id != ticket.project_id:
        queue = None
    item = None
    if queue is not None:
        item = (
            await db.execute(
                select(WorkQueueItem).where(
                    WorkQueueItem.work_queue_id == queue.id,
                    WorkQueueItem.ticket_id == ticket.id,
                )
            )
        ).scalar_one_or_none()
    return parent, config, queue, item


def _budget_view(status: BudgetStatus, *, dollars: bool) -> dict[str, Any]:
    cfg = status.config
    out: dict[str, Any] = {
        "tokens_used": status.tokens_used,
        "token_budget": cfg.token_budget,
        "tokens_remaining": (
            max(cfg.token_budget - status.tokens_used, 0)
            if cfg.token_budget is not None
            else None
        ),
        "rounds_used": status.rounds_used,
        "max_review_rounds": cfg.max_review_rounds,
        "rounds_remaining": (
            max(cfg.max_review_rounds - status.rounds_used, 0)
            if cfg.max_review_rounds is not None
            else None
        ),
    }
    # Dollar keys are ABSENT (not null) unless the caller may see dollars.
    if dollars and cfg.cost_budget_usd is not None:
        out["cost_budget_usd"] = cfg.cost_budget_usd
        out["cost_remaining_usd"] = max(
            cfg.cost_budget_usd - float(status.cost_usd_used), 0.0
        )
    return out


def _run_tokens(run: AgentRun) -> int:
    return sum(
        int(v or 0)
        for v in (
            run.input_tokens,
            run.output_tokens,
            run.cache_read_tokens,
            run.cache_write_tokens,
        )
    )


def _clean_text(limit: int):
    """Before-validator: strip NULs / lone surrogates (PostgreSQL rejects both
    and the request would 500) and cap the length."""

    def _inner(v: Any) -> Any:
        if not isinstance(v, str):
            return v
        v, _n = sanitize_archive_string(v)
        return v[:limit]

    return _inner


# ── 1. start-worker ──


class StartWorker(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ticket_id: str = Field(min_length=1, max_length=64)
    model: str | None = Field(default=None, max_length=120)
    provider: str | None = Field(default=None, max_length=40)
    branch: str | None = Field(default=None, max_length=200)
    head_sha: str | None = None
    tool: str = Field(default="generic", max_length=30)

    _clean_model = field_validator("model", mode="before")(classmethod(lambda c, v: _clean_text(100)(v)))
    _clean_provider = field_validator("provider", mode="before")(classmethod(lambda c, v: _clean_text(40)(v)))
    _clean_branch = field_validator("branch", mode="before")(classmethod(lambda c, v: _clean_text(200)(v)))
    _clean_tool = field_validator("tool", mode="before")(classmethod(lambda c, v: _clean_text(30)(v)))

    @field_validator("head_sha")
    @classmethod
    def _sha(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = v.strip().lower()
        if not _SHORT_SHA_RE.match(v):
            raise ValueError("head_sha must be 7-64 lowercase hex characters")
        return v


@router.post("/{project_id}/agent-runs/start-worker", status_code=201)
async def start_worker_run(
    project_id: str,
    body: StartWorker,
    auth: AuthContext = Depends(require_scope("loops:write")),
    ctx: UserContext = Depends(get_user_context),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Take the ticket lease and open an implementer run linked to the loop.

    Refuses with 409 ``loop_needs_human`` when the item is parked or a budget
    is already exhausted (the item is parked in the same call). The ticket
    start is the existing rowcount-guarded transition; the lease epoch it
    bumps is returned for fencing later writes.
    """
    project = await _editor_project(project_id, db, auth)
    _require_scopes(auth, ("tickets:write", "agent_runs:write"))
    ticket = (
        await db.execute(
            select(Ticket).where(
                Ticket.id == body.ticket_id, Ticket.project_id == project_id
            )
        )
    ).scalar_one_or_none()
    if ticket is None:
        raise _nf()
    await check_project_feature(ctx, "agent_tickets", project, db)
    await lock_project_scope_authorized(
        db, project, auth=auth, ctx=ctx, feature="agent_tickets"
    )

    parent, _config, queue, item = await _loop_for_ticket(db, ticket)
    dollars = await _show_dollars(db, auth, project)
    if item is not None:
        if item.item_status == "needs_human":
            raise _needs_human("This loop item is parked for a human.")
        if item.item_status in ("done", "failed"):
            raise HTTPException(
                409, {"error": "loop_item_closed", "message": "This loop item is closed."}
            )
    if parent is not None:
        status = await budget_status(db, ticket)
        if status.exceeded:
            parked = False
            if queue is not None and item is not None:
                parked = await park_item_needs_human(
                    db,
                    queue=queue,
                    item=item,
                    ticket=ticket,
                    budget=status,
                    actor_type=auth.actor_type,
                    service_key_name=auth.service_key_name,
                )
            await db.commit()
            raise _needs_human(
                f"The loop's {status.limit_summary()} is reached"
                + (" and the item is parked." if parked else ".")
            )

    try:
        await start_ticket(
            db,
            ticket,
            allowed_from={"open", "in_progress"},
            actor_user_id=auth.user.id,
            actor_type=auth.actor_type,
            service_key_name=auth.service_key_name,
        )
    except TransitionRefused as exc:
        raise HTTPException(exc.status_code, str(exc))

    # A loop-level run (e.g. the design review) is the natural parent; the
    # previous implementer run on this ticket is what this one continues.
    parent_run_id = None
    continues_run_id = None
    if parent is not None:
        parent_run_id = (
            await db.execute(
                select(AgentRun.id)
                .where(AgentRun.project_id == project_id, AgentRun.ticket_id == parent.id)
                .order_by(AgentRun.created_at.desc(), AgentRun.id.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
    continues_run_id = (
        await db.execute(
            select(AgentRun.id)
            .where(
                AgentRun.project_id == project_id,
                AgentRun.ticket_id == ticket.id,
                AgentRun.role == "implementer",
            )
            .order_by(AgentRun.created_at.desc(), AgentRun.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()

    now = datetime.now(timezone.utc)
    run = AgentRun(
        id=f"run_{uuid.uuid4().hex[:16]}",
        project_id=project_id,
        persona_name=(ticket.assigned_to or "implementer")[:50],
        tool=body.tool,
        trigger_source="manual",
        status="running",
        ticket_id=ticket.id,
        triggered_by_user_id=auth.user.id,
        actor_type=auth.actor_type,
        service_key_id=auth.service_key_id,
        service_key_name=auth.service_key_name,
        created_at=now,
        started_at=now,
        model=body.model,
        provider=body.provider,
        role="implementer",
        branch=body.branch,
        head_sha=None,
        base_sha=body.head_sha,
        parent_run_id=parent_run_id,
        continues_run_id=continues_run_id,
        work_queue_id=queue.id if queue is not None else None,
        work_queue_item_id=item.id if item is not None else None,
    )
    db.add(run)
    await db.commit()
    await db.refresh(ticket)

    view: dict[str, Any] | None = None
    if parent is not None:
        view = _budget_view(await budget_status(db, ticket), dollars=dollars)
    return {
        "run_id": run.id,
        "lease_epoch": ticket.lease_epoch,
        "ticket_id": ticket.id,
        "loop_id": parent.id if parent is not None else None,
        "budget": view,
    }


# ── 2. finish ──


class FinishUsage(_AgentRunUsage):
    cost_basis: str | None = None

    @field_validator("cost_basis")
    @classmethod
    def _basis(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = v.strip().lower()
        if v not in _VALID_COST_BASIS:
            raise ValueError(f"cost_basis must be one of: {list(_VALID_COST_BASIS)}")
        return v


class FinishWorker(BaseModel):
    model_config = ConfigDict(extra="forbid")

    head_sha: str
    branch: str | None = Field(default=None, max_length=200)
    exit_reason: str
    usage: FinishUsage | None = None
    summary: str | None = Field(default=None, max_length=20_000)

    _clean_branch = field_validator("branch", mode="before")(classmethod(lambda c, v: _clean_text(200)(v)))

    @field_validator("head_sha")
    @classmethod
    def _sha(cls, v: str) -> str:
        v = (v or "").strip().lower()
        if not _SHORT_SHA_RE.match(v):
            raise ValueError("head_sha must be 7-64 lowercase hex characters")
        return v

    @field_validator("exit_reason")
    @classmethod
    def _exit(cls, v: str) -> str:
        v = (v or "").strip().lower()
        if v not in _VALID_EXIT_REASONS:
            raise ValueError(f"exit_reason must be one of: {list(_VALID_EXIT_REASONS)}")
        return v


async def _finish_view(
    db: AsyncSession,
    run: AgentRun,
    ticket: Ticket | None,
    *,
    dollars: bool,
    idempotent: bool,
) -> dict[str, Any]:
    item = (
        await db.get(WorkQueueItem, run.work_queue_item_id)
        if run.work_queue_item_id
        else None
    )
    out: dict[str, Any] = {
        "run_id": run.id,
        "status": run.status,
        "ticket_id": run.ticket_id,
        "head_sha": run.head_sha,
        "branch": run.branch,
        "exit_reason": run.exit_reason,
        "tokens": _run_tokens(run),
        "item_status": item.item_status if item is not None else None,
        "needs_human": bool(item is not None and item.item_status == "needs_human"),
        "idempotent": idempotent,
    }
    if ticket is not None and ticket.parent_ticket_id:
        parent = await db.get(Ticket, ticket.parent_ticket_id)
        if parent is not None and parent.loop_config:
            out["loop_id"] = parent.id
            out["budget"] = _budget_view(await budget_status(db, ticket), dollars=dollars)
    if dollars and run.cost_usd is not None:
        out["cost_usd"] = float(run.cost_usd)
    return out


@router.post("/{project_id}/agent-runs/{run_id}/finish")
async def finish_worker_run(
    project_id: str,
    run_id: str,
    body: FinishWorker,
    auth: AuthContext = Depends(require_scope("loops:write")),
    ctx: UserContext = Depends(get_user_context),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Close a worker run: head sha, exit reason, usage, budget check.

    Idempotent on (run id, head_sha): repeating the same head returns the
    stored result without adding usage again; a different head after finish
    is a 409. Usage goes through the same additive SQL as the usage endpoint.
    """
    project = await _editor_project(project_id, db, auth)
    _require_scopes(auth, ("agent_runs:write",))
    run = (
        await db.execute(
            select(AgentRun).where(
                AgentRun.id == run_id,
                AgentRun.project_id == project_id,
                AgentRun.role.in_(("implementer", "ui-implementer")),
            )
        )
    ).scalar_one_or_none()
    if run is None:
        raise _nf()
    await check_project_feature(ctx, "agent_runs", project, db)
    dollars = await _show_dollars(db, auth, project)

    ticket = await db.get(Ticket, run.ticket_id) if run.ticket_id else None

    def _replay_or_conflict(current: AgentRun) -> bool:
        if current.head_sha == body.head_sha:
            return True
        raise HTTPException(
            409,
            {
                "error": "head_sha_conflict",
                "message": "This run already finished with a different head_sha.",
            },
        )

    if run.status not in ("queued", "running"):
        _replay_or_conflict(run)
        return await _finish_view(db, run, ticket, dollars=dollars, idempotent=True)

    usage_in = body.usage
    values: dict[str, Any] = {}
    cost = None
    if usage_in is not None:
        cost = usage_in.cost_usd
        values = _additive_usage(
            input_tokens=usage_in.input_tokens,
            output_tokens=usage_in.output_tokens,
            cache_read_tokens=usage_in.cache_read_tokens,
            cache_write_tokens=usage_in.cache_write_tokens,
            cost_usd=cost,
            cost_basis=usage_in.cost_basis,
        )
    now = datetime.now(timezone.utc)
    status = _EXIT_TO_STATUS[body.exit_reason]
    duration = None
    if run.started_at is not None:
        started = run.started_at
        if started.tzinfo is None:
            started = started.replace(tzinfo=timezone.utc)
        duration = int((now - started).total_seconds())
    summary = _sanitize_text(body.summary)[:_MAX_SUMMARY] if body.summary else None
    extra: dict[str, Any] = {}
    if body.branch:
        extra["branch"] = body.branch
    result = await db.execute(
        update(AgentRun)
        .where(
            AgentRun.id == run_id,
            AgentRun.project_id == project_id,
            AgentRun.status.in_(("queued", "running")),
            *_usage_within_caps(cost_usd=cost),
        )
        .values(
            status=status,
            completed_at=now,
            duration_seconds=duration,
            result_summary=summary,
            severity="none",
            policy_result="pass" if status == "passed" else "fail",
            exit_code=0 if status == "passed" else 1,
            head_sha=body.head_sha,
            exit_reason=body.exit_reason,
            occurred_at=now,
            **extra,
            **values,
        )
    )
    if result.rowcount != 1:
        await db.rollback()
        current = (
            await db.execute(
                select(AgentRun).where(
                    AgentRun.id == run_id, AgentRun.project_id == project_id
                )
            )
        ).scalar_one_or_none()
        if current is None:
            raise _nf()
        if current.status in ("queued", "running"):
            raise HTTPException(
                422, f"cost_usd total for a run must stay at or below {_MAX_COST_USD}"
            )
        _replay_or_conflict(current)
        ticket = await db.get(Ticket, current.ticket_id) if current.ticket_id else None
        return await _finish_view(db, current, ticket, dollars=dollars, idempotent=True)

    # Budget check: a cap that only reveals itself once usage lands moves the
    # item to needs_human (guarded park, one notification).
    await db.flush()
    if ticket is not None and run.work_queue_id and run.work_queue_item_id:
        queue = await db.get(WorkQueue, run.work_queue_id)
        item = await db.get(WorkQueueItem, run.work_queue_item_id)
        if queue is not None and item is not None:
            bstatus = await budget_status(db, ticket)
            if bstatus.exceeded:
                await park_item_needs_human(
                    db,
                    queue=queue,
                    item=item,
                    ticket=ticket,
                    budget=bstatus,
                    actor_type=auth.actor_type,
                    service_key_name=auth.service_key_name,
                )
    await db.commit()
    db.expire_all()
    run = (
        await db.execute(select(AgentRun).where(AgentRun.id == run_id))
    ).scalar_one()
    ticket = await db.get(Ticket, run.ticket_id) if run.ticket_id else None
    return await _finish_view(db, run, ticket, dollars=dollars, idempotent=False)


# ── 3. review rounds ──


class RoundFinding(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    severity: Literal["low", "medium", "high", "critical"]
    class_: str = Field(alias="class", min_length=1, max_length=64)
    harm: Literal["data-exposed", "money-spent", "wrong-actor", "crash", "other", "none"]
    title: str = Field(min_length=1, max_length=_MAX_TITLE)
    file: str | None = Field(default=None, max_length=300)
    line: int | None = Field(default=None, ge=1, le=10_000_000)
    status: Literal["open", "fixed", "accepted", "ticketed"] | None = None

    @field_validator("class_")
    @classmethod
    def _class(cls, v: str) -> str:
        if v != "new" and not _SLUG_RE.match(v):
            raise ValueError("class must be a lowercase slug or 'new'")
        return v


class ReviewRound(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ticket_id: str = Field(min_length=1, max_length=64)
    round: int = Field(ge=1, le=_MAX_ROUND)
    stage: Literal["design", "code"]
    reviewed_commit: str
    verdict: Literal["VERIFIED-CLEAN", "CHANGES-REQUESTED"]
    findings: list[RoundFinding] = Field(default_factory=list, max_length=_MAX_FINDINGS)
    model: str | None = Field(default=None, max_length=120)
    provider: str | None = Field(default=None, max_length=40)
    usage: FinishUsage | None = None

    _clean_model = field_validator("model", mode="before")(classmethod(lambda c, v: _clean_text(100)(v)))
    _clean_provider = field_validator("provider", mode="before")(classmethod(lambda c, v: _clean_text(40)(v)))

    @field_validator("round", mode="before")
    @classmethod
    def _round_int(cls, v: Any) -> Any:
        if isinstance(v, bool) or not isinstance(v, int):
            raise ValueError("round must be an integer")
        return v

    @field_validator("reviewed_commit")
    @classmethod
    def _commit(cls, v: str) -> str:
        v = (v or "").strip().lower()
        if not _COMMIT_RE.match(v):
            raise ValueError("reviewed_commit must be a full 40 or 64 hex commit id")
        return v


_SEV_ORDER = {"none": 0, "low": 1, "medium": 2, "high": 3, "critical": 4}


def _verdict_text(
    body: ReviewRound, findings: list[dict[str, Any]], ticket_id: str
) -> str:
    """Header + findings in the shape the review-state oracle parses."""
    label = "VERIFIED-CLEAN" if body.verdict == "VERIFIED-CLEAN" else "CHANGES REQUESTED"
    kind = "doc review" if body.stage == "design" else "review"
    lines = [f"Reviewer R{body.round} {kind} on {ticket_id}: {label}", ""]
    open_ones = [f for f in findings if f.get("status") in (None, "open")]
    if not open_ones:
        lines.append("Findings: none.")
    else:
        lines.append("Findings:")
        for f in open_ones:
            where = ""
            if f.get("file"):
                where = f" ({f['file']}" + (f":{f['line']}" if f.get("line") else "") + ")"
            lines.append(
                f" • {str(f['severity']).upper()} — {f['title']}{where} harm: {f['harm']}"
            )
    return "\n".join(lines)


async def _round_view(
    db: AsyncSession,
    run: AgentRun,
    ticket: Ticket,
    *,
    idempotent: bool,
    dollars: bool,
) -> dict[str, Any]:
    comment = (
        await db.get(TicketComment, run.trigger_ref) if run.trigger_ref else None
    )
    item = (
        await db.get(WorkQueueItem, run.work_queue_item_id)
        if run.work_queue_item_id
        else None
    )
    out: dict[str, Any] = {
        "run_id": run.id,
        "ticket_id": run.ticket_id,
        "round": run.review_round,
        "stage": "design" if run.role == "design-reviewer" else "code",
        "reviewed_commit": run.head_sha,
        "comment_id": comment.id if comment is not None else None,
        "verdict_trusted": bool(comment.verdict_trusted) if comment is not None else False,
        "findings_count": run.findings_count,
        "item_status": item.item_status if item is not None else None,
        "needs_human": bool(item is not None and item.item_status == "needs_human"),
        "idempotent": idempotent,
        "tokens": _run_tokens(run),
    }
    if ticket.parent_ticket_id:
        out["budget"] = _budget_view(await budget_status(db, ticket), dollars=dollars)
    if dollars and run.cost_usd is not None:
        out["cost_usd"] = float(run.cost_usd)
    return out


async def _reviewer_is_independent(
    db: AsyncSession, ticket: Ticket, auth: Any
) -> bool:
    """True only when the reviewer is provably not any recorded implementer.

    Uses the shared identity loaders (work-queue items, implementer runs, the
    current lease's start context, the resolver). Fails closed: an empty
    implementer set or an unresolvable service-key minter is NOT independent.
    """
    from sessionfs.server.services.review_state import (
        load_implementer_identities,
        load_key_minters,
    )

    identities = [
        (svc, usr)
        for svc, usr in (await load_implementer_identities(db, [ticket])).get(ticket.id, [])
        if svc or usr
    ]
    if not identities:
        return False
    reviewer_key = auth.service_key_id if auth.key_kind == "service" else None
    pseudo = [{"verdict_trusted": True, "service_key_id": reviewer_key}] if reviewer_key else []
    minters = await load_key_minters(db, pseudo, identities)

    reviewer_users: set[str] = {auth.user.id}
    if reviewer_key:
        minter = minters.get(reviewer_key)
        if not minter:
            return False
        reviewer_users.add(minter)
    for svc, usr in identities:
        if usr and usr in reviewer_users:
            return False
        if svc:
            if reviewer_key and svc == reviewer_key:
                return False
            minter = minters.get(svc)
            if not minter:
                return False
            if minter in reviewer_users:
                return False
    return True


@router.post("/{project_id}/loops/{loop_id}/rounds", status_code=201)
async def record_review_round(
    project_id: str,
    loop_id: str,
    body: ReviewRound,
    auth: AuthContext = Depends(require_scope("loops:write")),
    ctx: UserContext = Depends(get_user_context),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Record one review round for a loop ticket (or the parent, for design).

    Creates the reviewer run with its validated findings, posts the verdict
    comment through the work-queue verdict helper (trust comes from the
    authenticated credential and the trusted-reviewers registry, never from
    the body), records usage, proposes checklist items for ``class: new``
    findings and enforces the round/token caps. Idempotent on (ticket, round,
    stage): a repeat returns the stored round and posts no second verdict.
    """
    project = await _editor_project(project_id, db, auth)
    _require_scopes(auth, ("tickets:write", "agent_runs:write", "work_queues:write"))
    try:
        loop = await _load_loop_for_editor(db, project_id, loop_id)
    except HTTPException:
        raise _nf()
    config = _load_loop_config(loop.loop_config)
    queue_id = config.get("queue_id")
    queue = await db.get(WorkQueue, queue_id) if queue_id else None
    if queue is None or queue.project_id != project_id:
        raise _nf()

    if body.ticket_id == loop.id:
        ticket = loop
        if body.stage != "design":
            raise HTTPException(
                422, "code rounds target a child ticket; the loop parent only takes design rounds"
            )
    else:
        child = (
            await db.execute(
                select(Ticket).where(
                    Ticket.id == body.ticket_id,
                    Ticket.project_id == project_id,
                    Ticket.parent_ticket_id == loop.id,
                )
            )
        ).scalar_one_or_none()
        if child is None:
            raise _nf()
        ticket = child

    await check_project_feature(ctx, "agent_tickets", project, db)
    await lock_project_scope_authorized(
        db, project, auth=auth, ctx=ctx, feature="agent_tickets"
    )
    dollars = await _show_dollars(db, auth, project)

    item = None
    if ticket.id != loop.id:
        item = (
            await db.execute(
                select(WorkQueueItem).where(
                    WorkQueueItem.work_queue_id == queue.id,
                    WorkQueueItem.ticket_id == ticket.id,
                )
            )
        ).scalar_one_or_none()
        if item is None:
            raise _nf()

    # Serialize concurrent identical rounds: a no-op write takes the row lock
    # (PostgreSQL) before the idempotency read.
    now = datetime.now(timezone.utc)
    if item is not None:
        await db.execute(
            update(WorkQueueItem).where(WorkQueueItem.id == item.id).values(updated_at=now)
        )
    else:
        await db.execute(
            update(Ticket).where(Ticket.id == ticket.id).values(updated_at=ticket.updated_at)
        )

    role = "design-reviewer" if body.stage == "design" else "reviewer"
    existing = (
        await db.execute(
            select(AgentRun).where(
                AgentRun.project_id == project_id,
                AgentRun.ticket_id == ticket.id,
                AgentRun.role == role,
                AgentRun.review_round == body.round,
            )
        )
    ).scalars().first()
    if existing is not None:
        out = await _round_view(db, existing, ticket, idempotent=True, dollars=dollars)
        await db.rollback()
        return out

    if item is not None:
        if item.item_status == "needs_human":
            raise _needs_human("This loop item is parked for a human.")
        if item.item_status in ("done", "failed"):
            raise HTTPException(
                409, {"error": "loop_item_closed", "message": "This loop item is closed."}
            )

    findings: list[dict[str, Any]] = []
    for f in body.findings:
        d: dict[str, Any] = {
            "severity": f.severity,
            "class": f.class_,
            "harm": f.harm,
            "title": _sanitize_text(f.title)[:_MAX_TITLE],
            "round": body.round,
            "stage": body.stage,
        }
        if f.file:
            d["file"] = _sanitize_text(f.file)[:300]
        if f.line:
            d["line"] = f.line
        if f.status:
            d["status"] = f.status
        try:
            _validate_finding(d)
        except ValueError as exc:
            raise HTTPException(422, str(exc))
        findings.append(d)

    # Verdict through the shared work-queue helper — trust from the credential.
    text = _verdict_text(body, findings, ticket.id)
    try:
        comment, _jira_ids = await post_server_verdict(
            db,
            queue=queue,
            ticket_id=ticket.id,
            verdict_content=text,
            reviewed_commit=body.reviewed_commit,
            actor_user_id=auth.user.id,
            actor_org_id=project.org_id,
            actor_service_key_id=auth.service_key_id,
            actor_type=auth.actor_type,
            service_key_name=auth.service_key_name,
            now=now,
        )
    except StepEngineError as exc:
        raise HTTPException(exc.http_status, exc.message)

    usage_vals: dict[str, Any] = {}
    if body.usage is not None:
        u = body.usage
        if u.cost_usd is not None and u.cost_usd > _MAX_COST_USD:
            raise HTTPException(422, f"cost_usd must be at most {_MAX_COST_USD}")
        usage_vals = {
            "input_tokens": u.input_tokens,
            "output_tokens": u.output_tokens,
            "cache_read_tokens": u.cache_read_tokens,
            "cache_write_tokens": u.cache_write_tokens,
            "cost_usd": Decimal(str(u.cost_usd)) if u.cost_usd is not None else None,
            "cost_basis": u.cost_basis
            or ("reported" if u.cost_usd is not None else None),
        }
    top = max((_SEV_ORDER.get(str(f["severity"]), 0) for f in findings), default=0)
    severity = next(k for k, v in _SEV_ORDER.items() if v == top)
    run = AgentRun(
        id=f"run_{uuid.uuid4().hex[:16]}",
        project_id=project_id,
        persona_name=(queue.assigned_persona or "reviewer")[:50],
        tool="generic",
        trigger_source="manual",
        status="passed",
        ticket_id=ticket.id,
        trigger_ref=comment.id,
        triggered_by_user_id=auth.user.id,
        actor_type=auth.actor_type,
        service_key_id=auth.service_key_id,
        service_key_name=auth.service_key_name,
        created_at=now,
        started_at=now,
        completed_at=now,
        duration_seconds=0,
        occurred_at=now,
        severity=severity,
        findings=json.dumps(findings),
        findings_count=len(findings),
        policy_result="pass",
        exit_code=0,
        model=body.model,
        provider=body.provider,
        role=role,
        head_sha=body.reviewed_commit,
        work_queue_id=queue.id,
        work_queue_item_id=item.id if item is not None else None,
        review_round=body.round,
        **usage_vals,
    )
    db.add(run)
    await db.flush()

    # Item bookkeeping for code rounds: close on a trusted strict clean from
    # an identity other than the implementer; otherwise enforce the caps.
    if item is not None and body.stage == "code":
        closed = False
        if comment.verdict_trusted and body.verdict == "VERIFIED-CLEAN":
            rs = compute_review_state(
                await _review_comments_for_oracle(db, ticket_id=ticket.id)
            )
            clean = (
                rs is not None
                and rs.last_verdict_is_strict_verified_clean
                and not rs.open_findings
            )
            if clean and await _reviewer_is_independent(db, ticket, auth):
                res = await db.execute(
                    update(WorkQueueItem)
                    .where(
                        WorkQueueItem.id == item.id,
                        WorkQueueItem.item_status.in_(_OPEN_ITEM_STATUSES),
                    )
                    .values(
                        item_status="done",
                        open_directive_id=None,
                        open_directive_run_id=None,
                        next_eligible_at=None,
                        closed_by_user_id=None if auth.key_kind == "service" else auth.user.id,
                        closed_by_service_key_id=auth.service_key_id,
                        auto_close_review_kind=(
                            "resident_trusted" if auth.key_kind == "service" else "human"
                        ),
                        updated_at=now,
                    )
                )
                closed = res.rowcount == 1
        if not closed:
            bstatus = await budget_status(db, ticket)
            if bstatus.exceeded:
                await park_item_needs_human(
                    db,
                    queue=queue,
                    item=item,
                    ticket=ticket,
                    budget=bstatus,
                    actor_type=auth.actor_type,
                    service_key_name=auth.service_key_name,
                )

    run_id = run.id
    ticket_pk = ticket.id
    await db.commit()

    # Checklist proposals (class="new") strictly after the round is durable;
    # a failure never fails the round.
    try:
        from sessionfs.server.routes.loop_checklist import propose_from_findings

        await propose_from_findings(
            db,
            project_id,
            run_id,
            findings,
            None if auth.key_kind == "service" else auth.user.id,
            created_actor_type=auth.actor_type,
            created_service_key_id=auth.service_key_id,
            created_service_key_name=auth.service_key_name,
        )
    except Exception:
        await db.rollback()

    # Re-evaluate the ticket's merge check on a detached task so a slow or
    # failing provider never delays or fails the round.
    from sessionfs.server.services import merge_checks

    merge_checks.schedule_recheck(ticket_pk)

    db.expire_all()
    run = (await db.execute(select(AgentRun).where(AgentRun.id == run_id))).scalar_one()
    ticket = await db.get(Ticket, ticket_pk)
    assert ticket is not None
    return await _round_view(db, run, ticket, idempotent=False, dollars=dollars)


__all__ = ["router"]
