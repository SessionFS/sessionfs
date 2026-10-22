"""Shared persona resolution — project personas first, then org personas.

GitHub #70: a persona is scoped to ONE project (`project_id` set) or to ONE
organization (`org_id` set). Every server path that looks a persona up by
name from inside a project goes through `resolve_persona` so the precedence
rule lives in one place:

1. A persona in the project itself wins (a project persona with the same
   name OVERRIDES the org one — repo-specific tuning).
2. Otherwise a persona of the project's org, when the project belongs to
   an org.

Name matching is case-insensitive in BOTH scopes by default (exact-case
match preferred, then the oldest row), the same rule the project list uses
to hide an org persona behind a project override — so `Atlas` in the
project and `atlas` in the org can never resolve differently from what the
list shows. Callers store the returned row's canonical `.name`, never the
caller-supplied spelling.

`active_only=True` callers fall through to the org persona when the
project's same-named persona is soft-deleted: deleting a project override
restores the org default. `active_only=False` callers (provenance checks
that historically accepted inactive rows) get the project row whenever one
exists, active or not.

Resolution never authorizes, and project access alone does NOT grant
org-persona access: a project owner or someone with a historical session
keeps project access after the project moves to another org, but must not
see that org's personas. The org fallback therefore takes an explicit
`org_id` that callers obtain from `visible_persona_org_id()` — the
project's CURRENT org only when the caller is a current member of it
(service keys: only their own org), else None (project personas only).
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass

from sqlalchemy import and_, case, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server.db.models import (
    AgentPersona,
    AgentRun,
    Handoff,
    OrgAuditEvent,
    OrgMember,
    Organization,
    Project,
    ProjectRepo,
    Session,
    Ticket,
)

SCOPE_PROJECT = "project"
SCOPE_ORG = "org"


async def clear_represented_user(
    db: AsyncSession,
    *,
    user_id: str,
    actor_user_id: str,
    actor_email: str,
    actor_role: str,
    reason: str,
    org_id: str | None = None,
) -> int:
    """GitHub #81 — stop org personas representing ``user_id`` (in ``org_id``
    only, or in every org), with one ``persona_represented_user_changed``
    audit row per persona. Called when a member leaves an org or the user
    is deactivated, so rejoining later does not restore edit rights.
    Does not commit. Returns how many personas changed."""
    stmt = (
        select(AgentPersona, Organization.name)
        .join(Organization, Organization.id == AgentPersona.org_id)
        .where(AgentPersona.represented_user_id == user_id)
    )
    if org_id is not None:
        stmt = stmt.where(AgentPersona.org_id == org_id)
    rows = (await db.execute(stmt)).all()
    for persona, org_name in rows:
        persona.represented_user_id = None
        db.add(
            OrgAuditEvent(
                id=f"oae_{uuid.uuid4().hex[:24]}",
                org_id=persona.org_id,
                org_name_snapshot=org_name or "",
                event_type="persona_represented_user_changed",
                actor_user_id=actor_user_id,
                actor_email_snapshot=actor_email,
                actor_role_at_time=actor_role,
                target_type="persona",
                target_id=persona.id,
                before=json.dumps({"name": persona.name, "represented_user_id": user_id}),
                after=json.dumps(
                    {"name": persona.name, "represented_user_id": None, "reason": reason}
                ),
            )
        )
    return len(rows)


def persona_scope(persona: AgentPersona) -> str:
    """'org' for an org-level persona, else 'project'."""
    return SCOPE_ORG if persona.org_id is not None else SCOPE_PROJECT


async def project_org_id(db: AsyncSession, project_id: str) -> str | None:
    """The org a project belongs to, or None (personal / unknown project)."""
    return (
        await db.execute(select(Project.org_id).where(Project.id == project_id))
    ).scalar_one_or_none()


async def visible_persona_org_id(
    db: AsyncSession,
    project: Project,
    *,
    user_id: str | None,
    key_kind: str = "user",
    key_org_id: str | None = None,
) -> str | None:
    """The org whose personas the caller may see from inside `project`.

    The project's CURRENT org when the caller is a current member of it;
    for a service key, only when the project's org is the key's own org.
    Otherwise None — the caller sees project personas only.
    """
    org_id = project.org_id
    if not org_id:
        return None
    if key_kind == "service":
        return org_id if key_org_id == org_id else None
    if not user_id:
        return None
    member = (
        await db.execute(
            select(OrgMember.user_id)
            .where(OrgMember.org_id == org_id, OrgMember.user_id == user_id)
            .limit(1)
        )
    ).first()
    return org_id if member is not None else None


async def visible_persona_org_id_for_auth(
    db: AsyncSession, project: Project, auth
) -> str | None:
    """`visible_persona_org_id` for an AuthContext (user or service key)."""
    return await visible_persona_org_id(
        db,
        project,
        user_id=auth.user.id,
        key_kind=auth.key_kind,
        key_org_id=auth.org_id,
    )


def _name_filter(name: str, case_insensitive: bool):
    if case_insensitive:
        return func.lower(AgentPersona.name) == name.lower()
    return AgentPersona.name == name


async def _first_in_scope(
    db: AsyncSession,
    scope_filter,
    name: str,
    *,
    active_only: bool,
    case_insensitive: bool,
) -> AgentPersona | None:
    stmt = select(AgentPersona).where(scope_filter, _name_filter(name, case_insensitive))
    if active_only:
        stmt = stmt.where(AgentPersona.is_active.is_(True))
    # Deterministic: exact-case match first, then the oldest row. `.first()`
    # never raises MultipleResultsFound on legacy case-distinct duplicates.
    stmt = stmt.order_by(
        case((AgentPersona.name == name, 0), else_=1), AgentPersona.id
    )
    return (await db.execute(stmt)).scalars().first()


async def resolve_persona(
    db: AsyncSession,
    project_id: str,
    name: str,
    *,
    org_id: str | None,
    active_only: bool = True,
    case_insensitive: bool = True,
) -> AgentPersona | None:
    """Resolve `name` from inside `project_id`: project scope, then org scope.

    `org_id` is REQUIRED and must come from `visible_persona_org_id()` (or
    be None for project-only resolution) — never the project's raw org_id
    for a caller who is not a member of it. Internal consistency checks
    that do not return data to the caller (transfer guard) may pass the
    real org ids explicitly.
    """
    if not name:
        return None
    row = await _first_in_scope(
        db,
        AgentPersona.project_id == project_id,
        name,
        active_only=active_only,
        case_insensitive=case_insensitive,
    )
    if row is not None:
        return row
    if not org_id:
        return None
    return await _first_in_scope(
        db,
        AgentPersona.org_id == org_id,
        name,
        active_only=active_only,
        case_insensitive=case_insensitive,
    )


async def find_project_persona(
    db: AsyncSession,
    project_id: str,
    name: str,
    *,
    active_only: bool = False,
    case_insensitive: bool = True,
) -> AgentPersona | None:
    """Look up a project-scoped persona only (project write routes).
    Case-insensitive by default, exact-case match preferred."""
    return await _first_in_scope(
        db,
        AgentPersona.project_id == project_id,
        name,
        active_only=active_only,
        case_insensitive=case_insensitive,
    )


async def find_org_persona(
    db: AsyncSession,
    org_id: str,
    name: str,
    *,
    active_only: bool = False,
    case_insensitive: bool = True,
) -> AgentPersona | None:
    """Look up an org-scoped persona directly (org management routes).
    Case-insensitive by default (org names are case-insensitive-unique)."""
    return await _first_in_scope(
        db,
        AgentPersona.org_id == org_id,
        name,
        active_only=active_only,
        case_insensitive=case_insensitive,
    )


@dataclass
class EffectivePersona:
    persona: AgentPersona
    scope: str


async def list_effective_personas(
    db: AsyncSession,
    project: Project,
    *,
    org_id: str | None,
    include_inactive: bool = False,
) -> list[EffectivePersona]:
    """Project personas ∪ the personas of `org_id`, overrides applied.

    `org_id` comes from `visible_persona_org_id()` (None: project only).

    An org persona is omitted when the project has an ACTIVE persona with
    the same name (case-insensitive) — the project one is what resolves.
    Sorted by name, project rows before org rows on a tie.
    """
    stmt = select(AgentPersona).where(AgentPersona.project_id == project.id)
    if not include_inactive:
        stmt = stmt.where(AgentPersona.is_active.is_(True))
    project_rows = list((await db.execute(stmt)).scalars().all())
    result = [EffectivePersona(p, SCOPE_PROJECT) for p in project_rows]

    if org_id:
        shadowing = {p.name.lower() for p in project_rows if p.is_active}
        org_stmt = select(AgentPersona).where(AgentPersona.org_id == org_id)
        if not include_inactive:
            org_stmt = org_stmt.where(AgentPersona.is_active.is_(True))
        for p in (await db.execute(org_stmt)).scalars().all():
            if p.name.lower() in shadowing:
                continue
            result.append(EffectivePersona(p, SCOPE_ORG))

    result.sort(key=lambda e: (e.persona.name, e.scope != SCOPE_PROJECT))
    return result


NON_TERMINAL_TICKET_STATUSES = (
    "suggested", "open", "in_progress", "blocked", "review",
)
# Agent runs that still need their persona (start re-resolves it).
ACTIVE_AGENT_RUN_STATUSES = ("queued", "running")


def pending_handoffs_in_project(project_id):
    """Filter for PENDING handoffs whose provenance belongs to the
    project: the claim writes `Handoff.persona_name` into the recipient's
    bundle, so the persona must still resolve then.

    The durable anchor (`Handoff.source_project_id`, set at create and
    backfilled by migration 072) is authoritative. Rows without an anchor
    (created before the create path set it) fall back to the legacy
    `Session.project_id` match, then to the legacy remote resolution that
    mirrors `resolve_project_by_remote`: a NULL-project_id session whose
    `git_remote_normalized` belongs to the project (project_repos first,
    then projects.git_remote_normalized).
    """
    legacy_remote_sessions = select(Session.id).where(
        Session.project_id.is_(None),
        or_(
            Session.git_remote_normalized.in_(
                select(ProjectRepo.git_remote_normalized).where(
                    ProjectRepo.project_id == project_id
                )
            ),
            Session.git_remote_normalized.in_(
                select(Project.git_remote_normalized).where(
                    Project.id == project_id
                )
            ),
        ),
    )
    return (
        Handoff.status == "pending",
        or_(
            Handoff.source_project_id == project_id,
            and_(
                Handoff.source_project_id.is_(None),
                or_(
                    Handoff.session_id.in_(
                        select(Session.id).where(
                            Session.project_id == project_id
                        )
                    ),
                    Handoff.session_id.in_(legacy_remote_sessions),
                ),
            ),
        ),
    )


@dataclass
class StrandedPersona:
    persona: str
    ticket_ids: list[str]
    run_ids: list[str]
    handoff_ids: list[str]


async def personas_stranded_by_scope_change(
    db: AsyncSession,
    project_id: str,
    *,
    current_org_id: str | None,
    dest_org_id: str | None,
) -> list[StrandedPersona]:
    """Personas that non-terminal tickets rely on today but that would no
    longer resolve if the project moved from `current_org_id` to
    `dest_org_id` (GH #70, project transfers).

    Project personas move with the project, so only assignments that
    resolve through the CURRENT org (no active project persona of that
    name) and have no active persona of that name in the DESTINATION org
    are stranded. Assignments that do not resolve today (free-text owners,
    already-missing personas) are not the transfer's doing and are ignored.
    Returned sorted by persona name, ticket ids sorted.
    """
    if current_org_id == dest_org_id:
        return []
    rows = (
        await db.execute(
            select(Ticket.id, Ticket.assigned_to).where(
                Ticket.project_id == project_id,
                Ticket.assigned_to.is_not(None),
                Ticket.status.in_(NON_TERMINAL_TICKET_STATUSES),
            )
        )
    ).all()
    run_rows = (
        await db.execute(
            select(AgentRun.id, AgentRun.persona_name).where(
                AgentRun.project_id == project_id,
                AgentRun.status.in_(ACTIVE_AGENT_RUN_STATUSES),
            )
        )
    ).all()
    handoff_rows = (
        await db.execute(
            select(Handoff.id, Handoff.persona_name).where(
                *pending_handoffs_in_project(project_id),
                Handoff.persona_name.is_not(None),
            )
        )
    ).all()
    # lower(name) -> (a spelling seen on a reference, ticket/run/handoff ids)
    by_name: dict[str, tuple[str, list[str], list[str], list[str]]] = {}
    for ticket_id, assigned in rows:
        if assigned:
            by_name.setdefault(assigned.lower(), (assigned, [], [], []))[1].append(ticket_id)
    for run_id, run_persona in run_rows:
        if run_persona:
            by_name.setdefault(run_persona.lower(), (run_persona, [], [], []))[2].append(run_id)
    for handoff_id, handoff_persona in handoff_rows:
        if handoff_persona:
            by_name.setdefault(
                handoff_persona.lower(), (handoff_persona, [], [], [])
            )[3].append(handoff_id)

    stranded: list[StrandedPersona] = []
    for name, ticket_ids, run_ids, handoff_ids in by_name.values():
        now = await resolve_persona(db, project_id, name, org_id=current_org_id)
        if now is None or now.org_id is None:
            # Unresolved today, or resolved by a project persona that moves
            # with the project.
            continue
        after = await resolve_persona(db, project_id, name, org_id=dest_org_id)
        if after is None:
            stranded.append(
                StrandedPersona(
                    now.name, sorted(ticket_ids), sorted(run_ids), sorted(handoff_ids)
                )
            )
    stranded.sort(key=lambda s: s.persona.lower())
    return stranded
