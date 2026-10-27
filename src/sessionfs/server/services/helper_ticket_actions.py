"""Commit-free ticket mutations for helper write-action confirm.

The session helper only PROPOSES; ``routes/helper.py`` applies a confirmed
proposal by re-running the same rules the normal ticket routes use.  Those
routes own their ``db.commit()``, so the confirm path needs the mutation
without the commit — the confirm handler already transitioned the pending
``helper_actions`` row in this transaction and commits everything together
once every target is applied.

* ``create_ticket_action`` mirrors ``tickets.create_ticket`` (minus the
  service-key boundary; the helper is user-only).
* ``assign_ticket_action`` runs the PUT route's own write path
  (``services/ticket_writes.apply_ticket_update``) — edit-access rules,
  audit rows, re-approval, diff comment, notifications.
* ``create_comment_action`` runs the comment route's own insert
  (``services/ticket_writes.insert_ticket_comment``) — including the org's
  lease-epoch requirement — and never stores a trusted verdict.

Every function raises ``HTTPException`` on failure (same status codes the
routes return) so the confirm handler can record a per-target error and keep
going, exactly like the ``set_auto_narrative`` / ``trash_sessions`` targets.

The module also holds the helper's write-side validation shared by the
proposal tools and the confirm-time edits: draft limits, verdict-marker
detection and assignee resolution.
"""

from __future__ import annotations

import json
import re
import unicodedata
import uuid
from dataclasses import dataclass

from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server.auth.project_access import (
    load_project_for_user,
    user_can_edit_project,
    user_is_project_admin,
)
from sessionfs.server.db.models import AgentPersona, OrgMember, Project, Ticket, User
from sessionfs.server.services import ticket_watchers
from sessionfs.server.services.personas import (
    resolve_persona,
    visible_persona_org_id,
)
from sessionfs.server.services.ticket_writes import (
    apply_ticket_update,
    assert_ticket_fields_editable,
    authorize_ticket_edit,
    insert_ticket_comment,
)
from sessionfs.server.tier_gate import UserContext, check_project_feature

_VALID_KINDS = {"issue", "task"}
_VALID_PRIORITIES = {"critical", "high", "medium", "low"}

# Write limits for helper-drafted content.  The title and comment limits are
# the ticket routes' own (``TicketCreate`` 200, ``CommentCreate`` 10,000);
# ``TicketCreate`` has no description or criteria limit, so the helper sets
# its own bounds.  A draft over a limit is refused, never truncated.
MAX_TITLE_CHARS = 200
MAX_DESCRIPTION_CHARS = 20_000
MAX_CRITERIA = 30
MAX_CRITERION_CHARS = 1_000
MAX_COMMENT_CHARS = 10_000

# Shown on helper-created rows when the confirming user has no display name.
# Never the email: the line is visible to everyone who can read the ticket.
_ANONYMOUS_CONFIRMER = "a project member"

# A comment must never look like a review verdict: refuse a body with any
# line that starts with a Codex verdict header or a bare verdict keyword.
# Mirrors the header parser ``review_state._HEADER_RE`` plus the bare keywords
# a user might paste.  Matched per line (MULTILINE) after normalisation.
_VERDICT_LINE_RE = re.compile(
    r"^(?:"
    r"Codex\s+R\d+\s+(?:doc\s+)?review\s+on\s+tk_\w+\s*:"
    r"|VERIFIED[-_\s]*CLEAN"
    r"|CHANGES[\s_-]*REQUESTED"
    r"|NO[\s-]*CHANGES[\s-]*NEEDED"
    r"|APPROVED"
    r")",
    re.IGNORECASE | re.MULTILINE,
)
# Markdown decoration a line may lead with: emphasis, quotes, headings,
# code ticks, list bullets, strike-through — plus whitespace between them.
_MARKDOWN_PREFIX_RE = re.compile(r"^[\s*_>#`~+\-]+", re.MULTILINE)

VERDICT_REFUSAL = (
    "A helper comment cannot carry a review verdict header. Post the verdict "
    "through the review flow instead."
)


class HelperDraftError(ValueError):
    """A helper draft or edit breaks a write rule; the message says which."""


def display_name_for(user: User) -> str:
    """The confirmer name on the provenance line — never an email."""
    name = (user.display_name or "").strip()
    return name or _ANONYMOUS_CONFIRMER


def _provenance_line(conversation_id: str | None, display_name: str) -> str:
    """The final line appended to a helper-created description or comment."""
    conv = conversation_id or "unknown conversation"
    return (
        f"\n\nDrafted with the SessionFS helper from conversation {conv} "
        f"— confirmed by {display_name}"
    )


def _normalise_for_verdict_scan(text: str) -> str:
    """NFKC-fold, drop format (Cf) characters, then strip each line's
    markdown prefix, so ``**VERIFIED-CLEAN**``, ``> APPROVED``, ``# …`` or a
    zero-width-prefixed marker all reduce to the bare keyword."""
    folded = unicodedata.normalize("NFKC", text or "")
    folded = "".join(ch for ch in folded if unicodedata.category(ch) != "Cf")
    return _MARKDOWN_PREFIX_RE.sub("", folded)


def has_verdict_marker(text: str) -> bool:
    """True when any line of ``text`` starts with a review-verdict marker."""
    return _VERDICT_LINE_RE.search(_normalise_for_verdict_scan(text)) is not None


def check_comment_body(body: object) -> str:
    """Validate a helper comment body like ``CommentCreate`` does.

    Strips whitespace, requires content, caps at the route's 10,000
    characters, and refuses any verdict marker.  Returns the stripped body.
    """
    from sessionfs.server.routes.tickets import CommentCreate

    if not isinstance(body, str):
        raise HelperDraftError("comment body must be a string")
    try:
        content = CommentCreate(content=body).content
    except ValidationError as exc:
        raise HelperDraftError(_first_error(exc)) from None
    if has_verdict_marker(content):
        raise HelperDraftError(VERDICT_REFUSAL)
    return content


@dataclass
class TicketDraft:
    """A validated create-ticket draft (the fields the helper may set)."""

    title: str
    description: str
    acceptance_criteria: list[str]
    priority: str
    kind: str
    assigned_to: str | None


def _first_error(exc: ValidationError) -> str:
    errors = exc.errors()
    if not errors:
        return "invalid value"
    msg = str(errors[0].get("msg") or "invalid value")
    return msg.removeprefix("Value error, ")


def check_ticket_draft(
    *,
    title: object,
    description: object,
    acceptance_criteria: object,
    priority: object,
    kind: object,
    assigned_to: object,
) -> TicketDraft:
    """Validate a create-ticket draft through ``TicketCreate`` plus the
    helper's description/criteria limits.  Refuses (never truncates)."""
    from sessionfs.server.routes.tickets import TicketCreate

    if not isinstance(description, str):
        raise HelperDraftError("description must be a string")
    if len(description) > MAX_DESCRIPTION_CHARS:
        raise HelperDraftError(
            f"description must be {MAX_DESCRIPTION_CHARS:,} characters or fewer"
        )
    if not isinstance(acceptance_criteria, list) or not all(
        isinstance(c, str) for c in acceptance_criteria
    ):
        raise HelperDraftError("acceptance_criteria must be a list of strings")
    criteria = [c.strip() for c in acceptance_criteria if c.strip()]
    if len(criteria) > MAX_CRITERIA:
        raise HelperDraftError(f"at most {MAX_CRITERIA} acceptance criteria")
    for c in criteria:
        if len(c) > MAX_CRITERION_CHARS:
            raise HelperDraftError(
                f"each acceptance criterion must be {MAX_CRITERION_CHARS:,} "
                "characters or fewer"
            )
    if assigned_to is not None and not isinstance(assigned_to, str):
        raise HelperDraftError("assigned_to must be a string")
    try:
        model = TicketCreate(
            title=title,
            description=description,
            priority=priority if priority is not None else "medium",
            kind=kind if kind is not None else "task",
            acceptance_criteria=criteria,
            assigned_to=assigned_to,
        )
    except ValidationError as exc:
        raise HelperDraftError(_first_error(exc)) from None
    return TicketDraft(
        title=model.title,
        description=model.description,
        acceptance_criteria=list(model.acceptance_criteria),
        priority=model.priority,
        kind=model.kind,
        assigned_to=model.assigned_to,
    )


async def _represented_personas_for_human(
    db: AsyncSession, project: Project, name: str
) -> list[str]:
    """Active org personas whose represented human's display name or email
    matches ``name`` (case-insensitive), among the project's org members."""
    if project.org_id is None:
        return []
    member_ids = list(
        (
            await db.execute(
                select(User.id)
                .join(OrgMember, OrgMember.user_id == User.id)
                .where(
                    OrgMember.org_id == project.org_id,
                    or_(
                        func.lower(User.display_name) == name.lower(),
                        func.lower(User.email) == name.lower(),
                    ),
                )
            )
        ).scalars().all()
    )
    if not member_ids:
        return []
    rows = (
        await db.execute(
            select(AgentPersona.name)
            .where(
                AgentPersona.org_id == project.org_id,
                AgentPersona.represented_user_id.in_(member_ids),
                AgentPersona.is_active.is_(True),
            )
            .order_by(AgentPersona.name)
        )
    ).scalars().all()
    return list(rows)


async def resolve_assignee(
    db: AsyncSession, *, project: Project, user_id: str, name: str
) -> str:
    """Resolve an assignment target to a canonical active persona name.

    Persona name first (project scope, then the org the caller can see), then
    a human among the project's org members (display name/email) to their
    represented persona (#81).  No match or an ambiguous human raises
    ``HelperDraftError`` (listing the candidates when ambiguous).
    """
    name = name.strip()
    if not name:
        raise HelperDraftError("assignee cannot be empty")
    visible_org = await visible_persona_org_id(db, project, user_id=user_id)
    row = await resolve_persona(
        db, project.id, name, org_id=visible_org, active_only=True, case_insensitive=True
    )
    if row is not None:
        return row.name
    if visible_org is None:
        raise HelperDraftError(
            f"No active persona named {name!r} is visible in this project"
        )
    candidates = await _represented_personas_for_human(db, project, name)
    if len(candidates) == 1:
        return candidates[0]
    if len(candidates) > 1:
        raise HelperDraftError(
            f"Ambiguous assignment target {name!r}: candidates {', '.join(sorted(candidates))}"
        )
    raise HelperDraftError(f"No persona or org member matches {name!r}")


async def check_reassignment_allowed(
    db: AsyncSession, *, ticket: Ticket, project: Project, user_id: str
) -> str | None:
    """Why ``user_id`` can't reassign ``ticket`` now, else ``None``.

    The proposal-time form of the assign route's rules: the ticket creator
    or a project admin, and a non-editor can't change the assignee once the
    ticket is past suggested/open or while a work queue holds it.
    """
    try:
        await authorize_ticket_edit(db, ticket=ticket, project=project, user_id=user_id)
        await assert_ticket_fields_editable(
            db, ticket=ticket, project=project, user_id=user_id, fields=["assigned_to"]
        )
    except HTTPException as exc:
        detail = exc.detail
        if isinstance(detail, dict):
            return str(detail.get("message") or "not allowed")
        return str(detail)
    return None


async def _canonical_persona_name(
    db: AsyncSession,
    project: Project,
    name: str,
    *,
    user_id: str,
) -> str | None:
    """Best-effort normalize ``name`` to a canonical active persona name.

    Mirrors ``tickets.create_ticket``'s normalisation (project scope then the
    org the caller can see), returning ``None`` when no case-insensitive
    active match exists so the caller can keep the value as-is.
    """
    visible_org = await visible_persona_org_id(db, project, user_id=user_id)
    row = await resolve_persona(
        db,
        project.id,
        name,
        org_id=visible_org,
        active_only=True,
        case_insensitive=True,
    )
    return row.name if row is not None else None


async def create_ticket_action(
    db: AsyncSession,
    *,
    user_id: str,
    ctx: UserContext,
    project_id: str,
    title: str,
    description: str,
    priority: str,
    kind: str,
    assigned_to: str | None,
    acceptance_criteria: list[str],
    conversation_id: str | None,
    display_name: str,
) -> str:
    """Create a ticket as the confirming user; caller owns the commit.

    Returns the new ticket id.  A captured-session reader can create but the
    ticket starts ``suggested``; an editor's ticket starts ``open``.
    """
    if kind not in _VALID_KINDS:
        raise HTTPException(400, f"kind must be one of: {sorted(_VALID_KINDS)}")
    if priority not in _VALID_PRIORITIES:
        raise HTTPException(400, f"priority must be one of: {sorted(_VALID_PRIORITIES)}")

    project = await load_project_for_user(project_id, db, user_id)
    await check_project_feature(ctx, "agent_tickets", project, db)

    from sessionfs.server.services.scope_locks import lock_project_scope_authorized

    await lock_project_scope_authorized(
        db, project, user_id=user_id, ctx=ctx, feature="agent_tickets"
    )

    if kind == "issue" and not await user_is_project_admin(db, user_id, project):
        raise HTTPException(
            403,
            "Only the project owner or a project admin may file "
            "kind='issue' tickets. Issues are PM-triaged rollup containers.",
        )

    initial_status = "open"
    if not await user_can_edit_project(db, user_id, project):
        initial_status = "suggested"

    normalized_assigned_to = assigned_to or None
    if normalized_assigned_to is not None:
        canonical = await _canonical_persona_name(
            db, project, normalized_assigned_to, user_id=user_id
        )
        if canonical is not None:
            normalized_assigned_to = canonical

    ticket = Ticket(
        id=f"tk_{uuid.uuid4().hex[:16]}",
        project_id=project_id,
        title=title,
        description=description + _provenance_line(conversation_id, display_name),
        priority=priority,
        assigned_to=normalized_assigned_to,
        created_by_user_id=user_id,
        created_by_session_id=None,
        created_by_persona=None,
        status=initial_status,
        context_refs="[]",
        file_refs="[]",
        related_sessions="[]",
        acceptance_criteria=json.dumps(acceptance_criteria),
        actor_type="user",
        service_key_id=None,
        service_key_name=None,
        kind=kind,
        parent_ticket_id=None,
    )
    db.add(ticket)
    await db.flush()

    await ticket_watchers.subscribe_guarded(
        db, ticket_id=ticket.id, user_id=user_id, source="creator"
    )
    if normalized_assigned_to is not None and initial_status != "suggested":
        assignee_human = await ticket_watchers.map_persona_to_human(
            db,
            project_id=project_id,
            name=normalized_assigned_to,
            org_id=await visible_persona_org_id(db, project, user_id=user_id),
        )
        if assignee_human is not None:
            assignee_user_id, persona_name = assignee_human
            await ticket_watchers.subscribe_guarded(
                db,
                ticket_id=ticket.id,
                user_id=assignee_user_id,
                source="assignee",
                persona_name=persona_name,
            )
            await ticket_watchers.emit_notifications(
                db,
                ticket=ticket,
                kind="assignment",
                summary=f"Assigned to {normalized_assigned_to}",
                actor_user_id=user_id,
                your_turn_user_id=assignee_user_id,
            )
    return ticket.id


async def assign_ticket_action(
    db: AsyncSession,
    *,
    user_id: str,
    ctx: UserContext,
    ticket_id: str,
    assigned_to: str,
) -> None:
    """Assign a ticket as the confirming user; caller commits.

    Exactly the PUT route's assignment path: creator-or-admin authorization,
    the project scope lock with a re-read and re-authorization under it, then
    ``apply_ticket_update`` (non-editor edit-access rules and re-approval,
    ``TicketEdit`` audit rows, the system diff comment, route feedback and
    notifications).
    """
    from sessionfs.server.routes.tickets import TicketUpdate

    ticket = (
        await db.execute(select(Ticket).where(Ticket.id == ticket_id))
    ).scalar_one_or_none()
    if ticket is None:
        raise HTTPException(404, "Ticket not found")

    project = await load_project_for_user(ticket.project_id, db, user_id)
    await check_project_feature(ctx, "agent_tickets", project, db)
    await authorize_ticket_edit(db, ticket=ticket, project=project, user_id=user_id)

    from sessionfs.server.services.scope_locks import lock_project_scope_authorized

    await lock_project_scope_authorized(
        db, project, user_id=user_id, ctx=ctx, feature="agent_tickets"
    )

    await db.refresh(ticket)
    if ticket.project_id != project.id:
        raise HTTPException(404, "Ticket not found")
    await authorize_ticket_edit(db, ticket=ticket, project=project, user_id=user_id)

    await apply_ticket_update(
        db,
        project=project,
        ticket=ticket,
        user_id=user_id,
        ctx=ctx,
        body=TicketUpdate(assigned_to=assigned_to),
    )


async def create_comment_action(
    db: AsyncSession,
    *,
    user_id: str,
    ctx: UserContext,
    ticket_id: str,
    body: str,
    conversation_id: str | None,
    display_name: str,
) -> str:
    """Post a comment on a ticket as the confirming user; caller commits.

    Returns the comment id.  Goes through the comment route's own insert
    (``insert_ticket_comment``), so the org's lease-epoch requirement
    applies.  The comment is always a plain user comment: ``verdict_trusted``
    stays False (even for a registered trusted reviewer) and a body with a
    verdict marker is refused.
    """
    if has_verdict_marker(body):
        raise HTTPException(400, VERDICT_REFUSAL)

    ticket = (
        await db.execute(select(Ticket).where(Ticket.id == ticket_id))
    ).scalar_one_or_none()
    if ticket is None:
        raise HTTPException(404, "Ticket not found")

    project = await load_project_for_user(ticket.project_id, db, user_id)
    await check_project_feature(ctx, "agent_tickets", project, db)

    return await insert_ticket_comment(
        db,
        project=project,
        ticket_id=ticket.id,
        user_id=user_id,
        content=body + _provenance_line(conversation_id, display_name),
        author_persona=None,
        session_id=None,
        lease_epoch=None,
        verdict_trusted=False,
        auth=None,
    )
