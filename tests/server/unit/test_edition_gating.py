"""Community keyless edition gating chokepoint (tk_6a6f1c54d9224c0b).

``SFS_EDITION=community`` resolves every plan to Pro — a stored Free/Starter
plan is raised to the keyless floor and Team/Enterprise is clamped down to
it. The edition alone decides: there is no licence probe (licensed installs
run as ``enterprise`` via the Helm chart). Platform admins keep Enterprise,
and the Enterprise edition (the default) is unchanged.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server.db.models import (
    Entitlement,
    OrgMember,
    Organization,
    Project,
    User,
)
from sessionfs.server.tier_gate import (
    apply_edition_gating,
    get_effective_tier,
    project_ids_with_feature,
    resolve_org_plan_tier,
    resolve_project_plan_tiers,
)
from sessionfs.server.tiers import Tier, get_features_for_tier


async def _user(db: AsyncSession, tier: str) -> User:
    user = User(
        id=str(uuid.uuid4()),
        email=f"u-{uuid.uuid4().hex[:8]}@example.com",
        display_name="U",
        tier=tier,
        email_verified=True,
        created_at=datetime.now(timezone.utc),
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


async def _org(db: AsyncSession, tier: str, *members: User) -> Organization:
    org = Organization(
        id=f"org_{uuid.uuid4().hex[:8]}",
        name=f"Org-{uuid.uuid4().hex[:6]}",
        slug=f"o-{uuid.uuid4().hex[:6]}",
        tier=tier,
        created_at=datetime.now(timezone.utc),
    )
    db.add(org)
    await db.flush()
    for member in members:
        db.add(
            OrgMember(
                org_id=org.id,
                user_id=member.id,
                role="admin",
                joined_at=datetime.now(timezone.utc),
            )
        )
    await db.commit()
    return org


async def _project(db: AsyncSession, owner: User) -> Project:
    project = Project(
        id=f"proj_{uuid.uuid4().hex[:12]}",
        name=f"p-{uuid.uuid4().hex[:6]}",
        git_remote_normalized=f"github.com/acme/{uuid.uuid4().hex[:8]}",
        context_document="",
        owner_id=owner.id,
        org_id=None,
        created_at=datetime.now(timezone.utc),
    )
    db.add(project)
    await db.commit()
    return project


async def _org_with_enterprise_entitlement(db: AsyncSession) -> Organization:
    """An org with a stored enterprise tier AND an active enterprise
    entitlement (the shape activation creates for a licensed org)."""
    org = await _org(db, "enterprise")
    db.add(
        Entitlement(
            owner_type="org", owner_id=org.id, source="manual", tier="enterprise"
        )
    )
    await db.commit()
    return org


@pytest.mark.asyncio
async def test_community_keyless_floor(db_session: AsyncSession, monkeypatch):
    monkeypatch.setenv("SFS_EDITION", "community")
    for tier in (Tier.FREE, Tier.STARTER, Tier.PRO, Tier.TEAM, Tier.ENTERPRISE):
        assert apply_edition_gating(tier) == Tier.PRO


@pytest.mark.asyncio
async def test_community_free_user_gets_pro_features_only(
    db_session: AsyncSession, monkeypatch
):
    monkeypatch.setenv("SFS_EDITION", "community")
    user = await _user(db_session, "free")
    assert await get_effective_tier(user, db_session) == Tier.PRO
    features = get_features_for_tier(Tier.PRO)
    assert "project_context" in features
    assert "review_loop" not in features
    assert "session_helper" not in features


@pytest.mark.asyncio
async def test_community_project_ids_are_pro_scoped(
    db_session: AsyncSession, monkeypatch
):
    monkeypatch.setenv("SFS_EDITION", "community")
    owner = await _user(db_session, "free")
    project = await _project(db_session, owner)
    # Pro features are available; Team/Enterprise features are not.
    assert project.id in await project_ids_with_feature(
        [project], "project_context", db_session, caller_tier=Tier.FREE
    )
    assert project.id not in await project_ids_with_feature(
        [project], "review_loop", db_session, caller_tier=Tier.FREE
    )
    assert project.id not in await project_ids_with_feature(
        [project], "session_helper", db_session, caller_tier=Tier.FREE
    )


@pytest.mark.asyncio
async def test_community_bulk_project_plans_are_pro(
    db_session: AsyncSession, monkeypatch
):
    monkeypatch.setenv("SFS_EDITION", "community")
    free_owner = await _user(db_session, "free")
    team_owner = await _user(db_session, "team")
    free_project = await _project(db_session, free_owner)
    team_project = await _project(db_session, team_owner)
    plans = await resolve_project_plan_tiers(
        [free_project, team_project], db_session
    )
    assert plans[free_project.id] == Tier.PRO
    assert plans[team_project.id] == Tier.PRO


@pytest.mark.asyncio
async def test_community_enterprise_org_entitlement_resolves_to_pro(
    db_session: AsyncSession, monkeypatch
):
    monkeypatch.setenv("SFS_EDITION", "community")
    org = await _org_with_enterprise_entitlement(db_session)
    # The edition alone decides: even an org with a stored Enterprise
    # entitlement resolves to the Pro keyless floor in community.
    assert await resolve_org_plan_tier(org.id, db_session) == Tier.PRO
    assert "team_skill_library" not in get_features_for_tier(Tier.PRO)


@pytest.mark.asyncio
async def test_enterprise_edition_enterprise_org_entitlement_resolves_enterprise(
    db_session: AsyncSession, monkeypatch
):
    monkeypatch.setenv("SFS_EDITION", "enterprise")
    org = await _org_with_enterprise_entitlement(db_session)
    assert await resolve_org_plan_tier(org.id, db_session) == Tier.ENTERPRISE
    assert "team_skill_library" in get_features_for_tier(Tier.ENTERPRISE)


@pytest.mark.asyncio
async def test_enterprise_edition_unchanged(db_session: AsyncSession, monkeypatch):
    monkeypatch.setenv("SFS_EDITION", "enterprise")
    assert apply_edition_gating(Tier.FREE) == Tier.FREE
    assert apply_edition_gating(Tier.ENTERPRISE) == Tier.ENTERPRISE
    user = await _user(db_session, "free")
    assert await get_effective_tier(user, db_session) == Tier.FREE


@pytest.mark.asyncio
async def test_default_edition_is_enterprise(db_session: AsyncSession, monkeypatch):
    monkeypatch.delenv("SFS_EDITION", raising=False)
    assert apply_edition_gating(Tier.FREE) == Tier.FREE
    assert apply_edition_gating(Tier.ENTERPRISE) == Tier.ENTERPRISE


@pytest.mark.asyncio
async def test_community_admin_keeps_enterprise(
    db_session: AsyncSession, monkeypatch
):
    monkeypatch.setenv("SFS_EDITION", "community")
    admin = await _user(db_session, "admin")
    assert await get_effective_tier(admin, db_session) == Tier.ENTERPRISE


@pytest.mark.asyncio
async def test_community_enterprise_org_cannot_use_team_skill_library(
    db_session: AsyncSession, monkeypatch
):
    monkeypatch.setenv("SFS_EDITION", "community")
    org = await _org_with_enterprise_entitlement(db_session)
    from sessionfs.server.routes.marketplace import _org_has_feature

    # team_skill_library is Team+, so the Pro floor must refuse it.
    assert await _org_has_feature(db_session, org, "team_skill_library") is False


@pytest.mark.asyncio
async def test_community_enterprise_org_cannot_use_skills_policy(
    db_session: AsyncSession, monkeypatch
):
    monkeypatch.setenv("SFS_EDITION", "community")
    org = await _org_with_enterprise_entitlement(db_session)
    from sessionfs.server.services.skill_policy import org_has_policy_feature

    # skills_policy is Enterprise-only, so the Pro floor must refuse it.
    assert await org_has_policy_feature(db_session, org) is False


@pytest.mark.asyncio
async def test_community_enterprise_org_keeps_oidc_sso(
    db_session: AsyncSession, monkeypatch
):
    monkeypatch.setenv("SFS_EDITION", "community")
    org = await _org_with_enterprise_entitlement(db_session)
    from sessionfs.server.services.auto_join import org_has_oidc_sso

    # oidc_sso is Starter+ (not Team+), so the Community Pro floor still
    # includes it and OIDC domain auto-join keeps working.
    assert await org_has_oidc_sso(org, db_session) is True


@pytest.mark.asyncio
async def test_community_admin_keeps_enterprise_in_a_named_org(db_session: AsyncSession, monkeypatch):
    from sessionfs.server.tier_gate import build_org_user_context

    monkeypatch.setenv("SFS_EDITION", "community")
    admin = await _user(db_session, "admin")
    org = await _org(db_session, "enterprise", admin)
    ctx = await build_org_user_context(admin, org.id, db_session)
    assert ctx is not None and ctx.effective_tier == Tier.ENTERPRISE


def test_community_gating_blocks_a_team_plan_from_funding_an_org(monkeypatch):
    """Org creation decides on the creator's own plan; in Community that plan
    resolves to Pro, so a stored Team plan can't create an organization."""
    monkeypatch.setenv("SFS_EDITION", "community")
    assert apply_edition_gating(Tier.TEAM) == Tier.PRO
    monkeypatch.setenv("SFS_EDITION", "enterprise")
    assert apply_edition_gating(Tier.TEAM) == Tier.TEAM


def test_create_app_config_edition_applies_when_env_is_unset(monkeypatch):
    from sessionfs.server import tier_gate
    from sessionfs.server.app import create_app
    from sessionfs.server.config import ServerConfig

    monkeypatch.delenv("SFS_EDITION", raising=False)
    try:
        create_app(ServerConfig(edition="community", dashboard_dir="/nonexistent-sessionfs-dashboard"))
        assert tier_gate.get_edition() == "community"
        assert apply_edition_gating(Tier.ENTERPRISE) == Tier.PRO
    finally:
        tier_gate.configure_edition(None)
