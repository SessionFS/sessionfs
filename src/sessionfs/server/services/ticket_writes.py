"""Commit-free ticket writes shared by the ticket routes and helper confirm.

``routes/tickets.py`` owns the HTTP surface (auth, scope locks, commit,
response shaping).  The parts of a write that decide *what* changes —
who may change which field, the lease fence, per-field audit rows, the
system diff comment, sending a non-editor's change back for approval,
watcher notifications and the comment insert — live here so the session
helper's confirm path applies exactly the same rules without a second copy.

Nothing in this module commits.  Callers own the transaction.
"""

from __future__ import annotations

import json
import logging
import secrets
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import TYPE_CHECKING, Any

from fastapi import HTTPException
from sqlalchemy import func, insert, literal, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server.db.models import (
    AgentFeedback,
    Organization,
    Project,
    Ticket,
    TicketComment,
    TicketDependency,
    TicketEdit,
    User,
    WorkQueueItem,
)
from sessionfs.server.services import ticket_watchers
from sessionfs.server.services.ticket_events import record_ticket_event

if TYPE_CHECKING:
    from sessionfs.server.auth.dependencies import AuthContext

logger = logging.getLogger("sessionfs.api")

# Fields that redefine the work: a non-editor may not change them once the
# ticket is past suggested/open or while a work queue holds it.
_WORK_DEFINING_FIELDS = (
    "title",
    "description",
    "priority",
    "assigned_to",
    "assigned_user_id",
    "acceptance_criteria",
    "depends_on",
)

_DIFF_INLINE_MAX = 200  # chars per field before truncation in the comment


async def resolve_persona_name(
    db: AsyncSession, project_id: str, name: str, *, org_id: str | None
) -> str | None:
    """Normalize a persona name to the canonical DB-side casing.

    Codex v0.10.23 tk_884b2321fdb74170 — `.agents/*.md` filenames are
    conventionally capitalized (`atlas-backend.md`) while
    `agent_personas.name` rows are stored lowercase. Without
    normalization, tickets land with `assigned_to='Atlas'` and the
    start_ticket resolver's exact-match lookup against `'atlas'`
    fails. Returns the canonical persona name (whatever case the
    active row stores) if a case-insensitive match exists; else None.

    Codex R1 LOW (same ticket) — uses `.first()` with deterministic
    ordering rather than `.scalar_one_or_none()` so the resolver
    never raises `MultipleResultsFound` even if a project somehow has
    case-distinct duplicate active personas (e.g. `atlas` AND `Atlas`).
    The persona-create endpoint also rejects case-insensitive
    duplicates as a separate guard so this defensive `.first()` only
    matters for legacy rows.
    """
    # GH #70 — project persona first, then the project's org persona.
    from sessionfs.server.services.personas import resolve_persona

    row = await resolve_persona(
        db, project_id, name, org_id=org_id, active_only=True, case_insensitive=True
    )
    return row.name if row is not None else None


_ASSIGNEE_CONFLICT = {
    "error": "assignee_conflict",
    "message": "A ticket has one assignee at a time: a persona or a person, not both.",
}
_ASSIGNEE_NOT_ELIGIBLE = {
    "error": "assignee_not_eligible",
    "message": "Assignee must be an active teammate with access to this project.",
}


def check_assignee_conflict(
    assigned_to: str | None,
    assigned_user_id: str | None,
    assigned_user: str | None,
) -> None:
    """Refuse more than one assignment token in a single write.

    ``assigned_to`` is a persona; ``assigned_user_id`` and ``assigned_user``
    are the same person (id or email). Any two non-null tokens conflict.
    """
    person_tokens = (assigned_user_id is not None) + (assigned_user is not None)
    if assigned_to is not None and person_tokens:
        raise HTTPException(422, _ASSIGNEE_CONFLICT)
    if person_tokens > 1:
        raise HTTPException(422, _ASSIGNEE_CONFLICT)


def assigned_user_label(user: User) -> str:
    """Readable name for audits and summaries: display name, else email."""
    return user.display_name or user.email


async def _assigned_user_label(db: AsyncSession, user_id: str | None) -> str | None:
    """The readable label for an already-stored ``assigned_user_id``."""
    if not user_id:
        return None
    user = await db.get(User, user_id)
    return assigned_user_label(user) if user is not None else None


async def resolve_assigned_user_id(
    db: AsyncSession, *, project: Project, value: str
) -> User:
    """Resolve an id or email to an active user with read standing on
    ``project``. Every miss — unknown, inactive, or no read standing — raises
    the same 422 so existence and org membership are never revealed."""
    value = value.strip()
    if "@" in value:
        stmt = select(User).where(func.lower(User.email) == value.lower())
    else:
        stmt = select(User).where(User.id == value)
    matches = (await db.execute(stmt.limit(2))).scalars().all()
    user = matches[0] if len(matches) == 1 else None
    eligible = False
    if user is not None and user.is_active:
        from sessionfs.server.auth.project_access import user_has_read_standing

        eligible = await user_has_read_standing(db, user.id, project)
    if not eligible:
        raise HTTPException(422, _ASSIGNEE_NOT_ELIGIBLE)
    return user


async def maybe_capture_route_feedback(
    db: AsyncSession,
    project_id: str,
    ticket: Ticket,
    old_assigned: str | None,
    new_assigned: str,
    reassigned_by_user_id: str,
) -> None:
    """v0.17.0 — best-effort auto-capture of route feedback on reassignment.

    Fires when a ticket with agent provenance (a `created_by_persona` is set,
    or it was created by a service key) is MOVED from one persona to a different
    one. A first assignment (no previous owner) is not a routing mistake.
    Records one canonical `route / negative / wrong_route` row per
    (ticket, new owner), `source=auto`, attributed to the user who made the
    reassignment (this route only admits user keys). A ticket with no agent
    identity (e.g. created with source="agent" but no persona) is skipped:
    there is no agent the record could judge.

    Never raises — a feedback-write bug must not break ticket reassignment.
    """
    try:
        if not old_assigned or old_assigned == new_assigned:
            return
        agent_created = (
            ticket.created_by_persona is not None
            or ticket.actor_type == "service_key"
        )
        if not agent_created:
            return
        # Write the reassignment's own pending changes OUTSIDE the savepoint
        # first; otherwise the query below would autoflush them into it and a
        # savepoint rollback would silently discard the reassignment.
        await db.flush()
        # Once per (ticket, new owner). Everything below runs inside a
        # savepoint: on PostgreSQL any failed
        # statement (or a racing insert hitting the partial unique index)
        # would otherwise abort the outer transaction and turn a valid
        # reassignment into a 500. The savepoint rolls back on its own.
        async with db.begin_nested():
            # Only an earlier AUTOMATIC record counts as a duplicate: a human
            # recording the same judgement must not suppress the automatic one.
            existing = (
                await db.execute(
                    select(AgentFeedback.id)
                    .where(
                        AgentFeedback.ticket_id == ticket.id,
                        AgentFeedback.source == "auto",
                        AgentFeedback.label == "wrong_route",
                        AgentFeedback.final_owner == new_assigned,
                    )
                    .limit(1)
                )
            ).scalar_one_or_none()
            if existing is not None:
                return
            db.add(
                AgentFeedback(
                    id=f"afb_{secrets.token_hex(12)}",
                    project_id=project_id,
                    ticket_id=ticket.id,
                    feedback_type="route",
                    verdict="negative",
                    label="wrong_route",
                    final_owner=new_assigned,
                    source="auto",
                    created_at=datetime.now(timezone.utc),
                    created_by_user_id=reassigned_by_user_id,
                    actor_type="user",
                )
            )
    except IntegrityError:
        pass  # a concurrent reassignment already recorded it
    except Exception:  # noqa: BLE001 — best-effort; never fail reassignment
        logger.exception("auto-capture route feedback failed (best-effort)")


def _jsonable(value: object) -> object:
    """Fold date/datetime diff values to ISO strings for JSON encoding.

    ``apply_ticket_update`` keeps raw ``date`` objects in its edit tuples so
    the event ledger can encode them via ``encode_event_value``; the
    ``ticket_edits`` audit rows need a JSON-safe form instead.
    """
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return value


def format_edit_diff_summary(
    edits: list[tuple[str, object, object]],
    *,
    fence_applied: bool,
    assigned_user_labels: dict[str, str] | None = None,
) -> str:
    """Render the per-field diff for the auto-posted comment.

    Short scalar fields show 'field: "old" → "new"' inline. Long
    string fields (description) and list fields show shape changes
    with truncated previews. ``assigned_user_id`` edits carry user ids
    (not display labels), so they render as a readable "Assigned to" /
    "Unassigned" line instead of raw ids. The audit trail in
    `ticket_edits` retains the full JSON-encoded values; this rendering is
    for human consumption.
    """

    def _truncate(s: str) -> str:
        if len(s) <= _DIFF_INLINE_MAX:
            return s
        return s[:_DIFF_INLINE_MAX] + "…"

    def _fmt(value: object) -> str:
        if isinstance(value, list):
            return f"[{len(value)} item(s)]"
        if isinstance(value, str):
            return f'"{_truncate(value)}"'
        return _truncate(str(value))

    labels = assigned_user_labels or {}

    def _person_label(user_id: object) -> str:
        if isinstance(user_id, str) and user_id:
            return labels.get(user_id, user_id)
        return ""

    def _assignment_line(field_name: str, old_v: object, new_v: object) -> str:
        old_label = _person_label(old_v)
        new_label = _person_label(new_v)
        if new_v and not old_v:
            return f"- `{field_name}`: Assigned to {new_label}"
        if old_v and not new_v:
            return f"- `{field_name}`: Unassigned (was {old_label})"
        return f"- `{field_name}`: Assigned to {new_label} (was {old_label})"

    lines = ["**Ticket fields updated** via `update_ticket` (tk_835a876529de4551):"]
    for field_name, old_v, new_v in edits:
        if field_name == "assigned_user_id":
            lines.append(_assignment_line(field_name, old_v, new_v))
        else:
            lines.append(f"- `{field_name}`: {_fmt(old_v)} → {_fmt(new_v)}")
    if not fence_applied:
        lines.append(
            "\n_Unfenced edit_ — no `lease_epoch` passed; last-write-wins."
        )
    lines.append(
        "\n_Full per-field old/new values preserved in `ticket_edits`._"
    )
    return "\n".join(lines)


def assert_lease_epoch(ticket: Ticket, lease_epoch: int | None) -> None:
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


async def assert_lease_required_mode(
    project_id: str,
    db: AsyncSession,
    lease_epoch: int | None,
    auth: AuthContext | None = None,
) -> None:
    """v0.10.7 — defense-in-depth: enforce org-level lease_epoch requirement.

    When the project's org has settings.require_lease_epoch_on_ticket_writes
    set to true, reject any complete/comment/accept that omits lease_epoch
    with 422. Personal projects (no org_id) and orgs without the setting
    continue to accept omitted lease (existing v0.10.4 opt-in semantics).
    """
    if lease_epoch is not None:
        # Caller supplied it; assert_lease_epoch handles staleness.
        return
    project = (
        await db.execute(select(Project).where(Project.id == project_id))
    ).scalar_one_or_none()
    if project is None:
        return
    org_id = (
        auth.org_id
        if auth is not None and auth.key_kind == "service"
        else project.org_id
    )
    if org_id is None:
        return
    org = (
        await db.execute(select(Organization).where(Organization.id == org_id))
    ).scalar_one_or_none()
    if org is None:
        return
    try:
        settings = json.loads(org.settings or "{}")
    except (json.JSONDecodeError, TypeError):
        settings = {}
    if settings.get("require_lease_epoch_on_ticket_writes") is True:
        raise HTTPException(
            422,
            (
                "Organization requires lease_epoch on ticket writes "
                "(setting: require_lease_epoch_on_ticket_writes). Pass "
                "lease_epoch from your active ticket bundle, or ask an "
                "admin to disable the requirement."
            ),
        )


async def authorize_ticket_edit(
    db: AsyncSession, *, ticket: Ticket, project: Project, user_id: str
) -> None:
    """Ticket creator or project admin; persona name is routing, not auth."""
    if ticket.created_by_user_id != user_id:
        from sessionfs.server.auth.project_access import user_is_project_admin

        if not await user_is_project_admin(db, user_id, project):
            raise HTTPException(
                403,
                "Only the ticket creator or a project admin may update "
                "this ticket. Persona name does not grant edit rights.",
            )


async def assert_ticket_fields_editable(
    db: AsyncSession,
    *,
    ticket: Ticket,
    project: Project,
    user_id: str,
    fields: list[str],
) -> bool:
    """Refuse a non-editor's change to the work-defining ``fields``.

    tk_25872885e3274a7d — a creator who is not an editor (a captured-session
    reader) may keep editing their ticket, but an editor's approval must
    cover what is actually adopted and worked on:

    * suggested — edit freely (nothing is actionable yet);
    * open (approved, not started) — edits apply and send the ticket back
      to suggested for approval again (``apply_ticket_update``);
    * in progress or later, or adopted by a work queue that hasn't finished
      with it — only the reference lists (context_refs, file_refs,
      related_sessions) may change; anything that redefines the work
      (title, description, priority, assignee, acceptance criteria,
      dependencies) is 403 ``project_edit_forbidden`` while someone may be
      working on it.

    Returns whether the caller is an editor.
    """
    from sessionfs.server.auth.project_access import user_can_edit_project

    editor = await user_can_edit_project(db, user_id, project)
    if editor:
        return True
    # A ticket a work queue has adopted and not finished counts as in
    # progress: an agent may already be working from its text.
    queued = (
        await db.execute(
            select(WorkQueueItem.id)
            .where(
                WorkQueueItem.ticket_id == ticket.id,
                WorkQueueItem.item_status.notin_(("done", "failed")),
            )
            .limit(1)
        )
    ).first() is not None
    if queued or ticket.status not in ("suggested", "open"):
        locked_fields = [name for name in _WORK_DEFINING_FIELDS if name in fields]
        if locked_fields:
            raise HTTPException(
                403,
                {
                    "code": "project_edit_forbidden",
                    "message": (
                        (
                            "A work queue is already working on this ticket. "
                            if queued
                            else ""
                        )
                        + "Only the project owner or a member of the project's "
                        "organization can change "
                        + ", ".join(locked_fields)
                        + (
                            " on it now."
                            if queued
                            else f" on a ticket that is {ticket.status}."
                        )
                    ),
                    "fields": locked_fields,
                },
            )
    return False


@dataclass
class TicketUpdateOutcome:
    """What ``apply_ticket_update`` changed: ``(field, old, new)`` per edit."""

    edits: list[tuple[str, object, object]] = field(default_factory=list)


async def apply_ticket_update(
    db: AsyncSession,
    *,
    project: Project,
    ticket: Ticket,
    user_id: str,
    ctx: Any,
    body: Any,
    actor_type: str = "user",
) -> TicketUpdateOutcome:
    """Apply a ``TicketUpdate`` body to ``ticket``; the caller commits.

    The caller has already authorized the edit (``authorize_ticket_edit``),
    taken the project scope lock and re-read the ticket under it.  This runs
    everything after that, identically for the PUT route and helper confirm:

    1. the non-editor edit-access rules (``assert_ticket_fields_editable``);
    2. the optional ``lease_epoch`` fence (409 when stale);
    3. per-field diffs, one ``TicketEdit`` audit row each;
    4. a non-editor's change to an approved (open) ticket sends it back to
       suggested;
    5. the ``system`` diff comment;
    6. reassignment notifications/watchers and route-feedback capture.

    ``actor_type`` is the timeline actor vocabulary for the field_edited /
    due_date_set / estimate_set events (defaults to ``"user"``). ``user_id``
    is always the acting user id, so a ``jira`` pull can attribute the change
    to Jira while still carrying the clicking user in ``actor_user_id``.

    A no-op (nothing actually changed) writes no audit rows or comment.
    """
    project_id = project.id
    body_fields_set = getattr(body, "model_fields_set", ())
    present = [
        name
        for name in (
            "title",
            "description",
            "priority",
            "context_refs",
            "file_refs",
            "related_sessions",
            "acceptance_criteria",
            "depends_on",
            "due_date",
            "estimated_effort",
        )
        if getattr(body, name) is not None
    ]
    # Assignment is work-defining. Surface it whenever the caller set the
    # key: an explicit null is itself a change (unassign) and must pass the
    # non-editor editability gate too, not just non-null values.
    if "assigned_to" in body_fields_set:
        present.append("assigned_to")
    if "assigned_user_id" in body_fields_set or "assigned_user" in body_fields_set:
        present.append("assigned_user_id")
    # Scheduling fields are not work-defining, but an explicit null is a
    # change (clear the due date / estimate) and must pass the same gate.
    if "due_date" in body_fields_set:
        present.append("due_date")
    if "estimated_effort" in body_fields_set:
        present.append("estimated_effort")
    editor = await assert_ticket_fields_editable(
        db, ticket=ticket, project=project, user_id=user_id, fields=present
    )

    # Optimistic concurrency: fence on lease_epoch when caller passes
    # it. Atomic UPDATE WHERE lease_epoch=... so a concurrent writer
    # holding the same epoch can win exactly once.
    fence_applied = False
    if body.lease_epoch is not None:
        # Atomic check-then-mark: bump the lease_epoch row-level via
        # an UPDATE WHERE ... rowcount-1 guard. On hit we are the sole
        # writer for this epoch; on miss we 409.
        bumped = await db.execute(
            update(Ticket)
            .where(
                Ticket.id == ticket.id,
                Ticket.project_id == project_id,
                Ticket.lease_epoch == body.lease_epoch,
            )
            .values(lease_epoch=body.lease_epoch + 1)
            .execution_options(synchronize_session="fetch")
        )
        if bumped.rowcount != 1:
            current_epoch = (
                await db.execute(
                    select(Ticket.lease_epoch).where(Ticket.id == ticket.id)
                )
            ).scalar_one_or_none()
            raise HTTPException(
                409,
                {
                    "error": "stale_lease_epoch",
                    "message": (
                        "Ticket lease_epoch has advanced since you read it. "
                        "Re-fetch the ticket and retry."
                    ),
                    "passed_lease_epoch": body.lease_epoch,
                    "current_lease_epoch": current_epoch,
                },
            )
        # Re-fetch so subsequent reads + the response reflect the bump.
        await db.refresh(ticket)
        fence_applied = True

    # Compute per-field diffs BEFORE mutating so the audit row captures
    # the right old_value. Each entry: (field_name, old_value, new_value)
    # where values are JSON-serializable; the audit-write encodes them.
    outcome = TicketUpdateOutcome()
    edits = outcome.edits

    def _record(field_name: str, old_v: object, new_v: object) -> None:
        if old_v != new_v:
            edits.append((field_name, old_v, new_v))

    if body.title is not None:
        _record("title", ticket.title, body.title)
        ticket.title = body.title
    if body.description is not None:
        _record("description", ticket.description, body.description)
        ticket.description = body.description
    if body.priority is not None:
        _record("priority", ticket.priority, body.priority)
        ticket.priority = body.priority
    # ── Assignment: one assignee at a time (persona XOR person) ──
    # ``assigned_to`` is a persona; ``assigned_user_id``/``assigned_user``
    # target a person (by id or email). Setting one clears the other; an
    # explicit null unassigns that side. TicketEdit rows store user ids (not
    # display labels) so two same-named users never compare equal, while the
    # diff comment renders their readable labels.
    user_labels: dict[str, str] = {}

    async def _remember_label(user_id: str | None) -> None:
        if user_id and user_id not in user_labels:
            label = await _assigned_user_label(db, user_id)
            user_labels[user_id] = label if label else user_id

    check_assignee_conflict(
        body.assigned_to, body.assigned_user_id, body.assigned_user
    )

    if body.assigned_to is not None:
        # v0.10.23 tk_884b2321fdb74170 — same best-effort normalize as
        # create. Matches assign_persona MCP path that routes through
        # this PUT.
        from sessionfs.server.services.personas import visible_persona_org_id

        # GH #70 — the project scope lock was taken by the caller, before
        # the lease fence; it is held until commit.
        canonical = await resolve_persona_name(
            db,
            project_id,
            body.assigned_to,
            org_id=await visible_persona_org_id(db, project, user_id=user_id),
        )
        new_assigned = canonical if canonical is not None else body.assigned_to
        old_assigned = ticket.assigned_to
        _record("assigned_to", old_assigned, new_assigned)
        ticket.assigned_to = new_assigned
        if ticket.assigned_user_id is not None:
            await _remember_label(ticket.assigned_user_id)
            _record("assigned_user_id", ticket.assigned_user_id, None)
            ticket.assigned_user_id = None
        # v0.17.0 — auto-capture route feedback when an agent-created
        # ticket is reassigned to a *different* persona. Best-effort:
        # feedback capture must never fail the reassignment.
        await maybe_capture_route_feedback(
            db=db,
            project_id=project_id,
            ticket=ticket,
            old_assigned=old_assigned,
            new_assigned=new_assigned,
            reassigned_by_user_id=user_id,
        )
    elif body.assigned_user_id is not None or body.assigned_user is not None:
        token = (
            body.assigned_user_id
            if body.assigned_user_id is not None
            else body.assigned_user
        )
        assignee = await resolve_assigned_user_id(db, project=project, value=token)
        user_labels[assignee.id] = assigned_user_label(assignee)
        await _remember_label(ticket.assigned_user_id)
        _record("assigned_user_id", ticket.assigned_user_id, assignee.id)
        ticket.assigned_user_id = assignee.id
        if ticket.assigned_to is not None:
            _record("assigned_to", ticket.assigned_to, None)
            ticket.assigned_to = None
    else:
        # No non-null assignment token: explicit null(s) unassign whichever
        # side the caller named. The dashboard always sends both keys, so a
        # both-null body clears both a persona and a person.
        if "assigned_to" in body_fields_set:
            _record("assigned_to", ticket.assigned_to, None)
            ticket.assigned_to = None
        if "assigned_user_id" in body_fields_set or "assigned_user" in body_fields_set:
            await _remember_label(ticket.assigned_user_id)
            _record("assigned_user_id", ticket.assigned_user_id, None)
            ticket.assigned_user_id = None
    if body.context_refs is not None:
        old_refs = json.loads(ticket.context_refs or "[]")
        _record("context_refs", old_refs, body.context_refs)
        ticket.context_refs = json.dumps(body.context_refs)
    if body.file_refs is not None:
        old_refs = json.loads(ticket.file_refs or "[]")
        _record("file_refs", old_refs, body.file_refs)
        ticket.file_refs = json.dumps(body.file_refs)
    if body.related_sessions is not None:
        old_refs = json.loads(ticket.related_sessions or "[]")
        _record("related_sessions", old_refs, body.related_sessions)
        ticket.related_sessions = json.dumps(body.related_sessions)
    if body.acceptance_criteria is not None:
        old_refs = json.loads(ticket.acceptance_criteria or "[]")
        _record("acceptance_criteria", old_refs, body.acceptance_criteria)
        ticket.acceptance_criteria = json.dumps(body.acceptance_criteria)
    if body.depends_on is not None:
        # depends_on lives in the TicketDependency join table, not on
        # the Ticket row. v1 semantic: full replacement.
        #
        # Codex R1 MED #1 — apply the same guards as the create path
        # (dedup → same-project validation → cycle check) BEFORE the
        # wipe so a malformed input never reaches the join table.
        from sessionfs.server.routes.tickets import (
            _check_dependency_cycle,
            _ticket_dependencies,
            _validate_dependencies_same_project,
        )

        deduped = list(dict.fromkeys(body.depends_on))
        await _validate_dependencies_same_project(db, project_id, deduped)
        await _check_dependency_cycle(db, ticket.id, deduped)

        old_deps = await _ticket_dependencies(db, ticket.id)
        _record("depends_on", old_deps, deduped)
        # Wipe + re-insert the dependency rows for this ticket.
        await db.execute(
            TicketDependency.__table__.delete().where(
                TicketDependency.ticket_id == ticket.id
            )
        )
        for dep_id in deduped:
            db.add(TicketDependency(ticket_id=ticket.id, depends_on_id=dep_id))

    # Scheduling fields: due_date (calendar date) and estimated_effort (a
    # Jira-duration string). An explicit null clears them, so both sides of
    # the body_fields_set fence are handled.
    if body.due_date is not None:
        _record("due_date", ticket.due_date, body.due_date)
        ticket.due_date = body.due_date
    elif "due_date" in body_fields_set:
        _record("due_date", ticket.due_date, None)
        ticket.due_date = None
    if body.estimated_effort is not None:
        _record("estimated_effort", ticket.estimated_effort, body.estimated_effort)
        ticket.estimated_effort = body.estimated_effort
    elif "estimated_effort" in body_fields_set:
        _record("estimated_effort", ticket.estimated_effort, None)
        ticket.estimated_effort = None

    # No-op: nothing actually changed. Skip the audit-write + diff
    # comment so empty PUTs don't pollute the audit trail.
    if not edits:
        return outcome

    # Per-field audit rows.
    now = datetime.now(timezone.utc)
    editing_persona = (
        ctx.active_persona if hasattr(ctx, "active_persona") else None
    )
    for field_name, old_v, new_v in edits:
        db.add(
            TicketEdit(
                id=f"te_{uuid.uuid4().hex[:16]}",
                ticket_id=ticket.id,
                edited_by_user_id=user_id,
                edited_by_persona=editing_persona,
                field_name=field_name,
                old_value=json.dumps(_jsonable(old_v)),
                new_value=json.dumps(_jsonable(new_v)),
                edited_at=now,
                lease_epoch=ticket.lease_epoch,
            )
        )

    # Append-only event ledger, one row per changed field, in the same
    # transaction as the audit rows above. Assignment and scheduling fields
    # get their dedicated verbs; everything else is a generic field_edited.
    for field_name, old_v, new_v in edits:
        if field_name == "assigned_to":
            await record_ticket_event(
                db,
                ticket,
                "assigned" if new_v else "unassigned",
                field="assigned_to",
                before={"persona": old_v} if old_v else None,
                after={"persona": new_v} if new_v else None,
                actor_type=actor_type,
                actor_user_id=user_id,
                actor_persona=editing_persona,
            )
        elif field_name == "assigned_user_id":
            await record_ticket_event(
                db,
                ticket,
                "assigned" if new_v else "unassigned",
                field="assigned_user_id",
                before={"user_id": old_v} if old_v else None,
                after={"user_id": new_v} if new_v else None,
                actor_type=actor_type,
                actor_user_id=user_id,
                actor_persona=editing_persona,
            )
        elif field_name == "due_date":
            await record_ticket_event(
                db,
                ticket,
                "due_date_set",
                field="due_date",
                before=old_v,
                after=new_v,
                actor_type=actor_type,
                actor_user_id=user_id,
                actor_persona=editing_persona,
            )
        elif field_name == "estimated_effort":
            await record_ticket_event(
                db,
                ticket,
                "estimate_set",
                field="estimated_effort",
                before=old_v,
                after=new_v,
                actor_type=actor_type,
                actor_user_id=user_id,
                actor_persona=editing_persona,
            )
        else:
            await record_ticket_event(
                db,
                ticket,
                "field_edited",
                field=field_name,
                before=old_v,
                after=new_v,
                actor_type=actor_type,
                actor_user_id=user_id,
                actor_persona=editing_persona,
            )

    # A non-editor's change to an approved ticket withdraws the approval.
    if not editor and ticket.status == "open":
        ticket.status = "suggested"
        ticket.updated_at = now
        db.add(
            TicketEdit(
                id=f"te_{uuid.uuid4().hex[:16]}",
                ticket_id=ticket.id,
                edited_by_user_id=user_id,
                edited_by_persona=editing_persona,
                field_name="status",
                old_value=json.dumps("open"),
                new_value=json.dumps("suggested"),
                edited_at=now,
                lease_epoch=ticket.lease_epoch,
            )
        )
        await record_ticket_event(
            db,
            ticket,
            "status_changed",
            field="status",
            before="open",
            after="suggested",
            actor_type=actor_type,
            actor_user_id=user_id,
            actor_persona=editing_persona,
        )
        db.add(
            TicketComment(
                id=f"tc_{uuid.uuid4().hex[:16]}",
                ticket_id=ticket.id,
                author_user_id=user_id,
                author_persona="system",
                content=(
                    "Changed by a non-member after approval; needs approval "
                    "again before it can be worked on."
                ),
                created_at=now,
            )
        )

    # Auto-posted diff comment so the change shows up in the human-
    # readable comment thread. `system` author identity per design doc.
    diff_summary = format_edit_diff_summary(
        edits, fence_applied=fence_applied, assigned_user_labels=user_labels
    )
    db.add(
        TicketComment(
            id=f"tc_{uuid.uuid4().hex[:16]}",
            ticket_id=ticket.id,
            author_user_id=user_id,
            author_persona="system",
            content=diff_summary,
            created_at=now,
        )
    )

    # Ticket watchers: a reassignment notifies watchers and hands the new
    # assignee — the persona's human owner or the assigned person — a
    # "your turn", auto-subscribing them.
    assignment_edit = next(
        (edit for edit in edits if edit[0] in ("assigned_to", "assigned_user_id")),
        None,
    )
    if assignment_edit is not None:
        field_name, _, new_value = assignment_edit
        if field_name == "assigned_user_id":
            # The edit records the user id; render the readable label for
            # the hand-off summary. The recipient is the id now on the ticket.
            new_assignee_user_id = ticket.assigned_user_id
            new_assignee_persona = None
            label = (
                user_labels.get(new_assignee_user_id)
                if new_assignee_user_id
                else None
            )
            summary = f"Assigned to {label}" if label else "Unassigned"
        else:
            from sessionfs.server.services.personas import visible_persona_org_id

            new_assigned_name = new_value if isinstance(new_value, str) else ""
            new_assignee_human = await ticket_watchers.map_persona_to_human(
                db,
                project_id=project_id,
                name=new_assigned_name,
                org_id=await visible_persona_org_id(db, project, user_id=user_id),
            )
            new_assignee_user_id = new_assignee_human[0] if new_assignee_human else None
            new_assignee_persona = new_assignee_human[1] if new_assignee_human else None
            summary = f"Assigned to {new_assigned_name}" if new_assigned_name else "Unassigned"
        # A suggested ticket isn't actionable: watchers hear about the
        # reassignment, but the assignee is subscribed and handed "your turn"
        # only when an editor approves it.
        hand_off = new_assignee_user_id is not None and ticket.status != "suggested"
        await ticket_watchers.emit_notifications(
            db,
            ticket=ticket,
            kind="assignment",
            summary=summary,
            actor_user_id=user_id,
            your_turn_user_id=new_assignee_user_id if hand_off else None,
            subscribe_users=(
                [(new_assignee_user_id, "assignee", new_assignee_persona)]
                if hand_off and new_assignee_user_id
                else None
            ),
        )
    return outcome


async def insert_ticket_comment(
    db: AsyncSession,
    *,
    project: Project,
    ticket_id: str,
    user_id: str,
    content: str,
    author_persona: str | None,
    session_id: str | None,
    lease_epoch: int | None,
    verdict_trusted: bool,
    auth: AuthContext | None = None,
) -> str:
    """Insert one ticket comment and notify watchers; the caller commits.

    Shared by ``POST …/comments`` and helper confirm: enforces the org's
    ``require_lease_epoch_on_ticket_writes`` setting, fences on
    ``lease_epoch`` when given (the insert only lands while the ticket's
    epoch still matches), subscribes the author and emits the comment (or
    trusted review-verdict) notification.  ``verdict_trusted`` is decided by
    the caller from server-derived identity, never from the body.

    ``auth`` is the route's credential (service keys are attributed to the
    key, not the minter); ``None`` means a plain user acting as ``user_id``.
    Returns the new comment id.
    """
    await assert_lease_required_mode(project.id, db, lease_epoch, auth=auth)

    actor_type = auth.actor_type if auth is not None else "user"
    service_key_id = auth.service_key_id if auth is not None else None
    service_key_name = auth.service_key_name if auth is not None else None
    is_service = auth is not None and auth.key_kind == "service"

    comment_id = f"tc_{uuid.uuid4().hex[:16]}"
    now = datetime.now(timezone.utc)
    source = select(
        literal(comment_id),
        literal(ticket_id),
        literal(user_id),
        literal(author_persona),
        literal(content),
        literal(session_id),
        literal(now),
        literal(actor_type),
        literal(service_key_id),
        literal(service_key_name),
        literal(verdict_trusted),
    ).where(
        Ticket.id == ticket_id,
        Ticket.project_id == project.id,
        *(
            (Ticket.lease_epoch == lease_epoch,)
            if lease_epoch is not None
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
                "actor_type",
                "service_key_id",
                "service_key_name",
                "verdict_trusted",
            ],
            source,
        )
    )
    ticket = (
        await db.execute(
            select(Ticket).where(
                Ticket.id == ticket_id, Ticket.project_id == project.id
            )
        )
    ).scalar_one_or_none()
    if getattr(result, "rowcount", 0) != 1:
        if ticket is None:
            raise HTTPException(404, "Ticket not found")
        assert_lease_epoch(ticket, lease_epoch)
        raise HTTPException(409, "Cannot add comment due to concurrent update")
    if ticket is None:  # pragma: no cover - the insert just matched it
        raise HTTPException(404, "Ticket not found")

    # Ticket watchers: the author follows (as a commenter, or as a trusted
    # reviewer when the comment is an authoritative verdict) and every other
    # watcher learns about the new comment. A service key acts on its
    # minter's behalf, so the minter is NOT auto-subscribed and the action
    # is attributed to the service key (actor_user_id=None) — the minter is
    # then notified like any other watcher instead of silently suppressed.
    if not is_service:
        await ticket_watchers.subscribe_guarded(
            db,
            ticket_id=ticket.id,
            user_id=user_id,
            source="reviewer" if verdict_trusted else "commenter",
            persona_name=author_persona,
        )
    actor_user_id = None if is_service else user_id

    # A trusted reviewer's comment is only a *review verdict* when it carries
    # the structured Codex header the review-state parser recognizes;
    # otherwise it is a plain comment.
    verdict = ticket_watchers.parse_verdict(content) if verdict_trusted else None
    if verdict is not None:
        from sessionfs.server.services.review_state import VERDICT_CHANGES

        changes = verdict == VERDICT_CHANGES
        verdict_label = "changes requested" if changes else "verified clean"
        # A trusted "changes requested" verdict sends the implementer back
        # to work; a clean verdict hands the reporter the accept decision.
        your_turn_user_id: str | None
        if changes:
            assignee_human = await ticket_watchers.map_persona_to_human(
                db,
                project_id=project.id,
                name=ticket.assigned_to or "",
                org_id=project.org_id,
            )
            your_turn_user_id = (
                assignee_human[0] if assignee_human is not None else None
            )
        else:
            your_turn_user_id = ticket.created_by_user_id
        await ticket_watchers.emit_notifications(
            db,
            ticket=ticket,
            kind="review_verdict",
            summary=f"Review verdict: {verdict_label}",
            actor_user_id=actor_user_id,
            actor_type=actor_type,
            service_key_name=service_key_name,
            your_turn_user_id=your_turn_user_id,
        )
    else:
        await ticket_watchers.emit_notifications(
            db,
            ticket=ticket,
            kind="comment",
            summary="New comment",
            actor_user_id=actor_user_id,
            actor_type=actor_type,
            service_key_name=service_key_name,
        )
    return comment_id
