"""Ticket CRUD + lifecycle routes — v0.10.1 Phase 3.

State machine enforced server-side:

    suggested  ──approve──>  open  ──start────────>  in_progress
        │                     ^                          │
        └────dismiss────> cancelled                      ├──block──>  blocked
                                                         │              │
                                                         │              └──unblock──> in_progress
                                                         │
                                                         └──complete──> review
                                                                          │
                                                                          ├──accept──> done
                                                                          │
                                                                          └──reopen──> open

The DB column is plain VARCHAR with default 'open'. The FSM lives in
this module so route-layer transitions stay atomic and auditable. Any
attempt to drive a transition not listed in `_LEGAL_TRANSITIONS` returns
400 with the legal set from the current state.

Concurrency: `start_ticket` issues an atomic `UPDATE ... WHERE
status='open'` and checks `rowcount`; non-1 → 409 with the current
status. Same pattern as `accept_transfer` in routes/project_transfers.py
from v0.10.0.

Agent-created tickets default to 'suggested' and require acceptance
criteria + a 20+ char description + ≤3 per session (rate limit). The
human-created surface defaults to 'open'.

Dependency enrichment on accept: when a ticket moves to 'done', every
ticket that depends on it gets a comment with the completion notes,
the completed ticket's `knowledge_entry_ids` propagated into the
dependent's `context_refs`, and an auto-unblock if all upstream deps
are now done.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, field_validator
from sqlalchemy import insert, literal, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server.auth.dependencies import get_current_user
from sessionfs.server.db.engine import get_db
from sessionfs.server.db.models import (
    AgentPersona,
    KnowledgeEntry,
    RetrievalAuditContext,
    Ticket,
    TicketComment,
    TicketDependency,
    User,
)
from sessionfs.server.routes.wiki import _get_project_or_404
from sessionfs.server.tier_gate import UserContext, check_feature, get_user_context

router = APIRouter(prefix="/api/v1/projects", tags=["tickets"])


# ── FSM constants ──


# Maps current status → set of legal next statuses. Routes consult
# this table to validate every transition; a missing key means the
# starting state has no legal transitions (terminal state).
_LEGAL_TRANSITIONS: dict[str, set[str]] = {
    "suggested": {"open", "cancelled"},
    "open": {"in_progress", "cancelled"},
    "in_progress": {"blocked", "review"},
    "blocked": {"in_progress"},
    "review": {"done", "open"},
    "done": set(),
    "cancelled": set(),
}

_VALID_PRIORITIES = {"critical", "high", "medium", "low"}

_AGENT_TICKET_DESC_MIN = 20
_AGENT_TICKET_PER_SESSION_CAP = 3


def _legal_next(current: str) -> set[str]:
    return _LEGAL_TRANSITIONS.get(current, set())


def _assert_transition(current: str, target: str) -> None:
    if target not in _legal_next(current):
        legal = sorted(_legal_next(current)) or ["(none — terminal state)"]
        raise HTTPException(
            400,
            f"Cannot transition from {current!r} to {target!r}. "
            f"Legal transitions from {current!r}: {', '.join(legal)}.",
        )


# ── Request / response models ──


class TicketCreate(BaseModel):
    title: str
    description: str = ""
    priority: str = "medium"
    assigned_to: str | None = None
    context_refs: list[str] = []
    file_refs: list[str] = []
    related_sessions: list[str] = []
    acceptance_criteria: list[str] = []
    depends_on: list[str] = []
    # Source: "human" (default) creates with status='open'. "agent"
    # creates with status='suggested' AND applies quality gates.
    source: str = "human"
    # Set when an agent is working through MCP and the caller wants
    # to attribute the ticket to a session for the per-session cap.
    created_by_session_id: str | None = None
    # Optional persona attribution (matches Ticket.created_by_persona).
    created_by_persona: str | None = None

    @field_validator("title")
    @classmethod
    def _title_shape(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("title is required")
        if len(v) > 200:
            raise ValueError("title must be 200 characters or fewer")
        return v

    @field_validator("priority")
    @classmethod
    def _priority_shape(cls, v: str) -> str:
        if v not in _VALID_PRIORITIES:
            raise ValueError(
                f"priority must be one of: {sorted(_VALID_PRIORITIES)}"
            )
        return v

    @field_validator("source")
    @classmethod
    def _source_shape(cls, v: str) -> str:
        if v not in {"human", "agent"}:
            raise ValueError("source must be 'human' or 'agent'")
        return v


class TicketUpdate(BaseModel):
    """Partial update for non-lifecycle fields (re-assign, re-prioritize,
    edit description, attach files/criteria/context). Status transitions
    happen through dedicated lifecycle routes — NOT through this PUT.
    """

    title: str | None = None
    description: str | None = None
    priority: str | None = None
    assigned_to: str | None = None
    context_refs: list[str] | None = None
    file_refs: list[str] | None = None
    related_sessions: list[str] | None = None
    acceptance_criteria: list[str] | None = None

    @field_validator("title")
    @classmethod
    def _title_shape(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = v.strip()
        if not v:
            raise ValueError("title cannot be empty")
        if len(v) > 200:
            raise ValueError("title must be 200 characters or fewer")
        return v

    @field_validator("priority")
    @classmethod
    def _priority_shape(cls, v: str | None) -> str | None:
        if v is None:
            return None
        if v not in _VALID_PRIORITIES:
            raise ValueError(
                f"priority must be one of: {sorted(_VALID_PRIORITIES)}"
            )
        return v


class TicketResponse(BaseModel):
    id: str
    project_id: str
    title: str
    description: str
    priority: str
    assigned_to: str | None
    created_by_user_id: str
    created_by_session_id: str | None
    created_by_persona: str | None
    status: str
    lease_epoch: int
    context_refs: list[str]
    file_refs: list[str]
    related_sessions: list[str]
    acceptance_criteria: list[str]
    resolver_session_id: str | None
    resolver_user_id: str | None
    completion_notes: str | None
    changed_files: list[str]
    knowledge_entry_ids: list[str]
    depends_on: list[str]
    created_at: datetime
    updated_at: datetime
    resolved_at: datetime | None


class CompleteTicketRequest(BaseModel):
    notes: str
    changed_files: list[str] = []
    knowledge_entry_ids: list[str] = []
    resolver_session_id: str | None = None
    lease_epoch: int | None = None


class StartTicketResponse(BaseModel):
    """v0.10.1 Phase 4 — the start_ticket endpoint also returns the
    compiled persona+ticket context so MCP/CLI callers can inject it
    into the active AI tool's context window without a separate round
    trip."""

    ticket: TicketResponse
    compiled_context: str
    retrieval_audit_id: str | None = None


class CommentCreate(BaseModel):
    content: str
    author_persona: str | None = None
    session_id: str | None = None
    lease_epoch: int | None = None

    @field_validator("content")
    @classmethod
    def _content_shape(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("content is required")
        if len(v) > 10_000:
            raise ValueError("content must be 10000 characters or fewer")
        return v


class CommentResponse(BaseModel):
    id: str
    ticket_id: str
    author_user_id: str
    author_persona: str | None
    content: str
    session_id: str | None
    created_at: datetime


# ── Helpers ──


def _loads(s: str | None) -> list:
    if not s:
        return []
    try:
        data = json.loads(s)
    except (ValueError, TypeError):
        return []
    return data if isinstance(data, list) else []


async def _ticket_dependencies(db: AsyncSession, ticket_id: str) -> list[str]:
    rows = (
        await db.execute(
            select(TicketDependency.depends_on_id).where(
                TicketDependency.ticket_id == ticket_id
            )
        )
    ).scalars().all()
    return list(rows)


def _to_response(t: Ticket, deps: list[str]) -> TicketResponse:
    return TicketResponse(
        id=t.id,
        project_id=t.project_id,
        title=t.title,
        description=t.description,
        priority=t.priority,
        assigned_to=t.assigned_to,
        created_by_user_id=t.created_by_user_id,
        created_by_session_id=t.created_by_session_id,
        created_by_persona=t.created_by_persona,
        status=t.status,
        lease_epoch=t.lease_epoch,
        context_refs=_loads(t.context_refs),
        file_refs=_loads(t.file_refs),
        related_sessions=_loads(t.related_sessions),
        acceptance_criteria=_loads(t.acceptance_criteria),
        resolver_session_id=t.resolver_session_id,
        resolver_user_id=t.resolver_user_id,
        completion_notes=t.completion_notes,
        changed_files=_loads(t.changed_files),
        knowledge_entry_ids=_loads(t.knowledge_entry_ids),
        depends_on=deps,
        created_at=t.created_at,
        updated_at=t.updated_at,
        resolved_at=t.resolved_at,
    )


def _assert_lease_epoch(ticket: Ticket, lease_epoch: int | None) -> None:
    """Reject stale ticket writers when callers opt into lease fencing."""
    if lease_epoch is None:
        return
    if lease_epoch != ticket.lease_epoch:
        raise HTTPException(
            409,
            (
                f"Stale ticket lease: provided lease_epoch={lease_epoch}, "
                f"current lease_epoch={ticket.lease_epoch}."
            ),
        )


# v0.10.1 Phase 4 — persona context compilation.
#
# Assembles a markdown context block from the persona + ticket +
# explicit KB claims + recent comments + completion notes from
# already-done dependencies. NO automatic KB domain filtering by
# specializations — that's a deliberate Codex correction from the
# brief (KB context is too noisy for automatic prompt composition in
# v1). Only context_refs the reporter explicitly listed are included.
#
# Project rules are NOT inlined here — they already live in the tool-
# specific files (CLAUDE.md / .cursorrules / etc.) that the rules
# compiler maintains. Persona instructions stack on top of those.


_TOOL_TOKEN_LIMITS: dict[str, int] = {
    "claude-code": 16000,
    "codex": 8000,
    "gemini": 8000,
    "copilot": 8000,
    "cursor": 4000,
    "windsurf": 4000,
    "cline": 4000,
    "roo-code": 4000,
    "amp": 8000,
    # v0.10.1 cloud-agent aliases — Bedrock typically fronts Claude/Titan
    # (Claude-class budget); Vertex fronts Gemini (Gemini-class budget).
    # Documented in docs/integrations/bedrock-action-group.yaml and the
    # cloud-agents site page. Added per KB review of Cloud Agent Control
    # Plane ticket — without these aliases, callers passing tool=bedrock
    # silently fell back to "generic" (8k) instead of the claude-code
    # budget the docs promise.
    "bedrock": 16000,
    "vertex": 8000,
    "generic": 8000,
}


def _truncate_to_chars(text: str, max_tokens: int) -> str:
    """Cheap byte-level truncation. Roughly 4 chars per token; clip
    to that with a trailing note if we cut content off. Real tokenizer
    would be nicer but adds a dep — this is the simple, dep-free
    approach the rules compiler uses too.
    """
    max_chars = max_tokens * 4
    if len(text) <= max_chars:
        return text
    return text[:max_chars].rstrip() + "\n\n[...truncated to fit tool token limit]"


async def _compile_persona_context(
    db: AsyncSession,
    persona: AgentPersona | None,
    ticket: Ticket | None,
    tool: str = "generic",
) -> str:
    """Assemble persona + (optional) ticket + KB claims + comments + dep
    notes into a single markdown block sized for the target tool.

    v0.10.2 — `ticket` is now optional so AgentRun.start can compile
    persona-only context for ticket-less runs (e.g. CI security scans
    that aren't tied to a specific ticket). When `ticket` is None the
    function emits the persona block only, sized to the tool budget.
    """
    sections: list[str] = []

    if persona is not None:
        sections.append(
            f"# You are {persona.name} — {persona.role}\n\n{persona.content}"
        )

    if ticket is None:
        # Persona-only mode (AgentRun without ticket linkage). Skip every
        # ticket-derived section — there's nothing to compile from.
        compiled = "\n---\n\n".join(sections) if sections else ""
        max_tokens = _TOOL_TOKEN_LIMITS.get(tool, _TOOL_TOKEN_LIMITS["generic"])
        return _truncate_to_chars(compiled, max_tokens)

    ticket_section = f"# Current Ticket: {ticket.title}\n"
    ticket_section += f"Priority: {ticket.priority}\n"
    ticket_section += f"Status: {ticket.status}\n\n"
    if ticket.description:
        ticket_section += f"## Description\n{ticket.description}\n\n"

    criteria = _loads(ticket.acceptance_criteria)
    if criteria:
        ticket_section += "## Acceptance Criteria\n"
        for c in criteria:
            ticket_section += f"- [ ] {c}\n"
        ticket_section += "\n"

    files = _loads(ticket.file_refs)
    if files:
        ticket_section += "## Files to Review/Modify\n"
        for f in files:
            ticket_section += f"- {f}\n"
        ticket_section += "\n"

    # Explicit KB context (hand-picked claims from context_refs).
    # Project-scoped + active-claim filter (KB 332 HIGH fix): a caller
    # can stuff cross-project claim ids into context_refs at create/update
    # time; the compile path is the last line of defense, so it must
    # filter by project_id, active-claim status, and freshness.
    # context_refs is stored as list[str] (JSON-as-text column) but
    # KnowledgeEntry.id is Integer. Cast and drop non-numeric entries
    # rather than letting SQLAlchemy coerce inconsistently across drivers.
    raw_claim_ids = _loads(ticket.context_refs)
    claim_ids: list[int] = []
    for raw in raw_claim_ids:
        try:
            claim_ids.append(int(raw))
        except (TypeError, ValueError):
            continue
    if claim_ids:
        claims = (
            await db.execute(
                select(KnowledgeEntry).where(
                    KnowledgeEntry.id.in_(claim_ids),
                    KnowledgeEntry.project_id == ticket.project_id,
                    KnowledgeEntry.claim_class == "claim",
                    KnowledgeEntry.superseded_by.is_(None),
                    KnowledgeEntry.dismissed.is_(False),
                    KnowledgeEntry.freshness_class.in_(("current", "aging")),
                )
            )
        ).scalars().all()
        if claims:
            ticket_section += "## Relevant Project Knowledge\n"
            for claim in claims:
                ticket_section += f"- [{claim.entry_type}] {claim.content}\n"
            ticket_section += "\n"

    # Completion notes from already-done dependencies.
    # Same-project filter (KB 334 MEDIUM fix): the normal create/update
    # path rejects cross-project deps (Phase 3), but a stale bad
    # TicketDependency row from the pre-fix era or a manual DB edit
    # would otherwise leak a foreign project's completion_notes through
    # this compile path. Belt + suspenders defense-in-depth.
    upstream_ids = (
        await db.execute(
            select(TicketDependency.depends_on_id).where(
                TicketDependency.ticket_id == ticket.id
            )
        )
    ).scalars().all()
    if upstream_ids:
        done_deps = (
            await db.execute(
                select(Ticket).where(
                    Ticket.id.in_(list(upstream_ids)),
                    Ticket.project_id == ticket.project_id,
                    Ticket.status == "done",
                )
            )
        ).scalars().all()
        for dep in done_deps:
            if dep.completion_notes:
                ticket_section += (
                    f"## From completed dependency #{dep.id} "
                    f"({dep.title})\n{dep.completion_notes}\n\n"
                )

    sections.append(ticket_section)

    # Recent comments (chronological).
    comments = (
        await db.execute(
            select(TicketComment)
            .where(TicketComment.ticket_id == ticket.id)
            .order_by(TicketComment.created_at.desc())
            .limit(10)
        )
    ).scalars().all()
    if comments:
        comment_section = "## Recent Comments\n"
        for c in reversed(comments):
            author = c.author_persona or "user"
            date_str = c.created_at.strftime("%Y-%m-%d")
            comment_section += f"**{author}** ({date_str}): {c.content}\n\n"
        sections.append(comment_section)

    compiled = "\n---\n\n".join(sections)
    max_tokens = _TOOL_TOKEN_LIMITS.get(tool, _TOOL_TOKEN_LIMITS["generic"])
    return _truncate_to_chars(compiled, max_tokens)


async def _get_ticket_or_404(
    project_id: str, ticket_id: str, db: AsyncSession
) -> Ticket:
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


async def _validate_dependencies_same_project(
    db: AsyncSession, project_id: str, depends_on: list[str]
) -> None:
    """v0.10.1 Phase 3 Round 2 (KB 326) — every depends_on id must
    exist AND belong to the same project as the ticket being created.

    Without this gate, dangling IDs and cross-project references
    silently land in `ticket_dependencies`, then `_enrich_dependents`
    walks across project boundaries on accept and leaks completion
    notes / titles / KB refs into other projects' tickets. The fix
    is two-layered: pre-validate here, plus a defensive
    project_id filter in `_enrich_dependents` below.
    """
    if not depends_on:
        return
    rows = (
        await db.execute(
            select(Ticket.id).where(
                Ticket.id.in_(depends_on),
                Ticket.project_id == project_id,
            )
        )
    ).scalars().all()
    found = set(rows)
    missing = [d for d in depends_on if d not in found]
    if missing:
        raise HTTPException(
            400,
            f"depends_on references unknown or cross-project ticket(s): "
            f"{sorted(missing)}. Dependencies must exist in the same project.",
        )


async def _check_dependency_cycle(
    db: AsyncSession, ticket_id: str, new_depends_on: list[str]
) -> None:
    """Application-layer DAG enforcement.

    Before adding edges (ticket_id → dep_id), walk forward from each
    dep_id in BFS to confirm we never reach ticket_id. If we do, the
    proposed edge would create a cycle — raise 400.
    """
    if not new_depends_on:
        return
    for dep_id in new_depends_on:
        if dep_id == ticket_id:
            # Caught by ck_no_self_dep at the DB level too, but reject
            # earlier with a clearer message.
            raise HTTPException(
                400, "A ticket cannot depend on itself"
            )
        visited: set[str] = set()
        frontier = [dep_id]
        while frontier:
            current = frontier.pop()
            if current in visited:
                continue
            visited.add(current)
            if current == ticket_id:
                raise HTTPException(
                    400,
                    f"Adding dependency on {dep_id!r} would create a cycle "
                    f"reaching {ticket_id!r}.",
                )
            next_hops = (
                await db.execute(
                    select(TicketDependency.depends_on_id).where(
                        TicketDependency.ticket_id == current
                    )
                )
            ).scalars().all()
            frontier.extend(next_hops)


# ── Routes ──


@router.get("/{project_id}/tickets", response_model=list[TicketResponse])
async def list_tickets(
    project_id: str,
    assigned_to: str | None = None,
    status: str | None = None,
    priority: str | None = None,
    user: User = Depends(get_current_user),
    ctx: UserContext = Depends(get_user_context),
    db: AsyncSession = Depends(get_db),
) -> list[TicketResponse]:
    check_feature(ctx, "agent_tickets")
    await _get_project_or_404(project_id, db, user.id)

    stmt = select(Ticket).where(Ticket.project_id == project_id)
    if assigned_to is not None:
        stmt = stmt.where(Ticket.assigned_to == assigned_to)
    if status is not None:
        stmt = stmt.where(Ticket.status == status)
    if priority is not None:
        stmt = stmt.where(Ticket.priority == priority)
    stmt = stmt.order_by(Ticket.created_at.desc())
    rows = (await db.execute(stmt)).scalars().all()
    # Batch-load dependencies for all tickets in one query.
    if not rows:
        return []
    ids = [t.id for t in rows]
    dep_rows = (
        await db.execute(
            select(TicketDependency.ticket_id, TicketDependency.depends_on_id).where(
                TicketDependency.ticket_id.in_(ids)
            )
        )
    ).all()
    deps_by_ticket: dict[str, list[str]] = {tid: [] for tid in ids}
    for tid, dep_id in dep_rows:
        deps_by_ticket[tid].append(dep_id)
    return [_to_response(t, deps_by_ticket[t.id]) for t in rows]


@router.get(
    "/{project_id}/tickets/{ticket_id}", response_model=TicketResponse
)
async def get_ticket(
    project_id: str,
    ticket_id: str,
    user: User = Depends(get_current_user),
    ctx: UserContext = Depends(get_user_context),
    db: AsyncSession = Depends(get_db),
) -> TicketResponse:
    check_feature(ctx, "agent_tickets")
    await _get_project_or_404(project_id, db, user.id)
    ticket = await _get_ticket_or_404(project_id, ticket_id, db)
    deps = await _ticket_dependencies(db, ticket.id)
    return _to_response(ticket, deps)


@router.post(
    "/{project_id}/tickets",
    response_model=TicketResponse,
    status_code=201,
)
async def create_ticket(
    project_id: str,
    body: TicketCreate,
    user: User = Depends(get_current_user),
    ctx: UserContext = Depends(get_user_context),
    db: AsyncSession = Depends(get_db),
) -> TicketResponse:
    """Create a ticket.

    - source='human' (default): status='open', no quality gates.
    - source='agent': status='suggested', acceptance criteria required,
      description >=20 chars, max 3 per session.
    """
    check_feature(ctx, "agent_tickets")
    await _get_project_or_404(project_id, db, user.id)

    # Agent-created quality gates.
    if body.source == "agent":
        if not body.acceptance_criteria:
            raise HTTPException(
                400,
                "Agent-created tickets must include acceptance criteria",
            )
        if len(body.description.strip()) < _AGENT_TICKET_DESC_MIN:
            raise HTTPException(
                400,
                f"Agent-created tickets need a meaningful description "
                f"({_AGENT_TICKET_DESC_MIN}+ chars)",
            )
        if body.created_by_session_id:
            count = (
                await db.execute(
                    select(Ticket).where(
                        Ticket.created_by_session_id == body.created_by_session_id
                    )
                )
            ).scalars().all()
            if len(count) >= _AGENT_TICKET_PER_SESSION_CAP:
                raise HTTPException(
                    429,
                    f"Maximum {_AGENT_TICKET_PER_SESSION_CAP} tickets per session",
                )

    initial_status = "suggested" if body.source == "agent" else "open"

    ticket = Ticket(
        id=f"tk_{uuid.uuid4().hex[:16]}",
        project_id=project_id,
        title=body.title,
        description=body.description,
        priority=body.priority,
        assigned_to=body.assigned_to,
        created_by_user_id=user.id,
        created_by_session_id=body.created_by_session_id,
        created_by_persona=body.created_by_persona,
        status=initial_status,
        context_refs=json.dumps(body.context_refs),
        file_refs=json.dumps(body.file_refs),
        related_sessions=json.dumps(body.related_sessions),
        acceptance_criteria=json.dumps(body.acceptance_criteria),
    )
    db.add(ticket)
    await db.flush()

    # Attach dependencies AFTER existence + cycle check.
    if body.depends_on:
        # v0.10.1 Phase 3 Round 2 (KB 328) — dedup before validation +
        # insert. Without this, depends_on=[parent, parent] passed the
        # set-based _validate_dependencies_same_project but then the
        # loop below tried to INSERT two TicketDependency rows with
        # the same composite PK → IntegrityError → 500. Dedup
        # preserves the caller's intent (a duplicate is a no-op) AND
        # keeps the response's `depends_on` list clean.
        deduped_deps = list(dict.fromkeys(body.depends_on))
        await _validate_dependencies_same_project(db, project_id, deduped_deps)
        await _check_dependency_cycle(db, ticket.id, deduped_deps)
        for dep_id in deduped_deps:
            db.add(
                TicketDependency(ticket_id=ticket.id, depends_on_id=dep_id)
            )

    await db.commit()
    await db.refresh(ticket)
    deps = await _ticket_dependencies(db, ticket.id)
    return _to_response(ticket, deps)


@router.put(
    "/{project_id}/tickets/{ticket_id}", response_model=TicketResponse
)
async def update_ticket(
    project_id: str,
    ticket_id: str,
    body: TicketUpdate,
    user: User = Depends(get_current_user),
    ctx: UserContext = Depends(get_user_context),
    db: AsyncSession = Depends(get_db),
) -> TicketResponse:
    """Update non-lifecycle fields (title, description, priority,
    assigned_to, context_refs, file_refs, related_sessions,
    acceptance_criteria). Status transitions go through dedicated
    lifecycle routes."""
    check_feature(ctx, "agent_tickets")
    await _get_project_or_404(project_id, db, user.id)
    ticket = await _get_ticket_or_404(project_id, ticket_id, db)

    if body.title is not None:
        ticket.title = body.title
    if body.description is not None:
        ticket.description = body.description
    if body.priority is not None:
        ticket.priority = body.priority
    if body.assigned_to is not None:
        ticket.assigned_to = body.assigned_to
    if body.context_refs is not None:
        ticket.context_refs = json.dumps(body.context_refs)
    if body.file_refs is not None:
        ticket.file_refs = json.dumps(body.file_refs)
    if body.related_sessions is not None:
        ticket.related_sessions = json.dumps(body.related_sessions)
    if body.acceptance_criteria is not None:
        ticket.acceptance_criteria = json.dumps(body.acceptance_criteria)

    await db.commit()
    await db.refresh(ticket)
    deps = await _ticket_dependencies(db, ticket.id)
    return _to_response(ticket, deps)


# ── Lifecycle routes ──


@router.post(
    "/{project_id}/tickets/{ticket_id}/start",
    response_model=StartTicketResponse,
)
async def start_ticket(
    project_id: str,
    ticket_id: str,
    force: bool = False,
    tool: str = "generic",
    user: User = Depends(get_current_user),
    ctx: UserContext = Depends(get_user_context),
    db: AsyncSession = Depends(get_db),
) -> StartTicketResponse:
    """Move ticket from open → in_progress, return the compiled
    persona + ticket context the caller can inject into the AI tool.

    Atomic transition via `UPDATE ... WHERE status='open'` with
    rowcount check — prevents two concurrent starts from both
    succeeding. With `force=true`, also accepts blocked → in_progress
    as a recovery path. `tool` parameter sizes the compiled context
    to the target tool's token budget (claude-code=16k, codex=8k, etc).
    """
    check_feature(ctx, "agent_tickets")
    await _get_project_or_404(project_id, db, user.id)
    ticket = await _get_ticket_or_404(project_id, ticket_id, db)

    # Validate that the assigned persona exists at start time
    # (suggested→open conversion attribution check is handled in approve).
    persona: AgentPersona | None = None
    if ticket.assigned_to is not None:
        persona = (
            await db.execute(
                select(AgentPersona).where(
                    AgentPersona.project_id == project_id,
                    AgentPersona.name == ticket.assigned_to,
                    AgentPersona.is_active.is_(True),
                )
            )
        ).scalar_one_or_none()
        if persona is None:
            raise HTTPException(
                400,
                (
                    f"Ticket is assigned to persona {ticket.assigned_to!r} "
                    "but no active persona by that name exists in this project"
                ),
            )

    # Atomic FSM transition.
    allowed_from = {"open"}
    if force:
        allowed_from = {"open", "blocked"}
    result = await db.execute(
        update(Ticket)
        .where(
            Ticket.id == ticket_id,
            Ticket.status.in_(list(allowed_from)),
        )
        .values(
            status="in_progress",
            updated_at=datetime.now(timezone.utc),
            lease_epoch=Ticket.lease_epoch + 1,
        )
    )
    if result.rowcount != 1:
        # Refresh and report the actual current state.
        await db.rollback()
        current = (
            await db.execute(select(Ticket.status).where(Ticket.id == ticket_id))
        ).scalar_one_or_none()
        raise HTTPException(
            409,
            f"Cannot start ticket: current status is {current!r}, "
            f"expected one of {sorted(allowed_from)}. "
            f"Pass force=true to recover from 'blocked'.",
        )

    await db.commit()
    await db.refresh(ticket)
    audit_context = RetrievalAuditContext(
        id=f"ra_{uuid.uuid4().hex[:24]}",
        project_id=project_id,
        ticket_id=ticket_id,
        persona_name=ticket.assigned_to,
        lease_epoch=ticket.lease_epoch,
        created_by_user_id=user.id,
    )
    db.add(audit_context)
    await db.commit()
    deps = await _ticket_dependencies(db, ticket.id)
    compiled = await _compile_persona_context(db, persona, ticket, tool=tool)
    return StartTicketResponse(
        ticket=_to_response(ticket, deps),
        compiled_context=compiled,
        retrieval_audit_id=audit_context.id,
    )


@router.post(
    "/{project_id}/tickets/{ticket_id}/complete",
    response_model=TicketResponse,
)
async def complete_ticket(
    project_id: str,
    ticket_id: str,
    body: CompleteTicketRequest,
    user: User = Depends(get_current_user),
    ctx: UserContext = Depends(get_user_context),
    db: AsyncSession = Depends(get_db),
) -> TicketResponse:
    """Move ticket from in_progress → review."""
    check_feature(ctx, "agent_tickets")
    await _get_project_or_404(project_id, db, user.id)
    now = datetime.now(timezone.utc)
    result = await db.execute(
        update(Ticket)
        .where(
            Ticket.id == ticket_id,
            Ticket.project_id == project_id,
            Ticket.status == "in_progress",
            *(
                (Ticket.lease_epoch == body.lease_epoch,)
                if body.lease_epoch is not None
                else ()
            ),
        )
        .values(
            status="review",
            completion_notes=body.notes,
            changed_files=json.dumps(body.changed_files),
            knowledge_entry_ids=json.dumps(body.knowledge_entry_ids),
            resolver_session_id=body.resolver_session_id,
            resolver_user_id=user.id,
            updated_at=now,
        )
    )
    if result.rowcount != 1:
        await db.rollback()
        ticket = await _get_ticket_or_404(project_id, ticket_id, db)
        _assert_lease_epoch(ticket, body.lease_epoch)
        _assert_transition(ticket.status, "review")
        raise HTTPException(409, "Cannot complete ticket due to concurrent update")
    await db.commit()
    ticket = await _get_ticket_or_404(project_id, ticket_id, db)
    await db.refresh(ticket)
    deps = await _ticket_dependencies(db, ticket.id)
    return _to_response(ticket, deps)


async def _enrich_dependents(db: AsyncSession, completed: Ticket) -> None:
    """When a ticket transitions to 'done', enrich every ticket that
    depends on it: append a completion comment with the notes, merge
    knowledge_entry_ids into the dependent's context_refs, and
    auto-unblock if all upstream deps are now done.

    v0.10.1 Phase 3 Round 2 (KB 326) — defensive same-project filter.
    The pre-validation on create rejects cross-project depends_on
    rows, but legacy data or a raw SQL insert could still produce a
    cross-project edge. JOIN tickets here to enforce that we only
    enrich same-project dependents — never reach across project
    boundaries to write a comment or merge a KB ref.
    """
    # Find SAME-PROJECT dependents using the reverse-lookup index
    # added in Phase 1 Round 2 (idx_ticket_deps_depends_on), then
    # JOIN tickets to filter by project_id.
    dependent_ids = (
        await db.execute(
            select(Ticket.id)
            .join(TicketDependency, TicketDependency.ticket_id == Ticket.id)
            .where(
                TicketDependency.depends_on_id == completed.id,
                Ticket.project_id == completed.project_id,
            )
        )
    ).scalars().all()
    if not dependent_ids:
        return

    completed_kb_ids = _loads(completed.knowledge_entry_ids)

    for dep_id in dependent_ids:
        dependent = (
            await db.execute(select(Ticket).where(Ticket.id == dep_id))
        ).scalar_one_or_none()
        if dependent is None:
            continue
        if dependent.status not in {"open", "blocked", "suggested"}:
            # Don't disturb tickets that have moved past dependency
            # consumption (in_progress / review / done / cancelled).
            continue

        # 1. Add a completion-notes comment.
        comment_body = (
            f"Dependency #{completed.id} ({completed.title}) completed:\n\n"
            f"{completed.completion_notes or 'No notes provided.'}"
        )
        db.add(
            TicketComment(
                id=f"tc_{uuid.uuid4().hex[:16]}",
                ticket_id=dep_id,
                author_user_id=completed.resolver_user_id
                or completed.created_by_user_id,
                author_persona=completed.assigned_to,
                content=comment_body,
            )
        )

        # 2. Merge KB entry ids into the dependent's context_refs.
        if completed_kb_ids:
            existing_refs = _loads(dependent.context_refs)
            merged = list(dict.fromkeys(existing_refs + completed_kb_ids))
            dependent.context_refs = json.dumps(merged)

        # 3. Auto-unblock if every upstream dep is done.
        upstream = (
            await db.execute(
                select(TicketDependency.depends_on_id).where(
                    TicketDependency.ticket_id == dep_id
                )
            )
        ).scalars().all()
        if upstream:
            done_count = (
                await db.execute(
                    select(Ticket.id).where(
                        Ticket.id.in_(list(upstream)),
                        Ticket.status == "done",
                    )
                )
            ).scalars().all()
            if len(done_count) == len(upstream) and dependent.status == "blocked":
                dependent.status = "in_progress"


@router.post(
    "/{project_id}/tickets/{ticket_id}/accept",
    response_model=TicketResponse,
)
async def accept_ticket(
    project_id: str,
    ticket_id: str,
    lease_epoch: int | None = None,
    user: User = Depends(get_current_user),
    ctx: UserContext = Depends(get_user_context),
    db: AsyncSession = Depends(get_db),
) -> TicketResponse:
    """Move ticket from review → done. Enriches every dependent
    ticket with the completion notes + KB refs + auto-unblock.

    v0.10.1 Phase 3 Round 2 (KB 326) — atomic state transition via
    `UPDATE ... WHERE status='review'` with rowcount check. Without
    this guard, two concurrent accepts could BOTH observe `status='review'`
    in the SELECT, then BOTH run `_enrich_dependents()` before either
    commit lands → duplicate dependency comments + repeated KB-ref
    merges. The atomic UPDATE serializes the transition; only the
    request that owns the row mutation runs enrichment.
    """
    check_feature(ctx, "agent_tickets")
    await _get_project_or_404(project_id, db, user.id)

    now = datetime.now(timezone.utc)
    result = await db.execute(
        update(Ticket)
        .where(
            Ticket.id == ticket_id,
            Ticket.project_id == project_id,
            Ticket.status == "review",
            *(
                (Ticket.lease_epoch == lease_epoch,)
                if lease_epoch is not None
                else ()
            ),
        )
        .values(status="done", resolved_at=now, updated_at=now)
    )
    if result.rowcount != 1:
        # Either ticket doesn't exist, isn't in this project, or
        # wasn't in 'review'. Report the real situation.
        await db.rollback()
        existing = (
            await db.execute(
                select(Ticket).where(
                    Ticket.id == ticket_id,
                    Ticket.project_id == project_id,
                )
            )
        ).scalar_one_or_none()
        if existing is None:
            raise HTTPException(404, "Ticket not found")
        if lease_epoch is not None and existing.lease_epoch != lease_epoch:
            raise HTTPException(
                409,
                (
                    f"Stale ticket lease: provided lease_epoch={lease_epoch}, "
                    f"current lease_epoch={existing.lease_epoch}."
                ),
            )
        legal = sorted(_legal_next(existing.status))
        legal_str = ", ".join(legal) if legal else "(none — terminal state)"
        raise HTTPException(
            409,
            f"Cannot accept ticket: current status is {existing.status!r}, "
            f"expected 'review'. Legal transitions from "
            f"{existing.status!r}: {legal_str}.",
        )

    # Refresh the row after the atomic transition so enrichment sees
    # status='done' and the new resolved_at/updated_at.
    ticket = await _get_ticket_or_404(project_id, ticket_id, db)
    await _enrich_dependents(db, ticket)

    await db.commit()
    await db.refresh(ticket)
    deps = await _ticket_dependencies(db, ticket.id)
    return _to_response(ticket, deps)


@router.post(
    "/{project_id}/tickets/{ticket_id}/reopen",
    response_model=TicketResponse,
)
async def reopen_ticket(
    project_id: str,
    ticket_id: str,
    user: User = Depends(get_current_user),
    ctx: UserContext = Depends(get_user_context),
    db: AsyncSession = Depends(get_db),
) -> TicketResponse:
    """Move ticket from review → open (reporter requests changes)."""
    check_feature(ctx, "agent_tickets")
    await _get_project_or_404(project_id, db, user.id)
    ticket = await _get_ticket_or_404(project_id, ticket_id, db)
    _assert_transition(ticket.status, "open")

    ticket.status = "open"
    ticket.updated_at = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(ticket)
    deps = await _ticket_dependencies(db, ticket.id)
    return _to_response(ticket, deps)


@router.post(
    "/{project_id}/tickets/{ticket_id}/block",
    response_model=TicketResponse,
)
async def block_ticket(
    project_id: str,
    ticket_id: str,
    user: User = Depends(get_current_user),
    ctx: UserContext = Depends(get_user_context),
    db: AsyncSession = Depends(get_db),
) -> TicketResponse:
    """Move ticket from in_progress → blocked."""
    check_feature(ctx, "agent_tickets")
    await _get_project_or_404(project_id, db, user.id)
    ticket = await _get_ticket_or_404(project_id, ticket_id, db)
    _assert_transition(ticket.status, "blocked")

    ticket.status = "blocked"
    ticket.updated_at = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(ticket)
    deps = await _ticket_dependencies(db, ticket.id)
    return _to_response(ticket, deps)


@router.post(
    "/{project_id}/tickets/{ticket_id}/unblock",
    response_model=TicketResponse,
)
async def unblock_ticket(
    project_id: str,
    ticket_id: str,
    user: User = Depends(get_current_user),
    ctx: UserContext = Depends(get_user_context),
    db: AsyncSession = Depends(get_db),
) -> TicketResponse:
    """Move ticket from blocked → in_progress."""
    check_feature(ctx, "agent_tickets")
    await _get_project_or_404(project_id, db, user.id)
    ticket = await _get_ticket_or_404(project_id, ticket_id, db)
    _assert_transition(ticket.status, "in_progress")

    ticket.status = "in_progress"
    ticket.updated_at = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(ticket)
    deps = await _ticket_dependencies(db, ticket.id)
    return _to_response(ticket, deps)


@router.post(
    "/{project_id}/tickets/{ticket_id}/approve",
    response_model=TicketResponse,
)
async def approve_ticket(
    project_id: str,
    ticket_id: str,
    user: User = Depends(get_current_user),
    ctx: UserContext = Depends(get_user_context),
    db: AsyncSession = Depends(get_db),
) -> TicketResponse:
    """Move suggested → open (human reviews an agent-created ticket)."""
    check_feature(ctx, "agent_tickets")
    await _get_project_or_404(project_id, db, user.id)
    ticket = await _get_ticket_or_404(project_id, ticket_id, db)
    _assert_transition(ticket.status, "open")

    ticket.status = "open"
    ticket.updated_at = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(ticket)
    deps = await _ticket_dependencies(db, ticket.id)
    return _to_response(ticket, deps)


@router.post(
    "/{project_id}/tickets/{ticket_id}/dismiss",
    response_model=TicketResponse,
)
async def dismiss_ticket(
    project_id: str,
    ticket_id: str,
    user: User = Depends(get_current_user),
    ctx: UserContext = Depends(get_user_context),
    db: AsyncSession = Depends(get_db),
) -> TicketResponse:
    """Move suggested/open → cancelled."""
    check_feature(ctx, "agent_tickets")
    await _get_project_or_404(project_id, db, user.id)
    ticket = await _get_ticket_or_404(project_id, ticket_id, db)
    _assert_transition(ticket.status, "cancelled")

    ticket.status = "cancelled"
    ticket.updated_at = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(ticket)
    deps = await _ticket_dependencies(db, ticket.id)
    return _to_response(ticket, deps)


# ── Comments ──


@router.get(
    "/{project_id}/tickets/{ticket_id}/comments",
    response_model=list[CommentResponse],
)
async def list_ticket_comments(
    project_id: str,
    ticket_id: str,
    user: User = Depends(get_current_user),
    ctx: UserContext = Depends(get_user_context),
    db: AsyncSession = Depends(get_db),
) -> list[CommentResponse]:
    check_feature(ctx, "agent_tickets")
    await _get_project_or_404(project_id, db, user.id)
    await _get_ticket_or_404(project_id, ticket_id, db)  # validates ticket
    rows = (
        await db.execute(
            select(TicketComment)
            .where(TicketComment.ticket_id == ticket_id)
            .order_by(TicketComment.created_at)
        )
    ).scalars().all()
    return [
        CommentResponse(
            id=c.id,
            ticket_id=c.ticket_id,
            author_user_id=c.author_user_id,
            author_persona=c.author_persona,
            content=c.content,
            session_id=c.session_id,
            created_at=c.created_at,
        )
        for c in rows
    ]


@router.post(
    "/{project_id}/tickets/{ticket_id}/comments",
    response_model=CommentResponse,
    status_code=201,
)
async def create_ticket_comment(
    project_id: str,
    ticket_id: str,
    body: CommentCreate,
    user: User = Depends(get_current_user),
    ctx: UserContext = Depends(get_user_context),
    db: AsyncSession = Depends(get_db),
) -> CommentResponse:
    check_feature(ctx, "agent_tickets")
    await _get_project_or_404(project_id, db, user.id)
    comment_id = f"tc_{uuid.uuid4().hex[:16]}"
    now = datetime.now(timezone.utc)
    source = select(
        literal(comment_id),
        literal(ticket_id),
        literal(user.id),
        literal(body.author_persona),
        literal(body.content),
        literal(body.session_id),
        literal(now),
    ).where(
        Ticket.id == ticket_id,
        Ticket.project_id == project_id,
        *(
            (Ticket.lease_epoch == body.lease_epoch,)
            if body.lease_epoch is not None
            else ()
        ),
    )
    result = await db.execute(
        insert(TicketComment)
        .from_select(
            [
                "id",
                "ticket_id",
                "author_user_id",
                "author_persona",
                "content",
                "session_id",
                "created_at",
            ],
            source,
        )
    )
    if result.rowcount != 1:
        await db.rollback()
        ticket = await _get_ticket_or_404(project_id, ticket_id, db)
        _assert_lease_epoch(ticket, body.lease_epoch)
        raise HTTPException(409, "Cannot add comment due to concurrent update")
    await db.commit()
    comment = (
        await db.execute(select(TicketComment).where(TicketComment.id == comment_id))
    ).scalar_one()
    await db.refresh(comment)
    return CommentResponse(
        id=comment.id,
        ticket_id=comment.ticket_id,
        author_user_id=comment.author_user_id,
        author_persona=comment.author_persona,
        content=comment.content,
        session_id=comment.session_id,
        created_at=comment.created_at,
    )
