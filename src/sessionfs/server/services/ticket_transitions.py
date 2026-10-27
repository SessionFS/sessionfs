"""Ticket lifecycle transitions shared by the ticket routes and the Jira webhook.

The ticket routes (``routes/tickets.py``) own authentication, project access,
feature gates, lease-required mode and the per-action actor rules
(``_require_ticket_actor``, the project-admin gate on ``/close``). Everything
that happens once a caller is allowed to move a ticket lives here so that every
path into the FSM behaves the same:

* the FSM tables and legality check;
* the atomic ``UPDATE ... WHERE status = <expected>`` guards;
* ``resolved_at`` stamping on resolution;
* dependent enrichment and auto-unblock (``enrich_dependents``);
* watcher notifications.

Callers that are not a user — the inbound Jira webhook — pass
``actor_user_id=None`` and an ``actor_type`` (``"jira"``) so notifications are
attributed to the integration rather than to a person.

Authorization for the Jira webhook: an org owner/admin configured the
``status_map`` that turns a Jira status into a SessionFS action, so an inbound
transition acts on the org's behalf, exactly like an admin pressing the button.
It is still bound by the FSM and by what each ticket kind may do (only Tasks
accept from review; only Issues close; nothing jumps states), and a refused
transition is reported back to the caller — never forced.

Each function raises :class:`TransitionRefused` instead of an HTTP error; the
routes translate it to the ``HTTPException`` they always returned and the
webhook records it as an ``error`` sync event. No function commits.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server.db.models import Project, Ticket, TicketComment, TicketDependency
from sessionfs.server.services import ticket_watchers

# Maps current status → set of legal next statuses. Routes consult
# this table to validate every transition; a missing key means the
# starting state has no legal transitions (terminal state).
#
# Two FSMs keyed by Ticket.kind (v0.10.24 tk_dbccde26ed604b3c).
# Tasks keep the full executor FSM; Issues use a simpler container
# FSM (open → in_progress → closed, cancelled as escape valve).
# Issue status is manual-close by Compass — NOT auto-derived from
# children, per CEO direction.
LEGAL_TRANSITIONS_TASK: dict[str, set[str]] = {
    "suggested": {"open", "cancelled"},
    "open": {"in_progress", "cancelled"},
    "in_progress": {"blocked", "review"},
    "blocked": {"in_progress"},
    "review": {"done", "open"},
    "done": set(),
    "cancelled": set(),
}
LEGAL_TRANSITIONS_ISSUE: dict[str, set[str]] = {
    "open": {"in_progress", "cancelled"},
    "in_progress": {"closed", "open", "cancelled"},
    "closed": set(),
    "cancelled": set(),
}


class TransitionRefused(Exception):
    """A transition the FSM, the ticket kind or a concurrent update refused.

    ``status_code`` and ``detail`` are what the ticket routes return.
    """

    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


def legal_next(current: str, kind: str = "task") -> set[str]:
    table = LEGAL_TRANSITIONS_ISSUE if kind == "issue" else LEGAL_TRANSITIONS_TASK
    return table.get(current, set())


def illegal_transition_message(current: str, target: str, kind: str = "task") -> str:
    legal = sorted(legal_next(current, kind)) or ["(none — terminal state)"]
    return (
        f"Cannot transition from {current!r} to {target!r}. "
        f"Legal transitions from {current!r}: {', '.join(legal)}."
    )


def check_transition(current: str, target: str, kind: str = "task") -> None:
    """Raise ``TransitionRefused(400)`` unless ``current → target`` is legal."""
    if target not in legal_next(current, kind):
        raise TransitionRefused(400, illegal_transition_message(current, target, kind))


def _loads(s: str | None) -> list:
    if not s:
        return []
    try:
        data = json.loads(s)
    except (ValueError, TypeError):
        return []
    return data if isinstance(data, list) else []


async def _reload(db: AsyncSession, ticket_id: str, project_id: str | None = None) -> Ticket | None:
    """Re-read a ticket, overwriting any stale identity-map copy."""
    stmt = select(Ticket).where(Ticket.id == ticket_id)
    if project_id is not None:
        stmt = stmt.where(Ticket.project_id == project_id)
    return (
        await db.execute(stmt.execution_options(populate_existing=True))
    ).scalar_one_or_none()


# ── Side effects ──


async def enrich_dependents(
    db: AsyncSession, completed: Ticket, actor_user_id: str | None
) -> None:
    """When a ticket transitions to 'done', enrich every ticket that
    depends on it: append a completion comment with the notes, merge
    knowledge_entry_ids into the dependent's context_refs, and
    auto-unblock if all upstream deps are now done.

    ``actor_user_id`` is the user whose resolution of ``completed``
    triggered the unblock (``None`` for the Jira webhook); it is passed
    through to the watcher notification so dependents' watchers can see
    who unblocked them.

    v0.10.1 Phase 3 Round 2 (KB 326) — defensive same-project filter.
    The pre-validation on create rejects cross-project depends_on
    rows, but legacy data or a raw SQL insert could still produce a
    cross-project edge. JOIN tickets here to enforce that we only
    enrich same-project dependents — never reach across project
    boundaries to write a comment or merge a KB ref.
    """
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
                # Ticket watchers: the dependent became actionable again.
                await ticket_watchers.emit_notifications(
                    db,
                    ticket=dependent,
                    kind="dependency_unblocked",
                    summary=f"Unblocked — dependency #{completed.id} completed",
                    actor_user_id=actor_user_id,
                )


async def emit_status_notification(
    db: AsyncSession,
    *,
    ticket: Ticket,
    project: Project,
    summary: str,
    actor_user_id: str | None,
    actor_type: str = "user",
    your_turn_assignee: bool = False,
) -> None:
    """Emit a plain status notification, optionally handing the current
    assignee's human owner a "your turn" (and auto-subscribing them).

    ``your_turn_assignee`` is used for transitions that put the work back
    in the assignee's court (reopen, approve); the mapping is resolved
    from the persona's ``represented_user_id`` and is skipped when it is
    missing or ambiguous.
    """
    your_turn_user_id: str | None = None
    subscribe: list[tuple[str, str, str | None]] | None = None
    if your_turn_assignee:
        assignee_human = await ticket_watchers.map_persona_to_human(
            db,
            project_id=project.id,
            name=ticket.assigned_to or "",
            org_id=project.org_id,
        )
        if assignee_human is not None:
            your_turn_user_id = assignee_human[0]
            subscribe = [(assignee_human[0], "assignee", assignee_human[1])]
    await ticket_watchers.emit_notifications(
        db,
        ticket=ticket,
        kind="status",
        summary=summary,
        actor_user_id=actor_user_id,
        actor_type=actor_type,
        your_turn_user_id=your_turn_user_id,
        subscribe_users=subscribe,
    )


# ── Transitions ──


async def start_ticket(
    db: AsyncSession,
    ticket: Ticket,
    *,
    allowed_from: set[str],
    actor_user_id: str | None,
    actor_type: str = "user",
    service_key_name: str | None = None,
) -> Ticket:
    """open (or, with ``allowed_from``, blocked) → in_progress.

    Atomic ``UPDATE ... WHERE status IN allowed_from`` with a rowcount check
    (two concurrent starts can't both win) and a lease-epoch bump that fences
    any agent still holding the previous lease. Emits the watcher event.
    """
    result = await db.execute(
        update(Ticket)
        .where(
            Ticket.id == ticket.id,
            Ticket.status.in_(list(allowed_from)),
        )
        .values(
            status="in_progress",
            updated_at=datetime.now(timezone.utc),
            lease_epoch=Ticket.lease_epoch + 1,
        )
    )
    if result.rowcount != 1:
        current = await _reload(db, ticket.id)
        current_status = current.status if current is not None else None
        raise TransitionRefused(
            409,
            f"Cannot start ticket: current status is {current_status!r}, "
            f"expected one of {sorted(allowed_from)}. "
            f"Pass force=true to recover from 'blocked'.",
        )
    await db.refresh(ticket)
    # Ticket watchers: moving into in_progress is a status event for
    # followers (the assignee's human owner is already subscribed from
    # create/update, so they hear about it too).
    await ticket_watchers.emit_notifications(
        db,
        ticket=ticket,
        kind="status",
        summary="Started — in progress",
        actor_user_id=actor_user_id,
        actor_type=actor_type,
        service_key_name=service_key_name,
    )
    return ticket


async def accept_ticket(
    db: AsyncSession,
    *,
    project_id: str,
    ticket_id: str,
    lease_epoch: int | None,
    actor_user_id: str | None,
    actor_type: str = "user",
) -> Ticket:
    """review → done (Tasks). Stamps ``resolved_at``, enriches dependents.

    v0.10.1 Phase 3 Round 2 (KB 326) — atomic state transition via
    ``UPDATE ... WHERE status='review'`` with rowcount check. Without this
    guard, two concurrent accepts could BOTH observe ``status='review'`` and
    BOTH run dependent enrichment → duplicate dependency comments + repeated
    KB-ref merges. Only the caller that owns the row mutation enriches.
    """
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
        # Either ticket doesn't exist, isn't in this project, or wasn't in
        # 'review'. Report the real situation.
        existing = await _reload(db, ticket_id, project_id)
        if existing is None:
            raise TransitionRefused(404, "Ticket not found")
        if lease_epoch is not None and existing.lease_epoch != lease_epoch:
            raise TransitionRefused(
                409,
                (
                    f"Stale ticket lease: provided lease_epoch={lease_epoch}, "
                    f"current lease_epoch={existing.lease_epoch}."
                ),
            )
        legal = sorted(legal_next(existing.status, existing.kind))
        legal_str = ", ".join(legal) if legal else "(none — terminal state)"
        raise TransitionRefused(
            409,
            f"Cannot accept ticket: current status is {existing.status!r}, "
            f"expected 'review'. Legal transitions from "
            f"{existing.status!r}: {legal_str}.",
        )

    # Re-read so enrichment sees status='done' and the new resolved_at.
    ticket = await _reload(db, ticket_id, project_id)
    assert ticket is not None
    await enrich_dependents(db, ticket, actor_user_id=actor_user_id)
    # Ticket watchers: resolution is a plain status event for followers.
    await ticket_watchers.emit_notifications(
        db,
        ticket=ticket,
        kind="status",
        summary="Resolved",
        actor_user_id=actor_user_id,
        actor_type=actor_type,
    )
    return ticket


async def close_issue(
    db: AsyncSession,
    *,
    project: Project,
    ticket: Ticket,
    actor_user_id: str | None,
    actor_type: str = "user",
) -> Ticket:
    """in_progress → closed (Issues only). Stamps ``resolved_at``.

    The project-admin gate on who may close stays in the route; the webhook
    acts on the org's behalf (see the module docstring).
    """
    if ticket.kind != "issue":
        raise TransitionRefused(
            400,
            f"Cannot close ticket {ticket.id!r}: kind={ticket.kind!r}. "
            "The /close transition is for Issues only; Tasks finish via "
            "/complete + /accept.",
        )
    check_transition(ticket.status, "closed", ticket.kind)
    now = datetime.now(timezone.utc)
    ticket.status = "closed"
    ticket.resolved_at = now
    ticket.updated_at = now
    await emit_status_notification(
        db,
        ticket=ticket,
        project=project,
        summary="Closed",
        actor_user_id=actor_user_id,
        actor_type=actor_type,
    )
    return ticket


async def reopen_ticket(
    db: AsyncSession,
    *,
    project: Project,
    ticket: Ticket,
    actor_user_id: str | None,
    actor_type: str = "user",
) -> Ticket:
    """review → open (Tasks) / in_progress → open (Issues)."""
    check_transition(ticket.status, "open", ticket.kind)
    ticket.status = "open"
    ticket.updated_at = datetime.now(timezone.utc)
    await emit_status_notification(
        db,
        ticket=ticket,
        project=project,
        summary="Reopened — changes requested",
        actor_user_id=actor_user_id,
        actor_type=actor_type,
        your_turn_assignee=True,
    )
    return ticket
