"""Agent run CRUD + lifecycle routes — v0.10.2.

AgentRun is the execution record for ephemeral agent work. Six routes
under `/api/v1/projects/{project_id}/agent-runs`:

    POST   /                          — create queued
    GET    /                          — list with filters
    GET    /{run_id}                  — detail
    POST   /{run_id}/start            — queued → running, return compiled context
    POST   /{run_id}/complete         — running/queued → passed/failed/errored
    POST   /{run_id}/cancel           — queued/running → cancelled

Status FSM (enforced via atomic `UPDATE ... WHERE status=X` rowcount
guards, same pattern as v0.10.1 tickets):

    queued → running → passed | failed | errored
    queued → cancelled
    running → cancelled

Cross-project safety: when a run references a ticket or persona, those
references must belong to the same project as the run. Lookups filter
by project_id so a guessed run_id from another project surfaces as 404,
not as cross-project data leak.

Policy evaluation runs at complete time. Severity hierarchy:
    none < low < medium < high < critical
A run fails when severity meets or exceeds `fail_on`; `severity=none`
never trips a threshold. The stored `policy_result` is `"pass"` or
`"fail"`; `exit_code` is 0 (pass) or 1 (fail). The CLI exits with
`exit_code` when `--fail-on` is set so CI builds can gate on it.

Tier-gated via `agent_runs` (Team+ — same tier as tickets since runs
are typically execution records tied to ticket work).
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, field_validator
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server.auth.dependencies import get_current_user
from sessionfs.server.db.engine import get_db
from sessionfs.server.db.models import AgentPersona, AgentRun, Ticket, User
from sessionfs.server.routes.tickets import _compile_persona_context
from sessionfs.server.routes.wiki import _get_project_or_404
from sessionfs.server.tier_gate import UserContext, check_feature, get_user_context

router = APIRouter(prefix="/api/v1/projects", tags=["agent-runs"])


# ── Enums (validated client-side; server is the source of truth) ──

_VALID_TRIGGER_SOURCES: set[str] = {
    "manual", "ci", "webhook", "scheduled", "mcp", "api",
}
_VALID_STATUSES: set[str] = {
    "queued", "running", "passed", "failed", "errored", "cancelled",
}
_TERMINAL_STATUSES: set[str] = {"passed", "failed", "errored", "cancelled"}
_VALID_SEVERITIES: tuple[str, ...] = ("none", "low", "medium", "high", "critical")
_VALID_FAIL_ON: tuple[str, ...] = ("none", "low", "medium", "high", "critical")
_SEVERITY_ORDER: dict[str, int] = {
    "none": 0, "low": 1, "medium": 2, "high": 3, "critical": 4,
}
_VALID_CI_PROVIDERS: set[str] = {
    "github", "gitlab", "bitbucket", "circleci", "jenkins", "buildkite",
    "azure", "drone", "other",
}


# ── Pydantic models ──


class AgentRunCreate(BaseModel):
    persona_name: str
    tool: str = "generic"
    trigger_source: str = "manual"
    ticket_id: str | None = None
    trigger_ref: str | None = None
    ci_provider: str | None = None
    ci_run_url: str | None = None
    fail_on: str | None = None
    triggered_by_persona: str | None = None

    @field_validator("persona_name")
    @classmethod
    def _persona_shape(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("persona_name is required")
        if len(v) > 50:
            raise ValueError("persona_name must be 50 characters or fewer")
        return v

    @field_validator("tool")
    @classmethod
    def _tool_shape(cls, v: str) -> str:
        v = v.strip() or "generic"
        if len(v) > 50:
            raise ValueError("tool must be 50 characters or fewer")
        return v

    @field_validator("trigger_source")
    @classmethod
    def _trigger_source_shape(cls, v: str) -> str:
        v = (v or "manual").strip().lower()
        if v not in _VALID_TRIGGER_SOURCES:
            raise ValueError(
                f"trigger_source must be one of: {sorted(_VALID_TRIGGER_SOURCES)}"
            )
        return v

    @field_validator("fail_on")
    @classmethod
    def _fail_on_shape(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = v.strip().lower()
        if v not in _VALID_FAIL_ON:
            raise ValueError(
                f"fail_on must be one of: {list(_VALID_FAIL_ON)}"
            )
        return v

    @field_validator("ci_provider")
    @classmethod
    def _ci_provider_shape(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = v.strip().lower()
        if v not in _VALID_CI_PROVIDERS:
            raise ValueError(
                f"ci_provider must be one of: {sorted(_VALID_CI_PROVIDERS)}"
            )
        return v


class AgentRunComplete(BaseModel):
    """Final-state submission at run completion."""

    status: str = "passed"  # passed | failed | errored — never queued/running/cancelled here
    result_summary: str | None = None
    severity: str = "none"
    findings: list[dict[str, Any]] = []
    session_id: str | None = None

    @field_validator("status")
    @classmethod
    def _status_shape(cls, v: str) -> str:
        v = (v or "passed").strip().lower()
        if v not in {"passed", "failed", "errored"}:
            raise ValueError(
                "status at completion must be one of: passed, failed, errored"
            )
        return v

    @field_validator("severity")
    @classmethod
    def _severity_shape(cls, v: str) -> str:
        v = (v or "none").strip().lower()
        if v not in _VALID_SEVERITIES:
            raise ValueError(
                f"severity must be one of: {list(_VALID_SEVERITIES)}"
            )
        return v


class AgentRunResponse(BaseModel):
    id: str
    project_id: str
    persona_name: str
    tool: str
    trigger_source: str
    status: str
    ticket_id: str | None
    trigger_ref: str | None
    ci_provider: str | None
    ci_run_url: str | None
    result_summary: str | None
    severity: str | None
    findings_count: int
    findings: list[dict[str, Any]]
    fail_on: str | None
    policy_result: str | None
    exit_code: int | None
    session_id: str | None
    triggered_by_user_id: str | None
    triggered_by_persona: str | None
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    duration_seconds: int | None


class StartAgentRunResponse(BaseModel):
    run: AgentRunResponse
    compiled_context: str


# ── Helpers ──


def _row_to_response(row: AgentRun) -> AgentRunResponse:
    try:
        findings = json.loads(row.findings) if row.findings else []
        if not isinstance(findings, list):
            findings = []
    except (ValueError, TypeError):
        findings = []
    return AgentRunResponse(
        id=row.id,
        project_id=row.project_id,
        persona_name=row.persona_name,
        tool=row.tool,
        trigger_source=row.trigger_source,
        status=row.status,
        ticket_id=row.ticket_id,
        trigger_ref=row.trigger_ref,
        ci_provider=row.ci_provider,
        ci_run_url=row.ci_run_url,
        result_summary=row.result_summary,
        severity=row.severity,
        findings_count=row.findings_count,
        findings=findings,
        fail_on=row.fail_on,
        policy_result=row.policy_result,
        exit_code=row.exit_code,
        session_id=row.session_id,
        triggered_by_user_id=row.triggered_by_user_id,
        triggered_by_persona=row.triggered_by_persona,
        created_at=row.created_at,
        started_at=row.started_at,
        completed_at=row.completed_at,
        duration_seconds=row.duration_seconds,
    )


async def _get_run_or_404(
    project_id: str, run_id: str, db: AsyncSession
) -> AgentRun:
    """Project-scoped fetch. Cross-project run_id surfaces as 404."""
    row = (
        await db.execute(
            select(AgentRun).where(
                AgentRun.id == run_id,
                AgentRun.project_id == project_id,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(404, "Agent run not found")
    return row


async def _validate_same_project_ticket(
    project_id: str, ticket_id: str, db: AsyncSession
) -> Ticket:
    """Ticket must exist and belong to this project (no cross-project leak)."""
    ticket = (
        await db.execute(
            select(Ticket).where(
                Ticket.id == ticket_id,
                Ticket.project_id == project_id,
            )
        )
    ).scalar_one_or_none()
    if ticket is None:
        raise HTTPException(
            422,
            f"Ticket {ticket_id!r} not found in this project (cross-project "
            "ticket references are rejected).",
        )
    return ticket


async def _validate_active_persona(
    project_id: str, persona_name: str, db: AsyncSession
) -> AgentPersona:
    """Persona must exist and be active in this project."""
    persona = (
        await db.execute(
            select(AgentPersona).where(
                AgentPersona.project_id == project_id,
                AgentPersona.name == persona_name,
                AgentPersona.is_active.is_(True),
            )
        )
    ).scalar_one_or_none()
    if persona is None:
        raise HTTPException(
            422,
            f"Persona {persona_name!r} not found or inactive in this project.",
        )
    return persona


def _evaluate_policy(
    severity: str, fail_on: str | None
) -> tuple[str, int]:
    """Return (policy_result, exit_code) for a completed run.

    Logic:
    - If `fail_on` is None or "none": always pass (exit 0).
    - If `severity` is "none": always pass regardless of fail_on threshold
      (the run found nothing actionable; no threshold can trip).
    - Otherwise fail iff `_SEVERITY_ORDER[severity] >= _SEVERITY_ORDER[fail_on]`.

    Returns ("pass", 0) or ("fail", 1).
    """
    if not fail_on or fail_on == "none":
        return ("pass", 0)
    if severity == "none":
        return ("pass", 0)
    sev_order = _SEVERITY_ORDER.get(severity, 0)
    threshold = _SEVERITY_ORDER.get(fail_on, 0)
    if sev_order >= threshold:
        return ("fail", 1)
    return ("pass", 0)


def _new_run_id() -> str:
    return f"run_{uuid.uuid4().hex[:16]}"


# ── Routes ──


@router.post(
    "/{project_id}/agent-runs",
    response_model=AgentRunResponse,
    status_code=201,
)
async def create_agent_run(
    project_id: str,
    body: AgentRunCreate,
    user: User = Depends(get_current_user),
    ctx: UserContext = Depends(get_user_context),
    db: AsyncSession = Depends(get_db),
) -> AgentRunResponse:
    """Create a queued AgentRun. Validates same-project ticket + active persona."""
    check_feature(ctx, "agent_runs")
    await _get_project_or_404(project_id, db, user.id)

    # Validate persona exists + is active in this project.
    await _validate_active_persona(project_id, body.persona_name, db)

    # Validate ticket (if provided) is in this project.
    if body.ticket_id:
        await _validate_same_project_ticket(project_id, body.ticket_id, db)

    now = datetime.now(timezone.utc)
    row = AgentRun(
        id=_new_run_id(),
        project_id=project_id,
        persona_name=body.persona_name,
        tool=body.tool,
        trigger_source=body.trigger_source,
        status="queued",
        ticket_id=body.ticket_id,
        trigger_ref=body.trigger_ref,
        ci_provider=body.ci_provider,
        ci_run_url=body.ci_run_url,
        fail_on=body.fail_on,
        triggered_by_user_id=user.id,
        triggered_by_persona=body.triggered_by_persona,
        created_at=now,
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return _row_to_response(row)


@router.get(
    "/{project_id}/agent-runs",
    response_model=list[AgentRunResponse],
)
async def list_agent_runs(
    project_id: str,
    persona_name: str | None = Query(None),
    status: str | None = Query(None),
    trigger_source: str | None = Query(None),
    ticket_id: str | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
    user: User = Depends(get_current_user),
    ctx: UserContext = Depends(get_user_context),
    db: AsyncSession = Depends(get_db),
) -> list[AgentRunResponse]:
    check_feature(ctx, "agent_runs")
    await _get_project_or_404(project_id, db, user.id)

    stmt = select(AgentRun).where(AgentRun.project_id == project_id)
    if persona_name:
        stmt = stmt.where(AgentRun.persona_name == persona_name)
    if status:
        stmt = stmt.where(AgentRun.status == status)
    if trigger_source:
        stmt = stmt.where(AgentRun.trigger_source == trigger_source)
    if ticket_id:
        stmt = stmt.where(AgentRun.ticket_id == ticket_id)
    stmt = stmt.order_by(AgentRun.created_at.desc()).limit(limit)

    rows = (await db.execute(stmt)).scalars().all()
    return [_row_to_response(r) for r in rows]


@router.get(
    "/{project_id}/agent-runs/{run_id}",
    response_model=AgentRunResponse,
)
async def get_agent_run(
    project_id: str,
    run_id: str,
    user: User = Depends(get_current_user),
    ctx: UserContext = Depends(get_user_context),
    db: AsyncSession = Depends(get_db),
) -> AgentRunResponse:
    check_feature(ctx, "agent_runs")
    await _get_project_or_404(project_id, db, user.id)
    return _row_to_response(await _get_run_or_404(project_id, run_id, db))


@router.post(
    "/{project_id}/agent-runs/{run_id}/start",
    response_model=StartAgentRunResponse,
)
async def start_agent_run(
    project_id: str,
    run_id: str,
    tool: str | None = Query(
        None,
        description=(
            "Token-budget hint for compiled_context. Same vocabulary as "
            "ticket start: claude-code/bedrock (16k), codex/gemini/vertex/"
            "copilot/amp/generic (8k), cursor/windsurf/cline/roo-code (4k)."
        ),
    ),
    user: User = Depends(get_current_user),
    ctx: UserContext = Depends(get_user_context),
    db: AsyncSession = Depends(get_db),
) -> StartAgentRunResponse:
    """Atomic queued → running transition. Returns compiled persona +
    ticket context the caller should feed into its own model runtime.

    This does NOT mutate the ticket's FSM state (the run's lifecycle is
    separate from the ticket's). If the caller wants the ticket moved to
    in_progress, they call `start_ticket` separately — keeping the
    operations explicit avoids hiding 409 conflicts behind a single
    run-start call.
    """
    check_feature(ctx, "agent_runs")
    await _get_project_or_404(project_id, db, user.id)

    now = datetime.now(timezone.utc)
    # Atomic queued → running with rowcount-1 guard.
    result = await db.execute(
        update(AgentRun)
        .where(
            AgentRun.id == run_id,
            AgentRun.project_id == project_id,
            AgentRun.status == "queued",
        )
        .values(status="running", started_at=now)
    )
    if result.rowcount != 1:
        # Either the run doesn't exist in this project, or it wasn't queued.
        await db.rollback()
        existing = await _get_run_or_404(project_id, run_id, db)
        raise HTTPException(
            409,
            f"Cannot start agent run: current status is {existing.status!r}, "
            "expected 'queued'.",
        )
    # Commit the transition immediately so a concurrent start sees
    # status='running' and trips the rowcount guard. Without this commit
    # both requests would observe 'queued' in their own transactions and
    # the atomic UPDATE's rowcount would be 1 in both — duplicate
    # transitions. (Same pattern as tickets.start.)
    await db.commit()

    run = await _get_run_or_404(project_id, run_id, db)

    # Compile persona + ticket context. Reuse the ticket-side helper —
    # the cross-project leak defenses + project_id + active-claim filters
    # already live there.
    persona = await _validate_active_persona(project_id, run.persona_name, db)
    ticket = None
    if run.ticket_id:
        # Best-effort — if the ticket was deleted since run creation,
        # we don't fail the start; the persona context alone is still
        # useful. The same-project guard at creation time + the FK
        # CASCADE on tickets means the ticket either exists in this
        # project or it doesn't exist at all.
        ticket = (
            await db.execute(
                select(Ticket).where(
                    Ticket.id == run.ticket_id,
                    Ticket.project_id == project_id,
                )
            )
        ).scalar_one_or_none()

    compiled = await _compile_persona_context(
        db=db, persona=persona, ticket=ticket, tool=tool or run.tool,
    )

    return StartAgentRunResponse(
        run=_row_to_response(run),
        compiled_context=compiled,
    )


@router.post(
    "/{project_id}/agent-runs/{run_id}/complete",
    response_model=AgentRunResponse,
)
async def complete_agent_run(
    project_id: str,
    run_id: str,
    body: AgentRunComplete,
    user: User = Depends(get_current_user),
    ctx: UserContext = Depends(get_user_context),
    db: AsyncSession = Depends(get_db),
) -> AgentRunResponse:
    """Atomic running/queued → passed/failed/errored. Runs policy evaluation."""
    check_feature(ctx, "agent_runs")
    await _get_project_or_404(project_id, db, user.id)

    now = datetime.now(timezone.utc)
    # Read the row first so we can compute duration_seconds and evaluate policy.
    existing = await _get_run_or_404(project_id, run_id, db)
    if existing.status in _TERMINAL_STATUSES:
        raise HTTPException(
            409,
            f"Cannot complete agent run: current status is "
            f"{existing.status!r} (already terminal).",
        )

    findings_str = json.dumps(body.findings)
    findings_count = len(body.findings)
    duration = None
    if existing.started_at is not None:
        # SQLite returns naive datetimes from DateTime(timezone=True)
        # columns; normalize to UTC-aware so the subtraction works on
        # both backends. (PostgreSQL hands back tz-aware automatically.)
        started = existing.started_at
        if started.tzinfo is None:
            started = started.replace(tzinfo=timezone.utc)
        duration = int((now - started).total_seconds())

    policy_result, exit_code = _evaluate_policy(body.severity, existing.fail_on)
    # If the caller explicitly says the run failed (status='failed' or
    # 'errored'), respect that — the policy only adds enforcement on top
    # of the caller's status, it doesn't override 'failed' to 'pass'.
    final_status = body.status
    if final_status == "passed" and policy_result == "fail":
        final_status = "failed"

    # KB review HIGH (post-Round 1): an explicit `failed` or `errored`
    # status from the caller MUST force exit_code=1 + policy_result='fail'
    # for CI enforcement. Without this, a crashed review tool that
    # submits status='errored' + severity='none' would store exit_code=0
    # and `sfs agent complete --enforce` would let CI pass despite the
    # terminal run being errored. Severity/fail_on policy is layered on
    # top of caller-submitted status, not the other way around.
    if final_status in {"failed", "errored"}:
        policy_result = "fail"
        exit_code = 1

    # Atomic write — must not be in a terminal state, must currently be
    # queued or running. The pre-check above already raised if terminal,
    # but the atomic UPDATE is the actual guard against concurrent
    # completion races (KB 326 pattern).
    result = await db.execute(
        update(AgentRun)
        .where(
            AgentRun.id == run_id,
            AgentRun.project_id == project_id,
            AgentRun.status.in_(("queued", "running")),
        )
        .values(
            status=final_status,
            completed_at=now,
            duration_seconds=duration,
            result_summary=body.result_summary,
            severity=body.severity,
            findings=findings_str,
            findings_count=findings_count,
            session_id=body.session_id,
            policy_result=policy_result,
            exit_code=exit_code,
        )
    )
    if result.rowcount != 1:
        await db.rollback()
        # Re-fetch to surface the actual current state.
        current = await _get_run_or_404(project_id, run_id, db)
        raise HTTPException(
            409,
            f"Concurrent completion detected: current status is "
            f"{current.status!r}.",
        )

    await db.commit()
    return _row_to_response(await _get_run_or_404(project_id, run_id, db))


@router.post(
    "/{project_id}/agent-runs/{run_id}/cancel",
    response_model=AgentRunResponse,
)
async def cancel_agent_run(
    project_id: str,
    run_id: str,
    user: User = Depends(get_current_user),
    ctx: UserContext = Depends(get_user_context),
    db: AsyncSession = Depends(get_db),
) -> AgentRunResponse:
    """Atomic queued/running → cancelled."""
    check_feature(ctx, "agent_runs")
    await _get_project_or_404(project_id, db, user.id)

    now = datetime.now(timezone.utc)
    result = await db.execute(
        update(AgentRun)
        .where(
            AgentRun.id == run_id,
            AgentRun.project_id == project_id,
            AgentRun.status.in_(("queued", "running")),
        )
        .values(
            status="cancelled",
            completed_at=now,
            policy_result="pass",
            exit_code=0,
        )
    )
    if result.rowcount != 1:
        await db.rollback()
        existing = await _get_run_or_404(project_id, run_id, db)
        raise HTTPException(
            409,
            f"Cannot cancel agent run: current status is {existing.status!r} "
            "(already terminal).",
        )
    await db.commit()
    return _row_to_response(await _get_run_or_404(project_id, run_id, db))
