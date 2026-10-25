"""Project transfer routes — v0.10.0 Phase 2.

State machine: pending → accepted | rejected | cancelled.
The ProjectTransfer row IS the audit trail; every state transition
mutates the row in place (updated_at + accepted_at + accepted_by).
No separate audit_log table — KB entry 230 #1 invariant met via the
durable schema designed in Phase 1.

Routes:
    POST /api/v1/projects/{id}/transfer       initiate
    POST /api/v1/transfers/{xfer_id}/accept   target_user_id only
    POST /api/v1/transfers/{xfer_id}/reject   target_user_id only
    POST /api/v1/transfers/{xfer_id}/cancel   initiator only, pending only
    GET  /api/v1/transfers                    list (incoming|outgoing)

Authorization model:
    - Initiate: project owner OR org admin (when from_scope is the org).
    - Accept/reject: target_user_id only.
    - Cancel: initiated_by only (and only while state='pending').
    - List: filtered to the requesting user's incoming/outgoing transfers.

Race safety:
    - Accept does an atomic `UPDATE ... WHERE id=? AND state='pending'`
      and checks rowcount; non-1 → 409 STALE_STATE. This prevents two
      concurrent accept clicks (or accept-then-cancel races) from both
      committing.

Auto-accept:
    - If initiator == target (e.g. user transfers own personal project
      into their own org), the initiate route flips state directly to
      `accepted` at create time and applies the project.org_id change
      synchronously. No pending row sits in the inbox.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, field_validator
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server.auth.dependencies import get_current_user
from sessionfs.server.db.engine import get_db
from sessionfs.server.db.models import (
    AdminAction,
    OrgMember,
    Organization,
    Project,
    ProjectTransfer,
    User,
)
from sessionfs.server.roles import ADMIN_ROLES
from sessionfs.server.services.personas import personas_stranded_by_scope_change
from sessionfs.server.services.scope_locks import lock_project_scope
from sessionfs.server.services.tracker_scope import drop_project_tracker_mapping

router = APIRouter(tags=["project-transfers"])

logger = logging.getLogger("sessionfs.project_transfers")

# GH #96 — an admin-proposed transfer waits 7 days for the owner's consent.
PROPOSAL_EXPIRY_DAYS = 7
# GH #96 review round 1 — after a decline the proposer must wait a week
# before proposing the same project into the same org again; after a cancel
# the cooldown is only a day. A proposing org may also hold at most this many
# pending proposals at once.
PROPOSAL_DECLINE_COOLDOWN = timedelta(days=7)
PROPOSAL_CANCEL_COOLDOWN = timedelta(hours=24)
MAX_PENDING_PROPOSALS_PER_ORG = 10


# ── Request / response models ──


class InitiateTransferRequest(BaseModel):
    """`to`: either the literal "personal" or an org_id.

    `force` (GH #70): when the transfer auto-accepts, apply it even if
    non-terminal tickets are assigned to org personas that will not exist
    in the destination.
    """

    to: str
    force: bool = False


class ProposeTransferRequest(BaseModel):
    """An org admin proposes moving a project into their org, pending the
    project owner's consent (GH #96)."""

    reason: str

    @field_validator("reason")
    @classmethod
    def _reason_clean(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("reason must not be blank")
        if len(value) > 1000:
            raise ValueError("reason must be at most 1000 characters")
        return value


class ProposeTransferResponse(BaseModel):
    """Minimal propose response (GH #96 review round 1): the caller only
    needs the transfer id, the project it addresses, its state and expiry.
    No owner/target/scope details — those leak existence/ownership oracles
    to a proposer who has no relationship with the project."""

    transfer_id: str
    project_id: str
    status: str
    expires_at: datetime | None


class TransferResponse(BaseModel):
    id: str
    project_id: str | None
    project_git_remote_snapshot: str | None
    project_name_snapshot: str | None
    initiated_by: str
    target_user_id: str | None
    from_scope: str
    to_scope: str
    state: str
    accepted_by: str | None
    proposed_by_user_id: str | None
    proposed_by_name: str | None
    proposal_reason: str | None
    expires_at: datetime | None
    created_at: datetime
    accepted_at: datetime | None
    updated_at: datetime


class TransferListResponse(BaseModel):
    transfers: list[TransferResponse]


def _to_response(
    t: ProjectTransfer, proposed_by_name: str | None = None
) -> TransferResponse:
    return TransferResponse(
        id=t.id,
        project_id=t.project_id,
        project_git_remote_snapshot=t.project_git_remote_snapshot,
        project_name_snapshot=t.project_name_snapshot,
        initiated_by=t.initiated_by,
        target_user_id=t.target_user_id,
        from_scope=t.from_scope,
        to_scope=t.to_scope,
        state=t.state,
        accepted_by=t.accepted_by,
        proposed_by_user_id=t.proposed_by_user_id,
        proposed_by_name=proposed_by_name,
        proposal_reason=t.proposal_reason,
        expires_at=t.expires_at,
        created_at=t.created_at,
        accepted_at=t.accepted_at,
        updated_at=t.updated_at,
    )


# ── Helpers ──


async def _user_is_org_admin(
    db: AsyncSession, user_id: str, org_id: str
) -> bool:
    stmt = select(OrgMember).where(
        OrgMember.org_id == org_id,
        OrgMember.user_id == user_id,
        # Owners rank above admins and carry admin standing.
        OrgMember.role.in_(ADMIN_ROLES),
    )
    return (await db.execute(stmt)).scalar_one_or_none() is not None


async def _user_is_org_member(
    db: AsyncSession, user_id: str, org_id: str
) -> bool:
    stmt = select(OrgMember).where(
        OrgMember.org_id == org_id,
        OrgMember.user_id == user_id,
    )
    return (await db.execute(stmt)).scalar_one_or_none() is not None


async def _user_has_acceptor_standing(
    db: AsyncSession,
    user: User,
    transfer: ProjectTransfer,
    project: Project | None,
) -> bool:
    """Re-verify the user's CURRENT standing to act on this transfer.

    Standing must mirror the original target-selection requirement
    exactly. Codex Phase-2 round-2 (KB 248) introduced this helper
    for removal-from-org; round-3 (KB 250) tightened it for org→org
    where the original target selection required admin standing, not
    mere membership. An admin demoted to plain member between
    initiate and accept must NOT be able to mutate state.

    Standing rules (mirror _resolve_target_user_id at initiate):
      - Must still be the originally-targeted user.
      - to_scope == "personal": user must STILL be the project owner
        (custody-receiving role).
      - to_scope == org_id, from_scope == "personal" (rare pending —
        usually auto-accepts at initiate): user must STILL be a member
        of the destination org.
      - to_scope == org_id, from_scope == another org_id (org→org):
        user must STILL be an ADMIN of the destination org. This is
        the role the original target-selection picked; demotion to
        plain member breaks standing.
    """
    if transfer.target_user_id != user.id:
        return False

    if transfer.to_scope == "personal":
        if project is None:
            # Project was hard-deleted while pending — the audit row
            # survives, but the move can't apply. No standing.
            return False
        return project.owner_id == user.id

    # GH #96 — an admin-proposed transfer's acceptor is the project owner,
    # who must still own the project AND be a member of the destination org.
    # Unlike an owner-initiated org→org transfer (which an admin accepts),
    # a proposal is always accepted by the owner; the proposer is the admin.
    if transfer.proposed_by_user_id is not None:
        if project is None or project.owner_id != user.id:
            return False
        return await _user_is_org_member(db, user.id, transfer.to_scope)

    # to_scope is an org_id. Differentiate by from_scope: org→org
    # requires admin (matches initiate-time target selection); the
    # rare personal→org pending row requires only membership (the
    # auto-accept branch usually fires; this is the pending shape).
    if transfer.from_scope != "personal":
        return await _user_is_org_admin(db, user.id, transfer.to_scope)
    return await _user_is_org_member(db, user.id, transfer.to_scope)


async def _resolve_target_user_id(
    db: AsyncSession,
    project: Project,
    to_scope: str,
    initiator: User,
) -> str:
    """Decide who must accept the transfer.

    For org→personal: target = project owner (the user receiving the
    personal handle to the project). If the initiator IS the owner,
    that's the auto-accept shape.

    For personal→org (the initiator is the project owner): target =
    initiator. The route layer detects this and auto-accepts.

    For org→org: target = ANY admin of the destination org. We pick
    the first admin found; the route layer treats the resolved admin
    as the accept recipient. If no admin exists in the destination
    org, the route refuses to initiate.
    """
    if to_scope == "personal":
        # The recipient of personal ownership is the project owner.
        return project.owner_id

    # Destination is an org — the resolved target depends on the source.
    if project.org_id is None:
        # personal → org. The initiator (who must be the project owner)
        # is the only party with standing — auto-accept.
        return initiator.id

    # org → org. Find an admin-level member (admin or owner) of the
    # destination org; an org whose only admin-level member is its owner
    # must still be a valid target. Longest-tenured first so the pick is
    # deterministic.
    stmt = (
        select(OrgMember)
        .where(OrgMember.org_id == to_scope, OrgMember.role.in_(ADMIN_ROLES))
        .order_by(OrgMember.joined_at.asc(), OrgMember.user_id.asc())
        .limit(1)
    )
    admin = (await db.execute(stmt)).scalar_one_or_none()
    if admin is None:
        raise HTTPException(400, "Destination org has no admin to accept the transfer")
    return admin.user_id


async def _assert_no_stranded_personas(
    db: AsyncSession,
    project: Project,
    to_scope: str,
    force: bool,
) -> None:
    """GH #70 — refuse (409) a move that would leave non-terminal tickets
    assigned to org personas the destination does not have. Nothing is
    copied automatically: the caller reassigns the tickets, creates the
    personas in the destination (or as project personas), or passes
    force."""
    if force:
        return
    dest_org = None if to_scope == "personal" else to_scope
    stranded = await personas_stranded_by_scope_change(
        db, project.id, current_org_id=project.org_id, dest_org_id=dest_org
    )
    if not stranded:
        return
    summary = "; ".join(
        f"{s.persona}: {', '.join([*s.ticket_ids, *s.run_ids, *s.handoff_ids])}"
        for s in stranded
    )
    raise HTTPException(
        status_code=409,
        detail={
            "code": "personas_stranded",
            "message": (
                "Non-terminal tickets, queued/running agent runs or pending "
                "handoffs use org "
                "personas that will not exist after this transfer "
                f"({summary}). Reassign them, create the personas in the "
                "destination or as project personas, or retry with force."
            ),
            "personas": [
                {
                    "persona": s.persona,
                    "ticket_ids": s.ticket_ids,
                    "run_ids": s.run_ids,
                    "handoff_ids": s.handoff_ids,
                }
                for s in stranded
            ],
        },
    )


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ── GH #96 — admin-proposed transfer helpers ──


def _transfer_is_expired(transfer: ProjectTransfer, now: datetime) -> bool:
    """True if the transfer's consent window has passed. SQLite roundtrips
    DateTime(timezone=True) as naive — coerce to UTC before comparing."""
    if transfer.expires_at is None:
        return False
    expires = transfer.expires_at
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    return expires < now


def _proposal_audit(
    *,
    actor_id: str,
    action: str,
    project_id: str | None,
    transfer_id: str,
    proposer_id: str | None,
    owner_id: str | None,
    reason: str | None,
    before_scope: str,
    after_scope: str,
) -> AdminAction:
    """GH #96 — an AdminAction row (source 'api') recording a proposal
    state transition. Names the proposer and the acceptor/decliner in
    `details`; `admin_id` is whoever performed the transition."""
    return AdminAction(
        id=str(uuid.uuid4()),
        admin_id=actor_id,
        action=action,
        target_type="project",
        target_id=project_id or "",
        details=json.dumps(
            {
                "transfer_id": transfer_id,
                "proposer_user_id": proposer_id,
                "owner_user_id": owner_id,
            }
        ),
        reason=reason,
        before_state=json.dumps({"scope": before_scope}),
        after_state=json.dumps({"scope": after_scope}),
        source="api",
    )


async def _lazy_expire_proposal(
    db: AsyncSession, transfer: ProjectTransfer, actor_id: str
) -> bool:
    """Flip an expired-but-still-'pending' proposal to 'expired' and write an
    audit row. Rowcount-1 guarded so a concurrent expirer/canceler doesn't
    double-emit. The caller commits, then notifies the proposer by email."""
    now = _now()
    result = await db.execute(
        update(ProjectTransfer)
        .where(
            ProjectTransfer.id == transfer.id,
            ProjectTransfer.state == "pending",
        )
        .values(state="expired", updated_at=now)
    )
    if result.rowcount != 1:
        return False
    db.add(
        _proposal_audit(
            actor_id=actor_id,
            action="project_transfer_proposal_expired",
            project_id=transfer.project_id,
            transfer_id=transfer.id,
            proposer_id=transfer.proposed_by_user_id,
            owner_id=transfer.target_user_id,
            reason=transfer.proposal_reason,
            before_scope=transfer.from_scope,
            after_scope=transfer.from_scope,
        )
    )
    return True


async def _send_proposal_email(
    request: Request, method: str, *, to_email: str, **kwargs: object
) -> None:
    """Best-effort proposal email via an EmailProvider convenience method.
    Never raises; masks the recipient address in logs."""
    email_service = getattr(request.app.state, "email_service", None)
    if email_service is None:
        return
    try:
        await getattr(email_service, method)(to_email=to_email, **kwargs)
    except Exception:
        from sessionfs.server.email import mask_address

        logger.exception(
            "Project transfer email (%s) to %s failed", method, mask_address(to_email)
        )


async def _org_name(db: AsyncSession, org_id: str) -> str:
    org = (
        await db.execute(select(Organization).where(Organization.id == org_id))
    ).scalar_one_or_none()
    return org.name if org is not None else org_id


async def _notify_proposal_outcome(
    request: Request, db: AsyncSession, transfer: ProjectTransfer, method: str
) -> None:
    """Email the proposer that their proposal was declined or expired."""
    if not transfer.proposed_by_user_id:
        return
    proposer = (
        await db.execute(
            select(User).where(User.id == transfer.proposed_by_user_id)
        )
    ).scalar_one_or_none()
    if proposer is None:
        return
    owner = None
    if transfer.target_user_id:
        owner = (
            await db.execute(
                select(User).where(User.id == transfer.target_user_id)
            )
        ).scalar_one_or_none()
    await _send_proposal_email(
        request,
        method,
        to_email=proposer.email,
        project_name=transfer.project_name_snapshot or "Untitled project",
        org_name=await _org_name(db, transfer.to_scope),
        # GH #96 review round 1 — never leak the owner's email back to the
        # proposer in the decline/expiry email: use their display name, or a
        # neutral placeholder when they have none (or were deleted).
        owner_name=(owner.display_name if owner and owner.display_name else "the project owner"),
    )


async def _proposed_by_names(
    db: AsyncSession, transfers: list[ProjectTransfer]
) -> dict[str, str]:
    """Resolve display names for the proposals' proposers (bulk, no N+1)."""
    ids = {t.proposed_by_user_id for t in transfers if t.proposed_by_user_id}
    if not ids:
        return {}
    rows = (
        await db.execute(
            select(User.id, User.display_name, User.email).where(User.id.in_(ids))
        )
    ).all()
    return {uid: (name or email or uid) for uid, name, email in rows}


async def _proposer_has_project_relationship(
    db: AsyncSession, user: User, project: Project
) -> bool:
    """True when the proposer can already see/act on the project, so it is
    safe to give them a specific (non-404) refusal without leaking an
    existence/scope oracle. That is: they own it, or it lives in an org they
    belong to (membership of any role grants visibility)."""
    if project.owner_id == user.id:
        return True
    if project.org_id is not None:
        return await _user_is_org_member(db, user.id, project.org_id)
    return False


async def _refuse_unproposable_project(
    db: AsyncSession, user: User, project: Project, org_id: str
) -> None:
    """Raise the refusal for a project that cannot be proposed into `org_id`,
    or return silently when the proposal may proceed (GH #96 review round 2
    M1).

    Run against the row read BEFORE any lock, then re-run under the lock, so
    the route returns the uniform 404 for anything the proposer has no
    relationship with — a merged/tombstoned project, an org-scoped project
    outside their orgs, or a personal project whose owner is not a member of
    the destination org — all byte-identical to a missing id. A project
    already in the destination org, and an org-scoped project the caller CAN
    see, keep their specific codes (the caller is entitled to know).
    """
    # A merge tombstone is never proposable — always the plain 404, even for
    # a caller who could otherwise see it, and for a project merged in
    # between the pre-lock and under-lock reads.
    if project.merged_into_project_id is not None:
        raise HTTPException(404, "Project not found")

    from_scope = project.org_id if project.org_id is not None else "personal"
    if from_scope == org_id:
        raise HTTPException(
            409,
            {
                "code": "already_in_org",
                "message": "The project is already in that org; nothing to propose.",
            },
        )

    if project.org_id is not None:
        if await _proposer_has_project_relationship(db, user, project):
            raise HTTPException(
                409,
                {
                    "code": "proposal_personal_only",
                    "message": (
                        "Only a personal project can be proposed into an org; "
                        "org-scoped projects move through the source-org-admin "
                        "transfer flow."
                    ),
                },
            )
        raise HTTPException(404, "Project not found")

    if not await _user_is_org_member(db, project.owner_id, org_id):
        raise HTTPException(404, "Project not found")


async def _proposal_cooldown_seconds(
    db: AsyncSession, project_id: str, org_id: str, now: datetime
) -> int | None:
    """Retry-after seconds if the most recent decline/cancel for this
    project+org still blocks a new proposal, else None. Declines cool down
    for 7 days, cancels for 24 hours (GH #96 review round 1)."""
    recent = (
        await db.execute(
            select(ProjectTransfer)
            .where(
                ProjectTransfer.project_id == project_id,
                ProjectTransfer.to_scope == org_id,
                ProjectTransfer.state.in_(("rejected", "cancelled")),
            )
            .order_by(ProjectTransfer.updated_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if recent is None or recent.updated_at is None:
        return None
    cooldown = (
        PROPOSAL_DECLINE_COOLDOWN
        if recent.state == "rejected"
        else PROPOSAL_CANCEL_COOLDOWN
    )
    updated = recent.updated_at
    if updated.tzinfo is None:
        updated = updated.replace(tzinfo=timezone.utc)
    until = updated + cooldown
    if until <= now:
        return None
    return int((until - now).total_seconds())


async def _proposal_still_valid(
    db: AsyncSession, transfer: ProjectTransfer, project: Project | None
) -> bool:
    """Re-verify a proposal's preconditions at accept time (GH #96 review
    round 1). A proposal is only actionable while the project is still
    personal, the owner still owns it and is still a member of the
    destination org, and the proposer is still an owner/admin of that org."""
    if transfer.proposed_by_user_id is None:
        return False
    if project is None or project.org_id is not None:
        return False
    if project.owner_id != transfer.target_user_id:
        return False
    if not await _user_is_org_member(db, transfer.target_user_id, transfer.to_scope):
        return False
    return await _user_is_org_admin(db, transfer.proposed_by_user_id, transfer.to_scope)


async def _invalidate_proposal(
    db: AsyncSession, transfer: ProjectTransfer, actor_id: str
) -> bool:
    """Mark a proposal that can no longer proceed as expired and write an
    audit row naming the invalidation. Rowcount-1 guarded; the caller
    commits, then notifies the proposer."""
    now = _now()
    result = await db.execute(
        update(ProjectTransfer)
        .where(
            ProjectTransfer.id == transfer.id,
            ProjectTransfer.state == "pending",
        )
        .values(state="expired", updated_at=now)
    )
    if result.rowcount != 1:
        return False
    db.add(
        _proposal_audit(
            actor_id=actor_id,
            action="project_transfer_proposal_expired",
            project_id=transfer.project_id,
            transfer_id=transfer.id,
            proposer_id=transfer.proposed_by_user_id,
            owner_id=transfer.target_user_id,
            reason=transfer.proposal_reason,
            before_scope=transfer.from_scope,
            after_scope=transfer.from_scope,
        )
    )
    return True


# ── Routes ──


@router.post("/api/v1/projects/{project_id}/transfer", response_model=TransferResponse)
async def initiate_transfer(
    project_id: str,
    body: InitiateTransferRequest,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> TransferResponse:
    """Initiate a project transfer.

    Authorization:
        - If from_scope is personal (project.org_id IS NULL): only the
          project owner can initiate.
        - If from_scope is an org: the initiator must be an admin of
          that org. (Project owner-without-admin still cannot extract
          an org project; admin gate is by design.)

    Auto-accept fires when initiator == resolved target_user.
    """
    project = (
        await db.execute(select(Project).where(Project.id == project_id))
    ).scalar_one_or_none()
    if project is None:
        raise HTTPException(404, "Project not found")

    to_scope = body.to
    # GH #70 — take the project scope lock (plus the current and the
    # destination org locks) on EVERY initiation, before any check: all of
    # source scope, authorization, destination standing, the pending check
    # and the stranded-persona guard then run against state no concurrent
    # transfer, merge, ticket or persona write can change until we commit.
    # lock_project_scope re-reads the project after locking.
    await lock_project_scope(
        db, project, extra_org_ids=[None if to_scope == "personal" else to_scope]
    )

    # Source scope.
    from_scope = project.org_id if project.org_id is not None else "personal"

    if to_scope == from_scope:
        raise HTTPException(400, "Transfer source and destination are the same")

    # Validate target scope exists when it's an org.
    if to_scope != "personal":
        org = (
            await db.execute(
                select(Organization).where(Organization.id == to_scope)
            )
        ).scalar_one_or_none()
        if org is None:
            raise HTTPException(404, "Destination org not found")

    # Authorization on the source.
    if from_scope == "personal":
        if project.owner_id != user.id:
            raise HTTPException(403, "Only the project owner can transfer a personal project")
    else:
        # from_scope is an org_id — only org admins of that org can initiate.
        if not await _user_is_org_admin(db, user.id, from_scope):
            raise HTTPException(403, "Only an admin of the source org can initiate this transfer")

    # Authorization on the destination when moving INTO an org.
    # Codex Phase-2 round-1 HIGH (KB entry 246): without this check, a
    # personal project owner could move a project into ANY org they
    # know the ID of — `_resolve_target_user_id` returns the initiator
    # for personal→org which would then auto-accept. Membership is
    # the load-bearing gate. (Org→org is also gated here: a source-org
    # admin must also belong to the destination org to land the row.)
    if to_scope != "personal":
        if not await _user_is_org_member(db, user.id, to_scope):
            raise HTTPException(
                403,
                "Initiator must be a member of the destination org to transfer into it",
            )

    # Refuse if a pending transfer already exists for this project.
    # Codex Phase-2 round-1 MEDIUM (KB entry 246): without this, two
    # concurrent initiates can both reach 'accepted' and disagree on
    # project.org_id (last writer wins) plus leave two contradictory
    # accepted audit rows. Application-layer guard since SQLite doesn't
    # support partial-unique indexes the same way PG does.
    existing = (
        await db.execute(
            select(ProjectTransfer).where(
                ProjectTransfer.project_id == project.id,
                ProjectTransfer.state == "pending",
            )
        )
    ).scalar_one_or_none()
    expired_proposal: ProjectTransfer | None = None
    if existing is not None:
        # GH #96 review round 1 (L1) — an expired proposal must not keep
        # blocking an owner-initiated transfer: lazily expire it in this same
        # transaction (its proposer is notified after commit). A live pending
        # transfer still refuses the initiate.
        if existing.proposed_by_user_id is not None and _transfer_is_expired(
            existing, _now()
        ):
            if await _lazy_expire_proposal(db, existing, user.id):
                expired_proposal = existing
        else:
            raise HTTPException(
                409,
                {
                    "code": "transfer_pending",
                    "message": (
                        "A pending transfer already exists for this project; "
                        "cancel it before initiating a new one"
                    ),
                },
            )

    # Resolve target_user_id for the inbox.
    target_user_id = await _resolve_target_user_id(db, project, to_scope, user)

    # Auto-accept when initiator IS the target.
    auto_accept = target_user_id == user.id
    if auto_accept:
        # Guard + move + commit all under the locks taken above.
        await _assert_no_stranded_personas(db, project, to_scope, body.force)

    # P2 (§3.3 E1): resolve primary remote from project_repos
    # (source of truth), falling back to the denormalized column.
    from sessionfs.server.services.project_resolver import get_primary_remote
    primary_remote = await get_primary_remote(db, project.id)

    now = _now()
    transfer = ProjectTransfer(
        id=f"xfer_{uuid.uuid4().hex[:16]}",
        project_id=project.id,
        project_git_remote_snapshot=primary_remote or project.git_remote_normalized,
        project_name_snapshot=project.name,
        initiated_by=user.id,
        target_user_id=target_user_id,
        from_scope=from_scope,
        to_scope=to_scope,
        state="accepted" if auto_accept else "pending",
        accepted_by=user.id if auto_accept else None,
        accepted_at=now if auto_accept else None,
        created_at=now,
        updated_at=now,
    )
    db.add(transfer)

    if auto_accept:
        # Apply the project move synchronously.
        project.org_id = None if to_scope == "personal" else to_scope
        project.updated_at = now
        # The moved project no longer maps to its old org's Jira project; any
        # existing links go stale rather than writing to an unrelated issue.
        await drop_project_tracker_mapping(db, project.id)

    # The earlier SELECT-then-INSERT precheck handles the friendly
    # 409 message for sequential duplicates. For concurrent initiate
    # races (two requests both pass the SELECT), the partial-unique
    # index `idx_project_transfers_pending_unique` is the DB-level
    # backstop — the second commit collides and we translate it to
    # 409. Codex Phase-2 round-2 catch (KB entry 248).
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(
            409,
            {
                "code": "transfer_pending",
                "message": (
                    "A pending transfer already exists for this project "
                    "(concurrent initiate); cancel it before initiating a new one"
                ),
            },
        ) from None
    await db.refresh(transfer)
    # GH #96 review round 1 (L1) — notify the proposer only after the new
    # transfer is durably committed, so the email never fires for a rolled
    # back transition.
    if expired_proposal is not None:
        await _notify_proposal_outcome(
            request, db, expired_proposal, "send_transfer_proposal_expired"
        )
    return _to_response(transfer)


@router.post(
    "/api/v1/orgs/{org_id}/projects/{project_id}/propose-transfer",
    response_model=ProposeTransferResponse,
)
async def propose_transfer(
    org_id: str,
    project_id: str,
    body: ProposeTransferRequest,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ProposeTransferResponse:
    """An org admin proposes moving a project into their org, pending the
    project owner's consent (GH #96).

    Proposals are allowed only for PERSONAL projects whose owner is an
    active member of the destination org (review round 1 decision): org→org
    moves keep the source-org-admin-gated flows. The destination-org
    owner/admin check runs FIRST, and every project-related refusal for a
    project the proposer has no relationship with is a plain 404, so the
    route cannot be used as an existence/ownership oracle. The response is
    deliberately minimal.
    """
    # 1. Destination org must exist, and the caller must be an owner/admin
    #    of it — checked before any project lookup so a non-admin learns
    #    nothing about the project.
    dest_org = (
        await db.execute(select(Organization).where(Organization.id == org_id))
    ).scalar_one_or_none()
    if dest_org is None:
        raise HTTPException(404, "Destination org not found")
    if not await _user_is_org_admin(db, user.id, org_id):
        raise HTTPException(
            403, "Only an owner or admin of the destination org can propose this transfer"
        )

    # 2. Load the project ONCE and run the proposal preconditions BEFORE
    #    taking any lock. Anything the proposer has no relationship with —
    #    a missing, merged/tombstoned, org-scoped or owner-not-member
    #    project — is a uniform 404, byte-identical to a missing id, so the
    #    route cannot be used as an existence/ownership/merge oracle (review
    #    round 2 M1). This pre-lock path is one project SELECT plus at most
    #    one membership check.
    project = (
        await db.execute(select(Project).where(Project.id == project_id))
    ).scalar_one_or_none()
    if project is None:
        raise HTTPException(404, "Project not found")
    await _refuse_unproposable_project(db, user, project, org_id)

    # 3. Now lock (project + destination org) and re-check the same
    #    preconditions under the lock. lock_project_scope re-reads the
    #    project in place (populate_existing) and refuses only a hard delete;
    #    a project merged while we waited is re-checked below as a plain 404
    #    rather than the 409 project_merged the default require_live raises.
    await lock_project_scope(db, project, extra_org_ids=[org_id], require_live=False)
    await _refuse_unproposable_project(db, user, project, org_id)

    from_scope = project.org_id if project.org_id is not None else "personal"

    # 4. One pending transfer per project. An expired proposal is lazily
    #    expired in this same transaction so it frees the slot (no mid-
    #    request commit — review round 1 L4).
    expired_proposal: ProjectTransfer | None = None
    existing = (
        await db.execute(
            select(ProjectTransfer).where(
                ProjectTransfer.project_id == project.id,
                ProjectTransfer.state == "pending",
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        if existing.proposed_by_user_id is not None and _transfer_is_expired(
            existing, _now()
        ):
            if await _lazy_expire_proposal(db, existing, user.id):
                expired_proposal = existing
        else:
            raise HTTPException(
                409,
                {
                    "code": "transfer_pending",
                    "message": (
                        "A pending transfer already exists for this project; "
                        "cancel it before proposing a new one"
                    ),
                },
            )

    # 5. Cooldown + pending-cap (review round 1 M2).
    now = _now()
    cooldown = await _proposal_cooldown_seconds(db, project.id, org_id, now)
    if cooldown is not None:
        raise HTTPException(
            409,
            {
                "code": "proposal_cooldown",
                "message": (
                    "A proposal for this project was recently declined or "
                    "cancelled; wait before proposing it again."
                ),
                "retry_after": cooldown,
            },
            headers={"Retry-After": str(cooldown)},
        )
    # GH #96 review round 2 L2 — only LIVE pending proposals count toward the
    # cap: an overdue-but-still-'pending' proposal must not consume one of
    # the org's 10 slots (it is lazily expired on its own project's next
    # propose, but a different project's propose must not be blocked by it).
    pending_count = (
        await db.execute(
            select(func.count())
            .select_from(ProjectTransfer)
            .where(
                ProjectTransfer.to_scope == org_id,
                ProjectTransfer.proposed_by_user_id.is_not(None),
                ProjectTransfer.state == "pending",
                ProjectTransfer.expires_at > now,
            )
        )
    ).scalar_one()
    if pending_count >= MAX_PENDING_PROPOSALS_PER_ORG:
        raise HTTPException(
            409,
            {
                "code": "proposal_limit",
                "message": (
                    f"This org already has {MAX_PENDING_PROPOSALS_PER_ORG} "
                    "pending proposals; act on them before proposing more."
                ),
            },
        )

    expires_at = now + timedelta(days=PROPOSAL_EXPIRY_DAYS)

    from sessionfs.server.services.project_resolver import get_primary_remote
    primary_remote = await get_primary_remote(db, project.id)

    transfer = ProjectTransfer(
        id=f"xfer_{uuid.uuid4().hex[:16]}",
        project_id=project.id,
        project_git_remote_snapshot=primary_remote or project.git_remote_normalized,
        project_name_snapshot=project.name,
        initiated_by=user.id,
        target_user_id=project.owner_id,
        from_scope=from_scope,
        to_scope=org_id,
        state="pending",
        proposed_by_user_id=user.id,
        proposal_reason=body.reason,
        expires_at=expires_at,
        created_at=now,
        updated_at=now,
    )
    db.add(transfer)

    db.add(
        _proposal_audit(
            actor_id=user.id,
            action="project_transfer_proposed",
            project_id=project.id,
            transfer_id=transfer.id,
            proposer_id=user.id,
            owner_id=project.owner_id,
            reason=body.reason,
            before_scope=from_scope,
            after_scope=from_scope,
        )
    )

    # Capture values we need for the post-commit email before the commit can
    # expire them (production sessions use expire_on_commit=True).
    owner_id = project.owner_id
    project_name = project.name
    dest_org_name = dest_org.name

    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(
            409,
            {
                "code": "transfer_pending",
                "message": (
                    "A pending transfer already exists for this project "
                    "(concurrent propose); cancel it before proposing a new one"
                ),
            },
        ) from None

    await db.refresh(transfer)

    # Emails fire only after the row is durably committed (L4).
    if expired_proposal is not None:
        await _notify_proposal_outcome(
            request, db, expired_proposal, "send_transfer_proposal_expired"
        )
    owner = (
        await db.execute(select(User).where(User.id == owner_id))
    ).scalar_one_or_none()
    if owner is not None:
        await _send_proposal_email(
            request,
            "send_transfer_proposed",
            to_email=owner.email,
            project_name=project_name,
            org_name=dest_org_name,
            proposer_name=user.display_name or user.email,
            reason=body.reason,
            expires_at_human=expires_at.strftime("%Y-%m-%d"),
        )

    return ProposeTransferResponse(
        transfer_id=transfer.id,
        project_id=transfer.project_id or "",
        status=transfer.state,
        expires_at=transfer.expires_at,
    )


@router.post("/api/v1/transfers/{transfer_id}/accept", response_model=TransferResponse)
async def accept_transfer(
    transfer_id: str,
    request: Request,
    force: bool = Query(False, description=(
        "Accept even if non-terminal tickets are assigned to org personas "
        "that will not exist in the destination (GH #70)."
    )),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> TransferResponse:
    """Target user accepts the transfer.

    Atomic state transition via `UPDATE ... WHERE state='pending'`
    with rowcount check — prevents double-accept and accept-after-
    cancel races. An expired proposal is lazily flipped to 'expired'
    (and its proposer notified) instead of being accepted.
    """
    transfer = (
        await db.execute(
            select(ProjectTransfer).where(ProjectTransfer.id == transfer_id)
        )
    ).scalar_one_or_none()
    if transfer is None:
        raise HTTPException(404, "Transfer not found")

    if transfer.target_user_id != user.id:
        raise HTTPException(403, "Only the target user can accept this transfer")

    if transfer.state != "pending":
        raise HTTPException(409, f"Transfer is {transfer.state!r}, not pending")

    # GH #96 — an expired proposal cannot be accepted; lazily expire it
    # (frees the one-pending slot) and notify the proposer.
    if transfer.proposed_by_user_id is not None and _transfer_is_expired(
        transfer, _now()
    ):
        if await _lazy_expire_proposal(db, transfer, user.id):
            await db.commit()
            await _notify_proposal_outcome(
                request, db, transfer, "send_transfer_proposal_expired"
            )
        raise HTTPException(409, "Transfer has expired")

    current_project = (
        await db.execute(
            select(Project).where(Project.id == transfer.project_id)
        )
    ).scalar_one_or_none() if transfer.project_id is not None else None
    if current_project is not None:
        # GH #70 — lock first (project, then current + destination orgs);
        # every check below and the move + commit run under the locks.
        await lock_project_scope(
            db,
            current_project,
            extra_org_ids=[None if transfer.to_scope == "personal" else transfer.to_scope],
        )
        # The project must still be where this transfer said it was. A
        # concurrent transfer (e.g. a racing auto-accepted move) may have
        # moved it since; accepting would then move it out of an org the
        # initiator never had standing in.
        current_scope = current_project.org_id or "personal"
        if current_scope != transfer.from_scope:
            raise HTTPException(
                409,
                {
                    "code": "transfer_scope_changed",
                    "message": (
                        f"The project is no longer in {transfer.from_scope!r} "
                        "(it moved after this transfer was initiated). Cancel "
                        "or reject this transfer and start a new one."
                    ),
                },
            )

    # Revalidate CURRENT standing (under the lock) — target_user_id can go
    # stale if the user was removed from the destination org between
    # initiate and accept. Codex Phase-2 round-2 catch (KB entry 248).
    #
    # GH #96 review round 1 (M3) — a proposal re-verifies ALL of its
    # preconditions: the project is still personal, its owner still owns it
    # and is a member of the destination org, and the proposer is still an
    # owner/admin there. If any no longer holds, the proposal is marked
    # expired and the accept fails cleanly with 409 proposal_no_longer_valid.
    if transfer.proposed_by_user_id is not None:
        if not await _proposal_still_valid(db, transfer, current_project):
            invalidated = await _invalidate_proposal(db, transfer, user.id)
            if invalidated:
                await db.commit()
                await _notify_proposal_outcome(
                    request, db, transfer, "send_transfer_proposal_expired"
                )
            raise HTTPException(
                409,
                {
                    "code": "proposal_no_longer_valid",
                    "message": (
                        "This proposal can no longer be accepted: the project, "
                        "its owner's membership, or the proposer's standing "
                        "changed since it was proposed."
                    ),
                },
            )
    elif not await _user_has_acceptor_standing(db, user, transfer, current_project):
        raise HTTPException(
            403,
            "You no longer have standing to accept this transfer",
        )
    if current_project is not None:
        await _assert_no_stranded_personas(
            db, current_project, transfer.to_scope, force
        )

    now = _now()
    # Atomic state transition. Affected row count must be exactly 1.
    result = await db.execute(
        update(ProjectTransfer)
        .where(
            ProjectTransfer.id == transfer_id,
            ProjectTransfer.state == "pending",
        )
        .values(
            state="accepted",
            accepted_by=user.id,
            accepted_at=now,
            updated_at=now,
        )
    )
    if result.rowcount != 1:
        # A concurrent writer changed state between our SELECT and UPDATE.
        await db.rollback()
        raise HTTPException(409, "Transfer state changed concurrently; refresh and retry")

    # GH #96 — audit a proposal acceptance: names the proposer and the
    # accepting owner, records the scope move, carries the proposal reason.
    if transfer.proposed_by_user_id is not None:
        db.add(
            _proposal_audit(
                actor_id=user.id,
                action="project_transfer_proposal_accepted",
                project_id=transfer.project_id,
                transfer_id=transfer.id,
                proposer_id=transfer.proposed_by_user_id,
                owner_id=user.id,
                reason=transfer.proposal_reason,
                before_scope=transfer.from_scope,
                after_scope=transfer.to_scope,
            )
        )

    # Apply the project move now that state has flipped.
    if transfer.project_id is not None:
        project = (
            await db.execute(
                select(Project).where(Project.id == transfer.project_id)
            )
        ).scalar_one_or_none()
        if project is not None:
            project.org_id = (
                None if transfer.to_scope == "personal" else transfer.to_scope
            )
            project.updated_at = now
            # Drop the old org's tracker mapping; existing links go stale.
            await drop_project_tracker_mapping(db, project.id)

    await db.commit()
    await db.refresh(transfer)
    return _to_response(transfer)


@router.post("/api/v1/transfers/{transfer_id}/reject", response_model=TransferResponse)
async def reject_transfer(
    transfer_id: str,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> TransferResponse:
    """Target user rejects the transfer (project.org_id unchanged).

    For an admin-proposed transfer (GH #96) the decline also notifies the
    proposer by email and writes an AdminAction audit row.
    """
    transfer = (
        await db.execute(
            select(ProjectTransfer).where(ProjectTransfer.id == transfer_id)
        )
    ).scalar_one_or_none()
    if transfer is None:
        raise HTTPException(404, "Transfer not found")

    if transfer.target_user_id != user.id:
        raise HTTPException(403, "Only the target user can reject this transfer")

    if transfer.state != "pending":
        raise HTTPException(409, f"Transfer is {transfer.state!r}, not pending")

    # GH #96 — an expired proposal is lazily expired (and its proposer
    # notified) instead of rejected.
    if transfer.proposed_by_user_id is not None and _transfer_is_expired(
        transfer, _now()
    ):
        if await _lazy_expire_proposal(db, transfer, user.id):
            await db.commit()
            await _notify_proposal_outcome(
                request, db, transfer, "send_transfer_proposal_expired"
            )
        raise HTTPException(409, "Transfer has expired")

    # Standing recheck. A proposal is declined by the current project
    # owner — declining never moves the project, so destination-org
    # membership is NOT required (unlike accept). Every other transfer
    # re-uses the accept standing rules: a user removed from the
    # destination org is no longer in the loop.
    current_project = (
        await db.execute(
            select(Project).where(Project.id == transfer.project_id)
        )
    ).scalar_one_or_none() if transfer.project_id is not None else None
    if transfer.proposed_by_user_id is not None:
        if current_project is None or current_project.owner_id != user.id:
            raise HTTPException(
                403, "Only the current project owner can decline this proposal"
            )
    elif not await _user_has_acceptor_standing(db, user, transfer, current_project):
        raise HTTPException(
            403,
            "You no longer have standing to act on this transfer",
        )

    now = _now()
    result = await db.execute(
        update(ProjectTransfer)
        .where(
            ProjectTransfer.id == transfer_id,
            ProjectTransfer.state == "pending",
        )
        .values(state="rejected", updated_at=now)
    )
    if result.rowcount != 1:
        await db.rollback()
        raise HTTPException(409, "Transfer state changed concurrently; refresh and retry")

    # GH #96 — audit a proposal decline: names the proposer and the
    # declining owner; the scope is unchanged.
    if transfer.proposed_by_user_id is not None:
        db.add(
            _proposal_audit(
                actor_id=user.id,
                action="project_transfer_proposal_declined",
                project_id=transfer.project_id,
                transfer_id=transfer.id,
                proposer_id=transfer.proposed_by_user_id,
                owner_id=user.id,
                reason=transfer.proposal_reason,
                before_scope=transfer.from_scope,
                after_scope=transfer.from_scope,
            )
        )

    await db.commit()
    await db.refresh(transfer)

    # Notify the proposer (best-effort, never raises) after the row is
    # durably rejected so the email only fires for a real transition.
    if transfer.proposed_by_user_id is not None:
        await _notify_proposal_outcome(
            request, db, transfer, "send_transfer_proposal_declined"
        )
    return _to_response(transfer)


@router.post("/api/v1/transfers/{transfer_id}/cancel", response_model=TransferResponse)
async def cancel_transfer(
    transfer_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> TransferResponse:
    """Initiator cancels a pending transfer."""
    transfer = (
        await db.execute(
            select(ProjectTransfer).where(ProjectTransfer.id == transfer_id)
        )
    ).scalar_one_or_none()
    if transfer is None:
        raise HTTPException(404, "Transfer not found")

    if transfer.initiated_by != user.id:
        raise HTTPException(403, "Only the initiator can cancel this transfer")

    if transfer.state != "pending":
        raise HTTPException(409, f"Transfer is {transfer.state!r}, not pending")

    now = _now()
    result = await db.execute(
        update(ProjectTransfer)
        .where(
            ProjectTransfer.id == transfer_id,
            ProjectTransfer.state == "pending",
        )
        .values(state="cancelled", updated_at=now)
    )
    if result.rowcount != 1:
        await db.rollback()
        raise HTTPException(409, "Transfer state changed concurrently; refresh and retry")

    # GH #96 — a proposal cancel (by the proposing admin) is audited too.
    if transfer.proposed_by_user_id is not None:
        db.add(
            _proposal_audit(
                actor_id=user.id,
                action="project_transfer_proposal_cancelled",
                project_id=transfer.project_id,
                transfer_id=transfer.id,
                proposer_id=transfer.proposed_by_user_id,
                owner_id=transfer.target_user_id,
                reason=transfer.proposal_reason,
                before_scope=transfer.from_scope,
                after_scope=transfer.from_scope,
            )
        )

    await db.commit()
    await db.refresh(transfer)
    return _to_response(transfer)


@router.get("/api/v1/transfers", response_model=TransferListResponse)
async def list_transfers(
    request: Request,
    direction: Literal["incoming", "outgoing"] = "incoming",
    state: str | None = None,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> TransferListResponse:
    """List transfers visible to the requesting user.

    direction=incoming filters to transfers where target_user_id == user.
    direction=outgoing filters to transfers where initiated_by == user.
    Optional state filter (pending / accepted / rejected / cancelled).

    Overdue proposals are lazily expired (and their proposers notified)
    before the list is built, so an expired proposal never shows as pending.
    """
    # GH #96 review round 1 (L1) — expire overdue proposals visible in this
    # direction first, in their own transaction, then notify their proposers.
    overdue_stmt = select(ProjectTransfer).where(
        ProjectTransfer.state == "pending",
        ProjectTransfer.proposed_by_user_id.is_not(None),
        ProjectTransfer.expires_at.is_not(None),
    )
    if direction == "incoming":
        overdue_stmt = overdue_stmt.where(ProjectTransfer.target_user_id == user.id)
    else:
        overdue_stmt = overdue_stmt.where(ProjectTransfer.initiated_by == user.id)
    overdue = (await db.execute(overdue_stmt)).scalars().all()
    newly_expired: list[ProjectTransfer] = []
    for t in overdue:
        if _transfer_is_expired(t, _now()):
            if await _lazy_expire_proposal(db, t, user.id):
                newly_expired.append(t)
    if newly_expired:
        await db.commit()
        for t in newly_expired:
            await _notify_proposal_outcome(
                request, db, t, "send_transfer_proposal_expired"
            )

    stmt = select(ProjectTransfer)
    if direction == "incoming":
        stmt = stmt.where(ProjectTransfer.target_user_id == user.id)
    else:
        stmt = stmt.where(ProjectTransfer.initiated_by == user.id)
    if state:
        stmt = stmt.where(ProjectTransfer.state == state)
    stmt = stmt.order_by(ProjectTransfer.created_at.desc())

    rows = (await db.execute(stmt)).scalars().all()
    names = await _proposed_by_names(db, rows)
    return TransferListResponse(
        transfers=[_to_response(t, proposed_by_name=names.get(t.proposed_by_user_id)) for t in rows]
    )
