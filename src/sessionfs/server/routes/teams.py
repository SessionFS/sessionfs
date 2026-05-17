"""Team management routes — v0.10.9 Phase 3.

Teams are org sub-groups used as handoff recipients (Team+ tier).
Any org member can list teams; org admins can create/delete teams
and manage membership.

    POST   /api/v1/teams                                — create team (admin)
    GET    /api/v1/teams                                — list teams in caller's org
    GET    /api/v1/teams/{team_id}                      — team detail + members
    DELETE /api/v1/teams/{team_id}                      — delete team (admin)
    POST   /api/v1/teams/{team_id}/members              — add member (admin)
    GET    /api/v1/teams/{team_id}/members              — list members
    DELETE /api/v1/teams/{team_id}/members/{user_id}    — remove member (admin)

Tier-gated by `team_management` (Team+); team handoff itself gated by
`team_handoff` in routes/handoffs.py. Members must already belong to
the same org as the team (no cross-org leakage).
"""

from __future__ import annotations

import secrets
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server.auth.dependencies import get_current_user
from sessionfs.server.db.engine import get_db
from sessionfs.server.db.models import OrgMember, Team, TeamMember, User
from sessionfs.server.schemas.handoffs import (
    CreateTeamRequest,
    TeamMemberAddRequest,
    TeamMemberResponse,
    TeamResponse,
)
from sessionfs.server.tier_gate import (
    UserContext,
    check_feature,
    check_role,
    get_user_context,
)

router = APIRouter(prefix="/api/v1/teams", tags=["teams"])


def _generate_team_id() -> str:
    return f"tm_{secrets.token_hex(8)}"


def _team_to_response(team: Team, member_count: int = 0) -> TeamResponse:
    return TeamResponse(
        id=team.id,
        org_id=team.org_id,
        name=team.name,
        slug=team.slug,
        created_by=team.created_by,
        created_at=team.created_at,
        member_count=member_count,
    )


async def _require_org_admin(ctx: UserContext) -> str:
    """Caller must be an org admin AND have Team+ tier feature.
    Returns the caller's org_id."""
    check_feature(ctx, "team_management")
    check_role(ctx, "admin")
    if ctx.org is None:
        # Should be unreachable — check_role rejects non-org users.
        raise HTTPException(status_code=403, detail="Organization required")
    return ctx.org.id


async def _require_org_member(ctx: UserContext) -> str:
    """Any org member; still needs Team+ tier to even see team management
    surface. Returns the caller's org_id."""
    check_feature(ctx, "team_management")
    if not ctx.is_org_user or ctx.org is None:
        raise HTTPException(status_code=403, detail="Organization required")
    return ctx.org.id


@router.post("", status_code=201, response_model=TeamResponse)
async def create_team(
    body: CreateTeamRequest,
    user: User = Depends(get_current_user),
    ctx: UserContext = Depends(get_user_context),
    db: AsyncSession = Depends(get_db),
):
    """Create a team in the caller's org. Org admins only."""
    org_id = await _require_org_admin(ctx)
    now = datetime.now(timezone.utc)
    team = Team(
        id=_generate_team_id(),
        org_id=org_id,
        name=body.name,
        slug=body.slug,
        created_by=user.id,
        created_at=now,
    )
    db.add(team)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(
            status_code=409,
            detail=f"A team with slug {body.slug!r} already exists in this org",
        )
    await db.refresh(team)
    return _team_to_response(team, member_count=0)


@router.get("", response_model=list[TeamResponse])
async def list_teams(
    ctx: UserContext = Depends(get_user_context),
    db: AsyncSession = Depends(get_db),
):
    """List teams in the caller's org. Any org member."""
    org_id = await _require_org_member(ctx)
    teams = list(
        (
            await db.execute(
                select(Team).where(Team.org_id == org_id).order_by(Team.created_at.asc())
            )
        ).scalars().all()
    )
    if not teams:
        return []
    counts_rows = (
        await db.execute(
            select(TeamMember.team_id, func.count(TeamMember.id))
            .where(TeamMember.team_id.in_([t.id for t in teams]))
            .group_by(TeamMember.team_id)
        )
    ).all()
    counts = {row[0]: row[1] for row in counts_rows}
    return [_team_to_response(t, member_count=counts.get(t.id, 0)) for t in teams]


@router.get("/{team_id}", response_model=TeamResponse)
async def get_team(
    team_id: str,
    ctx: UserContext = Depends(get_user_context),
    db: AsyncSession = Depends(get_db),
):
    """Team detail with member count. Any org member of the team's org."""
    org_id = await _require_org_member(ctx)
    team = (
        await db.execute(select(Team).where(Team.id == team_id))
    ).scalar_one_or_none()
    # Existence is sensitive cross-org — return 404 not 403.
    if team is None or team.org_id != org_id:
        raise HTTPException(status_code=404, detail="Team not found")
    member_count = (
        await db.execute(
            select(func.count(TeamMember.id)).where(TeamMember.team_id == team.id)
        )
    ).scalar_one()
    return _team_to_response(team, member_count=member_count)


@router.delete("/{team_id}", status_code=204)
async def delete_team(
    team_id: str,
    ctx: UserContext = Depends(get_user_context),
    db: AsyncSession = Depends(get_db),
):
    """Delete a team. Org admins only. Cascade deletes team_members rows.
    Pending team handoffs targeting this team get recipient_team_id =
    NULL (FK SET NULL) and are effectively orphaned — sender should
    revoke + redirect after team deletion."""
    org_id = await _require_org_admin(ctx)
    team = (
        await db.execute(select(Team).where(Team.id == team_id))
    ).scalar_one_or_none()
    if team is None or team.org_id != org_id:
        raise HTTPException(status_code=404, detail="Team not found")
    await db.delete(team)
    await db.commit()


@router.post(
    "/{team_id}/members",
    status_code=201,
    response_model=TeamMemberResponse,
)
async def add_team_member(
    team_id: str,
    body: TeamMemberAddRequest,
    user: User = Depends(get_current_user),
    ctx: UserContext = Depends(get_user_context),
    db: AsyncSession = Depends(get_db),
):
    """Add a user to a team. Org admins only. The target user must
    already be a member of the same org — no cross-org membership."""
    org_id = await _require_org_admin(ctx)
    team = (
        await db.execute(select(Team).where(Team.id == team_id))
    ).scalar_one_or_none()
    if team is None or team.org_id != org_id:
        raise HTTPException(status_code=404, detail="Team not found")

    # Target user must already be an org member of the team's org.
    target_membership = (
        await db.execute(
            select(OrgMember).where(
                OrgMember.user_id == body.user_id,
                OrgMember.org_id == org_id,
            )
        )
    ).scalar_one_or_none()
    if target_membership is None:
        raise HTTPException(
            status_code=422,
            detail="Target user is not a member of this org",
        )

    now = datetime.now(timezone.utc)
    member = TeamMember(
        team_id=team_id,
        user_id=body.user_id,
        added_by=user.id,
        added_at=now,
    )
    db.add(member)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(
            status_code=409, detail="User is already a member of this team"
        )
    await db.refresh(member)

    target_email = (
        await db.execute(select(User.email).where(User.id == body.user_id))
    ).scalar_one_or_none()
    return TeamMemberResponse(
        user_id=member.user_id,
        user_email=target_email,
        added_by=member.added_by,
        added_at=member.added_at,
    )


@router.get(
    "/{team_id}/members",
    response_model=list[TeamMemberResponse],
)
async def list_team_members(
    team_id: str,
    ctx: UserContext = Depends(get_user_context),
    db: AsyncSession = Depends(get_db),
):
    """List members of a team. Any org member can view."""
    org_id = await _require_org_member(ctx)
    team = (
        await db.execute(select(Team).where(Team.id == team_id))
    ).scalar_one_or_none()
    if team is None or team.org_id != org_id:
        raise HTTPException(status_code=404, detail="Team not found")

    members = list(
        (
            await db.execute(
                select(TeamMember)
                .where(TeamMember.team_id == team_id)
                .order_by(TeamMember.added_at.asc())
            )
        ).scalars().all()
    )
    if not members:
        return []
    # Batch user-email lookup
    user_ids = {m.user_id for m in members}
    emails = {
        row[0]: row[1]
        for row in (
            await db.execute(
                select(User.id, User.email).where(User.id.in_(user_ids))
            )
        ).all()
    }
    return [
        TeamMemberResponse(
            user_id=m.user_id,
            user_email=emails.get(m.user_id),
            added_by=m.added_by,
            added_at=m.added_at,
        )
        for m in members
    ]


@router.delete("/{team_id}/members/{user_id}", status_code=204)
async def remove_team_member(
    team_id: str,
    user_id: str,
    ctx: UserContext = Depends(get_user_context),
    db: AsyncSession = Depends(get_db),
):
    """Remove a user from a team. Org admins only. No-op if the user
    was not a member (404)."""
    org_id = await _require_org_admin(ctx)
    team = (
        await db.execute(select(Team).where(Team.id == team_id))
    ).scalar_one_or_none()
    if team is None or team.org_id != org_id:
        raise HTTPException(status_code=404, detail="Team not found")
    member = (
        await db.execute(
            select(TeamMember).where(
                TeamMember.team_id == team_id,
                TeamMember.user_id == user_id,
            )
        )
    ).scalar_one_or_none()
    if member is None:
        raise HTTPException(status_code=404, detail="Team member not found")
    await db.delete(member)
    await db.commit()
