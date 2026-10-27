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
import logging
import uuid
from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, field_validator
from sqlalchemy import and_, func, or_, select, update
from sqlalchemy import false as sa_false
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server.auth.dependencies import AuthContext, get_current_user, require_scope
from sessionfs.server.db.engine import get_db
from sessionfs.server.db.models import (
    AgentPersona,
    KnowledgeEntry,
    OrgMember,
    Project,
    ProjectGroup,
    ProjectGroupAccess,
    ProjectGroupMember,
    RetrievalAuditContext,
    Ticket,
    TicketComment,
    TicketDependency,
    TicketEdit,
    TicketWatcher,
    TrustedReviewer,
    User,
)
from sessionfs.server.routes.knowledge import _get_project_for_auth
from sessionfs.server.routes.wiki import _get_project_or_404
from sessionfs.server.roles import ADMIN_ROLES
from sessionfs.server.services import jira_sync, ticket_transitions, ticket_watchers
from sessionfs.server.services.ticket_transitions import TransitionRefused
from sessionfs.server.services.ticket_writes import (
    apply_ticket_update,
    assert_lease_epoch as _assert_lease_epoch,
    assert_lease_required_mode as _assert_lease_required_mode,
    assigned_user_label,
    authorize_ticket_edit,
    check_assignee_conflict,
    insert_ticket_comment,
    resolve_assigned_user_id,
    resolve_persona_name as _resolve_persona_name,
)
from sessionfs.server.tier_gate import UserContext, check_project_feature, get_user_context

router = APIRouter(prefix="/api/v1/projects", tags=["tickets"])

logger = logging.getLogger("sessionfs.api")


# ── FSM constants ──


# Maps current status → set of legal next statuses. Routes consult
# this table to validate every transition; a missing key means the
# starting state has no legal transitions (terminal state).
#
# Two FSMs keyed by Ticket.kind (v0.10.24 tk_dbccde26ed604b3c).
# Tasks keep the full executor FSM; Issues use a simpler container
# FSM (open → in_progress → closed, cancelled as escape valve).
# Issue status is manual-close by Compass — NOT auto-derived from
# children, per CEO direction.
# The FSM tables live in services.ticket_transitions (shared with the Jira
# webhook); these names are kept for existing importers.
_LEGAL_TRANSITIONS_TASK = ticket_transitions.LEGAL_TRANSITIONS_TASK
_LEGAL_TRANSITIONS_ISSUE = ticket_transitions.LEGAL_TRANSITIONS_ISSUE
# Back-compat alias for any caller importing the old name. Tasks are
# the existing behavior, so `_LEGAL_TRANSITIONS` keeps mapping to the
# Task FSM.
_LEGAL_TRANSITIONS = _LEGAL_TRANSITIONS_TASK

_VALID_KINDS = {"issue", "task"}
_VALID_PRIORITIES = {"critical", "high", "medium", "low"}

_AGENT_TICKET_DESC_MIN = 20
_AGENT_TICKET_PER_SESSION_CAP = 3


def _legal_next(current: str, kind: str = "task") -> set[str]:
    return ticket_transitions.legal_next(current, kind)


def _assert_transition(current: str, target: str, kind: str = "task") -> None:
    if target not in _legal_next(current, kind):
        raise HTTPException(
            400, ticket_transitions.illegal_transition_message(current, target, kind)
        )


async def _jira_enqueue_status(
    db: AsyncSession,
    project: Project,
    ticket: Ticket,
    *,
    user_id: str,
    key_kind: str = "user",
) -> list[str]:
    """Queue the outbound Jira push for a ticket status change (two-way sync).

    Only an actor with edit standing on the project (or a service key, which
    already passed its org / project-allowlist boundary) drives Jira; a
    reader's lifecycle moves on tickets they created stay inside SessionFS.
    The outbox row is written in the request transaction (so a lost
    background task is retried by the sweep) inside a SAVEPOINT: any failure
    is logged and swallowed, never failing the ticket request.
    """
    try:
        if key_kind != "service":
            from sessionfs.server.auth.project_access import user_can_edit_project

            if not await user_can_edit_project(db, user_id, project):
                return []
        return await jira_sync.enqueue_status_change(db, ticket)
    except Exception as exc:  # noqa: BLE001 — best-effort side channel
        logger.warning(
            "jira outbound enqueue failed for ticket %s: %s",
            ticket.id,
            type(exc).__name__,
        )
        return []


# ── Request / response models ──


class TicketCreate(BaseModel):
    title: str
    description: str = ""
    priority: str = "medium"
    assigned_to: str | None = None
    # Direct-to-person assignment (GitHub #96): a teammate's user id, or an
    # email/id in ``assigned_user``. Mutually exclusive with ``assigned_to``;
    # setting one clears the other.
    assigned_user_id: str | None = None
    assigned_user: str | None = None
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
    # v0.10.24 tk_dbccde26ed604b3c — Issue/Task rollup. `kind` defaults
    # to 'task' (existing behavior). `parent_ticket_id` links a Task to
    # its containing Issue. Validated server-side: parent must exist,
    # must be in the same project, must be kind='issue', and the child
    # must be kind='task' (no Issue-under-Issue in v1).
    kind: str = "task"
    parent_ticket_id: str | None = None

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

    @field_validator("kind")
    @classmethod
    def _kind_shape(cls, v: str) -> str:
        if v not in _VALID_KINDS:
            raise ValueError(
                f"kind must be one of: {sorted(_VALID_KINDS)}"
            )
        return v


class TicketUpdate(BaseModel):
    """Partial update for non-lifecycle fields (re-assign, re-prioritize,
    edit description, attach files/criteria/context). Status transitions
    happen through dedicated lifecycle routes — NOT through this PUT.

    tk_835a876529de4551 — every successful call posts a `system`-authored
    diff comment on the ticket AND writes one TicketEdit audit row per
    mutated field. Callers do not need to manually add a comment to
    record what changed.

    Optimistic concurrency: if `lease_epoch` is present, the update is
    atomically rejected with 409 when the ticket's epoch has advanced.
    When omitted, last-write-wins is allowed but the auto-posted diff
    comment notes the unfenced edit.

    Authorization (route layer): caller must be the ticket creator or
    a project admin. Server-side lease-holder validation is deferred —
    the local active-ticket bundle is the canonical lease holder and
    is not durably reflected on the Ticket row in v1.
    """

    title: str | None = None
    description: str | None = None
    priority: str | None = None
    assigned_to: str | None = None
    assigned_user_id: str | None = None
    assigned_user: str | None = None
    context_refs: list[str] | None = None
    file_refs: list[str] | None = None
    related_sessions: list[str] | None = None
    acceptance_criteria: list[str] | None = None
    depends_on: list[str] | None = None
    lease_epoch: int | None = None

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


class TicketJiraLink(BaseModel):
    """Jira promotion link for a ticket (present only when promoted)."""

    issue_key: str
    issue_url: str
    last_known_status: str | None = None


class AssignedUser(BaseModel):
    """A teammate a ticket is assigned to (person assignment, GitHub #96)."""

    id: str
    display_name: str | None
    email: str


class AssignableUser(BaseModel):
    """A teammate who may be assigned a ticket in this project."""

    id: str
    display_name: str | None
    email: str
    can_edit: bool


class AssignableUsersResponse(BaseModel):
    users: list[AssignableUser]


class TicketResponse(BaseModel):
    id: str
    project_id: str
    title: str
    description: str
    priority: str
    assigned_to: str | None
    assigned_user_id: str | None = None
    assigned_user: AssignedUser | None = None
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
    # v0.10.24 tk_dbccde26ed604b3c — Issue/Task rollup. `kind` is
    # 'issue' or 'task'; existing rows default to 'task'. `parent_ticket_id`
    # is set on Tasks filed under an Issue; NULL otherwise. `child_ticket_ids`
    # is the rollup list for Issues; empty for Tasks (a Task can't have
    # children in v1).
    kind: str = "task"
    parent_ticket_id: str | None = None
    child_ticket_ids: list[str] = []
    created_at: datetime
    updated_at: datetime
    resolved_at: datetime | None
    # v0.23 — Jira promotion link (present only after `publish_jira`).
    jira: TicketJiraLink | None = None
    # True while a publish is in flight (a pending claim exists but the issue
    # is not linked yet); `jira` stays None until the link is actually live.
    jira_publish_in_progress: bool = False
    # True when a prior publish timed out mid-create; the row is ``unknown``
    # and a re-publish will search Jira by label and adopt-or-create.
    jira_publish_unknown: bool = False


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
    # GH #70 — canonical name of the persona the ticket resolved to (may
    # differ in case from ticket.assigned_to). Clients write THIS into the
    # local provenance bundle.
    persona_name: str | None = None


class CommentCreate(BaseModel):
    content: str
    author_persona: str | None = None
    session_id: str | None = None
    lease_epoch: int | None = None
    # Best-effort push of this comment to the linked Jira issue. Honored only
    # for editor-tier callers (two-way sync); a plain contributor's flag is
    # ignored so a read-mostly role can't drive outbound Jira comments.
    send_to_jira: bool = False

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
    # 'sessionfs' for comments written in-app/CLI; 'jira' for inbound Jira
    # comments and "Edited in Jira" system notes (never a trusted verdict).
    source: str = "sessionfs"


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


def _assigned_user_model(user: User, *, show_email: bool) -> AssignedUser:
    """The response shape for a person assignment.

    The full address is shown only to callers with real read standing on the
    project (owner / org member / group grantee, or a service key inside its
    org). A captured-session-only reader — possibly someone outside the org
    who merely synced a session on the same remote — sees a masked address,
    matching ``list_assignable_users``' refusal to list teammates to them.
    """
    from sessionfs.server.email import mask_address

    return AssignedUser(
        id=user.id,
        display_name=user.display_name,
        email=user.email if show_email else mask_address(user.email),
    )


async def _viewer_sees_emails(
    db: AsyncSession, auth: AuthContext, project: Project
) -> bool:
    """Whether the caller may see teammates' full email addresses."""
    if auth.key_kind == "service":
        # Service keys are already confined to their org + project allowlist.
        return True
    return await _user_sees_emails(db, auth.user.id, project)


async def _user_sees_emails(db: AsyncSession, user_id: str, project: Project) -> bool:
    """User-key form of ``_viewer_sees_emails`` (routes that take ``User``)."""
    from sessionfs.server.auth.project_access import user_has_read_standing

    return await user_has_read_standing(db, user_id, project)


async def _load_assigned_user(
    db: AsyncSession, assigned_user_id: str | None, *, show_email: bool = False
) -> AssignedUser | None:
    """Load the person assigned to a ticket, if any (detail-read path)."""
    if not assigned_user_id:
        return None
    user = await db.get(User, assigned_user_id)
    return (
        _assigned_user_model(user, show_email=show_email) if user is not None else None
    )


async def _batch_assigned_users(
    db: AsyncSession, user_ids: set[str], *, show_email: bool = False
) -> dict[str, AssignedUser]:
    """Load many assigned users in one query (list path — no N+1)."""
    if not user_ids:
        return {}
    rows = (
        await db.execute(select(User).where(User.id.in_(list(user_ids))))
    ).scalars().all()
    return {u.id: _assigned_user_model(u, show_email=show_email) for u in rows}


async def _ticket_response(
    db: AsyncSession,
    ticket: Ticket,
    deps: list[str],
    child_ticket_ids: list[str] | None = None,
    *,
    show_email: bool = False,
) -> TicketResponse:
    """Full response for one ticket, person assignment already resolved.

    ``show_email`` defaults to masked; read/create/update routes pass the
    caller's standing (``_viewer_sees_emails``)."""
    assigned_user = await _load_assigned_user(
        db, ticket.assigned_user_id, show_email=show_email
    )
    return _to_response(ticket, deps, child_ticket_ids, assigned_user)


def _to_response(
    t: Ticket,
    deps: list[str],
    child_ticket_ids: list[str] | None = None,
    assigned_user: AssignedUser | None = None,
) -> TicketResponse:
    return TicketResponse(
        id=t.id,
        project_id=t.project_id,
        title=t.title,
        description=t.description,
        priority=t.priority,
        assigned_to=t.assigned_to,
        assigned_user_id=t.assigned_user_id,
        assigned_user=assigned_user,
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
        kind=t.kind,
        parent_ticket_id=t.parent_ticket_id,
        child_ticket_ids=child_ticket_ids or [],
        created_at=t.created_at,
        updated_at=t.updated_at,
        resolved_at=t.resolved_at,
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
    # The docs already promised kilo-code the Cline-family 4k budget; it
    # was missing here and silently fell back to generic (8k).
    "kilo-code": 4000,
    "amp": 8000,
    # Antigravity (desktop app, agy CLI, IDE) fronts Gemini-class models.
    "antigravity": 8000,
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
    ticket_section += f"Kind: {ticket.kind}\n"
    ticket_section += f"Priority: {ticket.priority}\n"
    ticket_section += f"Status: {ticket.status}\n"

    # v0.10.24 Codex R1 MED #3 (tk_dbccde26ed604b3c) — surface the
    # Issue/Task rollup in the compiled context so an agent starting a
    # child Task sees its parent Issue, and an agent starting an Issue
    # sees its child Tasks. Without this, the rollup data was on the
    # API response but invisible to the agent reading compiled_context.
    if ticket.parent_ticket_id:
        parent_row = (
            await db.execute(
                select(Ticket).where(
                    Ticket.id == ticket.parent_ticket_id,
                    Ticket.project_id == ticket.project_id,
                )
            )
        ).scalar_one_or_none()
        if parent_row is not None:
            ticket_section += (
                f"Parent Issue: {parent_row.id} — {parent_row.title} "
                f"(status: {parent_row.status})\n"
            )
        else:
            ticket_section += f"Parent Issue: {ticket.parent_ticket_id}\n"
    if ticket.kind == "issue":
        child_rows = (
            await db.execute(
                select(Ticket.id, Ticket.title, Ticket.status).where(
                    Ticket.project_id == ticket.project_id,
                    Ticket.parent_ticket_id == ticket.id,
                )
            )
        ).all()
        if child_rows:
            ticket_section += f"\n## Child Tasks ({len(child_rows)})\n"
            for cid, ctitle, cstatus in child_rows:
                ticket_section += f"- {cid} [{cstatus}] {ctitle}\n"

    ticket_section += "\n"
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


TicketAction = Literal["approve", "dismiss", "execute", "accept", "reopen"]

_TICKET_ACTION_MESSAGES: dict[str, str] = {
    "approve": (
        "Only the project owner or a member of the project's organization "
        "can approve a suggested ticket."
    ),
    "dismiss": (
        "Only the ticket's creator, the project owner or a member of the "
        "project's organization can dismiss this ticket."
    ),
    "execute": (
        "Only the ticket's creator or resolver, the project owner or a member "
        "of the project's organization can start, complete, block or unblock "
        "this ticket."
    ),
    "accept": (
        "Only the ticket's creator, the project owner or a member of the "
        "project's organization can accept this ticket, and a creator who is "
        "not an owner or org member cannot accept work they resolved "
        "themselves."
    ),
    "reopen": (
        "Only the ticket's creator, the project owner or a member of the "
        "project's organization can reopen this ticket."
    ),
}


async def _require_ticket_actor(
    db: AsyncSession,
    project: Project,
    ticket: Ticket,
    user_id: str,
    *,
    action: TicketAction,
    key_kind: str = "user",
) -> None:
    """Lifecycle gate on top of project access.

    Editors (project owner, org member) may move any ticket through its
    lifecycle, as before. A reader whose only access is a captured session
    on the project's repository is limited to:

    * ``approve`` — never (suggested → open is an editor's triage call);
    * ``dismiss`` — tickets they created;
    * ``execute`` (start / complete / block / unblock) — tickets they
      created or resolved;
    * ``accept`` / ``reopen`` — tickets they created, and never ``accept``
      on a ticket they also resolved (no self-review closure).

    Anything else is 403 ``project_edit_forbidden``. Service keys keep their
    own org / allowlist boundary and are not affected.
    """
    if key_kind == "service":
        return
    from sessionfs.server.auth.project_access import user_can_edit_project

    if await user_can_edit_project(db, user_id, project):
        return
    is_creator = ticket.created_by_user_id == user_id
    is_resolver = ticket.resolver_user_id == user_id
    if action == "execute":
        allowed = is_creator or is_resolver
    elif action == "accept":
        allowed = is_creator and not is_resolver
    elif action in ("dismiss", "reopen"):
        allowed = is_creator
    else:  # approve
        allowed = False
    if not allowed:
        raise HTTPException(
            403,
            {"code": "project_edit_forbidden", "message": _TICKET_ACTION_MESSAGES[action]},
        )


async def _require_ticket_actor_by_id(
    db: AsyncSession,
    project: Project,
    project_id: str,
    ticket_id: str,
    user_id: str,
    *,
    action: TicketAction,
    key_kind: str = "user",
) -> None:
    """``_require_ticket_actor`` for routes that transition the ticket with
    an atomic UPDATE before loading it. A missing ticket is left to the
    route's own 404 path."""
    if key_kind == "service":
        return
    ticket = (
        await db.execute(
            select(Ticket).where(
                Ticket.id == ticket_id, Ticket.project_id == project_id
            )
        )
    ).scalar_one_or_none()
    if ticket is not None:
        await _require_ticket_actor(
            db, project, ticket, user_id, action=action, key_kind=key_kind
        )


async def _ticket_detail_response(
    ticket: Ticket, db: AsyncSession, *, show_email: bool = False
) -> TicketResponse:
    """Full detail payload for one ticket the caller is already allowed to read
    (dependencies, Issue rollup, Jira link state)."""
    deps = await _ticket_dependencies(db, ticket.id)
    child_ticket_ids: list[str] = []
    if ticket.kind == "issue":
        # v0.10.24 — rollup query. Single SELECT, indexed by
        # (project_id, parent_ticket_id). Tasks return empty list
        # (Tasks cannot have children in v1).
        child_rows = (
            await db.execute(
                select(Ticket.id).where(
                    Ticket.project_id == ticket.project_id,
                    Ticket.parent_ticket_id == ticket.id,
                )
            )
        ).scalars().all()
        child_ticket_ids = list(child_rows)
    response = await _ticket_response(
        db, ticket, deps, child_ticket_ids, show_email=show_email
    )
    from sessionfs.server.db.models import TicketTrackerLink

    link = (
        await db.execute(
            select(TicketTrackerLink).where(TicketTrackerLink.ticket_id == ticket.id)
        )
    ).scalar_one_or_none()
    if link is not None:
        if link.status == "linked":
            response.jira = TicketJiraLink(
                issue_key=link.issue_key,
                issue_url=link.issue_url,
                last_known_status=link.last_known_status,
            )
        elif link.status == "pending":
            # A pending claim is a publish in flight, not a link: reads must
            # never surface a half-written issue.
            response.jira_publish_in_progress = True
        elif link.status == "unknown":
            # A timed-out publish left no confirmed issue; surface it so the
            # caller knows a re-publish will adopt-or-create.
            response.jira_publish_unknown = True
    return response


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
    assigned_user: str | None = None,
    status: str | None = None,
    priority: str | None = None,
    kind: str | None = None,
    auth: AuthContext = Depends(require_scope("tickets:read")),
    ctx: UserContext = Depends(get_user_context),
    db: AsyncSession = Depends(get_db),
) -> list[TicketResponse]:
    project = await _get_project_for_auth(project_id, db, auth)
    from sessionfs.server.auth.dependencies import (
        assert_service_key_can_access_project,
    )
    await assert_service_key_can_access_project(db, auth, project)
    await check_project_feature(ctx, "agent_tickets", project, db)

    if kind is not None and kind not in _VALID_KINDS:
        raise HTTPException(
            400, f"kind must be one of: {sorted(_VALID_KINDS)}"
        )

    stmt = select(Ticket).where(Ticket.project_id == project_id)
    if assigned_to is not None:
        # Codex v0.10.23 R1 MEDIUM (tk_884b2321fdb74170) — discovery
        # must also be case-insensitive, otherwise an Atlas agent
        # filtering `list_tickets(assigned_to='atlas')` won't see a
        # legacy row with `assigned_to='Atlas'`. The start path was
        # already fixed in this ticket; the list path was the missing
        # half. Use func.lower() to match the row regardless of stored
        # case — same predicate shape the start_ticket resolver uses.
        stmt = stmt.where(
            func.lower(Ticket.assigned_to) == assigned_to.lower()
        )
    if assigned_user is not None:
        # GitHub #96 — filter by direct-to-person assignment. `me` means
        # "the authenticated user"; otherwise the value is a user id.
        if assigned_user == "me":
            if auth.key_kind == "service":
                raise HTTPException(
                    400,
                    "assigned_user=me is not available to service keys",
                )
            stmt = stmt.where(Ticket.assigned_user_id == auth.user.id)
        else:
            stmt = stmt.where(Ticket.assigned_user_id == assigned_user)
    if status is not None:
        stmt = stmt.where(Ticket.status == status)
    if priority is not None:
        stmt = stmt.where(Ticket.priority == priority)
    if kind is not None:
        stmt = stmt.where(Ticket.kind == kind)
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
    # v0.10.24 — batch-load child ticket ids for any Issues in the
    # result set so the response includes the rollup. Tasks return
    # an empty list (they cannot have children in v1).
    issue_ids = [t.id for t in rows if t.kind == "issue"]
    children_by_parent: dict[str, list[str]] = {iid: [] for iid in issue_ids}
    if issue_ids:
        # Codex R1 LOW #4 (tk_dbccde26ed604b3c) — defensive project_id
        # filter on the list rollup. Create path prevents cross-project
        # parentage, but the schema cannot enforce it (FK does not
        # constrain project_id), so legacy or manual rows could leak a
        # foreign child's id through this list query. Match the detail
        # endpoint's predicate shape.
        child_rows = (
            await db.execute(
                select(Ticket.id, Ticket.parent_ticket_id).where(
                    Ticket.project_id == project_id,
                    Ticket.parent_ticket_id.in_(issue_ids),
                )
            )
        ).all()
        for cid, pid in child_rows:
            children_by_parent.setdefault(pid, []).append(cid)
    # GitHub #96 — batch-load every person assignment in one query so a
    # long list never issues one SELECT per assigned ticket.
    assigned_user_ids = {t.assigned_user_id for t in rows if t.assigned_user_id}
    assigned_users = await _batch_assigned_users(
        db,
        assigned_user_ids,
        show_email=await _viewer_sees_emails(db, auth, project),
    )
    return [
        _to_response(
            t,
            deps_by_ticket[t.id],
            children_by_parent.get(t.id, []),
            assigned_users.get(t.assigned_user_id) if t.assigned_user_id else None,
        )
        for t in rows
    ]


@router.get(
    "/{project_id}/assignable-users", response_model=AssignableUsersResponse
)
async def list_assignable_users(
    project_id: str,
    auth: AuthContext = Depends(require_scope("tickets:read")),
    ctx: UserContext = Depends(get_user_context),
    db: AsyncSession = Depends(get_db),
) -> AssignableUsersResponse:
    """Active teammates this project's readers may assign a ticket to.

    GitHub #96 — the eligibility predicate matches ``resolve_assigned_user_id``:
    a teammate with read standing on the project (owner, org member of an
    unrestricted org, org admin/owner, or a group grant under restricted
    visibility). Captured-session-only readers are deliberately excluded — a
    person who merely synced a session is not a teammate you can hand work to.
    ``can_edit`` reflects editor standing so the dashboard can hint at the
    stronger role. This is a user-key endpoint: a service key has no person to
    speak for, so it gets 403.
    """
    if auth.key_kind == "service":
        raise HTTPException(
            403, "assignable-users requires a user key, not a service key"
        )
    project = await _get_project_for_auth(project_id, db, auth)
    await check_project_feature(ctx, "agent_tickets", project, db)

    from sessionfs.server.auth.project_access import (
        project_editor_user_ids,
        project_restricts_visibility,
        user_has_read_standing,
    )

    # GitHub #96 — listing who you may hand work to requires real read
    # standing, not just a captured session. A synced-session-only reader
    # isn't a teammate, so admit only owner / org member / group grantee.
    if not await user_has_read_standing(db, auth.user.id, project):
        raise HTTPException(
            403,
            {
                "error": "project_member_required",
                "message": "Only project members can list assignable users.",
            },
        )

    user_ids: set[str] = {project.owner_id}
    if project.org_id is not None:
        if await project_restricts_visibility(db, project):
            # Restricted org: org admins/owners, plus any member holding a
            # grant on a live group containing the project. Mirror
            # ``user_has_read_standing`` (any group role confers read).
            grant_users = (
                select(ProjectGroupMember.user_id)
                .join(
                    ProjectGroupAccess,
                    ProjectGroupAccess.group_id == ProjectGroupMember.group_id,
                )
                .join(ProjectGroup, ProjectGroup.id == ProjectGroupAccess.group_id)
                .where(
                    ProjectGroupAccess.project_id == project.id,
                    ProjectGroup.deleted_at.is_(None),
                    ProjectGroup.org_id == project.org_id,
                )
            )
            rows = (
                await db.execute(
                    select(OrgMember.user_id).where(
                        OrgMember.org_id == project.org_id,
                        or_(
                            OrgMember.role.in_(ADMIN_ROLES),
                            OrgMember.user_id.in_(grant_users),
                        ),
                    )
                )
            ).scalars().all()
        else:
            rows = (
                await db.execute(
                    select(OrgMember.user_id).where(
                        OrgMember.org_id == project.org_id
                    )
                )
            ).scalars().all()
        user_ids.update(rows)

    users = (
        await db.execute(
            select(User).where(User.id.in_(list(user_ids)), User.is_active.is_(True))
        )
    ).scalars().all()
    editor_ids = await project_editor_user_ids(db, project)
    result = [
        AssignableUser(
            id=u.id,
            display_name=u.display_name,
            email=u.email,
            can_edit=u.id in editor_ids,
        )
        for u in users
    ]
    result.sort(key=lambda u: (u.display_name or u.email).lower())
    return AssignableUsersResponse(users=result)


@router.get(
    "/{project_id}/tickets/{ticket_id}", response_model=TicketResponse
)
async def get_ticket(
    project_id: str,
    ticket_id: str,
    auth: AuthContext = Depends(require_scope("tickets:read")),
    ctx: UserContext = Depends(get_user_context),
    db: AsyncSession = Depends(get_db),
) -> TicketResponse:
    project = await _get_project_for_auth(project_id, db, auth)
    from sessionfs.server.auth.dependencies import (
        assert_service_key_can_access_project,
    )
    await assert_service_key_can_access_project(db, auth, project)
    await check_project_feature(ctx, "agent_tickets", project, db)
    ticket = await _get_ticket_or_404(project_id, ticket_id, db)
    return await _ticket_detail_response(
        ticket, db, show_email=await _viewer_sees_emails(db, auth, project)
    )


@router.get(
    "/{project_id}/tickets/{ticket_id}/review-state",
)
async def get_ticket_review_state(
    project_id: str,
    ticket_id: str,
    auth: AuthContext = Depends(require_scope("tickets:read")),
    ctx: UserContext = Depends(get_user_context),
    db: AsyncSession = Depends(get_db),
):
    """v0.10.11 tk_e025375272b84a95 — structured review-state derived
    from the ticket's comment thread.

    Returns null when the ticket has no codex-reviewer comments. For
    review tickets, returns a `ReviewState` dict with open_findings,
    closed_findings, severity_counts, last_verdict, etc. — see
    `sessionfs.server.services.review_state` for the shape.

    Auth: same as `GET /tickets/{id}` — project membership required.
    Cross-project access is denied by `_get_project_or_404` before
    we touch the comments table.
    """
    from sessionfs.server.services.review_state import compute_review_state

    project = await _get_project_for_auth(project_id, db, auth)
    from sessionfs.server.auth.dependencies import (
        assert_service_key_can_access_project,
    )
    await assert_service_key_can_access_project(db, auth, project)
    await check_project_feature(ctx, "agent_tickets", project, db)
    ticket = await _get_ticket_or_404(project_id, ticket_id, db)

    # tk_33a25a12a5cf4dc3 — Shield-SR LOW follow-up from v0.10.11.
    # Cap row count for DoS defense, matching list_ticket_comments
    # (also 500). Review threads cap at ~20-50 comments in practice;
    # 500 is a defensive ceiling, not an expected limit. If a thread
    # ever exceeds it, the oldest comments win — the earliest rounds
    # are what callers care about for the "open findings" picture.
    _REVIEW_STATE_COMMENT_LIMIT = 500
    rows = (
        await db.execute(
            select(TicketComment)
            .where(TicketComment.ticket_id == ticket.id)
            .order_by(TicketComment.created_at, TicketComment.id)
            .limit(_REVIEW_STATE_COMMENT_LIMIT)
        )
    ).scalars().all()

    comments = [
        {
            "id": r.id,
            "author_persona": r.author_persona,
            "content": r.content,
            "created_at": r.created_at,
            # tk_d42170b4670f4448 — server-stamped trust marker; the
            # authority signal for verdict derivation (NOT author_persona).
            "verdict_trusted": r.verdict_trusted,
        }
        for r in rows
    ]
    state = compute_review_state(comments)
    return {"ticket_id": ticket.id, "review_state": state.to_dict() if state else None}


@router.post(
    "/{project_id}/tickets",
    response_model=TicketResponse,
    status_code=201,
)
async def create_ticket(
    project_id: str,
    body: TicketCreate,
    auth: AuthContext = Depends(require_scope("tickets:write")),
    ctx: UserContext = Depends(get_user_context),
    db: AsyncSession = Depends(get_db),
) -> TicketResponse:
    """Create a ticket.

    - source='human' (default): status='open', no quality gates.
    - source='agent': status='suggested', acceptance criteria required,
      description >=20 chars, max 3 per session.

    v0.10.24 tk_dbccde26ed604b3c — Issue/Task rollup. `kind` defaults
    to 'task' (existing behavior). When `kind='issue'`:
      - Authorization gated (Compass-assigned OR project owner).
      - Suggested-quality gate skipped; Issues start at 'open' regardless
        of source. Agent-created Issues are still allowed but bypass
        the acceptance-criteria / 20-char-description / per-session cap
        because Issues are PM-triaged containers, not executor work.
      - Cannot have a parent (no Issue-under-Issue in v1).
    When `parent_ticket_id` is provided:
      - Parent must exist, be in the same project, and be kind='issue'.
      - Child must be kind='task' (enforced above).
      - No self-parent (the new ticket has no id yet so this is implicit).
    """
    user = auth.user
    project = await _get_project_for_auth(project_id, db, auth)
    from sessionfs.server.auth.dependencies import (
        assert_service_key_can_access_project,
    )
    await assert_service_key_can_access_project(db, auth, project)
    await check_project_feature(ctx, "agent_tickets", project, db)
    # GH #70 — every ticket create (assigned or not) takes the project
    # scope lock before any read or write: serializes with project merge
    # (which relocates tickets), transfers and the persona-delete guards,
    # and refuses a merged-away project.
    from sessionfs.server.services.scope_locks import (
        lock_project_scope_authorized,
    )

    # GH #70 — lock, then re-authorize against the re-read project.
    await lock_project_scope_authorized(db, project, auth=auth, ctx=ctx, feature="agent_tickets")

    # v0.10.24 — Issue authorization. Only the project owner or a
    # project admin (org-scoped projects: OrgMember.role=='admin') may
    # file kind='issue'. Reasoning: Issues are PM-triaged rollups; if
    # every project member could file Issues the rollup loses meaning.
    #
    # Codex R1 MED #1 (tk_dbccde26ed604b3c) — earlier scope had a path
    # for "assigned_to == 'compass'" but that authorizes on mutable
    # request input (anyone could pass it). The trusted-actor signal
    # is project ownership / org-admin membership, NOT the target
    # assignee. Drop the assignee branch entirely.
    if body.kind == "issue":
        from sessionfs.server.auth.project_access import user_is_project_admin

        if not await user_is_project_admin(db, user.id, project):
            raise HTTPException(
                403,
                "Only the project owner or a project admin may file "
                "kind='issue' tickets. Issues are PM-triaged rollup "
                "containers — see KB entry 'issue-vs-ticket-design'.",
            )
        if body.parent_ticket_id is not None:
            raise HTTPException(
                400,
                "Issues cannot be nested under other Issues in v1. "
                "Leave parent_ticket_id null on kind='issue' tickets.",
            )

    # v0.10.24 — parent validation. If parent_ticket_id is provided,
    # the parent must exist in the same project AND be kind='issue'.
    # Cross-project / wrong-kind references are 422.
    if body.parent_ticket_id is not None:
        parent = (
            await db.execute(
                select(Ticket).where(
                    Ticket.id == body.parent_ticket_id,
                    Ticket.project_id == project_id,
                )
            )
        ).scalar_one_or_none()
        if parent is None:
            raise HTTPException(
                422,
                f"Parent ticket {body.parent_ticket_id!r} not found in "
                f"project {project_id!r}. Cross-project parent linking "
                "is rejected.",
            )
        if parent.kind != "issue":
            raise HTTPException(
                422,
                f"Parent ticket {body.parent_ticket_id!r} is "
                f"kind={parent.kind!r}; parent_ticket_id requires the "
                "parent to be kind='issue'.",
            )

    # Agent-created quality gates (Task-only — Issues skip these because
    # they're PM-triaged containers, not executor work units).
    if body.source == "agent" and body.kind == "task":
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

    # Issues always start 'open' (no suggested-quality gate); Tasks
    # use the existing source-based default.
    if body.kind == "issue":
        initial_status = "open"
    else:
        initial_status = "suggested" if body.source == "agent" else "open"
    # tk_25872885e3274a7d — a ticket from a captured-session reader (someone
    # who synced a session on the repo but is neither the owner nor an org
    # member) always starts 'suggested', whatever its source: an editor
    # approves it before it is actionable, adoptable by work queues or
    # notifies an assignee.
    if auth.key_kind != "service":
        from sessionfs.server.auth.project_access import user_can_edit_project

        if not await user_can_edit_project(db, user.id, project):
            initial_status = "suggested"

    # v0.10.23 tk_884b2321fdb74170 — best-effort normalize assigned_to
    # to the canonical persona name. If the value matches an active
    # persona case-insensitively, store the persona's actual `.name`
    # so the later start_ticket resolver's exact match always hits.
    # If no persona match exists, keep the value as-is — assigned_to
    # has historically also been used as a free-text owner field, so
    # rejecting unknown names would break that contract. start_ticket
    # also case-insensitive-matches as a belt-and-suspenders read
    # path for legacy rows.
    #
    # GitHub #96 — a person assignment (id or email) is mutually exclusive
    # with a persona assignment, and resolves to an active teammate with
    # read standing on the project (else 422).
    check_assignee_conflict(body.assigned_to, body.assigned_user_id, body.assigned_user)
    normalized_assigned_to = body.assigned_to
    if body.assigned_to is not None:
        from sessionfs.server.services.personas import (
            visible_persona_org_id_for_auth,
        )

        # (project scope lock taken at the top of this route)
        canonical = await _resolve_persona_name(
            db,
            project_id,
            body.assigned_to,
            org_id=await visible_persona_org_id_for_auth(db, project, auth),
        )
        if canonical is not None:
            normalized_assigned_to = canonical

    resolved_assigned_user: User | None = None
    if body.assigned_user_id is not None or body.assigned_user is not None:
        token = (
            body.assigned_user_id
            if body.assigned_user_id is not None
            else body.assigned_user
        )
        assert token is not None
        resolved_assigned_user = await resolve_assigned_user_id(
            db, project=project, value=token
        )

    ticket = Ticket(
        id=f"tk_{uuid.uuid4().hex[:16]}",
        project_id=project_id,
        title=body.title,
        description=body.description,
        priority=body.priority,
        assigned_to=normalized_assigned_to,
        assigned_user_id=(
            resolved_assigned_user.id if resolved_assigned_user is not None else None
        ),
        created_by_user_id=user.id,
        created_by_session_id=body.created_by_session_id,
        created_by_persona=body.created_by_persona,
        status=initial_status,
        context_refs=json.dumps(body.context_refs),
        file_refs=json.dumps(body.file_refs),
        related_sessions=json.dumps(body.related_sessions),
        acceptance_criteria=json.dumps(body.acceptance_criteria),
        actor_type=auth.actor_type,
        service_key_id=auth.service_key_id,
        service_key_name=auth.service_key_name,
        kind=body.kind,
        parent_ticket_id=body.parent_ticket_id,
    )
    db.add(ticket)
    await db.flush()

    # Ticket watchers: the creator follows automatically, and so does the
    # assignee's human owner when the assignment maps to a persona that
    # represents one. Subscribing here (inside the same transaction) means
    # the follow commits atomically with the ticket. A service key acts on
    # its minter's behalf, so the minter is NOT auto-subscribed here.
    if auth.key_kind != "service":
        await ticket_watchers.subscribe_guarded(
            db, ticket_id=ticket.id, user_id=user.id, source="creator"
        )
    # A suggested ticket isn't actionable yet: the assignee is subscribed and
    # handed "your turn" only when an editor approves it (approve_ticket).
    if initial_status != "suggested":
        # Person assignment wins outright; a persona assignment maps to its
        # human owner only when the mapping is unambiguous.
        assignee_user_id: str | None = None
        assignee_persona_name: str | None = None
        assignee_summary: str | None = None
        if resolved_assigned_user is not None:
            assignee_user_id = resolved_assigned_user.id
            assignee_summary = f"Assigned to {assigned_user_label(resolved_assigned_user)}"
        elif normalized_assigned_to is not None:
            from sessionfs.server.services.personas import (
                visible_persona_org_id_for_auth,
            )

            assignee_human = await ticket_watchers.map_persona_to_human(
                db,
                project_id=project_id,
                name=normalized_assigned_to,
                org_id=await visible_persona_org_id_for_auth(db, project, auth),
            )
            if assignee_human is not None:
                assignee_user_id, assignee_persona_name = assignee_human
                assignee_summary = f"Assigned to {normalized_assigned_to}"
        if assignee_user_id is not None and assignee_summary is not None:
            await ticket_watchers.subscribe_guarded(
                db,
                ticket_id=ticket.id,
                user_id=assignee_user_id,
                source="assignee",
                persona_name=assignee_persona_name,
            )
            # A ticket filed already assigned hands the assignee's human
            # owner a "your turn" right away.
            await ticket_watchers.emit_notifications(
                db,
                ticket=ticket,
                kind="assignment",
                summary=assignee_summary,
                actor_user_id=None if auth.key_kind == "service" else user.id,
                actor_type=auth.actor_type,
                service_key_name=auth.service_key_name,
                your_turn_user_id=assignee_user_id,
            )

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
    return await _ticket_response(
        db, ticket, deps, show_email=await _viewer_sees_emails(db, auth, project)
    )


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
    """Update mutable, non-lifecycle ticket fields.

    Mutable: title, description, priority, assigned_to, assigned_user_id,
    assigned_user, context_refs, file_refs, related_sessions,
    acceptance_criteria, depends_on.
    Status transitions go through dedicated lifecycle routes (start /
    complete / accept / dismiss / etc.) — NOT through this PUT.

    tk_835a876529de4551 — every mutation:
      1. Requires authz: the caller must be the ticket creator OR a
         project admin. Persona name is routing, not authorization.
      2. Optionally fences on `lease_epoch`: 409 if the caller's epoch
         is stale, last-write-wins if omitted (with a noted comment).
      3. Writes one TicketEdit audit row per mutated field with
         JSON-encoded old/new values.
      4. Auto-posts a `system`-authored TicketComment with the diff
         summary so a human reader sees what changed.
    """
    project = await _get_project_or_404(project_id, db, user.id)
    await check_project_feature(ctx, "agent_tickets", project, db)
    ticket = await _get_ticket_or_404(project_id, ticket_id, db)

    # Reject lease_epoch-only payloads BEFORE touching the epoch.
    # Codex R1 MED #2 — without this gate, `{lease_epoch: N}` would
    # atomically bump the epoch, produce zero diffs, commit, and
    # return without an audit row or system comment. That lets
    # callers silently burn another worker's lease without leaving
    # the auditable trail this verb's whole point is to provide.
    _MUTABLE_FIELDS_PRESENT = (
        body.title is not None
        or body.description is not None
        or body.priority is not None
        or body.assigned_to is not None
        or body.assigned_user_id is not None
        or body.assigned_user is not None
        # GitHub #96 — an explicit ``null`` assignment token is a mutation:
        # it clears the assignment. The non-null branches above already
        # count a value, so the model_fields_set checks only widen the gate
        # to nulls.
        or "assigned_to" in body.model_fields_set
        or "assigned_user_id" in body.model_fields_set
        or "assigned_user" in body.model_fields_set
        or body.context_refs is not None
        or body.file_refs is not None
        or body.related_sessions is not None
        or body.acceptance_criteria is not None
        or body.depends_on is not None
    )
    if not _MUTABLE_FIELDS_PRESENT:
        raise HTTPException(
            400,
            "update_ticket requires at least one mutable field "
            "(title, description, priority, assigned_to, assigned_user_id, "
            "assigned_user, context_refs, file_refs, related_sessions, "
            "acceptance_criteria, depends_on). lease_epoch alone is not a "
            "mutation — it is the fence for one.",
        )

    # Authorization. The "lease holder" in the design doc lives in the
    # local active-ticket bundle (~/.sessionfs/active_ticket.json) and
    # is not durably reflected on the Ticket row — so server-side we
    # admit ticket creator + project admin. Future work could persist
    # a current-lease-holder column and admit that too.
    await authorize_ticket_edit(db, ticket=ticket, project=project, user_id=user.id)

    # GH #70 — EVERY mutation needs the project scope lock (serializes with
    # transfers, merges and org-persona deletes), not just reassignment. A
    # title/description/priority/… edit that passed its pre-lock check can
    # still flush onto a ticket a concurrent merge relocated to another
    # project — the flush UPDATE is PK-scoped, so the project boundary must
    # be re-checked under the lock. Take the lock NOW, before the lease-epoch
    # UPDATE row-locks the ticket and before any pending attribute change can
    # autoflush: global lock order is scope locks first, row locks after
    # (services/scope_locks.py).
    from sessionfs.server.services.scope_locks import (
        lock_project_scope_authorized,
    )

    # GH #70 — lock, then re-authorize against the re-read project.
    await lock_project_scope_authorized(db, project, user_id=user.id, ctx=ctx, feature="agent_tickets")
    # Re-load the ticket and re-run the creator/project-admin check against
    # state read under the lock (a transfer or role change may have committed
    # while we waited).
    await db.refresh(ticket)
    if ticket.project_id != project_id:
        raise HTTPException(404, "Ticket not found")
    await authorize_ticket_edit(db, ticket=ticket, project=project, user_id=user.id)

    # Edit-access rules for non-editors, the lease fence, per-field audit
    # rows, the re-approval rule, the system diff comment and reassignment
    # notifications — shared with helper confirm (services/ticket_writes.py).
    await apply_ticket_update(
        db, project=project, ticket=ticket, user_id=user.id, ctx=ctx, body=body
    )

    await db.commit()
    await db.refresh(ticket)
    deps = await _ticket_dependencies(db, ticket.id)
    return await _ticket_response(
        db, ticket, deps, show_email=await _user_sees_emails(db, user.id, project)
    )


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
    auth: AuthContext = Depends(require_scope("tickets:write")),
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
    user = auth.user
    project = await _get_project_for_auth(project_id, db, auth)
    from sessionfs.server.auth.dependencies import (
        assert_service_key_can_access_project,
    )
    await assert_service_key_can_access_project(db, auth, project)
    await check_project_feature(ctx, "agent_tickets", project, db)
    # GH #70 — scope lock BEFORE reading the project's org, the ticket and
    # the persona, held through the in_progress transition and the
    # retrieval-audit insert (one transaction), so the persona cannot be
    # resolved against an org the project is concurrently leaving.
    from sessionfs.server.services.scope_locks import (
        lock_project_scope_authorized,
    )

    # GH #70 — lock, then re-authorize against the re-read project.
    await lock_project_scope_authorized(db, project, auth=auth, ctx=ctx, feature="agent_tickets")
    ticket = await _get_ticket_or_404(project_id, ticket_id, db)
    await _require_ticket_actor(db, project, ticket, user.id, action="execute", key_kind=auth.key_kind)

    # Validate that the assigned persona exists at start time
    # (suggested→open conversion attribution check is handled in approve).
    # Codex v0.10.23 tk_884b2321fdb74170 — case-insensitive lookup so
    # legacy tickets created with `assigned_to='Atlas'` against an
    # `atlas` persona record start cleanly instead of 400ing. New
    # writes are normalized to the canonical DB name at create/update
    # time (see _resolve_persona_name), so this fallback only matters
    # for rows already in the wild.
    persona: AgentPersona | None = None
    if ticket.assigned_to is not None:
        # GH #70 — resolves project first, then the project's org (only
        # when the caller is a current member of it).
        from sessionfs.server.services.personas import (
            resolve_persona,
            visible_persona_org_id_for_auth,
        )

        persona = await resolve_persona(
            db,
            project_id,
            ticket.assigned_to,
            org_id=await visible_persona_org_id_for_auth(db, project, auth),
            active_only=True,
            case_insensitive=True,
        )
        if persona is None:
            raise HTTPException(
                400,
                (
                    f"Ticket is assigned to persona {ticket.assigned_to!r} "
                    "but no active persona by that name exists in this "
                    "project or its org"
                ),
            )

    # Atomic FSM transition (shared with the Jira webhook).
    allowed_from = {"open"}
    if force:
        allowed_from = {"open", "blocked"}
    try:
        await ticket_transitions.start_ticket(
            db,
            ticket,
            allowed_from=allowed_from,
            actor_user_id=None if auth.key_kind == "service" else user.id,
            actor_type=auth.actor_type,
            service_key_name=auth.service_key_name,
        )
    except TransitionRefused as exc:
        await db.rollback()
        raise HTTPException(exc.status_code, exc.detail) from None

    # Same transaction as the transition (GH #70): the scope lock makes a
    # concurrent start wait and then trip the rowcount guard, so the
    # transition no longer needs its own early commit.
    audit_context = RetrievalAuditContext(
        id=f"ra_{uuid.uuid4().hex[:24]}",
        project_id=project_id,
        ticket_id=ticket_id,
        # GH #70 — the resolved persona's canonical name, not the raw
        # assignment string.
        persona_name=persona.name if persona is not None else None,
        lease_epoch=ticket.lease_epoch,
        created_by_user_id=user.id,
    )
    db.add(audit_context)
    deps = await _ticket_dependencies(db, ticket.id)
    compiled = await _compile_persona_context(db, persona, ticket, tool=tool)
    jira_ids = await _jira_enqueue_status(
        db, project, ticket, user_id=user.id, key_kind=auth.key_kind
    )
    await db.commit()
    jira_sync.schedule_drain(jira_ids)
    return StartTicketResponse(
        ticket=await _ticket_response(
            db, ticket, deps, show_email=await _viewer_sees_emails(db, auth, project)
        ),
        compiled_context=compiled,
        retrieval_audit_id=audit_context.id,
        persona_name=persona.name if persona is not None else None,
    )


@router.post(
    "/{project_id}/tickets/{ticket_id}/complete",
    response_model=TicketResponse,
)
async def complete_ticket(
    project_id: str,
    ticket_id: str,
    body: CompleteTicketRequest,
    auth: AuthContext = Depends(require_scope("tickets:write")),
    ctx: UserContext = Depends(get_user_context),
    db: AsyncSession = Depends(get_db),
) -> TicketResponse:
    """Move ticket from in_progress → review."""
    user = auth.user
    project = await _get_project_for_auth(project_id, db, auth)
    from sessionfs.server.auth.dependencies import (
        assert_service_key_can_access_project,
    )
    await assert_service_key_can_access_project(db, auth, project)
    await check_project_feature(ctx, "agent_tickets", project, db)
    await _assert_lease_required_mode(project_id, db, body.lease_epoch, auth=auth)
    await _require_ticket_actor_by_id(
        db, project, project_id, ticket_id, user.id, action="execute", key_kind=auth.key_kind
    )
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
        _assert_transition(ticket.status, "review", ticket.kind)
        raise HTTPException(409, "Cannot complete ticket due to concurrent update")
    ticket = await _get_ticket_or_404(project_id, ticket_id, db)
    # Ticket watchers: entering review hands the reporter a "your turn" to
    # review and accept; every other watcher gets a plain status event.
    await ticket_watchers.emit_notifications(
        db,
        ticket=ticket,
        kind="status",
        summary="Moved to review — awaiting reporter",
        actor_user_id=None if auth.key_kind == "service" else user.id,
        actor_type=auth.actor_type,
        service_key_name=auth.service_key_name,
        your_turn_user_id=ticket.created_by_user_id,
    )
    jira_ids = await _jira_enqueue_status(
        db, project, ticket, user_id=user.id, key_kind=auth.key_kind
    )
    await db.commit()
    jira_sync.schedule_drain(jira_ids)
    await db.refresh(ticket)
    deps = await _ticket_dependencies(db, ticket.id)
    return await _ticket_response(
        db, ticket, deps, show_email=await _viewer_sees_emails(db, auth, project)
    )


# Shared with the Jira webhook (services.ticket_transitions); the old names
# stay for existing callers.
_enrich_dependents = ticket_transitions.enrich_dependents
_emit_status_notification = ticket_transitions.emit_status_notification


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
    project = await _get_project_or_404(project_id, db, user.id)
    await check_project_feature(ctx, "agent_tickets", project, db)
    await _assert_lease_required_mode(project_id, db, lease_epoch)
    await _require_ticket_actor_by_id(db, project, project_id, ticket_id, user.id, action="accept")

    try:
        ticket = await ticket_transitions.accept_ticket(
            db,
            project_id=project_id,
            ticket_id=ticket_id,
            lease_epoch=lease_epoch,
            actor_user_id=user.id,
        )
    except TransitionRefused as exc:
        await db.rollback()
        raise HTTPException(exc.status_code, exc.detail) from None

    jira_ids = await _jira_enqueue_status(db, project, ticket, user_id=user.id)
    await db.commit()
    jira_sync.schedule_drain(jira_ids)
    await db.refresh(ticket)
    deps = await _ticket_dependencies(db, ticket.id)
    return await _ticket_response(
        db, ticket, deps, show_email=await _user_sees_emails(db, user.id, project)
    )


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
    project = await _get_project_or_404(project_id, db, user.id)
    await check_project_feature(ctx, "agent_tickets", project, db)
    ticket = await _get_ticket_or_404(project_id, ticket_id, db)
    await _require_ticket_actor(db, project, ticket, user.id, action="reopen")
    try:
        await ticket_transitions.reopen_ticket(
            db, project=project, ticket=ticket, actor_user_id=user.id
        )
    except TransitionRefused as exc:
        raise HTTPException(exc.status_code, exc.detail) from None
    jira_ids = await _jira_enqueue_status(db, project, ticket, user_id=user.id)
    await db.commit()
    jira_sync.schedule_drain(jira_ids)
    await db.refresh(ticket)
    deps = await _ticket_dependencies(db, ticket.id)
    return await _ticket_response(
        db, ticket, deps, show_email=await _user_sees_emails(db, user.id, project)
    )


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
    project = await _get_project_or_404(project_id, db, user.id)
    await check_project_feature(ctx, "agent_tickets", project, db)
    ticket = await _get_ticket_or_404(project_id, ticket_id, db)
    await _require_ticket_actor(db, project, ticket, user.id, action="execute")
    _assert_transition(ticket.status, "blocked", ticket.kind)

    ticket.status = "blocked"
    ticket.updated_at = datetime.now(timezone.utc)
    await _emit_status_notification(
        db,
        ticket=ticket,
        project=project,
        summary="Blocked",
        actor_user_id=user.id,
    )
    jira_ids = await _jira_enqueue_status(db, project, ticket, user_id=user.id)
    await db.commit()
    jira_sync.schedule_drain(jira_ids)
    await db.refresh(ticket)
    deps = await _ticket_dependencies(db, ticket.id)
    return await _ticket_response(
        db, ticket, deps, show_email=await _user_sees_emails(db, user.id, project)
    )


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
    project = await _get_project_or_404(project_id, db, user.id)
    await check_project_feature(ctx, "agent_tickets", project, db)
    ticket = await _get_ticket_or_404(project_id, ticket_id, db)
    await _require_ticket_actor(db, project, ticket, user.id, action="execute")
    _assert_transition(ticket.status, "in_progress", ticket.kind)

    ticket.status = "in_progress"
    ticket.updated_at = datetime.now(timezone.utc)
    await _emit_status_notification(
        db,
        ticket=ticket,
        project=project,
        summary="Unblocked",
        actor_user_id=user.id,
    )
    jira_ids = await _jira_enqueue_status(db, project, ticket, user_id=user.id)
    await db.commit()
    jira_sync.schedule_drain(jira_ids)
    await db.refresh(ticket)
    deps = await _ticket_dependencies(db, ticket.id)
    return await _ticket_response(
        db, ticket, deps, show_email=await _user_sees_emails(db, user.id, project)
    )


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
    project = await _get_project_or_404(project_id, db, user.id)
    await check_project_feature(ctx, "agent_tickets", project, db)
    ticket = await _get_ticket_or_404(project_id, ticket_id, db)
    await _require_ticket_actor(db, project, ticket, user.id, action="approve")
    _assert_transition(ticket.status, "open", ticket.kind)

    now = datetime.now(timezone.utc)
    ticket.status = "open"
    ticket.updated_at = now
    # Record who approved it: work-queue auto-adoption takes a ticket filed
    # by a reader only once an editor has approved it
    # (services.work_queues._materialize_auto_adopt).
    db.add(
        TicketEdit(
            id=f"te_{uuid.uuid4().hex[:16]}",
            ticket_id=ticket.id,
            edited_by_user_id=user.id,
            field_name="status",
            old_value=json.dumps("suggested"),
            new_value=json.dumps("open"),
            edited_at=now,
            lease_epoch=ticket.lease_epoch,
        )
    )
    await _emit_status_notification(
        db,
        ticket=ticket,
        project=project,
        summary="Approved — ready to start",
        actor_user_id=user.id,
        your_turn_assignee=True,
    )
    jira_ids = await _jira_enqueue_status(db, project, ticket, user_id=user.id)
    await db.commit()
    jira_sync.schedule_drain(jira_ids)
    await db.refresh(ticket)
    deps = await _ticket_dependencies(db, ticket.id)
    return await _ticket_response(
        db, ticket, deps, show_email=await _user_sees_emails(db, user.id, project)
    )


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
    """Move suggested/open → cancelled. Issues can also dismiss from
    in_progress. Both kinds use the Task FSM's 'cancelled' terminal
    state; the Issue FSM allows cancelled from open AND in_progress."""
    project = await _get_project_or_404(project_id, db, user.id)
    await check_project_feature(ctx, "agent_tickets", project, db)
    ticket = await _get_ticket_or_404(project_id, ticket_id, db)
    await _require_ticket_actor(db, project, ticket, user.id, action="dismiss")
    _assert_transition(ticket.status, "cancelled", ticket.kind)

    ticket.status = "cancelled"
    ticket.updated_at = datetime.now(timezone.utc)
    await _emit_status_notification(
        db,
        ticket=ticket,
        project=project,
        summary="Cancelled",
        actor_user_id=user.id,
    )
    jira_ids = await _jira_enqueue_status(db, project, ticket, user_id=user.id)
    await db.commit()
    jira_sync.schedule_drain(jira_ids)
    await db.refresh(ticket)
    deps = await _ticket_dependencies(db, ticket.id)
    return await _ticket_response(
        db, ticket, deps, show_email=await _user_sees_emails(db, user.id, project)
    )


@router.post(
    "/{project_id}/tickets/{ticket_id}/close",
    response_model=TicketResponse,
)
async def close_issue(
    project_id: str,
    ticket_id: str,
    user: User = Depends(get_current_user),
    ctx: UserContext = Depends(get_user_context),
    db: AsyncSession = Depends(get_db),
) -> TicketResponse:
    """Move an Issue from in_progress → closed.

    v0.10.24 tk_dbccde26ed604b3c — the Issue lifecycle terminator. Tasks
    finish via complete → accept; Issues finish via close. Status is
    manually closed by the project owner / admin (NOT auto-derived from
    child Task status), per the CEO direction on the parent ticket: an
    Issue can stay open even after all children done if the PM hasn't
    confirmed the user-facing problem is resolved.

    Codex R1 MED #2 (tk_dbccde26ed604b3c) — gate the close on the same
    actor signal as Issue creation. A non-PM executor that happens to
    have project access must not be able to prematurely close.
    """
    from sessionfs.server.auth.project_access import user_is_project_admin

    project = await _get_project_or_404(project_id, db, user.id)
    await check_project_feature(ctx, "agent_tickets", project, db)
    ticket = await _get_ticket_or_404(project_id, ticket_id, db)
    if ticket.kind != "issue":
        raise HTTPException(
            400,
            f"Cannot close ticket {ticket.id!r}: kind={ticket.kind!r}. "
            "The /close transition is for Issues only; Tasks finish via "
            "/complete + /accept.",
        )
    if not await user_is_project_admin(db, user.id, project):
        raise HTTPException(
            403,
            "Only the project owner or a project admin may close an "
            "Issue. Issue status is manually closed by the PM — "
            "executors cannot terminate the rollup.",
        )
    try:
        await ticket_transitions.close_issue(
            db, project=project, ticket=ticket, actor_user_id=user.id
        )
    except TransitionRefused as exc:
        raise HTTPException(exc.status_code, exc.detail) from None
    jira_ids = await _jira_enqueue_status(db, project, ticket, user_id=user.id)
    await db.commit()
    jira_sync.schedule_drain(jira_ids)
    await db.refresh(ticket)
    deps = await _ticket_dependencies(db, ticket.id)
    return await _ticket_response(
        db, ticket, deps, show_email=await _user_sees_emails(db, user.id, project)
    )


# ── Comments ──


@router.get(
    "/{project_id}/tickets/{ticket_id}/comments",
    response_model=list[CommentResponse],
)
async def list_ticket_comments(
    project_id: str,
    ticket_id: str,
    since: datetime | None = Query(
        None,
        description=(
            "Cursor timestamp from the last seen comment. Pair with "
            "since_id when two comments share a created_at — the server "
            "uses (created_at > since) OR (created_at = since AND id > since_id) "
            "so no same-timestamp comments are skipped."
        ),
    ),
    since_id: str | None = Query(
        None,
        description=(
            "Cursor tiebreaker. Pass the id of the last seen comment "
            "together with `since` to handle same-timestamp ties safely. "
            "Without since_id, agents may skip same-millisecond comments."
        ),
    ),
    limit: int = Query(200, ge=1, le=500, description="Max comments to return (1-500)."),
    auth: AuthContext = Depends(require_scope("tickets:read")),
    ctx: UserContext = Depends(get_user_context),
    db: AsyncSession = Depends(get_db),
) -> list[CommentResponse]:
    """List ticket comments in chronological (oldest-first) order.

    v0.10.10 (tk_32f3dacf1c9749bc) — added `since` + `since_id` + `limit`
    for incremental polling. Agents tracking a review thread pass the
    timestamp + id of the last comment they processed; only strictly
    newer comments come back. Ordering is stable by (created_at, id)
    so the cursor advances monotonically even when two comments share
    a created_at (Codex review #1 fix — without the id tiebreaker, a
    same-timestamp poll could permanently skip one of two siblings).
    """
    project = await _get_project_for_auth(project_id, db, auth)
    from sessionfs.server.auth.dependencies import (
        assert_service_key_can_access_project,
    )
    await assert_service_key_can_access_project(db, auth, project)
    await check_project_feature(ctx, "agent_tickets", project, db)
    await _get_ticket_or_404(project_id, ticket_id, db)  # validates ticket
    query = (
        select(TicketComment)
        .where(TicketComment.ticket_id == ticket_id)
        .order_by(TicketComment.created_at, TicketComment.id)
        .limit(limit)
    )
    if since is not None:
        # Normalize to UTC so naive `since` values still compare cleanly
        # against the timezone-aware DB column.
        if since.tzinfo is None:
            since = since.replace(tzinfo=timezone.utc)
        if since_id:
            # Lexicographic tuple comparison: (created_at, id) > (since, since_id)
            # written explicitly so SQLite + Postgres both honor the
            # tiebreaker without needing row-value support.
            query = query.where(
                or_(
                    TicketComment.created_at > since,
                    and_(
                        TicketComment.created_at == since,
                        TicketComment.id > since_id,
                    ),
                )
            )
        else:
            query = query.where(TicketComment.created_at > since)
    rows = (await db.execute(query)).scalars().all()
    return [
        CommentResponse(
            id=c.id,
            ticket_id=c.ticket_id,
            author_user_id=c.author_user_id,
            author_persona=c.author_persona,
            content=c.content,
            session_id=c.session_id,
            created_at=c.created_at,
            source=c.source,
        )
        for c in rows
    ]


async def is_registered_trusted_reviewer(
    db: AsyncSession,
    *,
    project_id: str,
    org_id: str | None,
    user_id: str,
    service_key_id: str | None,
    claimed_persona: str | None,
) -> bool:
    """tk_d42170b4670f4448 — decide, from server-derived identity, whether
    this comment is an authoritative review verdict.

    True iff an ACTIVE trusted_reviewers row exists that (a) is scoped to
    this comment's project (project_id match) OR org-wide for the project's
    org, (b) matches the credential actually in use, and (c) is registered
    for the EXACT claimed_persona.

    Trust is decided from AuthContext (user_id / service_key_id), never from
    the request body. Identity isolation is strict:
      - a SERVICE-KEY request matches ONLY a service_key_id row (a service
        key "running as" a user must not inherit a human's reviewer trust);
      - a USER-KEY request matches ONLY a user_id row whose service_key_id
        IS NULL.
    """
    if not claimed_persona:
        return False

    scope_clause = or_(
        TrustedReviewer.project_id == project_id,
        and_(
            TrustedReviewer.project_id.is_(None),
            TrustedReviewer.org_id == org_id,
        )
        if org_id is not None
        else sa_false(),
    )

    if service_key_id is not None:
        identity_clause = TrustedReviewer.service_key_id == service_key_id
    else:
        identity_clause = and_(
            TrustedReviewer.user_id == user_id,
            TrustedReviewer.service_key_id.is_(None),
        )

    stmt = (
        select(TrustedReviewer.id)
        .where(
            TrustedReviewer.is_active.is_(True),
            TrustedReviewer.revoked_at.is_(None),
            TrustedReviewer.reviewer_persona == claimed_persona,
            scope_clause,
            identity_clause,
        )
        .limit(1)
    )
    return (await db.execute(stmt)).scalar_one_or_none() is not None


@router.post(
    "/{project_id}/tickets/{ticket_id}/comments",
    response_model=CommentResponse,
    status_code=201,
)
async def create_ticket_comment(
    project_id: str,
    ticket_id: str,
    body: CommentCreate,
    auth: AuthContext = Depends(require_scope("tickets:write")),
    ctx: UserContext = Depends(get_user_context),
    db: AsyncSession = Depends(get_db),
) -> CommentResponse:
    user = auth.user
    project = await _get_project_for_auth(project_id, db, auth)
    from sessionfs.server.auth.dependencies import (
        assert_service_key_can_access_project,
    )
    await assert_service_key_can_access_project(db, auth, project)
    await check_project_feature(ctx, "agent_tickets", project, db)

    # tk_d42170b4670f4448 — server-stamp the verdict trust decision from
    # the authenticated identity against the trusted_reviewers registry.
    # NEVER read trust from the request body. author_persona is still
    # stored (display/back-compat) but no longer carries verdict authority.
    verdict_trusted = await is_registered_trusted_reviewer(
        db,
        project_id=project.id,
        org_id=project.org_id,
        user_id=user.id,
        service_key_id=auth.service_key_id,
        claimed_persona=body.author_persona,
    )

    # Two-way sync: only an editor (or a service key, already inside its org
    # / project allowlist) drives outbound Jira comments. A read/contribute
    # caller's `send_to_jira` flag is ignored.
    from sessionfs.server.auth.project_access import user_can_edit_project

    can_drive_jira = auth.key_kind == "service" or await user_can_edit_project(
        db, user.id, project
    )
    send_to_jira = body.send_to_jira and can_drive_jira

    # The org's lease-epoch requirement, the fenced insert and watcher
    # notifications — shared with helper confirm (services/ticket_writes.py).
    jira_ids: list[str] = []
    try:
        comment_id = await insert_ticket_comment(
            db,
            project=project,
            ticket_id=ticket_id,
            user_id=user.id,
            content=body.content,
            author_persona=body.author_persona,
            session_id=body.session_id,
            lease_epoch=body.lease_epoch,
            verdict_trusted=verdict_trusted,
            auth=auth,
        )
        if send_to_jira and not await jira_sync.comment_forwarding_enabled(db, ticket_id):
            # An explicit "send to Jira" the org's sync settings won't honour
            # is refused rather than silently dropped, so the author knows the
            # comment stayed in SessionFS. Nothing is written (rolled back).
            raise HTTPException(
                409,
                {
                    "error": "jira_comment_sync_off",
                    "message": (
                        "This ticket is not linked to a Jira issue, or the org's Jira "
                        "comment sync is not set to outbound/both. Post without "
                        "'send to Jira', or ask an org admin to enable outbound "
                        "comment sync."
                    ),
                },
            )
        # Outbound Jira comment (two-way sync): an explicit editor "send to
        # Jira", or a trusted reviewer's VERIFIED-CLEAN verdict when the org
        # mirrors comments outbound. A trusted CHANGES-REQUESTED verdict or
        # plain comment is never forwarded. The outbox row is written in this
        # transaction and drained after commit (the sweep retries a lost or
        # failed delivery).
        if can_drive_jira:
            from sessionfs.server.services.review_state import VERDICT_CLEAN

            verdict = (
                ticket_watchers.parse_verdict(body.content) if verdict_trusted else None
            )
            if send_to_jira or verdict == VERDICT_CLEAN:
                ticket = await _get_ticket_or_404(project_id, ticket_id, db)
                jira_ids = await jira_sync.enqueue_comment(
                    db,
                    ticket,
                    comment_id,
                    reason="send_to_jira" if send_to_jira else "verified_clean",
                )
    except HTTPException:
        await db.rollback()
        raise
    await db.commit()
    jira_sync.schedule_drain(jira_ids)
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
        source=comment.source,
    )


# ── Ticket watching (follow / unfollow / mute / watchers) ──


class WatchRequest(BaseModel):
    muted: bool = False


class WatcherResponse(BaseModel):
    user_id: str
    persona_name: str | None
    source: str
    muted: bool
    created_at: datetime


async def _get_watch_project_or_404(
    project_id: str, db: AsyncSession, user_id: str
) -> Project:
    """Load a project for the watch routes, raising 404 (never 403) when
    the user cannot access it, so a cross-org request can't learn that the
    project (or its tickets) exists."""
    from sessionfs.server.auth.project_access import user_can_access_project

    project = (
        await db.execute(select(Project).where(Project.id == project_id))
    ).scalar_one_or_none()
    if project is None:
        raise HTTPException(404, "Project not found")
    if not await user_can_access_project(db, user_id, project):
        raise HTTPException(404, "Project not found")
    return project


@router.put(
    "/{project_id}/tickets/{ticket_id}/watch",
    response_model=WatcherResponse,
)
async def watch_ticket(
    project_id: str,
    ticket_id: str,
    body: WatchRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> WatcherResponse:
    """Follow a ticket (optionally muted). Idempotent: a second PUT
    updates the mute flag without duplicating the follow."""
    await _get_watch_project_or_404(project_id, db, user.id)
    await _get_ticket_or_404(project_id, ticket_id, db)

    await ticket_watchers.subscribe(
        db, ticket_id=ticket_id, user_id=user.id, source="explicit"
    )
    await ticket_watchers.set_muted(db, ticket_id=ticket_id, user_id=user.id, muted=body.muted)
    await db.commit()

    row = (
        await db.execute(
            select(TicketWatcher).where(
                TicketWatcher.ticket_id == ticket_id,
                TicketWatcher.user_id == user.id,
            )
        )
    ).scalar_one()
    return WatcherResponse(
        user_id=row.user_id,
        persona_name=row.persona_name,
        source=row.source,
        muted=row.muted,
        created_at=row.created_at,
    )


@router.delete(
    "/{project_id}/tickets/{ticket_id}/watch",
    status_code=204,
)
async def unwatch_ticket(
    project_id: str,
    ticket_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Response:
    """Stop following a ticket. Idempotent: unfollowing an unfollowed
    ticket is a no-op."""
    await _get_watch_project_or_404(project_id, db, user.id)
    await _get_ticket_or_404(project_id, ticket_id, db)
    await ticket_watchers.unsubscribe(db, ticket_id=ticket_id, user_id=user.id)
    await db.commit()
    return Response(status_code=204)


@router.get(
    "/{project_id}/tickets/{ticket_id}/watchers",
    response_model=list[WatcherResponse],
)
async def list_ticket_watchers(
    project_id: str,
    ticket_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[WatcherResponse]:
    """List a ticket's watchers (project members only)."""
    await _get_watch_project_or_404(project_id, db, user.id)
    await _get_ticket_or_404(project_id, ticket_id, db)
    rows = (
        await db.execute(
            select(TicketWatcher).where(TicketWatcher.ticket_id == ticket_id)
        )
    ).scalars().all()
    return [
        WatcherResponse(
            user_id=r.user_id,
            persona_name=r.persona_name,
            source=r.source,
            muted=r.muted,
            created_at=r.created_at,
        )
        for r in rows
    ]


# ── Ticket by id (no project in the URL) ──


by_id_router = APIRouter(prefix="/api/v1/tickets", tags=["tickets"])

_RECENT_COMMENTS = 3


class TicketPreviewResponse(TicketResponse):
    """The project-scoped detail payload plus what a link preview needs:
    the project's name and the latest comments (newest first)."""

    project_name: str
    comment_count: int
    recent_comments: list[CommentResponse]


@by_id_router.get("/{ticket_id}", response_model=TicketPreviewResponse)
async def get_ticket_by_id(
    ticket_id: str,
    auth: AuthContext = Depends(require_scope("tickets:read")),
    ctx: UserContext = Depends(get_user_context),
    db: AsyncSession = Depends(get_db),
) -> TicketPreviewResponse:
    """Look a ticket up by its id alone, for links that only carry the id
    (e.g. ``tk_…`` mentioned in a session-helper answer).

    Authorization is the project-scoped GET's, run against the ticket's own
    project. A ticket that doesn't exist and one in a project the caller can't
    read get the same 404, so the id space can't be probed. The plan gate runs
    only once the caller is known to have access, as on the project route.
    """
    from sessionfs.server.auth.dependencies import (
        assert_service_key_can_access_project,
    )

    not_found = HTTPException(404, "Ticket not found")
    ticket = (
        await db.execute(select(Ticket).where(Ticket.id == ticket_id))
    ).scalar_one_or_none()
    if ticket is None:
        raise not_found
    try:
        project = await _get_project_for_auth(ticket.project_id, db, auth)
        await assert_service_key_can_access_project(db, auth, project)
    except HTTPException as exc:
        if exc.status_code in (403, 404):
            raise not_found from None
        raise
    await check_project_feature(ctx, "agent_tickets", project, db)

    detail = await _ticket_detail_response(
        ticket, db, show_email=await _viewer_sees_emails(db, auth, project)
    )
    comment_count = (
        await db.execute(
            select(func.count())
            .select_from(TicketComment)
            .where(TicketComment.ticket_id == ticket.id)
        )
    ).scalar_one()
    recent = (
        await db.execute(
            select(TicketComment)
            .where(TicketComment.ticket_id == ticket.id)
            .order_by(TicketComment.created_at.desc(), TicketComment.id.desc())
            .limit(_RECENT_COMMENTS)
        )
    ).scalars().all()
    return TicketPreviewResponse(
        **detail.model_dump(),
        project_name=project.name,
        comment_count=int(comment_count or 0),
        recent_comments=[
            CommentResponse(
                id=c.id,
                ticket_id=c.ticket_id,
                author_user_id=c.author_user_id,
                author_persona=c.author_persona,
                content=c.content,
                session_id=c.session_id,
                created_at=c.created_at,
                source=c.source,
            )
            for c in recent
        ],
    )
