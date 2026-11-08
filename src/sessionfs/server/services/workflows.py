"""Record-only multi-agent workflow templates (GitHub #120, phase W1).

SessionFS never starts an agent or calls a model for a workflow. A template
is a small DAG of steps; starting one creates a review loop (the shared
``create_loop_records`` path) with ``loop_config.workflow`` pinned to the
template version and one child ticket per step slot (``fan_out`` slots per
step). The caller's own orchestrator asks for the plan, runs the ready steps
and reports them through ``start_worker_run`` / ``finish_worker_run`` /
``record_review_round`` with a ``step_id``, which is checked against the
template here.

Plan semantics (computed from stored rows on every call):

- A child of a worker step (implementer, documenter, dispatcher, verifier)
  is done when its ticket is ``done``, its loop queue item is ``done``, or
  its latest run reported for that step passed.
- A reviewer step reviews the children of the steps it depends on (its
  "subjects"); a dependency that is itself a reviewer step contributes its
  own subjects, so a chain of reviewers all review the same work. A reviewer
  step with no dependencies reviews its own children. A subject passes a
  reviewer step only when the step's OWN latest code round on it (a round
  recorded with this ``step_id``, or an untagged round by the step's agent)
  is a trusted strict VERIFIED-CLEAN at the subject's latest head from an
  actor independent of every implementer (the round's recorded user / key,
  same identity rules as merge readiness, failing closed), and the
  subject has been closed by the loop (queue item ``done``: an independent
  trusted clean) or its ticket is ``done``. Another reviewer step's verdict
  never counts.
- The gate is the step's exit gate, checked over the same subjects
  (``trusted_clean`` = merge readiness at the subject's latest head, only
  allowed on reviewer/verifier steps; ``approval_policy:<id>`` = the
  approvals engine: an approved request for the subject at its current head
  sha; a missing, foreign or deactivated policy never passes). A
  step whose work is finished but whose gate has not passed is not done, so
  nothing downstream becomes ready.
- A step is ``ready`` when every dependency is done and nothing has been
  reported for it yet, ``running`` once a report or ticket start exists,
  ``needs_human`` when one of its items is parked, ``blocked`` otherwise.

Design: docs/design/followons-2026-11.md §5.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import timezone
from typing import Any, Literal

from sqlalchemy import bindparam, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server.db.models import (
    AgentRun,
    ApprovalPolicy,
    Project,
    Ticket,
    TicketComment,
    WorkflowStepChild,
    WorkflowTemplate,
    WorkQueueItem,
)
from sessionfs.server.services import approvals
from sessionfs.workflow_definition import (
    MAX_DEFINITION_BYTES,
    ROUND_ROLES,
    WORKER_ROLES,
    StepDefinition,
    WorkflowDefinition,
    WorkflowDefinitionError,
    clean_multiline,
    dump_definition,
    parse_definition,
)

_PLACEHOLDER_RE = re.compile(r"\{(ticket\.title|ticket\.description|checklist)\}")

_WORKER_RUN_ROLES = ("implementer", "ui-implementer")
_REVIEW_RUN_ROLES = ("reviewer", "design-reviewer")
# Merge-readiness checks one plan may run (trusted_clean gates). A template
# has at most 30 steps x 10 slots; past this the gate reports why instead of
# running an unbounded number of queries.
MAX_GATE_TICKETS = 100
# Trusted verdicts per ticket a trusted_clean gate evaluates; past this the
# gate fails closed instead of judging a partial history.
MAX_VERDICT_HISTORY = 500
# IN-list chunk size for batched lookups.
_IN_CHUNK = 500


def _chunks(values: list[str], size: int = _IN_CHUNK) -> list[list[str]]:
    return [values[i : i + size] for i in range(0, len(values), size)] if values else []


# ── Brief rendering (data only) ──


def render_brief(
    template: str,
    *,
    ticket_title: str,
    ticket_description: str,
    checklist_items: list[dict[str, str]],
) -> str:
    """Fill the three supported placeholders in one pass.

    Values are inserted as text: the substitution never re-scans what it
    inserted, and no other syntax is interpreted. The project checklist is
    appended as "Known traps" when the template does not place it.
    """
    checklist = "\n".join(
        f"- **{item['title']}** — {item['check']}" for item in checklist_items
    )
    values = {
        "ticket.title": ticket_title,
        "ticket.description": ticket_description,
        "checklist": checklist,
    }
    body = template if template.strip() else "{ticket.description}"
    placed_checklist = "{checklist}" in body
    rendered = _PLACEHOLDER_RE.sub(lambda m: values[m.group(1)], body).strip()
    if checklist_items and not placed_checklist:
        rendered = (rendered + "\n\n" if rendered else "") + "## Known traps\n" + checklist
    return rendered


def step_has_own_tickets(step: StepDefinition) -> bool:
    """A reviewer step with dependencies reviews those steps' tickets and
    gets no tickets of its own; every other step gets ``fan_out`` tickets."""
    return not (step.role == "reviewer" and step.depends_on)


# ── Approval policies ──


async def _policy_row(db: AsyncSession, policy_id: str) -> ApprovalPolicy | None:
    return await db.get(ApprovalPolicy, policy_id)


async def _policy_live(db: AsyncSession, policy: ApprovalPolicy) -> bool:
    """A policy version is usable while it, or a later version of its
    lineage, is active. A deactivated lineage (no active version) never
    passes a gate."""
    if policy.active:
        return True
    active = (
        await db.execute(
            select(ApprovalPolicy.id)
            .where(
                ApprovalPolicy.lineage_id == policy.lineage_id,
                ApprovalPolicy.org_id == policy.org_id,
                ApprovalPolicy.active.is_(True),
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    return active is not None


def approval_policy_ids(definition: WorkflowDefinition) -> list[str]:
    return sorted(
        {
            s.gate.split(":", 1)[1]
            for s in definition.steps
            if s.gate.startswith("approval_policy:")
        }
    )


async def validate_policy_scope(
    db: AsyncSession, definition: WorkflowDefinition, *, org_id: str | None
) -> None:
    """Refuse an ``approval_policy:<id>`` gate that cannot belong to the
    template's scope (approval policies are org-scoped).

    Raises ``WorkflowDefinitionError``.
    """
    for policy_id in approval_policy_ids(definition):
        policy = await _policy_row(db, policy_id)
        if policy is None or not org_id or policy.org_id != org_id:
            raise WorkflowDefinitionError(
                f"approval policy {policy_id!r} is not available to this "
                "template's organization"
            )


# ── Start ──


class WorkflowAgentUnknown(Exception):
    def __init__(self, agent: str) -> None:
        super().__init__(agent)
        self.agent = agent


async def start_workflow(
    db: AsyncSession,
    *,
    project: Any,
    auth: Any,
    template: WorkflowTemplate,
    definition: WorkflowDefinition,
    from_ticket_id: str | None,
    title: str,
    description: str,
    reviewer_persona: str | None,
    loop_extra: dict[str, Any],
    checklist_items: list[dict[str, str]],
) -> tuple[Ticket, list[tuple[str, int, Ticket]], Any]:
    """Create the workflow loop: parent, one child per step slot, the queue
    and the slot rows, in the caller's transaction (no commit).

    The caller has authorized the write and holds the project scope lock.
    Every step's ``agent`` must resolve to an active persona visible from the
    project (project scope, then the caller-visible org scope); otherwise
    ``WorkflowAgentUnknown``. Returns (parent, [(step_id, index, ticket)],
    queue). The resolved persona per step is pinned in ``loop_config`` so
    review rounds for a step are attributed to (and trusted for) that agent.
    """
    import uuid

    from sessionfs.server.routes.loops import LoopChildSpec, create_loop_records
    from sessionfs.server.services.personas import (
        resolve_persona,
        visible_persona_org_id_for_auth,
    )

    org_id = await visible_persona_org_id_for_auth(db, project, auth)
    agents: dict[str, str] = {}
    for step in definition.steps:
        if step.agent in agents:
            continue
        persona = await resolve_persona(db, project.id, step.agent, org_id=org_id)
        if persona is None:
            raise WorkflowAgentUnknown(step.agent)
        agents[step.agent] = persona.name

    if reviewer_persona is None:
        for role in ("reviewer", "verifier"):
            match = next((s for s in definition.steps if s.role == role), None)
            if match is not None:
                reviewer_persona = agents[match.agent]
                break
    slots: list[tuple[str, int]] = []

    def _children(parent: Ticket) -> list[LoopChildSpec]:
        specs: list[LoopChildSpec] = []
        for step in _topological(definition):
            if not step_has_own_tickets(step):
                continue
            brief = clean_multiline(
                render_brief(
                    step.brief_template,
                    ticket_title=parent.title or "",
                    ticket_description=parent.description or "",
                    checklist_items=checklist_items,
                )
            )
            for index in range(step.fan_out):
                suffix = f" ({index + 1}/{step.fan_out})" if step.fan_out > 1 else ""
                specs.append(
                    LoopChildSpec(
                        title=(step.title + suffix)[:200],
                        brief_markdown=brief,
                        assigned_to=agents[step.agent],
                    )
                )
                slots.append((step.id, index))
        return specs

    pin = {
        "template_id": template.id,
        "lineage_id": template.lineage_id,
        "version": template.version,
        "agents": {s.id: agents[s.agent] for s in definition.steps},
    }
    parent, children, queue = await create_loop_records(
        db,
        project_id=project.id,
        auth=auth,
        from_ticket_id=from_ticket_id,
        title=title,
        description=description,
        reviewer_persona=reviewer_persona or "reviewer",
        loop_extra={**loop_extra, "workflow": pin},
        build_children=_children,
    )
    placed: list[tuple[str, int, Ticket]] = []
    for (step_id, index), child in zip(slots, children, strict=True):
        db.add(
            WorkflowStepChild(
                id=f"wsc_{uuid.uuid4().hex[:16]}",
                project_id=project.id,
                loop_ticket_id=parent.id,
                ticket_id=child.id,
                step_id=step_id,
                step_index=index,
            )
        )
        placed.append((step_id, index, child))
    await db.flush()
    return parent, placed, queue


# ── Loading ──


def workflow_pin(loop_config: dict[str, Any]) -> dict[str, Any] | None:
    pin = loop_config.get("workflow")
    if isinstance(pin, dict) and isinstance(pin.get("template_id"), str):
        return pin
    return None


def step_agent(loop_config: dict[str, Any], step: StepDefinition) -> str:
    """The persona pinned for ``step`` at start (falls back to the
    template's agent name for loops started before agents were pinned)."""
    pin = workflow_pin(loop_config) or {}
    agents = pin.get("agents")
    if isinstance(agents, dict):
        name = agents.get(step.id)
        if isinstance(name, str) and name:
            return name
    return step.agent


async def load_pinned_definition(
    db: AsyncSession, pin: dict[str, Any]
) -> tuple[WorkflowTemplate, WorkflowDefinition] | None:
    row = await db.get(WorkflowTemplate, pin["template_id"])
    if row is None:
        return None
    try:
        return row, parse_definition(row.definition)
    except WorkflowDefinitionError:
        return None


# ── Plan ──


@dataclass(frozen=True)
class _Run:
    """The AgentRun columns the plan reads (never the whole row)."""

    id: str
    ticket_id: str | None
    role: str | None
    status: str
    head_sha: str | None
    workflow_step_id: str | None
    persona_name: str | None
    trigger_ref: str | None
    # The round's recorded actor (who authenticated the report).
    triggered_by_user_id: str | None = None
    service_key_id: str | None = None
    # (created_at, id) for newest-first ordering across partitions.
    order: tuple[Any, str] = (None, "")


def _latest_runs_stmt(project_id: str, ticket_ids: list[str]) -> Any:
    """Per ticket, only the newest run of each kind the plan reads, bounded
    in SQL: one row per (ticket, role, workflow step, persona when the run
    is untagged, has-head). That keeps the latest worker result and the
    latest commit per step, and the latest round per reviewer step or
    agent, without loading a ticket's whole run history."""
    from sqlalchemy import case, literal

    persona_key = case(
        (AgentRun.workflow_step_id.is_(None), func.lower(AgentRun.persona_name)),
        else_=literal(""),
    )
    ranked = (
        select(
            AgentRun.id.label("id"),
            AgentRun.ticket_id.label("ticket_id"),
            AgentRun.role.label("role"),
            AgentRun.status.label("status"),
            AgentRun.head_sha.label("head_sha"),
            AgentRun.workflow_step_id.label("workflow_step_id"),
            AgentRun.persona_name.label("persona_name"),
            AgentRun.trigger_ref.label("trigger_ref"),
            AgentRun.triggered_by_user_id.label("triggered_by_user_id"),
            AgentRun.service_key_id.label("service_key_id"),
            AgentRun.created_at.label("created_at"),
            func.row_number()
            .over(
                partition_by=(
                    AgentRun.ticket_id,
                    AgentRun.role,
                    func.coalesce(AgentRun.workflow_step_id, ""),
                    persona_key,
                    AgentRun.head_sha.is_(None),
                ),
                order_by=(AgentRun.created_at.desc(), AgentRun.id.desc()),
            )
            .label("rn"),
        )
        .where(AgentRun.project_id == project_id, AgentRun.ticket_id.in_(ticket_ids))
        .subquery()
    )
    return select(
        ranked.c.id,
        ranked.c.ticket_id,
        ranked.c.role,
        ranked.c.status,
        ranked.c.head_sha,
        ranked.c.workflow_step_id,
        ranked.c.persona_name,
        ranked.c.trigger_ref,
        ranked.c.triggered_by_user_id,
        ranked.c.service_key_id,
        ranked.c.created_at,
    ).where(ranked.c.rn == 1)


def _run_sort_key(run: _Run) -> tuple[bool, float, str]:
    when, rid = run.order
    if when is None:
        return (False, 0.0, rid)
    # SQLite hands back naive UTC values; never let the host timezone
    # (and its DST shifts) reorder runs.
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return (True, when.timestamp(), rid)


def _run_from_row(row: Any) -> _Run:
    return _Run(
        id=row.id,
        ticket_id=row.ticket_id,
        role=row.role,
        status=row.status,
        head_sha=row.head_sha,
        workflow_step_id=row.workflow_step_id,
        persona_name=row.persona_name,
        trigger_ref=row.trigger_ref,
        triggered_by_user_id=row.triggered_by_user_id,
        service_key_id=row.service_key_id,
        order=(row.created_at, row.id),
    )


@dataclass
class _Child:
    ticket: Ticket
    index: int
    item: WorkQueueItem | None
    runs: list[_Run] = field(default_factory=list)  # newest first


@dataclass
class StepState:
    step: StepDefinition
    state: str
    reason: str | None
    done: bool
    work_done: bool
    children: list[_Child]
    subjects: list[_Child]
    ready_children: list[_Child]
    gate_passed: bool
    gate_reason: str | None
    # Reviewer steps with dependencies: the step's own rendered brief for its
    # ready subjects (None = use each subject ticket's brief).
    brief: str | None = None


@dataclass
class Plan:
    template: WorkflowTemplate
    definition: WorkflowDefinition
    steps: dict[str, StepState]
    children_by_ticket: dict[str, tuple[str, _Child]]


def _latest_head(child: _Child) -> str | None:
    for run in child.runs:
        if run.role in _WORKER_RUN_ROLES and run.head_sha:
            return run.head_sha
    # A reviewer step's own children have no worker runs: the commit the
    # reviewer last read is the head under review.
    for run in child.runs:
        if run.role in _REVIEW_RUN_ROLES and run.head_sha:
            return run.head_sha
    return None


class _GateContext:
    """Per-plan state shared by every gate check: readiness results cached
    by (ticket, head), approval-gate results cached by (policy, ticket), one
    cap on the checks of both kinds, and the scope org."""

    def __init__(self, db: AsyncSession, project_id: str) -> None:
        self.db = db
        self.project_id = project_id
        self.readiness: dict[tuple[str, str], tuple[bool, str | None]] = {}
        self.approvals: dict[tuple[str, str], tuple[bool, str | None]] = {}
        self.budget = MAX_GATE_TICKETS
        self._org_loaded = False
        self._org_id: str | None = None

    async def org_id(self) -> str | None:
        if not self._org_loaded:
            self._org_id = (
                await self.db.execute(
                    select(Project.org_id).where(Project.id == self.project_id)
                )
            ).scalar_one_or_none()
            self._org_loaded = True
        return self._org_id


async def _batch_readiness(
    ctx: _GateContext, pairs: list[tuple[Ticket, str]]
) -> None:
    """Merge readiness for many (ticket, head) pairs in a fixed number of
    queries (comments, implementer identities, key minters), into the cache.
    Mirrors ``delivery_ingest._ticket_merge_readiness``."""
    from sessionfs.server.services.review_state import (
        compute_merge_readiness,
        load_implementer_identities,
        load_key_minters,
    )

    todo = [(t, h) for t, h in pairs if (t.id, h) not in ctx.readiness]
    if not todo:
        return
    tickets = {t.id: t for t, _h in todo}
    # Only trusted verdicts contribute to review state (untrusted comments
    # are display-only), so read the NEWEST trusted verdicts per ticket,
    # bounded in SQL. One more than the cap tells us the history did not
    # fit: that ticket fails closed rather than being judged on a slice.
    ranked = (
        select(
            TicketComment.id.label("id"),
            TicketComment.ticket_id.label("ticket_id"),
            TicketComment.author_persona.label("author_persona"),
            TicketComment.content.label("content"),
            TicketComment.created_at.label("created_at"),
            TicketComment.verdict_trusted.label("verdict_trusted"),
            TicketComment.service_key_id.label("service_key_id"),
            TicketComment.author_user_id.label("author_user_id"),
            TicketComment.reviewed_commit.label("reviewed_commit"),
            func.row_number()
            .over(
                partition_by=TicketComment.ticket_id,
                order_by=(TicketComment.created_at.desc(), TicketComment.id.desc()),
            )
            .label("rn"),
        )
        .where(
            TicketComment.ticket_id.in_(bindparam("tids", expanding=True)),
            TicketComment.verdict_trusted.is_(True),
        )
        .subquery()
    )
    stmt = select(ranked).where(ranked.c.rn <= MAX_VERDICT_HISTORY + 1)
    comments: dict[str, list[dict[str, Any]]] = {tid: [] for tid in tickets}
    for chunk in _chunks(list(tickets)):
        for r in (await ctx.db.execute(stmt, {"tids": chunk})).all():
            comments[r.ticket_id].append(
                {
                    "id": r.id,
                    "author_persona": r.author_persona,
                    "content": r.content,
                    "created_at": r.created_at,
                    "verdict_trusted": r.verdict_trusted,
                    "service_key_id": r.service_key_id,
                    "author_user_id": r.author_user_id,
                    "reviewed_commit": r.reviewed_commit,
                    "_rn": r.rn,
                }
            )
    overflow: set[str] = set()
    for tid, bucket in comments.items():
        if len(bucket) > MAX_VERDICT_HISTORY:
            overflow.add(tid)
        # Chronological, as compute_merge_readiness expects.
        bucket.sort(key=lambda c: -c.pop("_rn"))
    identities = await load_implementer_identities(ctx.db, list(tickets.values()))
    all_comments = [c for bucket in comments.values() for c in bucket]
    all_identities = [i for tid in tickets for i in identities.get(tid, [])]
    minters = await load_key_minters(ctx.db, all_comments, all_identities)
    for ticket, head in todo:
        if ticket.id in overflow:
            ctx.readiness[(ticket.id, head)] = (
                False,
                f"{ticket.id} has more than {MAX_VERDICT_HISTORY} trusted verdicts; "
                "its review history is too long to evaluate completely",
            )
            continue
        readiness = compute_merge_readiness(
            comments=comments[ticket.id],
            sha=head,
            implementer_identities=identities.get(ticket.id, []),
            key_minters=minters,
        )
        if readiness.get("ready"):
            ctx.readiness[(ticket.id, head)] = (True, None)
        else:
            reasons = readiness.get("reasons") or []
            ctx.readiness[(ticket.id, head)] = (
                False,
                f"{ticket.id} is not merge-ready at {head[:12]}: "
                + "; ".join(str(r) for r in reasons[:3]),
            )


async def _trusted_clean_gate(
    ctx: _GateContext, subjects: list[_Child]
) -> tuple[bool, str | None]:
    pairs: list[tuple[Ticket, str]] = []
    for child in subjects:
        head = _latest_head(child)
        if head is None:
            return False, f"{child.ticket.id} has no finished run with a head commit"
        pairs.append((child.ticket, head))
    new = len({(t.id, h) for t, h in pairs if (t.id, h) not in ctx.readiness})
    if new > ctx.budget:
        return False, (
            f"too many tickets to check in one plan (at most {MAX_GATE_TICKETS})"
        )
    ctx.budget -= new
    await _batch_readiness(ctx, pairs)
    for ticket, head in pairs:
        ok, why = ctx.readiness[(ticket.id, head)]
        if not ok:
            return False, why
    return True, None


async def _approval_gate(
    ctx: _GateContext, policy_id: str, child: _Child
) -> tuple[bool, str | None]:
    """``services.approvals.workflow_gate_passed`` decides: passed only with
    an approved request for this ticket at its current head sha.

    The policy must exist, belong to the ticket's project org and still be
    live (its lineage has an active version); anything else fails closed.
    Any error is "not passed", inside a savepoint so the plan's transaction
    stays usable. Results are cached per plan by (policy, ticket): the head
    the gate reads is derived from the same rows for the whole plan. Each new
    pair spends one unit of the plan's gate budget; past it the gate fails
    closed.
    """
    key = (policy_id, child.ticket.id)
    cached = ctx.approvals.get(key)
    if cached is not None:
        return cached
    if ctx.budget < 1:
        return False, (
            f"too many tickets to check in one plan (at most {MAX_GATE_TICKETS})"
        )
    ctx.budget -= 1
    result = await _approval_gate_uncached(ctx, policy_id, child)
    ctx.approvals[key] = result
    return result


async def _approval_gate_uncached(
    ctx: _GateContext, policy_id: str, child: _Child
) -> tuple[bool, str | None]:
    db = ctx.db
    org_id = await ctx.org_id()
    try:
        async with db.begin_nested():
            policy = await _policy_row(db, policy_id)
            if policy is None or not org_id or policy.org_id != org_id:
                return False, f"approval policy {policy_id!r} is not available here"
            if not await _policy_live(db, policy):
                return False, f"approval policy {policy_id!r} is no longer active"
            passed, reason = await approvals.workflow_gate_passed(
                db, policy_id=policy_id, ticket=child.ticket
            )
    except Exception:
        return False, "approval gate could not be evaluated"
    return bool(passed), reason


async def _gate(
    ctx: _GateContext, step: StepDefinition, subjects: list[_Child]
) -> tuple[bool, str | None]:
    if step.gate == "none":
        return True, None
    if not subjects:
        return False, "gate has nothing to check"
    if step.gate == "trusted_clean":
        return await _trusted_clean_gate(ctx, subjects)
    for child in subjects:
        ok, why = await _approval_gate(ctx, step.gate.split(":", 1)[1], child)
        if not ok:
            return False, why
    return True, None


def _step_runs(child: _Child, step_id: str) -> list[_Run]:
    return [r for r in child.runs if r.workflow_step_id == step_id]


def _worker_child_done(child: _Child, step_id: str) -> bool:
    if child.ticket.status == "done":
        return True
    if child.item is not None and child.item.item_status == "done":
        return True
    for run in _step_runs(child, step_id):
        if run.role in _WORKER_RUN_ROLES:
            return run.status == "passed"
    return False


def _round_counts_for_step(run: _Run, step: StepDefinition, agent: str) -> bool:
    """A code round belongs to ``step`` when it was recorded with its step
    id, or (untagged) by the step's own agent persona."""
    if run.role != "reviewer":
        return False
    if run.workflow_step_id is not None:
        return run.workflow_step_id == step.id
    return (run.persona_name or "").lower() == agent.lower()


def _reviewed_by_step(
    child: _Child,
    step: StepDefinition,
    agent: str,
    verdicts: dict[str, bool],
    independent: dict[str, bool],
) -> bool:
    """The step's own latest round on ``child`` is a trusted strict
    VERIFIED-CLEAN at the child's latest head from a reviewer independent of
    every implementer, and the loop closed the child (or the ticket is
    done)."""
    closed = child.ticket.status == "done" or (
        child.item is not None and child.item.item_status == "done"
    )
    if not closed:
        return False
    head = _latest_head(child)
    for run in child.runs:  # newest first
        if not _round_counts_for_step(run, step, agent):
            continue
        if not (run.trigger_ref and verdicts.get(run.trigger_ref)):
            return False
        if not independent.get(run.id):
            return False
        return head is None or run.head_sha == head
    return False


async def _load_verdicts(db: AsyncSession, comment_ids: list[str]) -> dict[str, bool]:
    """comment id -> True when it is a trusted strict VERIFIED-CLEAN."""
    from sessionfs.server.services.review_state import (
        _HEADER_RE,
        is_strict_verified_clean,
    )

    out: dict[str, bool] = {}
    rows: list[Any] = []
    for chunk in _chunks(sorted(set(comment_ids))):
        rows.extend(
            (
                await db.execute(
                    select(
                        TicketComment.id, TicketComment.verdict_trusted, TicketComment.content
                    ).where(TicketComment.id.in_(chunk))
                )
            ).all()
        )
    for cid, trusted, content in rows:
        match = _HEADER_RE.search(content or "")
        out[cid] = (
            bool(trusted)
            and match is not None
            and is_strict_verified_clean(match.group("verdict"))
        )
    return out


async def _load_independence(
    db: AsyncSession, tickets: list[Ticket], review_runs: list[_Run]
) -> dict[str, bool]:
    """run id -> True only when the round's recorded actor is provably not
    any implementer of its ticket.

    Same rules as merge readiness and the loop's own close check (shared
    loaders, batched for the whole plan): a service key IS every user who
    minted or rotated it along its chain; two identities are the same when
    their user sets intersect; an empty implementer set, a reviewer key or
    implementer key whose minters can't be resolved, or a round with no
    recorded actor all fail closed (not independent).
    """
    from sessionfs.server.services.review_state import (
        load_implementer_identities,
        load_key_minters,
    )

    out: dict[str, bool] = {}
    if not review_runs:
        return out
    identities = await load_implementer_identities(db, tickets)
    pseudo = [
        {"verdict_trusted": True, "service_key_id": r.service_key_id}
        for r in review_runs
        if r.service_key_id
    ]
    all_ids = [i for tid in identities for i in identities[tid] if i[0] or i[1]]
    minters = await load_key_minters(db, pseudo, all_ids)
    for run in review_runs:
        impl = [(svc, usr) for svc, usr in identities.get(run.ticket_id or "", []) if svc or usr]
        out[run.id] = _is_independent(run, impl, minters)
    return out


def _is_independent(
    run: _Run,
    implementers: list[tuple[str | None, str | None]],
    minters: dict[str, frozenset[str] | None],
) -> bool:
    if not implementers or not (run.triggered_by_user_id or run.service_key_id):
        return False
    reviewer_users: set[str] = set()
    if run.triggered_by_user_id:
        reviewer_users.add(run.triggered_by_user_id)
    if run.service_key_id:
        minter = minters.get(run.service_key_id)
        if not minter:
            return False
        reviewer_users |= minter
    for svc, usr in implementers:
        if usr and usr in reviewer_users:
            return False
        if svc:
            if run.service_key_id and svc == run.service_key_id:
                return False
            minter = minters.get(svc)
            if not minter or minter & reviewer_users:
                return False
    return True


def _subjects_of(
    step: StepDefinition,
    by_step: dict[str, list[_Child]],
    states: dict[str, StepState],
    definition: WorkflowDefinition,
) -> list[_Child]:
    """A reviewer step with dependencies reviews its dependencies' tickets;
    a dependency that is itself such a reviewer step contributes the tickets
    it reviews (transitively), so reviewer chains never run out of work."""
    if not (step.role == "reviewer" and step.depends_on):
        return by_step[step.id]
    out: list[_Child] = []
    seen: set[str] = set()
    for dep in step.depends_on:
        dstep = definition.step(dep)
        source = (
            states[dep].subjects
            if dstep is not None and not step_has_own_tickets(dstep)
            else by_step[dep]
        )
        for c in source:
            if c.ticket.id not in seen:
                seen.add(c.ticket.id)
                out.append(c)
    return out


async def compute_plan(
    db: AsyncSession,
    loop: Ticket,
    loop_config: dict[str, Any],
    *,
    with_gates: bool = True,
    gate_steps: frozenset[str] | set[str] | None = None,
    checklist_items: list[dict[str, str]] | None = None,
) -> Plan | None:
    """The plan for a workflow loop, or None when the loop has no (readable)
    workflow pin.

    ``with_gates=False`` (step-report validation) evaluates exit gates only
    for ``gate_steps`` (the reported step's ancestors); other
    gated steps are left not passed. ``checklist_items`` renders reviewer
    steps' own briefs. A fixed number of queries regardless of fan-out.
    """
    pin = workflow_pin(loop_config)
    if pin is None:
        return None
    loaded = await load_pinned_definition(db, pin)
    if loaded is None:
        return None
    template, definition = loaded

    slots = (
        await db.execute(
            select(
                WorkflowStepChild.ticket_id,
                WorkflowStepChild.step_id,
                WorkflowStepChild.step_index,
            ).where(WorkflowStepChild.loop_ticket_id == loop.id)
        )
    ).all()
    ticket_ids = [s.ticket_id for s in slots]
    tickets: dict[str, Ticket] = {}
    items: dict[str, WorkQueueItem] = {}
    runs: dict[str, list[_Run]] = {}
    verdicts: dict[str, bool] = {}
    independent: dict[str, bool] = {}
    if ticket_ids:
        tickets = {
            t.id: t
            for t in (
                await db.execute(select(Ticket).where(Ticket.id.in_(ticket_ids)))
            ).scalars()
        }
        queue_id = loop_config.get("queue_id")
        if queue_id:
            items = {
                i.ticket_id: i
                for i in (
                    await db.execute(
                        select(WorkQueueItem).where(
                            WorkQueueItem.work_queue_id == queue_id,
                            WorkQueueItem.ticket_id.in_(ticket_ids),
                        )
                    )
                ).scalars()
            }
        review_refs: list[str] = []
        for chunk in _chunks(ticket_ids):
            for row in (await db.execute(_latest_runs_stmt(loop.project_id, chunk))).all():
                run = _run_from_row(row)
                runs.setdefault(run.ticket_id or "", []).append(run)
                if run.role == "reviewer" and run.trigger_ref:
                    review_refs.append(run.trigger_ref)
        for bucket in runs.values():
            bucket.sort(key=_run_sort_key, reverse=True)
        verdicts = await _load_verdicts(db, review_refs)
        review_runs = [
            r for rs in runs.values() for r in rs if r.role == "reviewer" and r.trigger_ref
        ]
        independent = await _load_independence(
            db, [tickets[t] for t in ticket_ids if t in tickets], review_runs
        )

    by_step: dict[str, list[_Child]] = {s.id: [] for s in definition.steps}
    children_by_ticket: dict[str, tuple[str, _Child]] = {}
    for slot in sorted(slots, key=lambda s: (s.step_id, s.step_index)):
        ticket = tickets.get(slot.ticket_id)
        if ticket is None or slot.step_id not in by_step:
            continue
        child = _Child(
            ticket=ticket,
            index=slot.step_index,
            item=items.get(ticket.id),
            runs=runs.get(ticket.id, []),
        )
        by_step[slot.step_id].append(child)
        children_by_ticket[ticket.id] = (slot.step_id, child)

    ctx = _GateContext(db, loop.project_id)
    states: dict[str, StepState] = {}
    for step in _topological(definition):
        own = by_step[step.id]
        subjects = _subjects_of(step, by_step, states, definition)
        deps_done = all(states[d].done for d in step.depends_on)

        if step.role == "reviewer":
            agent = step_agent(loop_config, step)
            pending = [
                c
                for c in subjects
                if not _reviewed_by_step(c, step, agent, verdicts, independent)
            ]
            work_done = bool(subjects) and not pending
        else:
            pending = [c for c in own if not _worker_child_done(c, step.id)]
            work_done = bool(own) and not pending
        if step.gate == "none":
            gate_passed, gate_reason = True, None
        elif not work_done:
            gate_passed, gate_reason = False, None
        elif with_gates or (gate_steps is not None and step.id in gate_steps):
            gate_passed, gate_reason = await _gate(ctx, step, subjects)
        else:
            gate_passed, gate_reason = False, "not evaluated"

        reported = any(_step_runs(c, step.id) for c in own + subjects) or any(
            c.ticket.status == "in_progress" for c in own
        )
        parked = [
            c for c in own + subjects
            if c.item is not None and c.item.item_status == "needs_human"
        ]
        done = deps_done and work_done and gate_passed
        reason: str | None = None
        ready_children: list[_Child] = []
        if done:
            state = "done"
        elif not deps_done:
            state = "blocked"
            waiting = [d for d in step.depends_on if not states[d].done]
            reason = "waiting for " + ", ".join(waiting)
        elif parked:
            state = "needs_human"
            reason = f"{parked[0].ticket.id} is parked for a human"
        elif work_done:
            state = "blocked"
            reason = f"gate {step.gate} not passed: {gate_reason or 'pending'}"
        else:
            running = any(
                r.status in ("queued", "running")
                for c in own + subjects
                for r in _step_runs(c, step.id)
            )
            state = "running" if (reported or running) else "ready"
            ready_children = [
                c
                for c in pending
                if not any(
                    r.status in ("queued", "running") for r in _step_runs(c, step.id)
                )
            ]
        brief: str | None = None
        if (
            checklist_items is not None
            and not step_has_own_tickets(step)
            and step.brief_template.strip()
        ):
            try:
                brief = clean_multiline(
                    render_brief(
                        step.brief_template,
                        ticket_title=loop.title or "",
                        ticket_description=loop.description or "",
                        checklist_items=checklist_items,
                    )
                )
            except WorkflowDefinitionError:
                brief = None
        states[step.id] = StepState(
            step=step,
            state=state,
            reason=reason,
            done=done,
            work_done=work_done,
            children=own,
            subjects=subjects,
            ready_children=ready_children,
            gate_passed=gate_passed,
            gate_reason=gate_reason,
            brief=brief,
        )
    return Plan(
        template=template,
        definition=definition,
        steps=states,
        children_by_ticket=children_by_ticket,
    )


def _topological(definition: WorkflowDefinition) -> list[StepDefinition]:
    done: set[str] = set()
    out: list[StepDefinition] = []
    remaining = list(definition.steps)
    while remaining:
        progressed = False
        for step in list(remaining):
            if all(d in done for d in step.depends_on):
                out.append(step)
                done.add(step.id)
                remaining.remove(step)
                progressed = True
        if not progressed:  # validated definitions are acyclic
            raise WorkflowDefinitionError("definition has a cycle")
    return out


def _child_view(
    child: _Child, *, brief: bool, brief_override: str | None = None
) -> dict[str, Any]:
    out: dict[str, Any] = {
        "ticket_id": child.ticket.id,
        "index": child.index,
        "title": child.ticket.title,
        "status": child.ticket.status,
        "item_status": child.item.item_status if child.item is not None else None,
    }
    if brief:
        out["brief_markdown"] = (
            brief_override if brief_override is not None else (child.ticket.description or "")
        )
        out["agent"] = child.ticket.assigned_to
    return out


def plan_view(loop: Ticket, plan: Plan, *, show_template: bool = True) -> dict[str, Any]:
    """The plan response. ``show_template=False`` (the caller cannot see the
    org that owns the template) omits everything that comes from the
    template itself — its name, step titles, agents, gates and reviewer
    brief text — and keeps step state and the caller's own ticket briefs."""
    steps = []
    for step in plan.definition.steps:
        st = plan.steps[step.id]
        reviewer_brief = st.brief if show_template else None
        steps.append(
            {
                "step_id": step.id,
                "title": step.title if show_template else None,
                "role": step.role,
                "agent": step.agent if show_template else None,
                "depends_on": list(step.depends_on),
                "fan_out": step.fan_out,
                "gate": step.gate if show_template else None,
                "state": st.state,
                "reason": st.reason,
                "gate_status": {"passed": st.gate_passed, "reason": st.gate_reason},
                "children": [_child_view(c, brief=False) for c in st.children],
                "reviews": (
                    [c.ticket.id for c in st.subjects]
                    if not step_has_own_tickets(step)
                    else []
                ),
                "ready_children": [
                    _child_view(c, brief=True, brief_override=reviewer_brief)
                    for c in st.ready_children
                ],
            }
        )
    return {
        "loop_id": loop.id,
        "template": {
            "id": plan.template.id,
            "lineage_id": plan.template.lineage_id,
            "name": plan.template.name if show_template else None,
            "version": plan.template.version,
        },
        "steps": steps,
        "ready_steps": [s["step_id"] for s in steps if s["state"] == "ready"],
        "done": all(st.done for st in plan.steps.values()),
    }


# ── Step report validation ──


def _ancestors(definition: WorkflowDefinition, step: StepDefinition) -> frozenset[str]:
    """Every step ``step`` depends on, directly or transitively."""
    out: set[str] = set()
    stack = list(step.depends_on)
    while stack:
        sid = stack.pop()
        if sid in out:
            continue
        out.add(sid)
        dep = definition.step(sid)
        if dep is not None:
            stack.extend(dep.depends_on)
    return frozenset(out)


class WorkflowStepMismatch(Exception):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


async def check_step_report(
    db: AsyncSession,
    *,
    loop: Ticket | None,
    loop_config: dict[str, Any],
    ticket: Ticket,
    step_id: str,
    kind: Literal["worker", "round"],
    check_dependencies: bool = True,
) -> StepDefinition:
    """Raise ``WorkflowStepMismatch`` unless ``step_id`` is a valid report
    for ``ticket`` in this workflow loop; return the step.

    Every dependency must be fully done, transitively: gates are evaluated
    for the step's ancestors only (never for unrelated steps)."""
    if loop is None or workflow_pin(loop_config) is None:
        raise WorkflowStepMismatch("step_id was given but this loop has no workflow")
    pin = workflow_pin(loop_config)
    assert pin is not None
    loaded = await load_pinned_definition(db, pin)
    if loaded is None:
        raise WorkflowStepMismatch("this loop's workflow template is unavailable")
    step = loaded[1].step(step_id)
    if step is None:
        raise WorkflowStepMismatch(f"step {step_id!r} is not in this workflow")
    allowed = WORKER_ROLES if kind == "worker" else ROUND_ROLES
    if step.role not in allowed:
        what = "worker runs" if kind == "worker" else "review rounds"
        raise WorkflowStepMismatch(
            f"step {step_id!r} has role {step.role!r}, which does not report {what}"
        )
    plan = await compute_plan(
        db,
        loop,
        loop_config,
        with_gates=False,
        gate_steps=_ancestors(loaded[1], step) if check_dependencies else frozenset(),
    )
    if plan is None:
        raise WorkflowStepMismatch("this loop's workflow template is unavailable")
    st = plan.steps[step.id]
    targets = {c.ticket.id for c in st.children}
    if kind == "round":
        targets |= {c.ticket.id for c in st.subjects}
    if ticket.id not in targets:
        raise WorkflowStepMismatch(
            f"ticket {ticket.id!r} is not a ticket of step {step_id!r}"
        )
    if check_dependencies:
        waiting = [d for d in step.depends_on if not plan.steps[d].done]
        if waiting:
            raise WorkflowStepMismatch(
                f"step {step_id!r} is waiting for: {', '.join(waiting)}"
            )
    return step


async def check_untagged_round(
    db: AsyncSession,
    *,
    loop: Ticket | None,
    loop_config: dict[str, Any],
    ticket: Ticket,
    persona: str | None,
) -> None:
    """Refuse an untagged code round that would count for a reviewer step
    whose dependencies are not done yet.

    A round recorded without ``step_id`` counts for every reviewer step whose
    pinned agent matches its persona and that reviews ``ticket`` (see
    ``_round_counts_for_step``), so it must respect those steps' order just
    like a tagged round does. Outside a workflow this is a no-op. Raises
    ``WorkflowStepMismatch``."""
    if loop is None or workflow_pin(loop_config) is None or not persona:
        return
    pin = workflow_pin(loop_config)
    assert pin is not None
    loaded = await load_pinned_definition(db, pin)
    if loaded is None:
        raise WorkflowStepMismatch("this loop's workflow template is unavailable")
    definition = loaded[1]
    candidates = [
        s
        for s in definition.steps
        if s.role == "reviewer"
        and step_agent(loop_config, s).lower() == persona.lower()
    ]
    if not candidates:
        return
    ancestors: set[str] = set()
    for s in candidates:
        ancestors |= _ancestors(definition, s)
    plan = await compute_plan(
        db, loop, loop_config, with_gates=False, gate_steps=frozenset(ancestors)
    )
    if plan is None:
        raise WorkflowStepMismatch("this loop's workflow template is unavailable")
    for s in candidates:
        st = plan.steps[s.id]
        if ticket.id not in {c.ticket.id for c in st.subjects}:
            continue
        waiting = [d for d in s.depends_on if not plan.steps[d].done]
        if waiting:
            raise WorkflowStepMismatch(
                f"this round would count for step {s.id!r}, which is waiting for: "
                f"{', '.join(waiting)} (pass step_id to report it for another step)"
            )


__all__ = [
    "MAX_DEFINITION_BYTES",
    "MAX_GATE_TICKETS",
    "Plan",
    "StepDefinition",
    "WorkflowDefinition",
    "WorkflowDefinitionError",
    "WorkflowAgentUnknown",
    "WorkflowStepMismatch",
    "approval_policy_ids",
    "start_workflow",
    "step_agent",
    "step_has_own_tickets",
    "check_step_report",
    "check_untagged_round",
    "clean_multiline",
    "compute_plan",
    "dump_definition",
    "load_pinned_definition",
    "parse_definition",
    "plan_view",
    "render_brief",
    "validate_policy_scope",
    "workflow_pin",
]
