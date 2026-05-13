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

import uuid
from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server.auth.dependencies import get_current_user
from sessionfs.server.db.engine import get_db
from sessionfs.server.db.models import (
    OrgMember,
    Organization,
    Project,
    ProjectTransfer,
    User,
)

router = APIRouter(tags=["project-transfers"])


# ── Request / response models ──


class InitiateTransferRequest(BaseModel):
    """`to`: either the literal "personal" or an org_id."""

    to: str


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
    created_at: datetime
    accepted_at: datetime | None
    updated_at: datetime


class TransferListResponse(BaseModel):
    transfers: list[TransferResponse]


def _to_response(t: ProjectTransfer) -> TransferResponse:
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
        OrgMember.role == "admin",
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

    # org → org. Find an admin of the destination org.
    stmt = (
        select(OrgMember)
        .where(OrgMember.org_id == to_scope, OrgMember.role == "admin")
        .limit(1)
    )
    admin = (await db.execute(stmt)).scalar_one_or_none()
    if admin is None:
        raise HTTPException(400, "Destination org has no admin to accept the transfer")
    return admin.user_id


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ── Routes ──


@router.post("/api/v1/projects/{project_id}/transfer", response_model=TransferResponse)
async def initiate_transfer(
    project_id: str,
    body: InitiateTransferRequest,
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

    # Source scope.
    from_scope = project.org_id if project.org_id is not None else "personal"
    to_scope = body.to

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
    if existing is not None:
        raise HTTPException(
            409,
            "A pending transfer already exists for this project; cancel it before initiating a new one",
        )

    # Resolve target_user_id for the inbox.
    target_user_id = await _resolve_target_user_id(db, project, to_scope, user)

    # Auto-accept when initiator IS the target.
    auto_accept = target_user_id == user.id

    now = _now()
    transfer = ProjectTransfer(
        id=f"xfer_{uuid.uuid4().hex[:16]}",
        project_id=project.id,
        project_git_remote_snapshot=project.git_remote_normalized,
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
            "A pending transfer already exists for this project (concurrent initiate); cancel it before initiating a new one",
        ) from None
    await db.refresh(transfer)
    return _to_response(transfer)


@router.post("/api/v1/transfers/{transfer_id}/accept", response_model=TransferResponse)
async def accept_transfer(
    transfer_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> TransferResponse:
    """Target user accepts the transfer.

    Atomic state transition via `UPDATE ... WHERE state='pending'`
    with rowcount check — prevents double-accept and accept-after-
    cancel races.
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

    # Revalidate CURRENT standing — target_user_id can go stale if
    # the user was removed from the destination org between initiate
    # and accept. Codex Phase-2 round-2 catch (KB entry 248).
    current_project = (
        await db.execute(
            select(Project).where(Project.id == transfer.project_id)
        )
    ).scalar_one_or_none() if transfer.project_id is not None else None
    if not await _user_has_acceptor_standing(db, user, transfer, current_project):
        raise HTTPException(
            403,
            "You no longer have standing to accept this transfer",
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

    await db.commit()
    await db.refresh(transfer)
    return _to_response(transfer)


@router.post("/api/v1/transfers/{transfer_id}/reject", response_model=TransferResponse)
async def reject_transfer(
    transfer_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> TransferResponse:
    """Target user rejects the transfer (project.org_id unchanged)."""
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

    # Same standing recheck as accept. A user who was removed from
    # the destination org should not be able to take any action on
    # the transfer — they're no longer in the loop.
    current_project = (
        await db.execute(
            select(Project).where(Project.id == transfer.project_id)
        )
    ).scalar_one_or_none() if transfer.project_id is not None else None
    if not await _user_has_acceptor_standing(db, user, transfer, current_project):
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

    await db.commit()
    await db.refresh(transfer)
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

    await db.commit()
    await db.refresh(transfer)
    return _to_response(transfer)


@router.get("/api/v1/transfers", response_model=TransferListResponse)
async def list_transfers(
    direction: Literal["incoming", "outgoing"] = "incoming",
    state: str | None = None,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> TransferListResponse:
    """List transfers visible to the requesting user.

    direction=incoming filters to transfers where target_user_id == user.
    direction=outgoing filters to transfers where initiated_by == user.
    Optional state filter (pending / accepted / rejected / cancelled).
    """
    stmt = select(ProjectTransfer)
    if direction == "incoming":
        stmt = stmt.where(ProjectTransfer.target_user_id == user.id)
    else:
        stmt = stmt.where(ProjectTransfer.initiated_by == user.id)
    if state:
        stmt = stmt.where(ProjectTransfer.state == state)
    stmt = stmt.order_by(ProjectTransfer.created_at.desc())

    rows = (await db.execute(stmt)).scalars().all()
    return TransferListResponse(transfers=[_to_response(t) for t in rows])
