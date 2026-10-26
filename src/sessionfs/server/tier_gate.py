"""Tier gating and RBAC middleware for API routes.

P2 (entitlement resolution switch): get_effective_tier and get_user_context
now resolve through the entitlements table FIRST (via resolve_entitlement),
falling back to the legacy org.tier / user.tier columns when no active
entitlement exists.  This ensures behavior is IDENTICAL for backfilled data
(the P1 backfill created entitlements matching current tiers), while new
entitlement mutations (via apply_entitlement) become authoritative.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Protocol

from fastapi import Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server.auth.dependencies import get_current_user
from sessionfs.server.db.engine import get_db
from sessionfs.server.db.models import (
    Entitlement,
    OrgMember,
    Organization,
    Project,
    ProjectFeatureGrandfather,
    User,
)
from sessionfs.server.roles import has_minimum_role
from sessionfs.server.services.entitlements import resolve_entitlement
from sessionfs.server.tiers import Tier, format_bytes, get_features_for_tier, get_minimum_tier_for_feature, get_storage_limit

logger = logging.getLogger("sessionfs.api")


@dataclass
class UserContext:
    """Full auth context for a request — tier + role + org."""

    user: User
    effective_tier: Tier
    org: Organization | None
    role: str | None  # OrgRole value or None for solo users
    is_org_user: bool


async def get_user_org_membership(
    user_id: str, db: AsyncSession
) -> OrgMember | None:
    """Get the user's PRIMARY organization membership, if any.

    A user may belong to several organizations; the membership for their
    `default_org_id` wins, then the earliest joined. Never raises
    MultipleResultsFound (which 500'd every tier-gated route for multi-org
    users)."""
    from sqlalchemy import case

    result = await db.execute(
        select(OrgMember)
        .join(User, User.id == OrgMember.user_id)
        .where(OrgMember.user_id == user_id)
        .order_by(
            case((OrgMember.org_id == User.default_org_id, 0), else_=1),
            OrgMember.joined_at,
            OrgMember.org_id,
        )
        .limit(1)
    )
    return result.scalars().first()


async def _get_org(org_id: str, db: AsyncSession) -> Organization | None:
    """Fetch an Organization by id (cached helper for get_user_context)."""
    result = await db.execute(
        select(Organization).where(Organization.id == org_id)
    )
    return result.scalar_one_or_none()


async def get_effective_tier(
    user: User, db: AsyncSession, *, membership: OrgMember | None = None
) -> Tier:
    """Resolve the user's effective tier.

    P2 (entitlement resolution switch):
    1. If the user is an org member → resolve org entitlement FIRST,
       fall back to org.tier if no active entitlement exists.
    2. Else → resolve user entitlement FIRST, fall back to user.tier
       if no active entitlement exists.
    3. Legacy admin tier: 'admin' is never an entitlement tier and always
       resolves to ENTERPRISE, checked before any entitlement. Migration 050
       gave legacy admins an active `free` entitlement, which would otherwise
       win and gate platform admins as Free.

    Callers that have already loaded the user's OrgMember row (e.g. the
    /me handler's joined query) may pass it via `membership` to avoid a
    redundant re-query (Codex R1 LOW). Omitting it preserves the original
    behavior for every other caller.
    """
    if user.tier == "admin":
        return Tier.ENTERPRISE

    if membership is None:
        membership = await get_user_org_membership(user.id, db)

    if membership:
        # Try entitlement resolution first for the org.
        ent = await resolve_entitlement("org", membership.org_id, db)
        if ent is not None:
            try:
                return Tier(ent.tier)
            except ValueError:
                pass

        # Fallback: legacy org.tier column.
        result = await db.execute(
            select(Organization).where(Organization.id == membership.org_id)
        )
        org = result.scalar_one_or_none()
        if org:
            try:
                return Tier(org.tier)
            except ValueError:
                pass

    # Try entitlement resolution for the user (no org, or org fallback missed).
    ent = await resolve_entitlement("user", user.id, db)
    if ent is not None:
        try:
            return Tier(ent.tier)
        except ValueError:
            pass

    # Fallback: legacy user.tier column.
    try:
        return Tier(user.tier)
    except ValueError:
        return Tier.FREE


async def _resolve_user_for_context(
    request,
    db: AsyncSession,
) -> User:
    """v0.10.10 — pull the authenticated User from request.state.auth_context
    if a scoped dependency (require_scope) has already run; otherwise fall
    back to the legacy get_current_user path. This avoids double-auth when
    a route depends on BOTH require_scope and get_user_context, and crucially
    avoids the get_current_user service-key rejection on routes that have
    already admitted the service key via require_scope.
    """
    auth_ctx = getattr(request.state, "auth_context", None)
    if auth_ctx is not None:
        return auth_ctx.user
    # Legacy path — fall through to get_current_user, which authenticates
    # AND rejects service keys with 403 service_key_not_allowed. This
    # preserves the deny-by-default behavior for routes that don't use
    # require_scope.
    from sessionfs.server.auth.dependencies import get_rate_limiter
    return await get_current_user(
        request=request, db=db, limiter=get_rate_limiter()
    )


async def get_user_context(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> UserContext:
    """Full auth context for a request — tier + role + org.

    v0.10.10 — sources the User from request.state.auth_context when a
    scoped dependency (require_scope) has already authenticated. Falls
    back to legacy get_current_user for routes that don't use scoped
    auth.

    P2 (entitlement resolution switch): resolves effective tier through
    the entitlements table FIRST, falling back to legacy org.tier /
    user.tier when no active entitlement exists.
    """
    user = await _resolve_user_for_context(request, db)
    return await build_user_context(user, db)


async def build_user_context(user: User, db: AsyncSession) -> UserContext:
    """The tier + role + org context for an already-authenticated user.

    The body of :func:`get_user_context` without the request plumbing, so
    code that runs outside a route dependency (the session helper's tools)
    resolves exactly the same context a route would.
    """
    membership = await get_user_org_membership(user.id, db)

    if membership:
        # Resolve org entitlement FIRST, fall back to legacy org.tier.
        ent = await resolve_entitlement("org", membership.org_id, db)
        if ent is not None:
            try:
                tier = Tier(ent.tier)
            except ValueError:
                tier = Tier.TEAM
            return UserContext(
                user=user,
                effective_tier=tier,
                org=await _get_org(membership.org_id, db),
                role=membership.role,
                is_org_user=True,
            )

        # Fallback: legacy org.tier column.
        result = await db.execute(
            select(Organization).where(Organization.id == membership.org_id)
        )
        org = result.scalar_one_or_none()
        if org:
            try:
                tier = Tier(org.tier)
            except ValueError:
                tier = Tier.TEAM
            return UserContext(
                user=user,
                effective_tier=tier,
                org=org,
                role=membership.role,
                is_org_user=True,
            )

    # Resolve user entitlement FIRST, fall back to legacy user.tier.
    ent = await resolve_entitlement("user", user.id, db)
    if ent is not None:
        try:
            tier = Tier(ent.tier)
        except ValueError:
            tier = Tier.FREE
        return UserContext(
            user=user,
            effective_tier=tier,
            org=None,
            role=None,
            is_org_user=False,
        )

    # Fallback: legacy user.tier column.
    try:
        tier = Tier(user.tier)
    except ValueError:
        tier = Tier.ENTERPRISE if user.tier == "admin" else Tier.FREE

    return UserContext(
        user=user,
        effective_tier=tier,
        org=None,
        role=None,
        is_org_user=False,
    )


def check_feature(ctx: UserContext, feature: str) -> None:
    """Raise 403 if the user's tier doesn't include the feature."""
    user_features = get_features_for_tier(ctx.effective_tier)
    if feature not in user_features:
        raise HTTPException(
            status_code=403,
            detail={
                "error": "upgrade_required",
                "feature": feature,
                "current_tier": ctx.effective_tier.value,
                "required_tier": get_minimum_tier_for_feature(feature),
                "upgrade_url": "https://sessionfs.dev/pricing",
                "message": f"This feature requires {get_minimum_tier_for_feature(feature)} or above.",
            },
        )


# Features that belong to a project rather than to the caller. Inside a
# project they are paid for by the project's owner (tk_a90e4015ad984cde).
PROJECT_SCOPED_FEATURES = frozenset(
    {"project_context", "agent_personas", "agent_tickets", "agent_runs"}
)


async def resolve_org_plan_tier(org_id: str, db: AsyncSession) -> Tier:
    """An org's plan: its active entitlement, else the legacy org.tier.

    An unrecognised value maps to TEAM, as in get_user_context.
    """
    ent = await resolve_entitlement("org", org_id, db)
    if ent is not None:
        try:
            return Tier(ent.tier)
        except ValueError:
            return Tier.TEAM
    org = await _get_org(org_id, db)
    if org is not None:
        try:
            return Tier(org.tier)
        except ValueError:
            return Tier.TEAM
    return Tier.FREE


def raise_project_upgrade_required(feature: str, plan: Tier, project_id: str | None) -> None:
    required = get_minimum_tier_for_feature(feature)
    detail: dict = {
        "error": "upgrade_required",
        "feature": feature,
        "plan_scope": "project",
        "current_tier": plan.value,
        "required_tier": required,
        "upgrade_url": "https://sessionfs.dev/pricing",
        "message": (
            f"This feature requires the project's plan to be {required} or above."
        ),
    }
    if project_id is not None:
        detail["project_id"] = project_id
    raise HTTPException(status_code=403, detail=detail)


async def resolve_project_plan_tier(project: Project, db: AsyncSession) -> Tier:
    """The plan that pays for a project: its org's, or its owner's.

    Org projects resolve the org entitlement first, then the legacy
    org.tier column (an unrecognised value maps to TEAM, as in
    get_user_context). Personal projects use the owner's effective tier,
    the same tier the owner gets everywhere else.
    """
    if project.org_id:
        return await resolve_org_plan_tier(project.org_id, db)
    owner = await db.get(User, project.owner_id)
    if owner is None:
        return Tier.FREE
    return await get_effective_tier(owner, db)


class _ProjectPlanSubject(Protocol):
    """Structural contract for bulk plan resolution — the three fields
    ``resolve_project_plan_tiers`` reads off each subject (a full ``Project``
    row or the sweep's lightweight, frozen due-project row). Declared as
    read-only properties so both settable ORM columns and frozen dataclass
    attributes satisfy it."""

    @property
    def id(self) -> str: ...
    @property
    def org_id(self) -> str | None: ...
    @property
    def owner_id(self) -> str: ...


# Chunk bulk ``IN (...)`` lookups so a very large due set never trips
# SQLite's host-parameter limit nor builds a single pathological query
# (GitHub #93 review round 2 LOW — the sweep selects a bounded batch, but a
# member's primary org can pull extra org ids into the entitlement lookup).
_IN_CHUNK_SIZE = 1000


def _chunks(ids: set[str]) -> list[list[str]]:
    ordered = list(ids)
    return [
        ordered[i : i + _IN_CHUNK_SIZE]
        for i in range(0, len(ordered), _IN_CHUNK_SIZE)
    ]


async def resolve_project_plan_tiers(
    projects: list[_ProjectPlanSubject], db: AsyncSession
) -> dict[str, Tier]:
    """Batched ``resolve_project_plan_tier`` for the KB compile sweep.

    Mirrors the single-project resolution — an org project resolves its
    org's active entitlement then legacy ``org.tier`` (unrecognised → TEAM);
    a personal project resolves its owner's effective tier (org membership
    first, then the user's own entitlement / tier). Issues a handful of bulk
    queries instead of one round-trip per project, so the sweep's selection
    stays O(1) round-trips even for a large due set (GitHub #93 review round
    1 MEDIUM — the sweep must not compile Free/Starter projects).
    """
    org_projects = [p for p in projects if p.org_id]
    personal_projects = [p for p in projects if not p.org_id]
    org_ids = {p.org_id for p in org_projects if p.org_id}
    owner_ids = {p.owner_id for p in personal_projects}

    # Owner rows drive both the personal-project plan and each owner's
    # primary org membership below.
    owners: dict[str, User] = {}
    if owner_ids:
        owner_rows: list[User] = []
        for chunk in _chunks(owner_ids):
            owner_rows.extend(
                (await db.execute(select(User).where(User.id.in_(chunk))))
                .scalars()
                .all()
            )
        owners = {u.id: u for u in owner_rows}

    # Primary org membership per owner (default org first, then earliest
    # joined), mirroring get_user_org_membership. Memberships may pull in
    # org ids beyond org_projects'.
    primary_membership: dict[str, OrgMember] = {}
    if owner_ids:
        from sqlalchemy import case

        for chunk in _chunks(owner_ids):
            member_rows = (
                await db.execute(
                    select(OrgMember, User.default_org_id)
                    .join(User, User.id == OrgMember.user_id)
                    .where(OrgMember.user_id.in_(chunk))
                    .order_by(
                        case((OrgMember.org_id == User.default_org_id, 0), else_=1),
                        OrgMember.joined_at,
                        OrgMember.org_id,
                    )
                )
            ).all()
            for member, _default_org in member_rows:
                if member.user_id not in primary_membership:
                    primary_membership[member.user_id] = member
                    org_ids.add(member.org_id)

    # Bulk-load active entitlements + legacy tiers for every org in scope,
    # and active entitlements for every personal owner.
    org_ent_tiers: dict[str, str] = {}
    org_legacy_tiers: dict[str, str] = {}
    if org_ids:
        for chunk in _chunks(org_ids):
            ent_rows = (
                await db.execute(
                    select(Entitlement.owner_id, Entitlement.tier).where(
                        Entitlement.owner_type == "org",
                        Entitlement.owner_id.in_(chunk),
                        Entitlement.status == "active",
                    )
                )
            ).all()
            org_ent_tiers.update({owner_id: tier for owner_id, tier in ent_rows})
        for chunk in _chunks(org_ids):
            org_rows = (
                await db.execute(
                    select(Organization.id, Organization.tier).where(
                        Organization.id.in_(chunk)
                    )
                )
            ).all()
            org_legacy_tiers.update({oid: tier for oid, tier in org_rows})

    user_ent_tiers: dict[str, str] = {}
    if owner_ids:
        for chunk in _chunks(owner_ids):
            user_ent_rows = (
                await db.execute(
                    select(Entitlement.owner_id, Entitlement.tier).where(
                        Entitlement.owner_type == "user",
                        Entitlement.owner_id.in_(chunk),
                        Entitlement.status == "active",
                    )
                )
            ).all()
            user_ent_tiers.update(
                {owner_id: tier for owner_id, tier in user_ent_rows}
            )

    def _tier(value: str | None, *, unrecognised: Tier) -> Tier:
        if value is None:
            return unrecognised
        try:
            return Tier(value)
        except ValueError:
            return unrecognised

    def org_plan(org_id: str) -> Tier:
        # resolve_org_plan_tier semantics: unrecognised values map to TEAM,
        # but a missing org (deleted since selection) is FREE.
        if org_id in org_ent_tiers:
            return _tier(org_ent_tiers[org_id], unrecognised=Tier.TEAM)
        if org_id in org_legacy_tiers:
            return _tier(org_legacy_tiers[org_id], unrecognised=Tier.TEAM)
        return Tier.FREE

    def owner_tier(user_id: str) -> Tier:
        # get_effective_tier semantics: admin always ENTERPRISE; otherwise
        # org membership first, then the user's own entitlement/tier, with
        # an unrecognised value falling THROUGH to the next source.
        user = owners.get(user_id)
        if user is None:
            return Tier.FREE
        if user.tier == "admin":
            return Tier.ENTERPRISE
        member = primary_membership.get(user_id)
        if member is not None:
            ent_tier = org_ent_tiers.get(member.org_id)
            if ent_tier is not None:
                try:
                    return Tier(ent_tier)
                except ValueError:
                    pass
            legacy_tier = org_legacy_tiers.get(member.org_id)
            if legacy_tier is not None:
                try:
                    return Tier(legacy_tier)
                except ValueError:
                    pass
        ent_tier = user_ent_tiers.get(user_id)
        if ent_tier is not None:
            try:
                return Tier(ent_tier)
            except ValueError:
                pass
        try:
            return Tier(user.tier)
        except ValueError:
            return Tier.FREE

    resolved: dict[str, Tier] = {}
    for project in org_projects:
        resolved[project.id] = org_plan(project.org_id) if project.org_id else Tier.FREE
    for project in personal_projects:
        resolved[project.id] = owner_tier(project.owner_id)
    return resolved


async def _is_grandfathered(project_id: str, feature: str, db: AsyncSession) -> bool:
    result = await db.execute(
        select(ProjectFeatureGrandfather.project_id).where(
            ProjectFeatureGrandfather.project_id == project_id,
            ProjectFeatureGrandfather.feature == feature,
        )
    )
    return result.first() is not None


async def check_project_feature(
    ctx: UserContext, feature: str, project: Project, db: AsyncSession
) -> None:
    """Raise 403 unless the feature is available inside this project.

    Call AFTER the caller is authorized for the project (and after the
    service-key boundary check), so the plan lookup never leaks anything
    about a project the caller cannot see.

    Allowed when the project's own plan includes the feature, or when the
    project already used the feature before v0.17.2 (recorded by migration
    065) and the caller's own plan includes it — that keeps the old
    caller-plan behaviour for existing usage only.
    """
    plan = await resolve_project_plan_tier(project, db)
    if feature in get_features_for_tier(plan):
        return
    if feature in get_features_for_tier(ctx.effective_tier) and await _is_grandfathered(
        project.id, feature, db
    ):
        return
    raise_project_upgrade_required(feature, plan, project.id)


def check_role(ctx: UserContext, min_role: str) -> None:
    """Raise 403 if the user's role is insufficient.

    Non-org users are always rejected — org role checks only apply to org members.
    """
    if not ctx.is_org_user:
        raise HTTPException(
            status_code=403,
            detail={
                "error": "insufficient_role",
                "current_role": "none",
                "required_role": min_role,
                "message": "This action requires an organization membership.",
            },
        )
    if not ctx.role or not has_minimum_role(ctx.role, min_role):
        raise HTTPException(
            status_code=403,
            detail={
                "error": "insufficient_role",
                "current_role": ctx.role or "none",
                "required_role": min_role,
                "message": f"This action requires the {min_role} role.",
            },
        )


def check_storage(ctx: UserContext, size_bytes: int) -> None:
    """Raise 403 if storage limit would be exceeded."""
    limit = get_storage_limit(ctx.effective_tier)
    current = ctx.user.storage_used_bytes or 0
    if ctx.is_org_user and ctx.org:
        current = ctx.org.storage_used_bytes or 0
        limit = ctx.org.storage_limit_bytes or limit

    if limit > 0 and (current + size_bytes) > limit:
        raise HTTPException(
            status_code=403,
            detail={
                "error": "storage_limit",
                "current_usage": current,
                "limit": limit,
                "message": f"Storage limit ({format_bytes(limit)}) reached. Upgrade for more.",
                "upgrade_url": "https://sessionfs.dev/pricing",
            },
        )
