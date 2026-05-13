"""Multi-org member management — v0.10.0 Phase 3a.

Parallels the existing single-org routes in `routes/org.py` but takes
an explicit `org_id` path parameter so callers can manage any org they
admin, not just their `default_org_id`. The single-org `/api/v1/org/*`
surface stays as a compatibility shim for the dashboard's existing
single-org assumptions; new dashboard code (Phase 3b) targets these
multi-org routes.

Endpoints:
    GET    /api/v1/orgs/{org_id}/members             — list (member or admin)
    POST   /api/v1/orgs/{org_id}/members/invite      — invite (admin only)
    PUT    /api/v1/orgs/{org_id}/members/{user_id}/role
                                                     — promote/demote (admin only)
    DELETE /api/v1/orgs/{org_id}/members/{user_id}   — remove (admin only)

CEO invariants enforced on removal (KB entry 230 #3 — data stays,
access revoked):
    1. Sessions stay with the user (no change — sessions are user-owned).
    2. Projects owned by the removed member that are org-scoped here
       auto-transfer to the removing admin (project.owner_id ← admin.id;
       project.org_id stays). Audited via a ProjectTransfer row in
       state='accepted' (initiated_by=admin, target_user=admin,
       from_scope=org_id, to_scope=org_id). Same shape as a normal
       org→org admin-initiated transfer.
    3. KB entries authored by the removed member stay; authored_by
       preserved (no FK CASCADE; honoring the audit identity).
    4. If the removed member's default_org_id was THIS org, null it
       (Phase 1 FK ON DELETE SET NULL semantics — we apply explicitly
       since SET NULL fires on org delete, not on member-removal).
    5. Pending transfers where the removed member is the target →
       cancel them (target standing is gone; KB entry 248 invariant).

Self-removal of the last admin is blocked. Role-demotion of the last
admin is blocked (extends the existing single-org route's guard).
"""

from __future__ import annotations

import logging
import secrets
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, field_validator
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server.auth.dependencies import get_current_user
from sessionfs.server.db.engine import get_db
from sessionfs.server.db.models import (
    OrgInvite,
    OrgMember,
    Organization,
    Project,
    ProjectTransfer,
    User,
)

logger = logging.getLogger("sessionfs.api")
router = APIRouter(prefix="/api/v1/orgs", tags=["org-members"])


# ── Request / response models ──


class InviteRequest(BaseModel):
    email: str
    role: str = "member"

    @field_validator("email")
    @classmethod
    def _norm_email(cls, v: str) -> str:
        return v.strip().lower()


class ChangeRoleRequest(BaseModel):
    role: str


class MemberInfo(BaseModel):
    user_id: str
    email: str
    display_name: str | None
    role: str
    joined_at: datetime | None


class MembersListResponse(BaseModel):
    org_id: str
    members: list[MemberInfo]
    seats_used: int
    seats_limit: int
    current_user_role: str | None


class InviteResponse(BaseModel):
    invite_id: str
    email: str
    role: str


class RemoveMemberResponse(BaseModel):
    removed: str
    projects_transferred: int
    pending_transfers_cancelled: int


# ── Helpers ──


async def _user_role_in_org(
    db: AsyncSession, user_id: str, org_id: str
) -> str | None:
    row = (
        await db.execute(
            select(OrgMember).where(
                OrgMember.org_id == org_id,
                OrgMember.user_id == user_id,
            )
        )
    ).scalar_one_or_none()
    return row.role if row else None


async def _count_admins(db: AsyncSession, org_id: str) -> int:
    result = await db.execute(
        select(OrgMember).where(
            OrgMember.org_id == org_id, OrgMember.role == "admin"
        )
    )
    return len(result.scalars().all())


async def _org_or_404(db: AsyncSession, org_id: str) -> Organization:
    org = (
        await db.execute(
            select(Organization).where(Organization.id == org_id)
        )
    ).scalar_one_or_none()
    if org is None:
        raise HTTPException(404, "Org not found")
    return org


async def _require_admin(
    db: AsyncSession, user: User, org_id: str
) -> None:
    role = await _user_role_in_org(db, user.id, org_id)
    if role != "admin":
        raise HTTPException(403, "Admin role required")


async def _require_member(
    db: AsyncSession, user: User, org_id: str
) -> str:
    role = await _user_role_in_org(db, user.id, org_id)
    if role is None:
        raise HTTPException(403, "You are not a member of this org")
    return role


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ── Routes ──


class MyOrgEntry(BaseModel):
    """One row of GET /api/v1/orgs — the current user's membership."""

    org_id: str
    name: str
    role: str


class MyOrgsResponse(BaseModel):
    orgs: list[MyOrgEntry]


@router.get("", response_model=MyOrgsResponse)
async def list_my_orgs(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> MyOrgsResponse:
    """List orgs the current user belongs to.

    v0.10.0 Phase 4 Round 3 (KB entry 278): the dashboard's `useMyOrgs`
    needs the full membership set so TransferPanel's destination
    dropdown isn't truncated to the single legacy "primary org". This
    endpoint joins OrgMember → Organization and returns every active
    membership for the requesting user.
    """
    rows = (
        await db.execute(
            select(OrgMember, Organization)
            .join(Organization, OrgMember.org_id == Organization.id)
            .where(OrgMember.user_id == user.id)
            .order_by(Organization.name)
        )
    ).all()
    return MyOrgsResponse(
        orgs=[
            MyOrgEntry(org_id=org.id, name=org.name, role=m.role)
            for m, org in rows
        ]
    )


# ─────────────────────────────────────────────────────────────────────────
# v0.10.0 Phase 6 — org-level KB creation defaults.
#
# DLP policy is handled by its own route (routes/dlp.py), feature-gated
# on `dlp_secrets`. This route owns the three kb_* defaults that new
# org-scoped projects inherit at project-create time (see
# routes/projects.py:create_project). Stored in Organization.settings
# JSON under the "general" key (same column used by DLP under "dlp";
# no migration needed). Admin role required to PUT; any member can GET.
#
# Round 3 (KB entry 298) intentionally removed `retention_days` and
# `compile_model` from this surface — they had no runtime consumers
# and gave admins a misleading success toast. Re-add them once the
# daemon retention path / compile-route model selection are wired.
# ─────────────────────────────────────────────────────────────────────────


class OrgGeneralSettings(BaseModel):
    """Org-level creation defaults for new projects.

    Phase 6 Round 3 (KB entry 298): pared down to the three kb_* fields
    that are actually consumed at project-creation time (see
    routes/projects.py:create_project). The earlier surface also
    exposed `retention_days` and `compile_model`, but Codex flagged
    that no runtime path consumed them — admins saw a success toast
    while production behavior didn't change. Those fields are removed
    from the schema until concrete runtime consumers exist; future
    phases can re-add them once wired."""

    kb_retention_days: int | None = None
    kb_max_context_words: int | None = None
    kb_section_page_limit: int | None = None


def _read_general_settings(org: Organization) -> OrgGeneralSettings:
    import json as _json
    try:
        raw = _json.loads(org.settings) if isinstance(org.settings, str) else (org.settings or {})
    except (ValueError, TypeError):
        raw = {}
    general = raw.get("general", {}) if isinstance(raw, dict) else {}
    return OrgGeneralSettings(
        kb_retention_days=general.get("kb_retention_days"),
        kb_max_context_words=general.get("kb_max_context_words"),
        kb_section_page_limit=general.get("kb_section_page_limit"),
    )


@router.get("/{org_id}/settings", response_model=OrgGeneralSettings)
async def get_org_settings(
    org_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> OrgGeneralSettings:
    """Return the org's general settings. Any member can read."""
    org = await _org_or_404(db, org_id)
    await _require_member(db, user, org_id)
    return _read_general_settings(org)


@router.put("/{org_id}/settings", response_model=OrgGeneralSettings)
async def update_org_settings(
    org_id: str,
    body: OrgGeneralSettings,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> OrgGeneralSettings:
    """Update the org's general settings. Admin only.

    Server-side validation: ranges that protect against accidentally
    setting nonsensical defaults that would brick new projects (e.g.,
    kb_retention_days=0). Negative values, zero, or values past the
    24-month / word / page ceilings are rejected.
    """
    org = await _org_or_404(db, org_id)
    role = await _require_member(db, user, org_id)
    if role != "admin":
        raise HTTPException(403, "Only org admins can change settings")

    # Range guards — None means "no override" and is always accepted.
    if body.kb_retention_days is not None and not (1 <= body.kb_retention_days <= 730):
        raise HTTPException(400, "kb_retention_days must be between 1 and 730")
    if body.kb_max_context_words is not None and not (
        100 <= body.kb_max_context_words <= 50000
    ):
        raise HTTPException(400, "kb_max_context_words must be between 100 and 50000")
    if body.kb_section_page_limit is not None and not (
        1 <= body.kb_section_page_limit <= 200
    ):
        raise HTTPException(400, "kb_section_page_limit must be between 1 and 200")

    import json as _json
    try:
        raw = _json.loads(org.settings) if isinstance(org.settings, str) else (org.settings or {})
    except (ValueError, TypeError):
        raw = {}
    if not isinstance(raw, dict):
        raw = {}
    raw["general"] = {
        k: v for k, v in body.model_dump().items() if v is not None
    }
    org.settings = _json.dumps(raw)
    await db.commit()
    await db.refresh(org)
    return _read_general_settings(org)


@router.get("/{org_id}/members", response_model=MembersListResponse)
async def list_members(
    org_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> MembersListResponse:
    """List members of an org. Any member (admin or plain) can see the roster."""
    org = await _org_or_404(db, org_id)
    role = await _require_member(db, user, org_id)

    rows = (
        await db.execute(
            select(OrgMember, User)
            .join(User, OrgMember.user_id == User.id)
            .where(OrgMember.org_id == org_id)
        )
    ).all()

    members = [
        MemberInfo(
            user_id=m.user_id,
            email=u.email,
            display_name=u.display_name,
            role=m.role,
            joined_at=m.joined_at,
        )
        for m, u in rows
    ]
    return MembersListResponse(
        org_id=org_id,
        members=members,
        seats_used=len(members),
        seats_limit=org.seats_limit,
        current_user_role=role,
    )


@router.post("/{org_id}/members/invite", response_model=InviteResponse)
async def invite_member(
    org_id: str,
    data: InviteRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> InviteResponse:
    """Invite a user to the org. Admin only."""
    org = await _org_or_404(db, org_id)
    await _require_admin(db, user, org_id)

    if data.role not in ("admin", "member"):
        raise HTTPException(400, "Role must be 'admin' or 'member'")

    # Seat limit.
    current = (
        await db.execute(select(OrgMember).where(OrgMember.org_id == org_id))
    ).scalars().all()
    if len(current) >= org.seats_limit:
        raise HTTPException(
            403,
            {
                "error": "seat_limit",
                "seats_used": len(current),
                "seats_limit": org.seats_limit,
                "message": "All seats are in use. Upgrade for more seats.",
            },
        )

    # Active invite for the same email?
    if (
        await db.execute(
            select(OrgInvite).where(
                OrgInvite.org_id == org_id,
                OrgInvite.email == data.email,
                OrgInvite.accepted_at.is_(None),
            )
        )
    ).scalar_one_or_none():
        raise HTTPException(409, "An active invite already exists for this email")

    # Existing member?
    target_user = (
        await db.execute(select(User).where(User.email == data.email))
    ).scalar_one_or_none()
    if target_user:
        existing = (
            await db.execute(
                select(OrgMember).where(
                    OrgMember.org_id == org_id,
                    OrgMember.user_id == target_user.id,
                )
            )
        ).scalar_one_or_none()
        if existing:
            raise HTTPException(409, "This user is already a member")

    invite_id = f"inv_{secrets.token_hex(8)}"
    db.add(
        OrgInvite(
            id=invite_id,
            org_id=org_id,
            email=data.email,
            role=data.role,
            invited_by=user.id,
            created_at=_now(),
            expires_at=_now().replace(microsecond=0) + _invite_ttl(),
        )
    )
    await db.commit()
    return InviteResponse(invite_id=invite_id, email=data.email, role=data.role)


def _invite_ttl():
    # 7-day TTL — matches the existing single-org invite contract.
    from datetime import timedelta

    return timedelta(days=7)


async def perform_role_change(
    db: AsyncSession,
    actor: User,
    org_id: str,
    target_user_id: str,
    new_role: str,
) -> dict:
    """Shared role-change service called by BOTH the new multi-org
    route AND the legacy `/api/v1/org/members/{user_id}/role` route.

    Codex Phase-3a round-5 MEDIUM (KB entry 262): without this
    extraction the legacy route bypassed the admin→member source-
    authority cleanup that the new route enforced. Both routes now
    delegate so the demotion-side cleanup fires regardless of URL.

    Guards:
      - new_role must be 'admin' or 'member' (400)
      - actor cannot change their own role (400)
      - non-member target (404)
      - last-admin demotion (400) — restored from the legacy route
        (it had been accidentally dropped during earlier round
        refactoring; this extraction makes the guard load-bearing on
        both surfaces and adds explicit test coverage)

    Side effect on admin→member demotion:
      - `cancel_outgoing_pending_from_org()` revokes source-authority
        pending transfers initiated from this org by the demoted user.

    Caller MUST have already verified that `actor` is an admin of
    `org_id` (legacy via `check_role`, new via `_require_admin`).
    Service trusts that and runs the rest.
    """
    if new_role not in ("admin", "member"):
        raise HTTPException(400, "Role must be 'admin' or 'member'")
    if target_user_id == actor.id:
        raise HTTPException(400, "Cannot change your own role")

    member = (
        await db.execute(
            select(OrgMember).where(
                OrgMember.org_id == org_id,
                OrgMember.user_id == target_user_id,
            )
        )
    ).scalar_one_or_none()
    if member is None:
        raise HTTPException(404, "Member not found in this org")

    # Last-admin demotion guard — Codex Phase-3a round-6 MEDIUM (KB
    # 264) noted the pre-UPDATE COUNT was concurrency-unsafe: two
    # concurrent cross-demotions could both observe `count == 2` and
    # both commit, leaving 0 admins. Fix: SELECT ... FOR UPDATE on
    # ALL admin rows of this org BEFORE counting. PG row-locks the
    # admin set; a concurrent demotion blocks until our transaction
    # commits, then sees the updated count and fires its own guard.
    # SQLite ignores FOR UPDATE but its single-writer model already
    # serializes by default, so the race doesn't exist there.
    #
    # The lock is acquired on every role-change call (not just
    # demotions) so the post-UPDATE recount and the legacy single-
    # statement test paths both honor it consistently.
    if member.role == "admin" and new_role == "member":
        await db.execute(
            select(OrgMember)
            .where(
                OrgMember.org_id == org_id,
                OrgMember.role == "admin",
            )
            .with_for_update()
        )
        if await _count_admins(db, org_id) <= 1:
            raise HTTPException(
                400, "Cannot demote the last admin of the org"
            )

    # Snapshot the old role for the post-UPDATE demotion-edge check.
    was_admin = member.role == "admin"

    await db.execute(
        update(OrgMember)
        .where(
            OrgMember.org_id == org_id, OrgMember.user_id == target_user_id
        )
        .values(role=new_role)
    )

    # Admin→member demotion revokes source-side authority. Symmetric
    # with `perform_member_removal` (KB entries 258, 260, 262).
    if was_admin and new_role == "member":
        await cancel_outgoing_pending_from_org(
            db, target_user_id, org_id, _now()
        )

    await db.commit()
    return {"user_id": target_user_id, "role": new_role}


@router.put("/{org_id}/members/{user_id}/role")
async def change_member_role(
    org_id: str,
    user_id: str,
    data: ChangeRoleRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Change a member's role. Admin only.

    Thin route — all logic delegated to `perform_role_change()` so
    the legacy `/api/v1/org/members/{user_id}/role` route applies
    the same guards and demotion-cleanup (Codex Phase-3a round-5
    MEDIUM, KB entry 262).
    """
    await _org_or_404(db, org_id)
    await _require_admin(db, user, org_id)
    return await perform_role_change(db, user, org_id, user_id, data.role)


async def cancel_outgoing_pending_from_org(
    db: AsyncSession,
    user_id: str,
    org_id: str,
    now: datetime,
) -> int:
    """Cancel pending transfers whose source authority was the
    user's admin role in `org_id`.

    Called from `perform_member_removal` (user removed entirely) AND
    `change_member_role` when demoting admin→member (Codex Phase-3a
    round-4 MEDIUM, KB entry 260). In both cases the user has lost
    the source-side authority that authorized the transfer at
    initiate-time.

    Returns the count of transfers cancelled.

    `from_scope == 'personal'` outgoing transfers stay — their
    source authority is project ownership, not org role. Outgoing
    transfers from OTHER orgs stay — different authority source.
    """
    outgoing = (
        await db.execute(
            select(ProjectTransfer).where(
                ProjectTransfer.state == "pending",
                ProjectTransfer.initiated_by == user_id,
                ProjectTransfer.from_scope == org_id,
            )
        )
    ).scalars().all()
    cancelled = 0
    for transfer in outgoing:
        if transfer.state != "pending":
            continue
        transfer.state = "cancelled"
        transfer.updated_at = now
        cancelled += 1
    return cancelled


async def perform_member_removal(
    db: AsyncSession,
    removing_admin: User,
    org_id: str,
    target_user_id: str,
) -> RemoveMemberResponse:
    """Shared service: enforce ALL Phase 3a CEO invariants on member
    removal regardless of which route called us.

    Called by both:
      - the new `/api/v1/orgs/{org_id}/members/{user_id}` DELETE
      - the legacy `/api/v1/org/members/{user_id}` DELETE (so the
        old compatibility surface ALSO honors the data-stays
        invariants — Codex Phase-3a round-1 HIGH, KB entry 254).

    Caller MUST have already confirmed `removing_admin` is an admin
    of `org_id` (the legacy route does this via ctx.role; the new
    route does via `_require_admin`). This function trusts that and
    runs the invariants.

    Invariants enforced (KB 230 #3):
      1. Member-owned org-scoped projects auto-transfer to admin.
      2. ProjectTransfer audit row created per transferred project.
      3. removed_user.default_org_id ← NULL if it pointed at this org.
      4. Pending transfers whose target standing was THIS org's
         membership are cancelled — narrowed (Codex Phase-3a round-1
         MEDIUM, KB 254): only transfers with `to_scope == org_id`,
         not the user's global pending inbox.
      5. OrgMember row deleted last (no FK cascade issues).

    Guards (raised as HTTPException for clean propagation through
    both routes):
      - self-removal blocked (400)
      - last-admin removal blocked (400)
      - non-member target (404)
    """
    if target_user_id == removing_admin.id:
        raise HTTPException(
            400,
            "Cannot remove yourself. Promote another admin and ask them to remove you, or delete the org.",
        )

    member = (
        await db.execute(
            select(OrgMember).where(
                OrgMember.org_id == org_id,
                OrgMember.user_id == target_user_id,
            )
        )
    ).scalar_one_or_none()
    if member is None:
        raise HTTPException(404, "Member not found in this org")

    if member.role == "admin":
        # Same TOCTOU concern as `perform_role_change`'s demotion
        # guard (Codex Phase-3a rounds 6/8, KB 264/266): two
        # concurrent cross-removals could each observe `count == 2`
        # and both succeed, leaving 0 admins. Lock the admin set
        # before counting. PG row-locks admin rows; SQLite ignores
        # FOR UPDATE but serializes writes globally.
        await db.execute(
            select(OrgMember)
            .where(
                OrgMember.org_id == org_id,
                OrgMember.role == "admin",
            )
            .with_for_update()
        )
        if await _count_admins(db, org_id) <= 1:
            raise HTTPException(
                400,
                "Cannot remove the last admin; promote another member first",
            )

    now = _now()
    projects_transferred = 0
    pending_cancelled = 0

    # 1. Auto-transfer member-owned org-scoped projects. Track the
    #    project_ids whose ownership we change so step 3 can also
    #    cancel pending org→personal transfers that now-stale-target
    #    the removed user (Codex round-2 MEDIUM, KB entry 256).
    member_projects = (
        await db.execute(
            select(Project).where(
                Project.org_id == org_id,
                Project.owner_id == target_user_id,
            )
        )
    ).scalars().all()
    ownership_flipped_project_ids: list[str] = []
    for project in member_projects:
        db.add(
            ProjectTransfer(
                id=f"xfer_{uuid.uuid4().hex[:16]}",
                project_id=project.id,
                project_git_remote_snapshot=project.git_remote_normalized,
                project_name_snapshot=project.name,
                initiated_by=removing_admin.id,
                target_user_id=removing_admin.id,
                from_scope=org_id,
                to_scope=org_id,
                state="accepted",
                accepted_by=removing_admin.id,
                accepted_at=now,
                created_at=now,
                updated_at=now,
            )
        )
        project.owner_id = removing_admin.id
        project.updated_at = now
        projects_transferred += 1
        ownership_flipped_project_ids.append(project.id)

    # 2. Clear default_org_id pointer if it was this org.
    await db.execute(
        update(User)
        .where(
            User.id == target_user_id, User.default_org_id == org_id
        )
        .values(default_org_id=None)
    )

    # 3. Cancel stale pending transfers tied to this membership.
    #    Two distinct stale classes need cancellation:
    #
    #    (a) INCOMING (target_user_id == removed_user): the removed
    #        user no longer has standing to accept. Sub-classified
    #        by to_scope:
    #          - `to_scope == org_id`: target standing came from
    #            admin/member role here, now gone. CANCEL.
    #          - `to_scope == 'personal'` AND project just flipped:
    #            target was waiting to receive personal custody of
    #            a project they no longer own. CANCEL.
    #          - `to_scope == 'personal'` AND project NOT flipped:
    #            standing comes from owning a different project;
    #            unrelated. LEAVE.
    #          - `to_scope == different_org_id`: standing comes from
    #            a different org's membership; unrelated. LEAVE.
    #
    #    (b) OUTGOING (initiated_by == removed_user, from_scope ==
    #        org_id): the removed admin's source-side authority for
    #        this org just disappeared. The target accept path only
    #        re-checks TARGET standing, not initiator standing, so a
    #        pending outgoing transfer would otherwise survive
    #        removal and let the target later land a project move
    #        that no current source-org admin authorized. CANCEL
    #        (Codex Phase-3a round-3 MEDIUM, KB entry 258).
    #
    #        Outgoing transfers with `from_scope == 'personal'` or a
    #        different org_id stay — those rely on personal/other-
    #        org authority that isn't touched by removing the user
    #        from THIS org.
    incoming_pending = (
        await db.execute(
            select(ProjectTransfer).where(
                ProjectTransfer.state == "pending",
                ProjectTransfer.target_user_id == target_user_id,
            )
        )
    ).scalars().all()
    for transfer in incoming_pending:
        target_was_here = transfer.to_scope == org_id
        target_was_owner_of_flipped = (
            transfer.to_scope == "personal"
            and transfer.project_id in ownership_flipped_project_ids
        )
        if target_was_here or target_was_owner_of_flipped:
            transfer.state = "cancelled"
            transfer.updated_at = now
            pending_cancelled += 1

    pending_cancelled += await cancel_outgoing_pending_from_org(
        db, target_user_id, org_id, now
    )

    # 4. Delete the membership row last.
    await db.execute(
        delete(OrgMember).where(
            OrgMember.org_id == org_id, OrgMember.user_id == target_user_id
        )
    )

    await db.commit()

    return RemoveMemberResponse(
        removed=target_user_id,
        projects_transferred=projects_transferred,
        pending_transfers_cancelled=pending_cancelled,
    )


@router.delete(
    "/{org_id}/members/{user_id}", response_model=RemoveMemberResponse
)
async def remove_member(
    org_id: str,
    user_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> RemoveMemberResponse:
    """Remove a member from the org. Admin only.

    Delegates to `perform_member_removal()` so the legacy
    `/api/v1/org/members/{user_id}` route can enforce the exact
    same CEO invariants — Codex Phase-3a round-1 HIGH (KB 254).
    """
    await _org_or_404(db, org_id)
    await _require_admin(db, user, org_id)
    return await perform_member_removal(db, user, org_id, user_id)
