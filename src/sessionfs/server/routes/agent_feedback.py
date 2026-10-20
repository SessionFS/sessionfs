"""Structured agent-feedback routes — v0.17.0.

Three routes under `/api/v1/projects/{project_id}/agent-feedback`:

    POST   /                 — create an append-only feedback record
    GET    /                 — list with filters + cursor pagination
    GET    /summary          — grouped counts (feedback_type × verdict × label)

A record points at at least one target — an agent run, a ticket, or a
ticket comment — and every target must belong to `project_id`. Lookups
filter by project so a guessed cross-project id surfaces as 404, never
as a data leak.

Tier-gated via `agent_runs` (Team+) — feedback is an execution-record
adjunct, so it rides the same entitlement as runs and tickets.

Service-key scopes: dedicated `agent_feedback:read` / `agent_feedback:write`
so a reviewer principal can post/consume feedback without the broader
`agent_runs` write surface (least privilege).
"""

from __future__ import annotations

import secrets
from datetime import datetime, timezone
from typing import Any, cast

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server.auth.dependencies import (
    AuthContext,
    require_scope,
)
from sessionfs.server.db.engine import get_db
from sessionfs.server.db.models import (
    AgentFeedback,
    AgentRun,
    Ticket,
    TicketComment,
)
from sessionfs.server.routes.knowledge import _get_project_for_auth
from sessionfs.server.schemas.agent_feedback import (
    FEEDBACK_TYPES,
    VERDICTS,
    label_valid_for_type,
)
from sessionfs.server.tier_gate import UserContext, check_project_feature, get_user_context

router = APIRouter(prefix="/api/v1/projects", tags=["agent-feedback"])

# Notes cap — mirrors the free-text length guards on neighbouring routes.
_NOTES_MAX = 4000
_OWNER_MAX = 100


# ── Pydantic models ──


class AgentFeedbackCreate(BaseModel):
    """Create a feedback record. At least one target required, all must
    belong to the same project. `source` is NOT caller-settable — the
    route records 'human' (explicit recording); only the internal
    auto-capture path writes 'auto'."""

    feedback_type: str
    verdict: str
    label: str | None = None
    final_owner: str | None = Field(None, max_length=_OWNER_MAX)
    notes: str | None = Field(None, max_length=_NOTES_MAX)
    agent_run_id: str | None = Field(None, max_length=64)
    ticket_id: str | None = Field(None, max_length=64)
    comment_id: str | None = Field(None, max_length=64)

    @field_validator(
        "final_owner", "notes", "label", "agent_run_id", "ticket_id", "comment_id"
    )
    @classmethod
    def _no_nul(cls, value: str | None) -> str | None:
        # PostgreSQL text columns reject NUL; refuse it here as a 422 rather
        # than a 500 at commit.
        if value is not None and "\x00" in value:
            raise ValueError("must not contain NUL characters")
        return value

    @model_validator(mode="after")
    def _validate_shape(self):
        if self.feedback_type not in FEEDBACK_TYPES:
            raise ValueError(
                f"feedback_type must be one of: {list(FEEDBACK_TYPES)}"
            )
        if self.verdict not in VERDICTS:
            raise ValueError(f"verdict must be one of: {list(VERDICTS)}")
        if not label_valid_for_type(self.feedback_type, self.label):
            raise ValueError(
                f"label {self.label!r} is not valid for feedback_type "
                f"{self.feedback_type!r}"
            )
        if (
            self.agent_run_id is None
            and self.ticket_id is None
            and self.comment_id is None
        ):
            raise ValueError(
                "at least one target is required: agent_run_id, ticket_id, "
                "or comment_id"
            )
        return self


class AgentFeedbackResponse(BaseModel):
    id: str
    project_id: str
    agent_run_id: str | None
    ticket_id: str | None
    comment_id: str | None
    feedback_type: str
    verdict: str
    label: str | None
    final_owner: str | None
    notes: str | None
    source: str
    created_by_user_id: str | None
    actor_type: str | None
    service_key_id: str | None
    service_key_name: str | None
    created_at: datetime


class AgentFeedbackSummaryResponse(BaseModel):
    total: int
    groups: list[dict[str, Any]]


# ── Helpers ──


def _row_to_response(row: AgentFeedback) -> AgentFeedbackResponse:
    return AgentFeedbackResponse(
        id=row.id,
        project_id=row.project_id,
        agent_run_id=row.agent_run_id,
        ticket_id=row.ticket_id,
        comment_id=row.comment_id,
        feedback_type=row.feedback_type,
        verdict=row.verdict,
        label=row.label,
        final_owner=row.final_owner,
        notes=row.notes,
        source=row.source,
        created_by_user_id=row.created_by_user_id,
        actor_type=row.actor_type,
        service_key_id=row.service_key_id,
        service_key_name=row.service_key_name,
        created_at=row.created_at,
    )


def _reject_nul(*values: str | None) -> None:
    # PostgreSQL rejects NUL in bound text parameters (a 500); refuse it as a
    # client error before any query runs.
    if any(v is not None and "\x00" in v for v in values):
        raise HTTPException(422, "Parameters must not contain NUL characters")


def _new_feedback_id() -> str:
    return f"afb_{secrets.token_hex(12)}"


async def _validate_targets(
    project_id: str,
    body: AgentFeedbackCreate,
    db: AsyncSession,
) -> None:
    """Every supplied target must exist AND belong to this project.

    Raises 404 on any missing/cross-project target so a guessed id from
    another project surfaces as not-found, never as a leak. The
    comment check additionally verifies the comment's ticket lives in
    this project (comment→ticket mismatch is a cross-project signal).
    """
    if body.agent_run_id is not None:
        run = (
            await db.execute(
                select(AgentRun.id).where(
                    AgentRun.id == body.agent_run_id,
                    AgentRun.project_id == project_id,
                )
            )
        ).scalar_one_or_none()
        if run is None:
            raise HTTPException(404, "Agent run not found")
    if body.ticket_id is not None:
        ticket = (
            await db.execute(
                select(Ticket.id).where(
                    Ticket.id == body.ticket_id,
                    Ticket.project_id == project_id,
                )
            )
        ).scalar_one_or_none()
        if ticket is None:
            raise HTTPException(404, "Ticket not found")
    if body.comment_id is not None:
        comment_ticket_id = (
            await db.execute(
                select(TicketComment.ticket_id)
                .join(Ticket, TicketComment.ticket_id == Ticket.id)
                .where(
                    TicketComment.id == body.comment_id,
                    Ticket.project_id == project_id,
                )
            )
        ).scalar_one_or_none()
        if comment_ticket_id is None:
            raise HTTPException(404, "Ticket comment not found")
        # When both are given, the comment must be on that ticket.
        if body.ticket_id is not None and comment_ticket_id != body.ticket_id:
            raise HTTPException(
                422, "comment_id does not belong to the given ticket_id"
            )


def _apply_persona_filter(stmt, persona: str | None):
    """Constrain feedback to records whose run or ticket involves a
    persona. Uses EXISTS subqueries (not joins) so a record that targets
    both a run and a ticket is never duplicated by the filter."""
    if not persona:
        return stmt
    run_exists = select(AgentRun.id).where(
        AgentRun.id == AgentFeedback.agent_run_id,
        AgentRun.persona_name == persona,
    )
    # A ticket's feedback judges the agent that created it (e.g. a mis-route)
    # or the one working it, so match either. Matching only the current
    # assignee would credit a wrong_route record to the persona it was
    # rerouted TO rather than the agent that routed it.
    ticket_exists = select(Ticket.id).where(
        Ticket.id == AgentFeedback.ticket_id,
        or_(Ticket.created_by_persona == persona, Ticket.assigned_to == persona),
    )
    # Feedback can target only a comment; judge it by the comment's ticket.
    comment_exists = (
        select(TicketComment.id)
        .join(Ticket, TicketComment.ticket_id == Ticket.id)
        .where(
            TicketComment.id == AgentFeedback.comment_id,
            or_(Ticket.created_by_persona == persona, Ticket.assigned_to == persona),
        )
    )
    return stmt.where(
        or_(run_exists.exists(), ticket_exists.exists(), comment_exists.exists())
    )


# ── Routes ──


@router.post(
    "/{project_id}/agent-feedback",
    response_model=AgentFeedbackResponse,
    status_code=201,
)
async def create_agent_feedback(
    project_id: str,
    body: AgentFeedbackCreate,
    auth: AuthContext = Depends(require_scope("agent_feedback:write")),
    ctx: UserContext = Depends(get_user_context),
    db: AsyncSession = Depends(get_db),
) -> AgentFeedbackResponse:
    """Create an append-only feedback record. Targets validated same-project."""
    _reject_nul(project_id)
    user = auth.user
    project = await _get_project_for_auth(project_id, db, auth)
    from sessionfs.server.auth.dependencies import (
        assert_service_key_can_access_project,
    )
    await assert_service_key_can_access_project(db, auth, project)
    await check_project_feature(ctx, "agent_runs", project, db)

    await _validate_targets(project_id, body, db)

    row = AgentFeedback(
        id=_new_feedback_id(),
        project_id=project_id,
        agent_run_id=body.agent_run_id,
        ticket_id=body.ticket_id,
        comment_id=body.comment_id,
        feedback_type=body.feedback_type,
        verdict=body.verdict,
        label=body.label,
        final_owner=body.final_owner,
        notes=body.notes,
        source="human",
        created_at=datetime.now(timezone.utc),
        created_by_user_id=user.id,
        actor_type=auth.actor_type,
        service_key_id=auth.service_key_id,
        service_key_name=auth.service_key_name,
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return _row_to_response(row)


@router.get(
    "/{project_id}/agent-feedback",
    response_model=list[AgentFeedbackResponse],
)
async def list_agent_feedback(
    project_id: str,
    agent_run_id: str | None = Query(None),
    ticket_id: str | None = Query(None),
    feedback_type: str | None = Query(None),
    verdict: str | None = Query(None),
    label: str | None = Query(None),
    persona: str | None = Query(None),
    since: datetime | None = Query(
        None,
        description="Cursor timestamp — return only records newer than this.",
    ),
    since_id: str | None = Query(
        None,
        description="Cursor tiebreaker — pair with `since` for same-timestamp ties.",
    ),
    limit: int = Query(100, ge=1, le=500, description="Max records to return."),
    auth: AuthContext = Depends(require_scope("agent_feedback:read")),
    ctx: UserContext = Depends(get_user_context),
    db: AsyncSession = Depends(get_db),
) -> list[AgentFeedbackResponse]:
    """List feedback, oldest-first with a stable (created_at, id) cursor."""
    _reject_nul(project_id, agent_run_id, ticket_id, feedback_type, verdict, label, persona, since_id)
    project = await _get_project_for_auth(project_id, db, auth)
    from sessionfs.server.auth.dependencies import (
        assert_service_key_can_access_project,
    )
    await assert_service_key_can_access_project(db, auth, project)
    await check_project_feature(ctx, "agent_runs", project, db)

    stmt = select(AgentFeedback).where(AgentFeedback.project_id == project_id)
    if agent_run_id:
        stmt = stmt.where(AgentFeedback.agent_run_id == agent_run_id)
    if ticket_id:
        stmt = stmt.where(AgentFeedback.ticket_id == ticket_id)
    if feedback_type:
        stmt = stmt.where(AgentFeedback.feedback_type == feedback_type)
    if verdict:
        stmt = stmt.where(AgentFeedback.verdict == verdict)
    if label:
        stmt = stmt.where(AgentFeedback.label == label)
    stmt = _apply_persona_filter(stmt, persona)
    if since is not None:
        # Normalize to UTC: SQLite drops the offset when binding, PostgreSQL
        # doesn't, so an aware non-UTC value would filter differently.
        since = (
            since.replace(tzinfo=timezone.utc)
            if since.tzinfo is None
            else since.astimezone(timezone.utc)
        )
        cursor_ts = None
        if since_id:
            # Compare against the cursor row's stored timestamp, not the
            # round-tripped `since`: SQLite keeps server-default timestamps
            # without fractional seconds, so an equality test against the
            # re-bound value misses rows created in the same second.
            cursor_ts = (
                await db.execute(
                    select(AgentFeedback.created_at).where(
                        AgentFeedback.id == since_id,
                        AgentFeedback.project_id == project_id,
                    )
                )
            ).scalar_one_or_none()
        if cursor_ts is not None:
            cursor_subquery = (
                select(AgentFeedback.created_at)
                .where(AgentFeedback.id == since_id)
                .scalar_subquery()
            )
            stmt = stmt.where(
                or_(
                    AgentFeedback.created_at > cursor_subquery,
                    and_(
                        AgentFeedback.created_at == cursor_subquery,
                        AgentFeedback.id > since_id,
                    ),
                )
            )
        else:
            stmt = stmt.where(AgentFeedback.created_at > since)
    stmt = (
        stmt.order_by(AgentFeedback.created_at, AgentFeedback.id).limit(limit)
    )

    rows = (await db.execute(stmt)).scalars().all()
    return [_row_to_response(r) for r in rows]


@router.get(
    "/{project_id}/agent-feedback/summary",
    response_model=AgentFeedbackSummaryResponse,
)
async def get_agent_feedback_summary(
    project_id: str,
    persona: str | None = Query(None),
    since: datetime | None = Query(
        None,
        description="Only count records newer than this timestamp.",
    ),
    auth: AuthContext = Depends(require_scope("agent_feedback:read")),
    ctx: UserContext = Depends(get_user_context),
    db: AsyncSession = Depends(get_db),
) -> AgentFeedbackSummaryResponse:
    """Grouped counts by feedback_type × verdict × label (pure SQL, no LLM)."""
    _reject_nul(project_id, persona)
    project = await _get_project_for_auth(project_id, db, auth)
    from sessionfs.server.auth.dependencies import (
        assert_service_key_can_access_project,
    )
    await assert_service_key_can_access_project(db, auth, project)
    await check_project_feature(ctx, "agent_runs", project, db)

    stmt = (
        select(
            AgentFeedback.feedback_type,
            AgentFeedback.verdict,
            AgentFeedback.label,
            func.count(AgentFeedback.id),
        )
        .where(AgentFeedback.project_id == project_id)
    )
    stmt = _apply_persona_filter(stmt, persona)
    if since is not None:
        # Normalize to UTC: SQLite drops the offset when binding, PostgreSQL
        # doesn't, so an aware non-UTC value would filter differently.
        since = (
            since.replace(tzinfo=timezone.utc)
            if since.tzinfo is None
            else since.astimezone(timezone.utc)
        )
        stmt = stmt.where(AgentFeedback.created_at > since)
    stmt = stmt.group_by(
        AgentFeedback.feedback_type,
        AgentFeedback.verdict,
        AgentFeedback.label,
    ).order_by(
        AgentFeedback.feedback_type,
        AgentFeedback.verdict,
        AgentFeedback.label,
    )

    rows = (await db.execute(stmt)).all()
    groups: list[dict[str, Any]] = []
    total = 0
    for feedback_type, verdict, label, count in rows:
        groups.append(
            {
                "feedback_type": feedback_type,
                "verdict": verdict,
                "label": label,
                "count": count,
            }
        )
        total += cast(int, count)  # SQL COUNT is always an integer
    return AgentFeedbackSummaryResponse(total=total, groups=groups)
