"""v0.10.9 — shared helpers for the handoff route surface.

Kept separate from `routes/handoffs.py` so the route file stays focused
on REST handlers. Holds:

- `write_event` — append a row to handoff_events. Sanitizes payload size.
- `validate_attachments_for_sender` — project-scope check at create time.
- `validate_attachments_for_recipient` — project-scope check at claim time,
  returns the list of refs to silently drop (Codex I.7).
- `validate_provenance_for_sender` — same scope check applied to ticket_id
  and persona_name on create (mirrors v0.10.7 `_validate_revision_provenance`).
- `effective_status_with_lazy_expire` — read handoff and, if pending past
  expires_at, atomically flip to `expired` + write the event. Persisted
  expiry per Codex G (no background sweeper in v0.10.9).
- `member_user_ids_for_team` — collects user_ids of team members for
  inbox lookups.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Iterable

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from fastapi import HTTPException

from sessionfs.server.db.models import (
    AgentPersona,
    Handoff,
    HandoffAttachment,
    HandoffEvent,
    KnowledgeEntry,
    KnowledgePage,
    OrgMember,
    Project,
    ProjectRepo,
    Session as SessionModel,
    TeamMember,
    Ticket,
    User,
)


EVENT_PAYLOAD_MAX_BYTES = 16 * 1024


async def write_event(
    db: AsyncSession,
    *,
    handoff_id: str,
    event_type: str,
    actor_user_id: str | None,
    payload: dict | None = None,
    actor_type: str = "user",
    service_key_id: str | None = None,
    service_key_name: str | None = None,
) -> None:
    """Append a handoff_events row. Caller must commit.

    Payload size capped at 16 KiB with a `_truncated` marker (mirrors
    v0.10.4 retrieval-audit-event handling). Never include session
    content or raw recipient_email in payload — those live on the
    Handoff row itself.

    v0.10.10 (Codex R1 HIGH 2) — `actor_type` + `service_key_name`
    capture service-key provenance so audit viewers can distinguish
    "user X did Y" from "service key K (minted by X) did Y". Defaults
    preserve user-key behavior for routes that haven't been converted
    to use AuthContext yet.
    """
    serialized = json.dumps(payload or {}, default=str)
    if len(serialized.encode("utf-8")) > EVENT_PAYLOAD_MAX_BYTES:
        serialized = json.dumps({"_truncated": True})
    db.add(
        HandoffEvent(
            handoff_id=handoff_id,
            event_type=event_type,
            actor_user_id=actor_user_id,
            payload=serialized,
            actor_type=actor_type,
            service_key_id=service_key_id,
            service_key_name=service_key_name,
        )
    )


async def _accessible_project_ids(db: AsyncSession, user_id: str) -> set[str]:
    """Project IDs the user can read. Three access paths:

    1. Owner — `Project.owner_id == user_id`
    2. Org member — `Project.org_id IN (orgs the user belongs to)`
       (Codex R2 MEDIUM #2 — without this, a user added to an org but
       without a synced session for the org's repo would falsely lose
       access to org-project attachments at handoff claim time. Exactly
       the Team handoff use case.)
    3. Via shared git remote — `Project.git_remote_normalized ==
       Session.git_remote_normalized` for some Session owned by the user
       (matches existing wiki `_get_project_or_404` fallback).
    """
    owner_ids = (
        await db.execute(
            select(Project.id).where(Project.owner_id == user_id)
        )
    ).scalars().all()
    org_member_ids = (
        await db.execute(
            select(Project.id)
            .join(OrgMember, OrgMember.org_id == Project.org_id)
            .where(OrgMember.user_id == user_id)
        )
    ).scalars().all()
    # P2 (§3.3 B2): primary path — collect distinct project_ids from
    # the user's sessions (session.project_id, not remote-based join).
    # After multi-repo, project.git_remote_normalized is only the primary
    # remote — the old join-by-remote would miss projects where the
    # user has sessions only on non-primary repos.
    via_session_project_ids = (
        await db.execute(
            select(SessionModel.project_id)
            .where(
                SessionModel.user_id == user_id,
                SessionModel.project_id.isnot(None),
            )
            .distinct()
        )
    ).scalars().all()
    # Legacy fallback: sessions with NULL project_id (pre-dating
    # session→project linking, migration 036) matched by git_remote.
    via_legacy_remote_ids = (
        await db.execute(
            select(Project.id)
            .join(
                SessionModel,
                SessionModel.git_remote_normalized == Project.git_remote_normalized,
            )
            .where(SessionModel.user_id == user_id)
            .distinct()
        )
    ).scalars().all()
    # P2-fix: also match legacy sessions through project_repos (non-primary
    # repos).  The legacy path above only joins on the primary remote; a
    # session on a linked non-primary repo with NULL project_id would be
    # missed otherwise.
    via_repo_remote_ids = (
        await db.execute(
            select(ProjectRepo.project_id)
            .join(
                SessionModel,
                SessionModel.git_remote_normalized == ProjectRepo.git_remote_normalized,
            )
            .where(SessionModel.user_id == user_id)
            .distinct()
        )
    ).scalars().all()
    return (
        set(owner_ids)
        | set(org_member_ids)
        | set(via_session_project_ids)
        | set(via_legacy_remote_ids)
        | set(via_repo_remote_ids)
    )


async def validate_provenance_for_sender(
    db: AsyncSession,
    *,
    sender_id: str,
    session: SessionModel,
    ticket_id: str | None,
    persona_name: str | None,
) -> tuple[str | None, str | None]:
    """Validate sender's ticket_id + persona_name attribution at create
    time. Returns (snapshot_ticket_title, snapshot_persona_name).
    Raises ValueError on cross-project ref (caller maps to 422).

    Project scope: the session's project (resolved via git_remote_normalized).
    Mirrors v0.10.7 `_validate_revision_provenance` pattern.
    """
    snapshot_ticket_title: str | None = None
    snapshot_persona_name: str | None = None
    if not ticket_id and not persona_name:
        return None, None
    # Prefer session.project_id (authoritative FK) before falling
    # back to remote resolution (§3.3 A5).
    project: Project | None = None
    if session.project_id:
        project = (await db.execute(
            select(Project).where(Project.id == session.project_id)
        )).scalar_one_or_none()
    if project is None and session.git_remote_normalized:
        from sessionfs.server.services.project_resolver import (
            resolve_project_by_remote,
        )
        project = await resolve_project_by_remote(
            db, session.git_remote_normalized,
        )
    if project is None:
        raise ValueError(
            "Cannot attach ticket_id or persona_name to a handoff whose "
            "session's project is not registered."
        )
    if ticket_id:
        row = (
            await db.execute(
                select(Ticket.title).where(
                    Ticket.id == ticket_id,
                    Ticket.project_id == project.id,
                )
            )
        ).one_or_none()
        if row is None:
            raise ValueError(
                f"ticket_id {ticket_id!r} not found in the session's project"
            )
        snapshot_ticket_title = row[0]
    if persona_name:
        persona_id = (
            await db.execute(
                select(AgentPersona.id).where(
                    AgentPersona.project_id == project.id,
                    AgentPersona.name == persona_name,
                )
            )
        ).scalar_one_or_none()
        if persona_id is None:
            raise ValueError(
                f"persona_name {persona_name!r} not found in the session's project"
            )
        snapshot_persona_name = persona_name
    return snapshot_ticket_title, snapshot_persona_name


async def validate_attachments_for_sender(
    db: AsyncSession,
    *,
    sender_id: str,
    session: SessionModel,
    attachments: list[dict],
) -> None:
    """Validate each attachment ref resolves to an entity in the
    session's project at create time. Raises ValueError on cross-project
    (caller maps to 422). v0.10.9 attachment kinds: kb_entry, wiki_page,
    ticket."""
    if not attachments:
        return
    # Prefer session.project_id (authoritative FK) before falling
    # back to remote resolution (§3.3 A6).
    project: Project | None = None
    if session.project_id:
        project = (await db.execute(
            select(Project).where(Project.id == session.project_id)
        )).scalar_one_or_none()
    if project is None and session.git_remote_normalized:
        from sessionfs.server.services.project_resolver import (
            resolve_project_by_remote,
        )
        project = await resolve_project_by_remote(
            db, session.git_remote_normalized,
        )
    if project is None:
        raise ValueError("Session's project is not registered")
    pid = project.id
    for att in attachments:
        kind = att.get("kind") if isinstance(att, dict) else att.kind
        ref_id = att.get("ref_id") if isinstance(att, dict) else att.ref_id
        if kind == "kb_entry":
            # KB entries are integer IDs project-scoped via KnowledgeEntry.project_id
            try:
                eid = int(ref_id)
            except (TypeError, ValueError):
                raise ValueError(f"Invalid kb_entry ref_id {ref_id!r} (expected int)")
            exists = (
                await db.execute(
                    select(KnowledgeEntry.id).where(
                        KnowledgeEntry.id == eid,
                        KnowledgeEntry.project_id == pid,
                    )
                )
            ).scalar_one_or_none()
            if exists is None:
                raise ValueError(
                    f"kb_entry {eid} not found in the session's project"
                )
        elif kind == "wiki_page":
            exists = (
                await db.execute(
                    select(KnowledgePage.id).where(
                        KnowledgePage.project_id == pid,
                        KnowledgePage.slug == ref_id,
                    )
                )
            ).scalar_one_or_none()
            if exists is None:
                raise ValueError(
                    f"wiki_page {ref_id!r} not found in the session's project"
                )
        elif kind == "ticket":
            exists = (
                await db.execute(
                    select(Ticket.id).where(
                        Ticket.id == ref_id,
                        Ticket.project_id == pid,
                    )
                )
            ).scalar_one_or_none()
            if exists is None:
                raise ValueError(
                    f"ticket {ref_id!r} not found in the session's project"
                )
        else:
            raise ValueError(f"Unknown attachment kind {kind!r}")


async def validate_attachments_for_recipient(
    db: AsyncSession,
    *,
    recipient_id: str,
    attachments: list[HandoffAttachment],
) -> list[dict]:
    """At claim time, drop attachment refs the recipient can't access.
    Returns the list of dropped refs (with reasons) for the claim
    response + audit-event recording. Codex I.7 — silent drop but
    structured warning back to caller.

    Codex R2 MEDIUM #3 — each attachment row stores `project_id` set at
    create time so this validator can do an unambiguous (project_id,
    kind, ref_id) lookup. Slugs are project-local; without the stored
    project_id, two projects with the same `auth-flow` slug would
    collide during recipient validation.
    """
    if not attachments:
        return []
    accessible = await _accessible_project_ids(db, recipient_id)
    dropped: list[dict] = []
    for att in attachments:
        # First check: is the attachment's project accessible to the
        # recipient at all? If not, drop without bothering to check the
        # specific entity.
        if att.project_id not in accessible:
            dropped.append(
                {
                    "kind": att.kind,
                    "ref_id": att.ref_id,
                    "reason": "not_accessible",
                }
            )
            continue
        # Second check: does the specific entity still exist within
        # that project? (handles soft-delete / hard-delete races between
        # handoff create and claim.)
        if att.kind == "kb_entry":
            try:
                eid = int(att.ref_id)
            except (TypeError, ValueError):
                dropped.append(
                    {"kind": att.kind, "ref_id": att.ref_id, "reason": "invalid_id"}
                )
                continue
            exists = (
                await db.execute(
                    select(KnowledgeEntry.id).where(
                        KnowledgeEntry.id == eid,
                        KnowledgeEntry.project_id == att.project_id,
                    )
                )
            ).scalar_one_or_none()
            if exists is None:
                dropped.append(
                    {"kind": att.kind, "ref_id": att.ref_id, "reason": "deleted"}
                )
        elif att.kind == "wiki_page":
            exists = (
                await db.execute(
                    select(KnowledgePage.id).where(
                        KnowledgePage.project_id == att.project_id,
                        KnowledgePage.slug == att.ref_id,
                    )
                )
            ).scalar_one_or_none()
            if exists is None:
                dropped.append(
                    {"kind": att.kind, "ref_id": att.ref_id, "reason": "deleted"}
                )
        elif att.kind == "ticket":
            exists = (
                await db.execute(
                    select(Ticket.id).where(
                        Ticket.id == att.ref_id,
                        Ticket.project_id == att.project_id,
                    )
                )
            ).scalar_one_or_none()
            if exists is None:
                dropped.append(
                    {"kind": att.kind, "ref_id": att.ref_id, "reason": "deleted"}
                )
        else:
            dropped.append(
                {"kind": att.kind, "ref_id": att.ref_id, "reason": "unknown_kind"}
            )
    return dropped


async def member_user_ids_for_teams(
    db: AsyncSession, team_ids: Iterable[str]
) -> set[str]:
    """User IDs that belong to any of the given teams. Used by inbox to
    include team handoffs visible to the caller via team membership."""
    team_ids = list(team_ids)
    if not team_ids:
        return set()
    rows = (
        await db.execute(
            select(TeamMember.user_id).where(TeamMember.team_id.in_(team_ids))
        )
    ).scalars().all()
    return set(rows)


async def team_ids_for_user(db: AsyncSession, user_id: str) -> list[str]:
    """All team IDs the user is a member of."""
    return list(
        (
            await db.execute(
                select(TeamMember.team_id).where(TeamMember.user_id == user_id)
            )
        ).scalars().all()
    )


def lazy_expire(handoff: Handoff) -> bool:
    """If handoff is pending past expires_at, mutate status to 'expired'
    in-memory. Caller is responsible for persisting via UPDATE + writing
    the `expired` event. Returns True if expiry was applied this call.
    """
    if handoff.status != "pending":
        return False
    if handoff.expires_at is None:
        return False
    exp = handoff.expires_at
    if exp.tzinfo is None:
        exp = exp.replace(tzinfo=timezone.utc)
    if exp >= datetime.now(timezone.utc):
        return False
    handoff.status = "expired"
    return True


async def persist_lazy_expire(
    db: AsyncSession, *, handoff_id: str, current_status: str
) -> bool:
    """Atomic 'expire' transition. Only flips pending→expired if the
    handoff is still pending at the row level. Caller writes the event
    + commits."""
    if current_status != "pending":
        return False
    result = await db.execute(
        update(Handoff)
        .where(Handoff.id == handoff_id, Handoff.status == "pending")
        .values(status="expired")
    )
    return result.rowcount == 1


def parse_event_payload(raw: str | None) -> dict | None:
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return None


def is_recipient(user: User, handoff: Handoff) -> bool:
    """True if the user is the (individual) recipient of this handoff.
    For team handoffs, use `is_team_recipient` which requires a DB lookup."""
    if handoff.recipient_user_id and handoff.recipient_user_id == user.id:
        return True
    if handoff.recipient_email and user.email:
        if handoff.recipient_email.strip().lower() == user.email.strip().lower():
            return True
    return False


async def is_team_recipient(
    db: AsyncSession, user: User, handoff: Handoff
) -> bool:
    """True if the handoff is a team handoff and the user is a member
    of the recipient team."""
    if handoff.handoff_kind != "team" or not handoff.recipient_team_id:
        return False
    row = (
        await db.execute(
            select(TeamMember.id).where(
                TeamMember.team_id == handoff.recipient_team_id,
                TeamMember.user_id == user.id,
            )
        )
    ).scalar_one_or_none()
    return row is not None


async def claim_inbox_match(
    db: AsyncSession, user: User, handoff: Handoff
) -> bool:
    """Does this user have inbox visibility / claim rights on this handoff?
    Covers individual (email/user_id) AND team membership."""
    if is_recipient(user, handoff):
        return True
    return await is_team_recipient(db, user, handoff)


async def assert_service_key_handoff_boundary(
    db: AsyncSession, auth, source_session: SessionModel | None
) -> None:
    """v0.10.10 — service-key project boundary for handoff routes.

    Codex R5 HIGH 2 + R6 HIGH — service keys must be anchored to the
    handoff's source session's project before mutation. User-key callers
    are unaffected.

    Resolution order (R6 fix — the prior 'prefer org_id match'
    fallback let cross-org shared-remote attacks slip through):
      1. AUTHORITATIVE — source_session.project_id if set.
      2. LEGACY FALLBACK — git_remote_normalized lookup, but DENY if
         multiple projects share the remote (ambiguity is unresolvable
         without project_id; we refuse rather than guess).
      3. CONSERVATIVE — if source session is missing/None or has no
         linkage, deny (no orphan-handoff mutation by service keys).
    """
    if auth.key_kind != "service":
        return

    # R6 MEDIUM — service keys cannot mutate orphan handoffs (source
    # session deleted or never existed). Boundary cannot be verified
    # → deny conservatively.
    if source_session is None:
        raise HTTPException(
            status_code=403,
            detail={
                "error": "service_key_project_required",
                "message": (
                    "Service keys cannot act on a handoff whose source "
                    "session is unavailable"
                ),
            },
        )

    # R6 HIGH — authoritative anchor first. sessions.project_id is the
    # truth; git_remote is the legacy fallback for rows that predate
    # session→project linking (migration 036).
    # P2 (§3.3 A7): legacy fallback now routes through the multi-repo
    # resolver.  The old len(matching) > 1 ambiguity guard is now a
    # compile-time invariant (project_repos UNIQUE on git_remote_normalized
    # guarantees at most one project per remote); kept as defense-in-depth.
    project: Project | None = None
    if source_session.project_id:
        project = (
            await db.execute(
                select(Project).where(Project.id == source_session.project_id)
            )
        ).scalar_one_or_none()
    elif source_session.git_remote_normalized:
        from sessionfs.server.services.project_resolver import (
            resolve_project_by_remote,
        )
        project = await resolve_project_by_remote(
            db, source_session.git_remote_normalized,
        )

    if project is None:
        raise HTTPException(
            status_code=403,
            detail={
                "error": "service_key_project_not_registered",
                "message": (
                    "Source session has no resolvable project anchor — "
                    "service-key boundary cannot be verified"
                ),
            },
        )

    # Reuse the canonical boundary check from auth/dependencies.
    from sessionfs.server.auth.dependencies import (
        assert_service_key_can_access_project,
    )
    await assert_service_key_can_access_project(db, auth, project)
