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
from datetime import date

from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server.auth.project_access import (
    load_project_for_user,
    user_can_edit_project,
    user_is_project_admin,
)
from sessionfs.server.db.models import (
    AgentPersona,
    JiraTemplate,
    JiraTemplateVersion,
    OrgMember,
    OrgTrackerConnection,
    Project,
    Ticket,
    TicketTrackerLink,
    TrackerProjectMapping,
    User,
)
from sessionfs.server.integrations.trackers.templates import is_builtin_slug
from sessionfs.server.services import jira_sync, ticket_watchers
from sessionfs.server.services.jira_links import JiraLinkError, link_ticket_to_jira
from sessionfs.server.services.personas import (
    resolve_persona,
    visible_persona_org_id,
)
from sessionfs.server.services.review_state import REVIEWER_NAME_PATTERN
from sessionfs.server.services.ticket_events import record_ticket_event
from sessionfs.server.services.ticket_writes import (
    apply_ticket_update,
    assert_ticket_fields_editable,
    assigned_user_label,
    authorize_ticket_edit,
    insert_ticket_comment,
    resolve_assigned_user_id,
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
# line that starts with a review verdict header (any reviewer name) or a bare
# verdict keyword. Mirrors the header parser ``review_state._HEADER_RE`` plus
# the bare keywords a user might paste.  Matched per line (MULTILINE) after
# normalisation.
_VERDICT_LINE_RE = re.compile(
    rf"^(?:"
    rf"{REVIEWER_NAME_PATTERN}\s+R\d+\s+(?:doc\s+)?review\s+on\s+tk_\w+\s*:"
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
    """True when any line of ``text`` starts with a review-verdict marker.

    Scans BOTH the raw text and the NFKC-normalised text. The normalised
    scan catches markdown/format decoration (``**VERIFIED-CLEAN**``, a
    zero-width prefix), while the raw scan catches a ticket-id token whose
    NFKC fold breaks the word run — e.g. ``tk_a¼`` folds to ``tk_a1⁄4``,
    so the normalised ``tk_\\w+`` no longer reaches the colon while the raw
    parser still matches ``¼`` as a word character.
    """
    return (
        _VERDICT_LINE_RE.search(text or "")
        or _VERDICT_LINE_RE.search(_normalise_for_verdict_scan(text))
    ) is not None


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
    due_date: date | None
    estimated_effort: str | None


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
    due_date: object = None,
    estimated_effort: object = None,
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
            due_date=due_date,
            estimated_effort=estimated_effort,
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
        due_date=model.due_date,
        estimated_effort=model.estimated_effort,
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


async def _assignable_users_by_display_name(
    db: AsyncSession, project: Project, name: str
) -> list[User]:
    """Active users whose display name exactly matches ``name`` (case-
    insensitive), among the assignable population (the project owner plus the
    project's org members), and who hold read standing on ``project``.

    Read standing is the same predicate as the assignable-users endpoint:
    owner, org member of an unrestricted org, org admin/owner, or a group
    grant — never a captured-session-only reader.  The owner/org-member
    restriction bounds the per-user read-standing checks to the same
    population the assignable-users endpoint lists, and the LIMIT caps a
    display-name collision so it can never scan the whole users table.
    """
    from sessionfs.server.auth.project_access import user_has_read_standing

    population = User.id == project.owner_id
    if project.org_id is not None:
        population = or_(
            User.id == project.owner_id,
            User.id.in_(
                select(OrgMember.user_id).where(OrgMember.org_id == project.org_id)
            ),
        )
    rows = (
        await db.execute(
            select(User)
            .where(
                func.lower(User.display_name) == name.lower(),
                User.is_active.is_(True),
                population,
            )
            .limit(20)
        )
    ).scalars().all()
    eligible: list[User] = []
    for user in rows:
        if await user_has_read_standing(db, user.id, project):
            eligible.append(user)
    return eligible


@dataclass
class ResolvedAssignee:
    """A proposal-time assignment target: a persona or a person.

    ``kind`` is ``"persona"`` (``persona_name`` set) or ``"person"``
    (``user_id`` set, with ``display_name`` for the audit/summary label).
    """

    kind: str
    persona_name: str | None = None
    user_id: str | None = None
    display_name: str | None = None


async def resolve_assignee(
    db: AsyncSession, *, project: Project, user_id: str, name: str
) -> ResolvedAssignee:
    """Resolve an assignment target to a persona or a person.

    Precedence:

    1. a persona whose name matches exactly (project scope, then the org the
       caller can see);
    2. a person email or user id (direct assignment, GitHub #96);
    3. a human's display name to their represented persona when exactly one
       active persona represents them (#81), refusing when several do;
    4. otherwise a display name to the assignable teammate (case-insensitive
       exact, GitHub #96), refusing when several match;
    5. nothing matches — refuse.

    No match or an ambiguous target raises ``HelperDraftError`` (listing the
    candidates when ambiguous; candidate emails appear only when the caller
    could see them via the assignable-users endpoint).
    """
    name = name.strip()
    if not name:
        raise HelperDraftError("assignee cannot be empty")
    visible_org = await visible_persona_org_id(db, project, user_id=user_id)
    row = await resolve_persona(
        db, project.id, name, org_id=visible_org, active_only=True, case_insensitive=True
    )
    if row is not None:
        return ResolvedAssignee(kind="persona", persona_name=row.name)
    # A person email or user id assigns directly to that teammate (GitHub #96).
    is_email = "@" in name
    is_user_id = False
    try:
        uuid.UUID(name)
        is_user_id = True
    except ValueError:
        pass
    if is_email or is_user_id:
        try:
            user = await resolve_assigned_user_id(db, project=project, value=name)
        except HTTPException as exc:
            detail = exc.detail
            message = detail.get("message") if isinstance(detail, dict) else str(detail)
            raise HelperDraftError(message) from None
        return ResolvedAssignee(
            kind="person",
            user_id=user.id,
            display_name=assigned_user_label(user),
        )
    # A human's display name resolves to their represented persona (#81) when
    # exactly one active org persona represents them; several refuse. This
    # runs before the display-name person path so a represented human keeps
    # resolving to their persona.
    if visible_org is not None:
        candidates = await _represented_personas_for_human(db, project, name)
        if len(candidates) == 1:
            return ResolvedAssignee(kind="persona", persona_name=candidates[0])
        if len(candidates) > 1:
            raise HelperDraftError(
                f"Ambiguous assignment target {name!r}: candidates {', '.join(sorted(candidates))}"
            )
    # Otherwise a display name assigns directly to that person (GitHub #96).
    # One exact case-insensitive match → person; several → refuse and list
    # candidates; none → fall through to the final refusal.
    matches = await _assignable_users_by_display_name(db, project, name)
    if len(matches) == 1:
        return ResolvedAssignee(
            kind="person",
            user_id=matches[0].id,
            display_name=assigned_user_label(matches[0]),
        )
    if len(matches) > 1:
        from sessionfs.server.auth.project_access import user_has_read_standing

        show_email = await user_has_read_standing(db, user_id, project)

        def _label(user: User) -> str:
            base = user.display_name or user.email
            return f"{base} ({user.email})" if (show_email and user.email) else base

        raise HelperDraftError(
            f"Ambiguous assignment target {name!r}: "
            f"candidates {', '.join(sorted(_label(u) for u in matches))}"
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


async def _project_for_actor(
    db: AsyncSession, project_id: str, user_id: str, auth
) -> Project:
    """Load the project for a user, or for a service key by its org boundary.

    ``auth`` is optional: without it (the helper's own paths) this is exactly
    ``load_project_for_user``. A service key is authorized by its org and
    allowlist, never by its minter's personal project standing.
    """
    if auth is not None and auth.key_kind == "service":
        from sessionfs.server.auth.dependencies import (
            assert_service_key_can_access_project,
        )

        project = (
            await db.execute(select(Project).where(Project.id == project_id))
        ).scalar_one_or_none()
        if project is None:
            raise HTTPException(404, "Project not found")
        await assert_service_key_can_access_project(db, auth, project)
        return project
    return await load_project_for_user(project_id, db, user_id)


async def _visible_org(db: AsyncSession, project: Project, user_id: str, auth) -> str | None:
    if auth is not None:
        return await visible_persona_org_id(
            db,
            project,
            user_id=user_id,
            key_kind=auth.key_kind,
            key_org_id=auth.org_id,
        )
    return await visible_persona_org_id(db, project, user_id=user_id)


async def _canonical_persona_name(
    db: AsyncSession,
    project: Project,
    name: str,
    *,
    user_id: str,
    auth=None,
) -> str | None:
    """Best-effort normalize ``name`` to a canonical active persona name.

    Mirrors ``tickets.create_ticket``'s normalisation (project scope then the
    org the caller can see), returning ``None`` when no case-insensitive
    active match exists so the caller can keep the value as-is.
    """
    visible_org = await _visible_org(db, project, user_id, auth)
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
    assigned_user_id: str | None,
    acceptance_criteria: list[str],
    conversation_id: str | None,
    display_name: str,
    due_date: date | str | None = None,
    estimated_effort: str | None = None,
    context_refs: list[str] | None = None,
    append_provenance: bool = True,
    actor_type: str = "user",
    service_key_id: str | None = None,
    service_key_name: str | None = None,
    auth=None,
    require_editor: bool = False,
) -> str:
    """Create a ticket as the confirming user; caller owns the commit.

    Returns the new ticket id.  A captured-session reader can create but the
    ticket starts ``suggested``; an editor's ticket starts ``open``.
    ``assigned_user_id`` is a direct-to-person assignment (GitHub #96),
    re-validated here against the project's assignee eligibility.
    ``due_date`` / ``estimated_effort`` are already validated by
    ``check_ticket_draft`` at propose and confirm time; ``due_date`` may
    arrive as its stored ISO string, so it is parsed back to a ``date`` here.
    """
    if kind not in _VALID_KINDS:
        raise HTTPException(400, f"kind must be one of: {sorted(_VALID_KINDS)}")
    if priority not in _VALID_PRIORITIES:
        raise HTTPException(400, f"priority must be one of: {sorted(_VALID_PRIORITIES)}")
    if isinstance(due_date, str):
        due_date = date.fromisoformat(due_date)

    project = await _project_for_actor(db, project_id, user_id, auth)
    await check_project_feature(ctx, "agent_tickets", project, db)

    from sessionfs.server.services.scope_locks import lock_project_scope_authorized

    await lock_project_scope_authorized(
        db, project, user_id=user_id, ctx=ctx, feature="agent_tickets", auth=auth,
        edit=require_editor,
    )

    if kind == "issue" and not await user_is_project_admin(db, user_id, project):
        raise HTTPException(
            403,
            "Only the project owner or a project admin may file "
            "kind='issue' tickets. Issues are PM-triaged rollup containers.",
        )

    initial_status = "open"
    # A service key is authorized by its scopes, not the minter's project
    # standing (same rule as the ticket route), so it is never demoted.
    if actor_type != "service_key" and not await user_can_edit_project(
        db, user_id, project
    ):
        initial_status = "suggested"

    normalized_assigned_to = assigned_to or None
    if normalized_assigned_to is not None:
        canonical = await _canonical_persona_name(
            db, project, normalized_assigned_to, user_id=user_id, auth=auth
        )
        if canonical is not None:
            normalized_assigned_to = canonical

    resolved_assigned_user: User | None = None
    if assigned_user_id:
        resolved_assigned_user = await resolve_assigned_user_id(
            db, project=project, value=assigned_user_id
        )

    ticket = Ticket(
        id=f"tk_{uuid.uuid4().hex[:16]}",
        project_id=project_id,
        title=title,
        description=(
            description + _provenance_line(conversation_id, display_name)
            if append_provenance
            else description
        ),
        priority=priority,
        assigned_to=normalized_assigned_to,
        assigned_user_id=(
            resolved_assigned_user.id if resolved_assigned_user is not None else None
        ),
        created_by_user_id=user_id,
        created_by_session_id=None,
        created_by_persona=None,
        status=initial_status,
        context_refs=json.dumps(context_refs or []),
        file_refs="[]",
        related_sessions="[]",
        acceptance_criteria=json.dumps(acceptance_criteria),
        actor_type=actor_type,
        service_key_id=service_key_id,
        service_key_name=service_key_name,
        kind=kind,
        parent_ticket_id=None,
        due_date=due_date,
        estimated_effort=estimated_effort,
    )
    db.add(ticket)
    await db.flush()

    # The ticket timeline: the same create-time events the ticket route
    # writes, attributed to the confirming user.
    if actor_type == "service_key":
        # The timeline vocabulary says ``service``; a key has no user actor.
        actor: dict[str, str | None] = {
            "actor_type": "service",
            "actor_user_id": None,
            "actor_label": service_key_name,
        }
    else:
        actor = {"actor_type": "user", "actor_user_id": user_id}
    await record_ticket_event(db, ticket, "created", **actor)
    if due_date is not None:
        await record_ticket_event(
            db,
            ticket,
            "due_date_set",
            field="due_date",
            after=due_date,
            **actor,
        )
    if estimated_effort is not None:
        await record_ticket_event(
            db,
            ticket,
            "estimate_set",
            field="estimated_effort",
            after=estimated_effort,
            **actor,
        )
    if resolved_assigned_user is not None:
        await record_ticket_event(
            db,
            ticket,
            "assigned",
            field="assigned_user_id",
            after={"user_id": resolved_assigned_user.id},
            **actor,
        )
    elif normalized_assigned_to is not None:
        await record_ticket_event(
            db,
            ticket,
            "assigned",
            field="assigned_to",
            after={"persona": normalized_assigned_to},
            **actor,
        )

    if actor_type != "service_key":
        # A service key acts on its minter's behalf: the minter is not
        # auto-subscribed (same rule as the ticket route).
        await ticket_watchers.subscribe_guarded(
            db, ticket_id=ticket.id, user_id=user_id, source="creator"
        )
    if initial_status != "suggested":
        # Person assignment wins outright; a persona assignment maps to its
        # human owner only when the mapping is unambiguous.
        notify_user_id: str | None = None
        notify_persona_name: str | None = None
        notify_summary: str | None = None
        if resolved_assigned_user is not None:
            notify_user_id = resolved_assigned_user.id
            notify_summary = (
                f"Assigned to {assigned_user_label(resolved_assigned_user)}"
            )
        elif normalized_assigned_to is not None:
            assignee_human = await ticket_watchers.map_persona_to_human(
                db,
                project_id=project_id,
                name=normalized_assigned_to,
                org_id=await _visible_org(db, project, user_id, auth),
            )
            if assignee_human is not None:
                notify_user_id, notify_persona_name = assignee_human
                notify_summary = f"Assigned to {normalized_assigned_to}"
        if notify_user_id is not None and notify_summary is not None:
            await ticket_watchers.subscribe_guarded(
                db,
                ticket_id=ticket.id,
                user_id=notify_user_id,
                source="assignee",
                persona_name=notify_persona_name,
            )
            await ticket_watchers.emit_notifications(
                db,
                ticket=ticket,
                kind="assignment",
                summary=notify_summary,
                actor_user_id=None if actor_type == "service_key" else user_id,
                actor_type=actor_type,
                service_key_name=service_key_name,
                your_turn_user_id=notify_user_id,
            )
    return ticket.id


async def assign_ticket_action(
    db: AsyncSession,
    *,
    user_id: str,
    ctx: UserContext,
    ticket_id: str,
    assigned_to: str,
    assigned_user_id: str | None = None,
) -> None:
    """Assign a ticket as the confirming user; caller commits.

    Exactly the PUT route's assignment path: creator-or-admin authorization,
    the project scope lock with a re-read and re-authorization under it, then
    ``apply_ticket_update`` (non-editor edit-access rules and re-approval,
    ``TicketEdit`` audit rows, the system diff comment, route feedback and
    notifications). ``assigned_user_id`` is a direct-to-person assignment.
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

    update = (
        TicketUpdate(assigned_user_id=assigned_user_id)
        if assigned_user_id
        else TicketUpdate(assigned_to=assigned_to)
    )
    await apply_ticket_update(
        db,
        project=project,
        ticket=ticket,
        user_id=user_id,
        ctx=ctx,
        body=update,
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
    send_to_jira: bool = False,
    issue_key: str | None = None,
) -> tuple[str, list[str]]:
    """Post a comment on a ticket as the confirming user; caller commits.

    Returns the comment id plus the ids of any outbound Jira sync events
    queued for this comment (empty when ``send_to_jira`` is false).  Goes
    through the comment route's own insert (``insert_ticket_comment``), so the
    org's lease-epoch requirement applies.  The comment is always a plain user
    comment: ``verdict_trusted`` stays False (even for a registered trusted
    reviewer) and a body with a verdict marker is refused.

    When ``send_to_jira`` is true, the Jira preconditions are re-checked before
    the comment is written: editor access, the ``jira_integration`` plan gate,
    a live link that still points at the issue key the proposal pinned, and
    outbound comment forwarding.  A removed link or disabled forwarding raises
    ``jira_comment_sync_off``; a link re-pointed at a different issue since the
    proposal raises ``jira_link_changed``; lost editor access raises
    ``jira_edit_required``.  Every failure raises before ``insert_ticket_comment``
    runs, so the caller's later commit writes nothing (the helper confirm handler
    does not roll back on its own).  Authorization runs first, so a reader who
    kept a captured session never learns the link was removed or re-pointed.
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

    jira_event_ids: list[str] = []
    if send_to_jira:
        # Authorize before touching the live link, so a reader with a captured
        # session can't distinguish "removed", "re-pointed" or "disabled".
        if not await user_can_edit_project(db, user_id, project):
            raise HTTPException(
                403,
                {
                    "error": "jira_edit_required",
                    "detail": "Posting to Jira requires editor access.",
                },
            )
        await check_project_feature(ctx, "jira_integration", project, db)
        target = await jira_sync._sync_target(db, ticket.id)
        if target is None:
            raise HTTPException(
                409,
                {
                    "error": "jira_comment_sync_off",
                    "detail": "The ticket's Jira link was removed.",
                },
            )
        link, _conn = target
        if issue_key and link.issue_key != issue_key:
            raise HTTPException(
                409,
                {
                    "error": "jira_link_changed",
                    "detail": (
                        f"The ticket's Jira link now points at {link.issue_key}, "
                        f"not the {issue_key} this comment was proposed against."
                    ),
                },
            )
        if not await jira_sync.comment_forwarding_enabled(db, ticket.id):
            raise HTTPException(
                409,
                {
                    "error": "jira_comment_sync_off",
                    "detail": "Jira comment forwarding is not enabled.",
                },
            )

    comment_id = await insert_ticket_comment(
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

    if send_to_jira:
        jira_event_ids = await jira_sync.enqueue_comment(
            db, ticket, comment_id, reason="helper_confirm"
        )

    return comment_id, jira_event_ids


async def link_jira_action(
    db: AsyncSession,
    *,
    user_id: str,
    ctx: UserContext,
    ticket_id: str,
    issue_key: str,
) -> TicketTrackerLink:
    """Link a ticket to an existing Jira issue; caller commits.

    Re-checks the proposal's preconditions (editor standing, the
    ``jira_integration`` plan gate, a live connection + project mapping) and
    then runs the shared link service, which reads the remote issue and inserts
    the link row.  Raises ``HTTPException`` for the gates (so the confirm
    handler records the phase-1 codes) and ``JiraLinkError`` for the remote or
    race failures.
    """
    ticket = (
        await db.execute(select(Ticket).where(Ticket.id == ticket_id))
    ).scalar_one_or_none()
    if ticket is None:
        raise HTTPException(404, "Ticket not found")

    project = await load_project_for_user(ticket.project_id, db, user_id)
    if not await user_can_edit_project(db, user_id, project):
        raise HTTPException(
            403,
            {
                "error": "jira_edit_required",
                "detail": "Linking to Jira requires editor access.",
            },
        )
    await check_project_feature(ctx, "jira_integration", project, db)

    if project.org_id is None:
        raise HTTPException(
            409,
            {"error": "jira_not_connected", "detail": "This project is not org-scoped."},
        )
    conn = (
        await db.execute(
            select(OrgTrackerConnection).where(
                OrgTrackerConnection.org_id == project.org_id
            )
        )
    ).scalar_one_or_none()
    if conn is None or not conn.enabled:
        raise HTTPException(
            409,
            {
                "error": "jira_not_connected",
                "detail": "This organization has not connected Jira.",
            },
        )
    mapping = (
        await db.execute(
            select(TrackerProjectMapping).where(
                TrackerProjectMapping.project_id == project.id,
                TrackerProjectMapping.org_id == project.org_id,
            )
        )
    ).scalar_one_or_none()
    if mapping is None:
        raise HTTPException(
            409,
            {
                "error": "jira_project_unmapped",
                "detail": "Ask your admin to add a mapping",
            },
        )

    return await link_ticket_to_jira(
        db,
        project=project,
        ticket=ticket,
        conn=conn,
        mapping=mapping,
        issue_key=issue_key,
        actor_user_id=user_id,
        actor_type="user",
    )


async def jira_project_key_override_error(
    db: AsyncSession, *, org_id: str, jira_project_key: str, user_id: str
) -> str | None:
    """None when the caller may edit a project mapped to ``jira_project_key``.

    The helper's safe default (GitHub #113 C): an override is allowed only when
    some SessionFS project mapping in the org uses the key AND the caller has
    editor standing on at least one such project.  Merely mapping the key is not
    enough — the caller must be able to publish into the project it maps to.
    """
    key = jira_project_key.strip().upper()
    if not key:
        return "jira_project_key is empty"
    # One generic refusal for every disallowed key so probing can never confirm
    # whether a key is unmapped or merely mapped to a project the caller can't
    # edit (GitHub #113 M1).
    generic = f"jira_project_key {jira_project_key!r} is not a Jira project you can publish to"
    project_ids = (
        await db.execute(
            select(TrackerProjectMapping.project_id).where(
                TrackerProjectMapping.org_id == org_id,
                func.upper(TrackerProjectMapping.jira_project_key) == key,
            )
        )
    ).scalars().all()
    if not project_ids:
        return generic
    for project_id in project_ids:
        project = await db.get(Project, project_id)
        if project is not None and await user_can_edit_project(db, user_id, project):
            return None
    return generic


async def jira_template_override_error(
    db: AsyncSession, *, org_id: str, slug: str
) -> str | None:
    """None when ``slug`` is a built-in or an active org-custom Jira template.

    An org custom template wins over a built-in that shares its slug, so the
    custom row is checked first (it is versioned and pinned like any custom).
    """
    row = await _get_org_template(db, org_id, slug)
    if row is None:
        if is_builtin_slug(slug):
            return None
        return f"unknown Jira template {slug!r}"
    if row.archived_at is not None:
        return f"Jira template {slug!r} is archived and can't be newly selected"
    return None


async def jira_template_override_version(
    db: AsyncSession, *, org_id: str, slug: str
) -> int | None:
    """The current version of an org-custom template, or None for built-ins."""
    row = await _get_org_template(db, org_id, slug)
    if row is None:
        return None
    return row.version


async def jira_template_changed_error(
    db: AsyncSession,
    *,
    org_id: str,
    slug: str,
    version: int | None,
    builtin: bool | None = None,
) -> str | None:
    """None when the proposed template is still publishable at confirm.

    A built-in (no org custom row) has no version and never changes, so it is
    always publishable.  A custom template publishes with its PINNED version
    snapshot; ``jira_template_changed`` is refused only when that version no
    longer exists (including the template being deleted) or the template was
    archived since it was proposed (GitHub #113 L1).

    ``builtin`` records what the proposal pinned at propose time: ``True`` means
    the slug was resolved as a built-in and must stay a built-in even if a
    custom template has since taken the slug; ``False`` (a pinned custom
    version) must never fall back to a same-slug built-in (L-c).
    """
    if builtin is True:
        if is_builtin_slug(slug):
            return None
        return f"Jira template {slug!r} changed since it was proposed"
    row = await _get_org_template(db, org_id, slug)
    if row is None:
        if version is None and is_builtin_slug(slug):
            return None
        return f"Jira template {slug!r} changed since it was proposed"
    if row.archived_at is not None:
        return f"Jira template {slug!r} was archived since it was proposed"
    if version is not None:
        snap = (
            await db.execute(
                select(JiraTemplateVersion.id).where(
                    JiraTemplateVersion.template_id == row.id,
                    JiraTemplateVersion.version == version,
                )
            )
        ).scalar_one_or_none()
        if snap is None:
            return f"Jira template {slug!r} changed since it was proposed"
    return None


async def _get_org_template(
    db: AsyncSession, org_id: str, slug: str
) -> JiraTemplate | None:
    return (
        await db.execute(
            select(JiraTemplate).where(
                JiraTemplate.org_id == org_id,
                JiraTemplate.slug == slug,
            )
        )
    ).scalar_one_or_none()


async def jira_publish_preflight(
    db: AsyncSession,
    *,
    user_id: str,
    ctx: UserContext,
    project_id: str,
    jira_project_key: str | None = None,
    jira_template: str | None = None,
    jira_template_version: int | None = None,
    jira_template_builtin: bool | None = None,
    jira_sprint_id: int | None = None,
    auth=None,
) -> tuple[Project, OrgTrackerConnection, TrackerProjectMapping]:
    """Re-check publish preconditions for a create-ticket-and-publish confirm.

    The same gates ``link_jira_action`` checks, keyed on the project the ticket
    is being created in.  Raises ``HTTPException`` with the phase-1 codes
    (``jira_edit_required`` / ``jira_not_connected`` / ``jira_project_unmapped``)
    so the confirm handler records the same per-target failures.  Called BEFORE
    the ticket is created so a failed gate creates nothing.
    """
    project = await _project_for_actor(db, project_id, user_id, auth)
    # A service key is org-bound by ``_project_for_actor`` (the Jira route's
    # rule); the editor predicate is a human-standing check.
    if (auth is None or auth.key_kind != "service") and not await user_can_edit_project(
        db, user_id, project
    ):
        raise HTTPException(
            403,
            {
                "error": "jira_edit_required",
                "detail": "Publishing to Jira requires editor access.",
            },
        )
    await check_project_feature(ctx, "jira_integration", project, db)

    if project.org_id is None:
        raise HTTPException(
            409,
            {"error": "jira_not_connected", "detail": "This project is not org-scoped."},
        )
    conn = (
        await db.execute(
            select(OrgTrackerConnection).where(
                OrgTrackerConnection.org_id == project.org_id
            )
        )
    ).scalar_one_or_none()
    if conn is None or not conn.enabled:
        raise HTTPException(
            409,
            {
                "error": "jira_not_connected",
                "detail": "This organization has not connected Jira.",
            },
        )
    mapping = (
        await db.execute(
            select(TrackerProjectMapping).where(
                TrackerProjectMapping.project_id == project.id,
                TrackerProjectMapping.org_id == project.org_id,
            )
        )
    ).scalar_one_or_none()
    if mapping is None:
        raise HTTPException(
            409,
            {
                "error": "jira_project_unmapped",
                "detail": "Ask your admin to add a mapping",
            },
        )

    # A requested sprint needs the connection's sprint field; refuse before the
    # ticket is created rather than fail after an outbound call (L2).
    if jira_sprint_id is not None and not conn.sprint_field_id:
        raise HTTPException(
            422,
            {
                "error": "jira_sprint_field_unknown",
                "message": (
                    "This Jira connection has no sprint field discovered; ask "
                    "an org admin to save the connection or set a sprint field id."
                ),
            },
        )

    # The helper's optional publish overrides (GitHub #113 C) are re-checked at
    # confirm so a lost mapping/template between propose and confirm refuses the
    # publish before the ticket is created.  The target project's own mapping
    # key is always acceptable; anything else must be mapped elsewhere in org.
    if jira_project_key is not None and jira_project_key.strip().upper() != mapping.jira_project_key.strip().upper():
        err = await jira_project_key_override_error(
            db, org_id=project.org_id, jira_project_key=jira_project_key, user_id=user_id
        )
        if err is not None:
            raise HTTPException(
                422, {"error": "jira_project_key_invalid", "message": err}
            )
    if jira_template is not None:
        # L1: a custom template publishes with its PINNED version snapshot, so
        # a newer current version is fine; refuse ``jira_template_changed`` only
        # when that snapshot is gone or the template was archived.
        err = await jira_template_changed_error(
            db,
            org_id=project.org_id,
            slug=jira_template,
            version=jira_template_version,
            builtin=jira_template_builtin,
        )
        if err is not None:
            raise HTTPException(
                422,
                {
                    "error": "jira_template_changed",
                    "message": (
                        f"{err}; propose again to publish with the new version."
                    ),
                },
            )
    return project, conn, mapping


async def publish_ticket_to_jira(
    db: AsyncSession,
    *,
    project_id: str,
    ticket_id: str,
    actor_user_id: str,
    dashboard_url: str = "",
    overwrite_existing: bool = False,
    jira_project_key: str | None = None,
    template_slug: str | None = None,
    template_version: int | None = None,
    template_builtin: bool | None = None,
    sprint_id: int | None = None,
    auth=None,
) -> dict:
    """Publish a just-created ticket to Jira through the shared publish path.

    Loads the project/connection/mapping fresh (the caller committed the ticket
    before this runs), resolves the ticket's template the way the publish route
    does (with ``template_slug`` as the per-ticket override), then renders the
    server's own deterministic draft — ``collect_sources`` +
    ``build_deterministic_sections``, with no LLM rewrite — and hands the
    rendered ``summary``/``sections`` to ``publish_ticket_draft``.
    That function commits its own claim/link writes and returns the publish
    payload, or raises ``HTTPException`` with the route's error envelope.  The
    caller must have already committed the ticket so a publish failure leaves it
    in place.

    ``dashboard_url`` is the dashboard base resolved from the app config by the
    confirm handler (not a fresh ``ServerConfig``) and ``overwrite_existing``
    opts into replacing an attached-only link's Jira summary/description.
    ``jira_project_key`` / ``template_slug`` / ``sprint_id`` are the optional
    helper publish overrides (GitHub #113 C); the confirm handler has already
    re-checked them before the ticket was created.  ``template_version`` pins a
    custom template to the proposal-time snapshot (L1); ``template_builtin``
    records that the proposal pinned a built-in slug so a later custom template
    with the same slug can't shadow it (L-c).
    """
    from sessionfs.server.integrations.trackers.promote import (
        build_deterministic_sections,
        collect_sources,
    )
    from sessionfs.server.routes.jira import (
        _resolve_ticket_template,
        publish_ticket_draft,
    )

    ticket = await db.get(Ticket, ticket_id)
    if ticket is None or ticket.project_id != project_id:
        raise HTTPException(404, "Ticket not found")
    project = await _project_for_actor(db, project_id, actor_user_id, auth)
    if project.org_id is None:
        raise HTTPException(
            409,
            {"error": "jira_not_connected", "detail": "This project is not org-scoped."},
        )
    conn = (
        await db.execute(
            select(OrgTrackerConnection).where(
                OrgTrackerConnection.org_id == project.org_id
            )
        )
    ).scalar_one_or_none()
    if conn is None or not conn.enabled:
        raise HTTPException(
            409,
            {
                "error": "jira_not_connected",
                "detail": "This organization has not connected Jira.",
            },
        )
    mapping = (
        await db.execute(
            select(TrackerProjectMapping).where(
                TrackerProjectMapping.project_id == project.id,
                TrackerProjectMapping.org_id == project.org_id,
            )
        )
    ).scalar_one_or_none()
    if mapping is None:
        raise HTTPException(
            409,
            {
                "error": "jira_project_unmapped",
                "detail": "Ask your admin to add a mapping",
            },
        )

    resolved = await _resolve_ticket_template(
        db,
        org_id=project.org_id,
        mapping=mapping,
        conn=conn,
        link=None,
        override=template_slug,
        version=template_version,
        builtin=template_builtin,
    )
    sources = await collect_sources(ticket, db)
    rendered = build_deterministic_sections(resolved.template, sources)
    # Publish-shape text: drop the render-only ``mode`` key so ``payload_hash``
    # matches the route's ``model_dump()`` output exactly.
    sections = [
        {"key": s["key"], "heading": s["heading"], "paragraphs": s["paragraphs"]}
        for s in rendered
    ]

    actor = await db.get(User, actor_user_id)
    return await publish_ticket_draft(
        db,
        project=project,
        ticket=ticket,
        conn=conn,
        mapping=mapping,
        resolved=resolved,
        summary=sources.title,
        sections=sections,
        actor=actor,
        actor_type=auth.actor_type if auth is not None else "user",
        service_key_id=auth.service_key_id if auth is not None else None,
        service_key_name=auth.service_key_name if auth is not None else None,
        dashboard_url=dashboard_url,
        overwrite_existing=overwrite_existing,
        jira_project_key=jira_project_key,
        sprint_id=sprint_id,
        auth=auth,
    )


async def jira_overwrite_action(
    db: AsyncSession,
    *,
    user_id: str,
    ctx: UserContext,
    ticket_id: str,
    direction: str,
    dashboard_url: str = "",
    proposed_issue_key: str | None = None,
) -> dict:
    """Overwrite a linked Jira issue from the ticket, or pull the issue back in.

    ``direction`` is ``"to_jira"`` (publish the ticket, replacing the linked
    issue's summary/description) or ``"from_jira"`` (replace the ticket's
    title/description with the issue's).  Re-checks the proposal's preconditions
    — editor standing, the ``jira_integration`` plan gate, a live connection +
    project mapping, and a linked issue — then runs the shared publish/pull
    path.  Raises ``HTTPException`` for the gates (phase-1 codes) and
    ``JiraLinkError`` for the remote/race failures, exactly like
    ``link_jira_action``.  The to_jira publish commits its own claim/link
    writes; the from_jira pull leaves the commit to the caller.
    """
    ticket = (
        await db.execute(select(Ticket).where(Ticket.id == ticket_id))
    ).scalar_one_or_none()
    if ticket is None:
        raise HTTPException(404, "Ticket not found")

    project = await load_project_for_user(ticket.project_id, db, user_id)
    if not await user_can_edit_project(db, user_id, project):
        raise HTTPException(
            403,
            {
                "error": "jira_edit_required",
                "detail": "Overwriting Jira requires editor access.",
            },
        )
    await check_project_feature(ctx, "jira_integration", project, db)

    if project.org_id is None:
        raise HTTPException(
            409,
            {"error": "jira_not_connected", "detail": "This project is not org-scoped."},
        )
    conn = (
        await db.execute(
            select(OrgTrackerConnection).where(
                OrgTrackerConnection.org_id == project.org_id
            )
        )
    ).scalar_one_or_none()
    if conn is None or not conn.enabled:
        raise HTTPException(
            409,
            {
                "error": "jira_not_connected",
                "detail": "This organization has not connected Jira.",
            },
        )
    mapping = (
        await db.execute(
            select(TrackerProjectMapping).where(
                TrackerProjectMapping.project_id == project.id,
                TrackerProjectMapping.org_id == project.org_id,
            )
        )
    ).scalar_one_or_none()
    if mapping is None:
        raise HTTPException(
            409,
            {
                "error": "jira_project_unmapped",
                "detail": "Ask your admin to add a mapping",
            },
        )

    link = (
        await db.execute(
            select(TicketTrackerLink).where(TicketTrackerLink.ticket_id == ticket_id)
        )
    ).scalar_one_or_none()
    if link is None or link.status != "linked":
        raise HTTPException(
            409,
            {
                "error": "jira_not_linked",
                "detail": "This ticket is not linked to a Jira issue.",
            },
        )

    # The proposal pinned the linked issue's key; if the ticket was re-pointed
    # (or relinked) since, refuse rather than overwrite an issue the caller
    # never approved. This runs after the auth/feature/connection gates so a
    # reader learns nothing from the refusal.
    if proposed_issue_key is not None and link.issue_key != proposed_issue_key:
        raise JiraLinkError(
            "jira_link_changed",
            "The ticket's Jira link changed since the overwrite was proposed.",
            status_code=409,
        )

    if direction == "to_jira":
        published = await publish_ticket_to_jira(
            db,
            project_id=project.id,
            ticket_id=ticket_id,
            actor_user_id=user_id,
            dashboard_url=dashboard_url,
            overwrite_existing=True,
        )
        return {
            "issue_key": published["issue_key"],
            "issue_url": published["issue_url"],
        }

    from sessionfs.server.routes.jira import pull_ticket_from_jira

    actor = await db.get(User, user_id)
    issue_key, updated_fields = await pull_ticket_from_jira(
        db,
        project=project,
        ticket=ticket,
        conn=conn,
        actor=actor,
        actor_type="user",
        ctx=ctx,
    )
    return {"issue_key": issue_key, "updated_fields": updated_fields}
