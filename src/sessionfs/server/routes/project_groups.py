"""Project groups — org-scoped flat groups of projects (GitHub #101, phase 1).

Phase 1 is the backend core only: data model, access rules, REST API and
audit. The CLI / MCP / dashboard surfaces follow in later phases.

A group is always scoped to one org (``org_id`` immutable) and can only ever
contain projects from that org. A member grant is ``reader`` (read tier),
``editor`` (edit tier) or ``admin`` (edit + manage the group). No group role
grants project-ADMIN standing. Org admins/owners have admin standing on every
group in their org.

Visibility: org admins/owners see every group of their org; any other member
sees only the groups they hold a grant on. Non-members get 404 on every
org-scoped group route, so a group's (or org's) existence never leaks.

Plan gating: creating a group, granting a member and adding a project need
the org plan's ``project_groups`` feature (Team+). Reads, rename, revoke,
remove project and delete keep working without it so admins can clean up
after a downgrade.

REST surface:

    GET    /api/v1/orgs/{org_id}/groups                       — list (member)
    POST   /api/v1/orgs/{org_id}/groups                       — create (org admin)
    GET    /api/v1/orgs/{org_id}/groups/{group_id}            — detail (member with standing)
    PATCH  /api/v1/orgs/{org_id}/groups/{group_id}            — rename/desc/compile_enabled
                                                                (group admin; org admins
                                                                have admin standing)
    DELETE /api/v1/orgs/{org_id}/groups/{group_id}            — soft delete (org admin)
    PUT    /api/v1/orgs/{org_id}/groups/{group_id}/members/{user_id}
                                                              — grant/update (group admin)
    DELETE /api/v1/orgs/{org_id}/groups/{group_id}/members/{user_id}
                                                              — revoke (group admin)
    PUT    /api/v1/orgs/{org_id}/groups/{group_id}/projects/{project_id}
                                                              — add project (group admin
                                                                with edit standing on it)
    DELETE /api/v1/orgs/{org_id}/groups/{group_id}/projects/{project_id}
                                                              — remove project (group admin)
    GET    /api/v1/projects/{project_id}/groups               — groups of a project
                                                                (reader who is an org member)
    GET    /api/v1/me/groups                                  — my groups across orgs
"""

from __future__ import annotations

import re
import secrets
from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, field_validator
from sqlalchemy import and_, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server.auth.dependencies import get_current_user
from sessionfs.server.auth.project_access import (
    GROUP_ROLE_ADMIN,
    GROUP_ROLE_EDITOR,
    accessible_project_ids_subquery,
    load_project_for_user,
    project_edit_forbidden,
    user_can_access_project,
    user_can_edit_project,
)
from sessionfs.server.db.engine import get_db
from sessionfs.server.db.models import (
    OrgMember,
    Organization,
    Project,
    ProjectGroup,
    ProjectGroupAccess,
    ProjectGroupMember,
    User,
)
from sessionfs.server.roles import ADMIN_ROLES
from sessionfs.server.routes.org_members import _org_or_404
from sessionfs.server.services import group_compiler
from sessionfs.server.services.project_groups import group_audit_event
from sessionfs.server.tier_gate import (
    resolve_org_plan_tier,
)
from sessionfs.server.tiers import (
    get_features_for_tier,
    get_minimum_tier_for_feature,
)

router = APIRouter(prefix="/api/v1/orgs", tags=["project-groups"])
project_router = APIRouter(prefix="/api/v1/projects", tags=["project-groups"])
me_router = APIRouter(prefix="/api/v1/me", tags=["project-groups"])

_SLUG_CHARS_RE = re.compile(r"[^a-z0-9]+")
_SLUG_RE = re.compile(r"[a-z0-9][a-z0-9-]*")


def _new_group_id() -> str:
    return f"pg_{secrets.token_hex(12)}"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _slugify(name: str) -> str:
    slug = _SLUG_CHARS_RE.sub("-", name.lower()).strip("-")
    return slug[:100] or f"group-{secrets.token_hex(4)}"


def _clean_name(v: str) -> str:
    v = (v or "").strip()
    if not v or len(v) > 255:
        raise ValueError("name must be 1–255 characters")
    return v


def _clean_slug(v: str) -> str:
    v = v.strip().lower()
    if not v or len(v) > 100 or not _SLUG_RE.fullmatch(v):
        raise ValueError("slug must be 1–100 lowercase alphanumeric/hyphen chars")
    return v


# ── Request / response models ──


class GroupCreateRequest(BaseModel):
    name: str
    slug: str | None = None
    description: str | None = None

    @field_validator("name")
    @classmethod
    def _validate_name(cls, v: str) -> str:
        return _clean_name(v)

    @field_validator("slug")
    @classmethod
    def _validate_slug(cls, v: str | None) -> str | None:
        return None if v is None else _clean_slug(v)


class GroupUpdateRequest(BaseModel):
    """Partial update. ``name``/``slug`` are validated exactly as on create;
    an explicit ``null`` for either leaves it unchanged. ``description: null``
    clears the description. ``compile_enabled`` opts the group into (or out
    of) the group knowledge-context compile; ``null`` leaves it unchanged."""

    name: str | None = None
    slug: str | None = None
    description: str | None = None
    compile_enabled: bool | None = None

    @field_validator("name")
    @classmethod
    def _validate_name(cls, v: str | None) -> str | None:
        return None if v is None else _clean_name(v)

    @field_validator("slug")
    @classmethod
    def _validate_slug(cls, v: str | None) -> str | None:
        return None if v is None else _clean_slug(v)


class GrantMemberRequest(BaseModel):
    role: Literal["reader", "editor", "admin"]


class ProjectGroupSummary(BaseModel):
    id: str
    org_id: str
    slug: str
    name: str
    description: str | None = None
    member_count: int = 0
    project_count: int = 0
    my_role: str | None = None


class ProjectGroupMemberResponse(BaseModel):
    user_id: str
    email: str
    role: str
    granted_by_user_id: str | None = None
    created_at: datetime | None = None


class ProjectGroupDetail(BaseModel):
    id: str
    org_id: str
    slug: str
    name: str
    description: str | None = None
    my_role: str | None = None
    compile_enabled: bool = False
    context_compiled_at: datetime | None = None
    members: list[ProjectGroupMemberResponse]
    projects: list[str]


class GroupKnowledgeHit(BaseModel):
    project_id: str
    project_name: str
    entry_id: int
    entry_type: str
    confidence: float
    freshness: str
    created_at: datetime | None = None
    content: str


class GroupPageItem(BaseModel):
    slug: str
    title: str
    project_id: str
    project_name: str
    updated_at: datetime | None = None


class GroupContextResponse(BaseModel):
    group_id: str
    slug: str
    document: str
    compiled_at: datetime | None = None
    compiled_by_user_id: str | None = None
    sources: list[str] = []


# ── Helpers ──


async def _require_group_feature(db: AsyncSession, org_id: str) -> None:
    """The org's plan must include the project_groups feature (Team+)."""
    plan = await resolve_org_plan_tier(org_id, db)
    if "project_groups" not in get_features_for_tier(plan):
        raise HTTPException(
            status_code=403,
            detail={
                "error": "upgrade_required",
                "feature": "project_groups",
                "plan_scope": "org",
                "current_tier": plan.value,
                "required_tier": get_minimum_tier_for_feature("project_groups"),
                "message": "Project groups require the Team plan or above.",
            },
        )


async def _org_for_member(
    db: AsyncSession, user: User, org_id: str
) -> tuple[Organization, str]:
    """(org, caller's org role). 404 for a missing org AND for a non-member,
    so a non-member learns nothing about the org or its groups."""
    org = await _org_or_404(db, org_id)
    role = (
        await db.execute(
            select(OrgMember.role).where(
                OrgMember.org_id == org_id, OrgMember.user_id == user.id
            )
        )
    ).scalar_one_or_none()
    if role is None:
        raise HTTPException(404, "Org not found")
    return org, role


async def _group_or_404(
    db: AsyncSession, org_id: str, group_id: str
) -> ProjectGroup:
    """Fetch a live group, scoped to org_id (cross-org reads 404, never 403)."""
    group = (
        await db.execute(
            select(ProjectGroup).where(
                ProjectGroup.id == group_id,
                ProjectGroup.org_id == org_id,
                ProjectGroup.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if group is None:
        raise HTTPException(404, "Project group not found")
    return group


async def _explicit_grant(
    db: AsyncSession, group_id: str, user_id: str
) -> ProjectGroupMember | None:
    return (
        await db.execute(
            select(ProjectGroupMember).where(
                ProjectGroupMember.group_id == group_id,
                ProjectGroupMember.user_id == user_id,
            )
        )
    ).scalar_one_or_none()


async def _caller_group_role(
    db: AsyncSession, user: User, group: ProjectGroup, org_role: str
) -> str | None:
    """Effective role of a caller already known to be an org member."""
    if org_role in ADMIN_ROLES:
        return GROUP_ROLE_ADMIN
    grant = await _explicit_grant(db, group.id, user.id)
    return grant.role if grant is not None else None


async def _visible_group_or_404(
    db: AsyncSession, user: User, org_id: str, group_id: str, org_role: str
) -> tuple[ProjectGroup, str]:
    """A live group the caller has standing on: (group, my_role). A member
    without a grant gets 404, like a missing group."""
    group = await _group_or_404(db, org_id, group_id)
    my_role = await _caller_group_role(db, user, group, org_role)
    if my_role is None:
        raise HTTPException(404, "Project group not found")
    return group, my_role


async def _group_by_slug_or_404(
    db: AsyncSession, org_id: str, slug: str
) -> ProjectGroup:
    """Fetch a live group by its org-scoped slug (cross-org reads 404)."""
    group = (
        await db.execute(
            select(ProjectGroup).where(
                ProjectGroup.org_id == org_id,
                ProjectGroup.slug == slug,
                ProjectGroup.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if group is None:
        raise HTTPException(404, "Project group not found")
    return group


async def _visible_group_by_slug_or_404(
    db: AsyncSession, user: User, org_id: str, slug: str, org_role: str
) -> tuple[ProjectGroup, str]:
    """A live slug-addressed group the caller has standing on."""
    group = await _group_by_slug_or_404(db, org_id, slug)
    my_role = await _caller_group_role(db, user, group, org_role)
    if my_role is None:
        raise HTTPException(404, "Project group not found")
    return group, my_role


def _require_group_admin(my_role: str | None) -> None:
    """Raise 403 unless the caller has admin standing on this group.

    Admin standing = org admin/owner (ADMIN_ROLES) or an explicit ``admin``
    grant on the group."""
    if my_role != GROUP_ROLE_ADMIN:
        raise HTTPException(403, "Group admin role required")


def _require_org_admin(org_role: str) -> None:
    if org_role not in ADMIN_ROLES:
        raise HTTPException(403, "Admin or owner role required")


def _audit(
    db: AsyncSession,
    *,
    org: Organization,
    user: User,
    actor_role: str | None,
    event_type: str,
    target_id: str,
    before: dict | None,
    after: dict | None,
) -> None:
    db.add(
        group_audit_event(
            org_id=org.id,
            org_name=org.name,
            actor_user_id=user.id,
            actor_email=user.email,
            actor_role=actor_role,
            event_type=event_type,
            group_id=target_id,
            before=before,
            after=after,
        )
    )


def _group_snapshot(group: ProjectGroup) -> dict:
    return {
        "id": group.id,
        "org_id": group.org_id,
        "slug": group.slug,
        "name": group.name,
        "description": group.description,
        "compile_enabled": bool(group.compile_enabled),
    }


async def _summaries(
    db: AsyncSession, groups: list[ProjectGroup], user_id: str
) -> list[ProjectGroupSummary]:
    """Summaries for ``groups`` with counts and the caller's role, in a fixed
    number of queries regardless of how many groups there are."""
    if not groups:
        return []
    ids = [g.id for g in groups]
    member_counts = dict(
        (
            await db.execute(
                select(ProjectGroupMember.group_id, func.count())
                .where(ProjectGroupMember.group_id.in_(ids))
                .group_by(ProjectGroupMember.group_id)
            )
        ).all()
    )
    project_counts = dict(
        (
            await db.execute(
                select(ProjectGroupAccess.group_id, func.count())
                .where(ProjectGroupAccess.group_id.in_(ids))
                .group_by(ProjectGroupAccess.group_id)
            )
        ).all()
    )
    org_roles = dict(
        (
            await db.execute(
                select(OrgMember.org_id, OrgMember.role).where(
                    OrgMember.user_id == user_id,
                    OrgMember.org_id.in_({g.org_id for g in groups}),
                )
            )
        ).all()
    )
    grants = dict(
        (
            await db.execute(
                select(ProjectGroupMember.group_id, ProjectGroupMember.role).where(
                    ProjectGroupMember.user_id == user_id,
                    ProjectGroupMember.group_id.in_(ids),
                )
            )
        ).all()
    )

    def _my_role(g: ProjectGroup) -> str | None:
        org_role = org_roles.get(g.org_id)
        if org_role is None:
            return None
        if org_role in ADMIN_ROLES:
            return GROUP_ROLE_ADMIN
        return grants.get(g.id)

    return [
        ProjectGroupSummary(
            id=g.id,
            org_id=g.org_id,
            slug=g.slug,
            name=g.name,
            description=g.description,
            member_count=member_counts.get(g.id, 0),
            project_count=project_counts.get(g.id, 0),
            my_role=_my_role(g),
        )
        for g in groups
    ]


async def project_group_summaries(
    db: AsyncSession, project: Project, user_id: str
) -> list[ProjectGroupSummary]:
    """The live groups ``project`` belongs to — shown only to a caller who is
    a member of the project's org (empty otherwise, and for a personal
    project). Shared by ``GET /projects/{id}`` and
    ``GET /projects/{id}/groups``."""
    if project.org_id is None:
        return []
    is_member = (
        await db.execute(
            select(OrgMember.id).where(
                OrgMember.org_id == project.org_id, OrgMember.user_id == user_id
            )
        )
    ).first()
    if is_member is None:
        return []
    groups = list(
        (
            await db.execute(
                select(ProjectGroup)
                .join(
                    ProjectGroupAccess,
                    ProjectGroupAccess.group_id == ProjectGroup.id,
                )
                .where(
                    ProjectGroupAccess.project_id == project.id,
                    ProjectGroup.org_id == project.org_id,
                    ProjectGroup.deleted_at.is_(None),
                )
                .order_by(ProjectGroup.created_at.asc())
            )
        ).scalars().all()
    )
    return await _summaries(db, groups, user_id)


async def _detail(
    db: AsyncSession, group: ProjectGroup, *, my_role: str | None
) -> ProjectGroupDetail:
    member_rows = (
        await db.execute(
            select(ProjectGroupMember, User.email)
            .join(User, User.id == ProjectGroupMember.user_id)
            .where(ProjectGroupMember.group_id == group.id)
            .order_by(ProjectGroupMember.created_at)
        )
    ).all()
    project_ids = (
        await db.execute(
            select(ProjectGroupAccess.project_id).where(
                ProjectGroupAccess.group_id == group.id
            )
        )
    ).scalars().all()
    return ProjectGroupDetail(
        id=group.id,
        org_id=group.org_id,
        slug=group.slug,
        name=group.name,
        description=group.description,
        my_role=my_role,
        compile_enabled=bool(group.compile_enabled),
        context_compiled_at=group.context_compiled_at,
        members=[
            ProjectGroupMemberResponse(
                user_id=m.user_id,
                email=email,
                role=m.role,
                granted_by_user_id=m.granted_by_user_id,
                created_at=m.created_at,
            )
            for m, email in member_rows
        ],
        projects=list(project_ids),
    )


# ── Org-scoped group CRUD ──


@router.get("/{org_id}/groups", response_model=list[ProjectGroupSummary])
async def list_groups(
    org_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[ProjectGroupSummary]:
    """List the org's live groups: every group for an org admin/owner, only
    the groups the caller holds a grant on for any other member."""
    _org, org_role = await _org_for_member(db, user, org_id)
    stmt = select(ProjectGroup).where(
        ProjectGroup.org_id == org_id,
        ProjectGroup.deleted_at.is_(None),
    )
    if org_role not in ADMIN_ROLES:
        stmt = stmt.join(
            ProjectGroupMember,
            and_(
                ProjectGroupMember.group_id == ProjectGroup.id,
                ProjectGroupMember.user_id == user.id,
            ),
        )
    groups = list(
        (await db.execute(stmt.order_by(ProjectGroup.created_at.asc()))).scalars().all()
    )
    return await _summaries(db, groups, user.id)


@router.post("/{org_id}/groups", status_code=201, response_model=ProjectGroupDetail)
async def create_group(
    org_id: str,
    body: GroupCreateRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ProjectGroupDetail:
    """Create a group. Org admin/owner only."""
    org, actor_role = await _org_for_member(db, user, org_id)
    _require_org_admin(actor_role)
    await _require_group_feature(db, org_id)

    slug = body.slug or _slugify(body.name)
    group = ProjectGroup(
        id=_new_group_id(),
        org_id=org_id,
        slug=slug,
        name=body.name,
        description=body.description,
        created_by_user_id=user.id,
        created_at=_now(),
        updated_at=_now(),
    )
    db.add(group)
    try:
        await db.flush()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(
            409, f"A project group with slug '{slug}' already exists in this org"
        )
    _audit(
        db,
        org=org,
        user=user,
        actor_role=actor_role,
        event_type="project_group_created",
        target_id=group.id,
        before=None,
        after=_group_snapshot(group),
    )
    await db.commit()
    return await _detail(db, group, my_role=GROUP_ROLE_ADMIN)


@router.get("/{org_id}/groups/{group_id}", response_model=ProjectGroupDetail)
async def get_group(
    org_id: str,
    group_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ProjectGroupDetail:
    """Group detail (members + projects). Org admins/owners and members
    holding a grant on the group; 404 for everyone else."""
    _org, org_role = await _org_for_member(db, user, org_id)
    group, my_role = await _visible_group_or_404(db, user, org_id, group_id, org_role)
    return await _detail(db, group, my_role=my_role)


@router.patch("/{org_id}/groups/{group_id}", response_model=ProjectGroupDetail)
async def update_group(
    org_id: str,
    group_id: str,
    body: GroupUpdateRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ProjectGroupDetail:
    """Rename / re-slug / re-describe a group, or turn its knowledge-context
    compile on or off. Group admin only (org admins/owners have admin
    standing on every group). Turning ``compile_enabled`` on needs the
    ``project_groups`` feature; turning it off never does, so admins can
    clean up after a downgrade."""
    org, org_role = await _org_for_member(db, user, org_id)
    group, my_role = await _visible_group_or_404(db, user, org_id, group_id, org_role)
    _require_group_admin(my_role)

    before = _group_snapshot(group)
    fields = body.model_fields_set
    if "compile_enabled" in fields and body.compile_enabled is True and not group.compile_enabled:
        await _require_group_feature(db, org_id)
    if "name" in fields and body.name is not None:
        group.name = body.name
    if "slug" in fields and body.slug is not None:
        group.slug = body.slug
    if "description" in fields:
        group.description = body.description
    if "compile_enabled" in fields and body.compile_enabled is not None:
        group.compile_enabled = body.compile_enabled
    group.updated_at = _now()
    new_slug = group.slug
    try:
        await db.flush()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(
            409, f"A project group with slug '{new_slug}' already exists in this org"
        )
    _audit(
        db,
        org=org,
        user=user,
        actor_role=org_role,
        event_type="project_group_updated",
        target_id=group.id,
        before=before,
        after=_group_snapshot(group),
    )
    await db.commit()
    return await _detail(db, group, my_role=my_role)


@router.delete("/{org_id}/groups/{group_id}", status_code=204)
async def delete_group(
    org_id: str,
    group_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    """Soft-delete a group. Org admin/owner only.

    Grants and project links are left in place (FK CASCADE would remove them
    on hard delete; a soft delete keeps them so a later restore — or a direct
    audit read — preserves history). A soft-deleted group grants nothing. The
    slug frees for reuse."""
    org, actor_role = await _org_for_member(db, user, org_id)
    _require_org_admin(actor_role)
    group = await _group_or_404(db, org_id, group_id)

    before = _group_snapshot(group)
    group.deleted_at = _now()
    group.updated_at = _now()
    _audit(
        db,
        org=org,
        user=user,
        actor_role=actor_role,
        event_type="project_group_deleted",
        target_id=group.id,
        before=before,
        after=_group_snapshot(group),
    )
    await db.commit()


# ── Membership grants ──


@router.put(
    "/{org_id}/groups/{group_id}/members/{user_id}",
    response_model=ProjectGroupDetail,
)
async def grant_member(
    org_id: str,
    group_id: str,
    user_id: str,
    body: GrantMemberRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ProjectGroupDetail:
    """Grant or update a member's role in a group. Group admin only.

    The grantee must be a current member of the group's org. The grantee's
    user scope lock and the org scope lock are taken first (the same order
    member removal uses), then the grantee's membership row is read FOR
    SHARE, so a concurrent removal either runs before (we 422) or waits for
    this grant and then deletes it — never leaving an orphan grant.
    """
    from sessionfs.server.services.scope_locks import lock_scopes

    org, org_role = await _org_for_member(db, user, org_id)
    await _require_group_feature(db, org_id)
    await lock_scopes(db, user_ids=[user_id], org_ids=[org_id])
    # Re-read the caller's standing under the locks.
    org, org_role = await _org_for_member(db, user, org_id)
    group, my_role = await _visible_group_or_404(db, user, org_id, group_id, org_role)
    _require_group_admin(my_role)

    grantee = (
        await db.execute(
            select(OrgMember.id)
            .where(OrgMember.org_id == org_id, OrgMember.user_id == user_id)
            .with_for_update(read=True)
        )
    ).first()
    if grantee is None:
        raise HTTPException(
            422,
            {
                "code": "not_org_member",
                "message": "Only active members of this org can be group members.",
            },
        )

    existing = await _explicit_grant(db, group.id, user_id)
    before = (
        {"role": existing.role, "user_id": user_id} if existing is not None else None
    )
    if existing is not None:
        existing.role = body.role
        existing.granted_by_user_id = user.id
    else:
        db.add(
            ProjectGroupMember(
                group_id=group.id,
                user_id=user_id,
                role=body.role,
                granted_by_user_id=user.id,
                created_at=_now(),
            )
        )
    try:
        await db.flush()
    except IntegrityError:
        # A concurrent first grant for the same (group, user) won the race.
        await db.rollback()
        raise HTTPException(
            409,
            {
                "code": "grant_conflict",
                "message": "This member's grant changed concurrently; retry.",
            },
        )
    _audit(
        db,
        org=org,
        user=user,
        actor_role=org_role,
        event_type="project_group_member_granted",
        target_id=group.id,
        before=before,
        after={"user_id": user_id, "role": body.role},
    )
    await db.commit()
    return await _detail(
        db, group, my_role=await _caller_group_role(db, user, group, org_role)
    )


@router.delete(
    "/{org_id}/groups/{group_id}/members/{user_id}",
    response_model=ProjectGroupDetail,
)
async def revoke_member(
    org_id: str,
    group_id: str,
    user_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ProjectGroupDetail:
    """Revoke a member's grant. Group admin only. Works without the plan
    feature so admins can clean up after a downgrade."""
    org, org_role = await _org_for_member(db, user, org_id)
    group, my_role = await _visible_group_or_404(db, user, org_id, group_id, org_role)
    _require_group_admin(my_role)

    existing = await _explicit_grant(db, group.id, user_id)
    if existing is not None:
        revoked_role = existing.role
        await db.delete(existing)
        _audit(
            db,
            org=org,
            user=user,
            actor_role=org_role,
            event_type="project_group_member_revoked",
            target_id=group.id,
            before={"user_id": user_id, "role": revoked_role},
            after=None,
        )
    await db.commit()
    my_role_after = await _caller_group_role(db, user, group, org_role)
    if my_role_after is None:
        # The caller revoked their own grant: they no longer see the group.
        return ProjectGroupDetail(
            id=group.id,
            org_id=group.org_id,
            slug=group.slug,
            name=group.name,
            description=group.description,
            my_role=None,
            members=[],
            projects=[],
        )
    return await _detail(db, group, my_role=my_role_after)


# ── Project placement ──


@router.put(
    "/{org_id}/groups/{group_id}/projects/{project_id}",
    response_model=ProjectGroupDetail,
)
async def add_project_to_group(
    org_id: str,
    group_id: str,
    project_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ProjectGroupDetail:
    """Add a project to a group. Group admin only.

    The caller must be able to read the project (404 otherwise, so the
    project's existence never leaks) and — unless they are an org
    admin/owner — must have edit standing on it: a group admin cannot pull a
    project into their group (and so widen who reads it) without already
    being one of its editors. The project must belong to this org (the
    same-org invariant that keeps a grant from ever exposing a project across
    orgs)."""
    from sessionfs.server.services.scope_locks import lock_scopes

    org, org_role = await _org_for_member(db, user, org_id)
    await _require_group_feature(db, org_id)
    # Lock the project, then the org, before any write: a concurrent transfer
    # (which takes the project lock) can't move it out of the org under us.
    await lock_scopes(db, project_ids=[project_id])
    await lock_scopes(db, org_ids=[org_id])
    org, org_role = await _org_for_member(db, user, org_id)
    group, my_role = await _visible_group_or_404(db, user, org_id, group_id, org_role)
    _require_group_admin(my_role)

    project = (
        await db.execute(
            select(Project)
            .where(Project.id == project_id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if project is None or not await user_can_access_project(db, user.id, project):
        raise HTTPException(404, "Project not found")
    if project.org_id != org_id:
        raise HTTPException(
            422,
            {
                "code": "project_not_in_org",
                "message": "Only projects in this org can join the group.",
            },
        )
    if org_role not in ADMIN_ROLES and not await user_can_edit_project(
        db, user.id, project
    ):
        raise project_edit_forbidden()

    existing = (
        await db.execute(
            select(ProjectGroupAccess).where(
                ProjectGroupAccess.group_id == group.id,
                ProjectGroupAccess.project_id == project_id,
            )
        )
    ).scalar_one_or_none()
    if existing is None:
        db.add(
            ProjectGroupAccess(
                group_id=group.id,
                project_id=project_id,
                added_by_user_id=user.id,
                created_at=_now(),
            )
        )
        try:
            await db.flush()
        except IntegrityError:
            await db.rollback()
            raise HTTPException(
                409,
                {
                    "code": "group_project_conflict",
                    "message": "This project was added concurrently; retry.",
                },
            )
        _audit(
            db,
            org=org,
            user=user,
            actor_role=org_role,
            event_type="project_group_project_added",
            target_id=group.id,
            before=None,
            after={"project_id": project_id},
        )
    await db.commit()
    return await _detail(db, group, my_role=my_role)


@router.delete(
    "/{org_id}/groups/{group_id}/projects/{project_id}",
    response_model=ProjectGroupDetail,
)
async def remove_project_from_group(
    org_id: str,
    group_id: str,
    project_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ProjectGroupDetail:
    """Remove a project from a group. Group admin only. Works without the
    plan feature so admins can clean up after a downgrade."""
    org, org_role = await _org_for_member(db, user, org_id)
    group, my_role = await _visible_group_or_404(db, user, org_id, group_id, org_role)
    _require_group_admin(my_role)

    existing = (
        await db.execute(
            select(ProjectGroupAccess).where(
                ProjectGroupAccess.group_id == group.id,
                ProjectGroupAccess.project_id == project_id,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        await db.delete(existing)
        _audit(
            db,
            org=org,
            user=user,
            actor_role=org_role,
            event_type="project_group_project_removed",
            target_id=group.id,
            before={"project_id": project_id},
            after=None,
        )
    await db.commit()
    return await _detail(db, group, my_role=my_role)


# ── Project / me scoped reads ──


@project_router.get("/{project_id}/groups", response_model=list[ProjectGroupSummary])
async def list_project_groups(
    project_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[ProjectGroupSummary]:
    """List the live groups a project belongs to. Project readers only; the
    list is empty for a reader who is not a member of the project's org."""
    project = await load_project_for_user(project_id, db, user.id)
    return await project_group_summaries(db, project, user.id)


@me_router.get("/groups", response_model=list[ProjectGroupSummary])
async def list_my_groups(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[ProjectGroupSummary]:
    """Every live group the caller has standing on: an explicit grant in an
    org they still belong to, or any group of an org they administer."""
    admin_org_ids = (
        select(OrgMember.org_id)
        .where(OrgMember.user_id == user.id, OrgMember.role.in_(ADMIN_ROLES))
        .correlate(None)
    )
    granted_group_ids = (
        select(ProjectGroupMember.group_id)
        .join(ProjectGroup, ProjectGroup.id == ProjectGroupMember.group_id)
        .join(
            OrgMember,
            and_(
                OrgMember.org_id == ProjectGroup.org_id,
                OrgMember.user_id == ProjectGroupMember.user_id,
            ),
        )
        .where(ProjectGroupMember.user_id == user.id)
        .correlate(None)
    )
    groups = list(
        (
            await db.execute(
                select(ProjectGroup)
                .where(
                    ProjectGroup.deleted_at.is_(None),
                    (ProjectGroup.org_id.in_(admin_org_ids))
                    | (ProjectGroup.id.in_(granted_group_ids)),
                )
                .order_by(ProjectGroup.created_at.asc())
            )
        ).scalars().all()
    )
    return await _summaries(db, groups, user.id)


# ── Group knowledge context (phase 2) ──


_ENTRY_TYPES = {"decision", "pattern", "discovery", "convention", "bug", "dependency"}
_SEARCH_MIN_CHARS = 3
_SEARCH_MAX_CHARS = 200


@router.get(
    "/{org_id}/groups/{slug}/knowledge/search",
    response_model=list[GroupKnowledgeHit],
)
async def group_knowledge_search(
    org_id: str,
    slug: str,
    q: str | None = Query(None, max_length=_SEARCH_MAX_CHARS),
    entry_type: str | None = None,
    limit: int = Query(100, ge=1, le=100),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[GroupKnowledgeHit]:
    """Search active KB claims across the group's readable member projects.

    ``q`` (optional) is a case-insensitive substring match of 3–200
    characters; ``%`` and ``_`` match literally."""
    _org, org_role = await _org_for_member(db, user, org_id)
    group, _my_role = await _visible_group_by_slug_or_404(
        db, user, org_id, slug, org_role
    )
    if entry_type is not None and entry_type not in _ENTRY_TYPES:
        raise HTTPException(
            422, f"Invalid entry_type. Must be one of: {', '.join(sorted(_ENTRY_TYPES))}"
        )
    if q is not None and q.strip() and len(q.strip()) < _SEARCH_MIN_CHARS:
        # Same floor as the project KB search (routes/knowledge.py): 1–2
        # characters can't use the trigram index and are nearly always typos.
        raise HTTPException(422, "`q` must be at least 3 characters")
    hits = await group_compiler.search_group_knowledge(
        db, group, user.id, query=q, entry_type=entry_type, limit=limit
    )
    return [GroupKnowledgeHit(**hit) for hit in hits]


@router.get("/{org_id}/groups/{slug}/pages", response_model=list[GroupPageItem])
async def group_pages(
    org_id: str,
    slug: str,
    limit: int = Query(200, ge=1, le=500),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[GroupPageItem]:
    """Wiki pages across the group's readable member projects, newest first
    (``limit`` defaults to 200, max 500)."""
    _org, org_role = await _org_for_member(db, user, org_id)
    group, _my_role = await _visible_group_by_slug_or_404(
        db, user, org_id, slug, org_role
    )
    pages = await group_compiler.list_group_pages(db, group, user.id, limit=limit)
    return [GroupPageItem(**page) for page in pages]


@router.post("/{org_id}/groups/{slug}/compile", response_model=GroupContextResponse)
async def compile_group(
    org_id: str,
    slug: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> GroupContextResponse:
    """Compile the group's deterministic knowledge overview.

    Group editor/admin or org admin only. Requires the ``project_groups``
    feature and ``compile_enabled`` on the group (409 otherwise). The output
    is deterministic (no LLM) and stores who compiled it plus the
    contributing project ids."""
    org, org_role = await _org_for_member(db, user, org_id)
    group, my_role = await _visible_group_by_slug_or_404(
        db, user, org_id, slug, org_role
    )
    if my_role not in (GROUP_ROLE_EDITOR, GROUP_ROLE_ADMIN):
        raise HTTPException(403, "Group editor or admin role required")
    await _require_group_feature(db, org_id)
    if not group.compile_enabled:
        raise HTTPException(
            409,
            {
                "code": "group_compile_disabled",
                "message": "This group has not enabled knowledge context compilation.",
            },
        )

    before = {"context_compiled_at": _iso(group.context_compiled_at)}
    stats = await group_compiler.compile_group_context(db, group, user.id)
    group.updated_at = _now()
    _audit(
        db,
        org=org,
        user=user,
        actor_role=org_role,
        event_type="project_group_compiled",
        target_id=group.id,
        before=before,
        after={
            "projects": stats["projects"],
            "claims": stats["claims"],
            "claims_omitted": stats["claims_omitted"],
            "truncated": stats["truncated"],
            "context_compiled_at": _iso(group.context_compiled_at),
        },
    )
    await db.commit()
    return _group_context_response(group)


@router.get("/{org_id}/groups/{slug}/context", response_model=GroupContextResponse)
async def get_group_context(
    org_id: str,
    slug: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> GroupContextResponse:
    """The group's compiled overview.

    Returned only to a caller who can still read every contributing project;
    if a source project is no longer readable (deleted, removed, or now
    restricted), the stored document stays hidden with 403
    ``group_context_partial`` rather than leaking a partial picture."""
    _org, org_role = await _org_for_member(db, user, org_id)
    group, _my_role = await _visible_group_by_slug_or_404(
        db, user, org_id, slug, org_role
    )
    if group.context_compiled_at is None:
        raise HTTPException(404, "No compiled context for this group")

    sources = set(group.context_sources or [])
    readable: set[str] = set()
    if sources:
        # One query: which of the sources can the caller read right now?
        readable = set(
            (
                await db.execute(
                    select(Project.id).where(
                        Project.id.in_(sorted(sources)),
                        Project.id.in_(await accessible_project_ids_subquery(db, user.id)),
                    )
                )
            ).scalars().all()
        )
    if readable != sources:
        raise HTTPException(
            403,
            {
                "code": "group_context_partial",
                "message": "This context draws on a project you can no longer read.",
            },
        )
    return _group_context_response(group)


def _group_context_response(group: ProjectGroup) -> GroupContextResponse:
    return GroupContextResponse(
        group_id=group.id,
        slug=group.slug,
        document=group.context_document,
        compiled_at=group.context_compiled_at,
        compiled_by_user_id=group.compiled_by_user_id,
        sources=list(group.context_sources or []),
    )


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None
