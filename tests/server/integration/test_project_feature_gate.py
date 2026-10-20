"""Project-scoped feature gates use the project's plan (tk_a90e4015ad984cde).

Inside a project, project_context / agent_personas / agent_tickets /
agent_runs are paid for by the project's owner — the owning org, or the
owner's own plan for personal projects — not by whichever org the caller
happens to belong to. Existing usage is grandfathered: a project that
already used a feature before v0.17.2 (migration 065) keeps it for any
caller whose own plan includes it.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server.auth.keys import generate_api_key, hash_api_key
from sessionfs.server.db.models import (
    AgentFeedback,
    AgentRun,
    ApiKey,
    Entitlement,
    OrgMember,
    Organization,
    Project,
    ProjectFeatureGrandfather,
    Ticket,
    User,
)
from sessionfs.server.services.merge import (
    _step_feature_grandfathers,
    _step_straight_reassign,
)
from sessionfs.server.tier_gate import resolve_project_plan_tier
from sessionfs.server.tiers import Tier


def _hdrs(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


def _error(body: dict) -> dict:
    err = body.get("error") or body.get("detail") or body
    if isinstance(err, dict):
        inner = err.get("details") or err.get("detail")
        if isinstance(inner, dict):
            return inner
        return err
    return {}


async def _user(db: AsyncSession, tier: str = "free") -> tuple[User, str]:
    user = User(
        id=str(uuid.uuid4()),
        email=f"u-{uuid.uuid4().hex[:8]}@example.com",
        display_name="U",
        tier=tier,
        email_verified=True,
        created_at=datetime.now(timezone.utc),
    )
    db.add(user)
    await db.flush()
    raw = generate_api_key()
    db.add(
        ApiKey(
            id=str(uuid.uuid4()),
            user_id=user.id,
            key_hash=hash_api_key(raw),
            name="k",
            is_active=True,
            key_kind="user",
            scopes=json.dumps(["*"]),
            created_at=datetime.now(timezone.utc),
        )
    )
    await db.commit()
    return user, raw


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


async def _set_primary(db: AsyncSession, user: User, org: Organization) -> None:
    user.default_org_id = org.id
    db.add(user)
    await db.commit()


async def _project(db: AsyncSession, owner: User, org: Organization | None = None) -> Project:
    project = Project(
        id=f"proj_{uuid.uuid4().hex[:12]}",
        name=f"p-{uuid.uuid4().hex[:6]}",
        git_remote_normalized=f"github.com/acme/{uuid.uuid4().hex[:8]}",
        context_document="",
        owner_id=owner.id,
        org_id=org.id if org else None,
        created_at=datetime.now(timezone.utc),
    )
    db.add(project)
    await db.commit()
    return project


async def _grandfather(db: AsyncSession, project: Project, *features: str) -> None:
    for feature in features:
        db.add(ProjectFeatureGrandfather(project_id=project.id, feature=feature))
    await db.commit()


def _tickets(project: Project) -> str:
    return f"/api/v1/projects/{project.id}/tickets"


def _runs(project: Project) -> str:
    return f"/api/v1/projects/{project.id}/agent-runs"


def _personas(project: Project) -> str:
    return f"/api/v1/projects/{project.id}/personas"


# ── paid org project: members get what the org pays for ──────────


@pytest.mark.asyncio
async def test_paid_org_project_allows_member_whose_own_org_is_free(
    client: AsyncClient, db_session: AsyncSession
):
    """The wrongful-denial case: primary org free, project in a Team org."""
    user, key = await _user(db_session)
    free_org = await _org(db_session, "free", user)
    team_org = await _org(db_session, "team", user)
    await _set_primary(db_session, user, free_org)
    project = await _project(db_session, user, team_org)

    for url in (_tickets(project), _runs(project), _personas(project)):
        resp = await client.get(url, headers=_hdrs(key))
        assert resp.status_code == 200, (url, resp.text)


@pytest.mark.asyncio
async def test_org_entitlement_wins_over_legacy_org_tier(
    client: AsyncClient, db_session: AsyncSession
):
    user, key = await _user(db_session)
    org = await _org(db_session, "free", user)
    db_session.add(
        Entitlement(owner_type="org", owner_id=org.id, source="manual", tier="team")
    )
    await db_session.commit()
    project = await _project(db_session, user, org)
    assert await resolve_project_plan_tier(project, db_session) == Tier.TEAM
    resp = await client.get(_tickets(project), headers=_hdrs(key))
    assert resp.status_code == 200, resp.text


# ── free org project: caller's own plan no longer unlocks new use ──


@pytest.mark.asyncio
async def test_free_org_project_denies_new_usage_through_callers_plan(
    client: AsyncClient, db_session: AsyncSession
):
    user, key = await _user(db_session)
    team_org = await _org(db_session, "team", user)
    free_org = await _org(db_session, "free", user)
    await _set_primary(db_session, user, team_org)
    project = await _project(db_session, user, free_org)

    resp = await client.get(_tickets(project), headers=_hdrs(key))
    assert resp.status_code == 403
    err = _error(resp.json())
    assert err.get("error") == "upgrade_required"
    assert err.get("plan_scope") == "project"
    assert err.get("current_tier") == "free"
    assert err.get("feature") == "agent_tickets"


@pytest.mark.asyncio
async def test_grandfathered_project_keeps_feature_for_paid_caller(
    client: AsyncClient, db_session: AsyncSession
):
    user, key = await _user(db_session)
    team_org = await _org(db_session, "team", user)
    free_org = await _org(db_session, "free", user)
    await _set_primary(db_session, user, team_org)
    project = await _project(db_session, user, free_org)
    await _grandfather(db_session, project, "agent_tickets")

    resp = await client.get(_tickets(project), headers=_hdrs(key))
    assert resp.status_code == 200, resp.text
    # Grandfathering is per feature: tickets do not unlock agent runs.
    resp = await client.get(_runs(project), headers=_hdrs(key))
    assert resp.status_code == 403
    assert _error(resp.json()).get("feature") == "agent_runs"


@pytest.mark.asyncio
async def test_grandfathered_project_still_needs_callers_own_plan(
    client: AsyncClient, db_session: AsyncSession
):
    """Grandfathering keeps the OLD rule; it never grants a free caller."""
    user, key = await _user(db_session)
    free_org = await _org(db_session, "free", user)
    project = await _project(db_session, user, free_org)
    await _grandfather(db_session, project, "agent_tickets")

    resp = await client.get(_tickets(project), headers=_hdrs(key))
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_grandfathered_project_context_keeps_wiki_writes(
    client: AsyncClient, db_session: AsyncSession
):
    user, key = await _user(db_session)
    team_org = await _org(db_session, "team", user)
    free_org = await _org(db_session, "free", user)
    await _set_primary(db_session, user, team_org)
    project = await _project(db_session, user, free_org)
    url = f"/api/v1/projects/{project.id}/pages/notes"
    body = {"title": "Notes", "content": "hello"}

    resp = await client.put(url, json=body, headers=_hdrs(key))
    assert resp.status_code == 403
    await _grandfather(db_session, project, "project_context")
    resp = await client.put(url, json=body, headers=_hdrs(key))
    assert resp.status_code == 200, resp.text


# ── personal projects use the owner's plan ───────────────────────


@pytest.mark.asyncio
async def test_personal_project_uses_owner_plan(
    client: AsyncClient, db_session: AsyncSession
):
    owner, key = await _user(db_session, tier="pro")
    project = await _project(db_session, owner)
    # Pro includes personas but not tickets.
    assert (await client.get(_personas(project), headers=_hdrs(key))).status_code == 200
    resp = await client.get(_tickets(project), headers=_hdrs(key))
    assert resp.status_code == 403
    assert _error(resp.json()).get("current_tier") == "pro"


@pytest.mark.asyncio
async def test_personal_project_of_team_org_member_keeps_team_features(
    client: AsyncClient, db_session: AsyncSession
):
    """The owner's plan is their effective plan — a Team seat included."""
    owner, key = await _user(db_session)
    await _org(db_session, "team", owner)
    project = await _project(db_session, owner)
    resp = await client.get(_tickets(project), headers=_hdrs(key))
    assert resp.status_code == 200, resp.text


# ── authorization comes before the plan check ────────────────────


@pytest.mark.asyncio
async def test_outsider_gets_access_denied_not_upgrade_prompt(
    client: AsyncClient, db_session: AsyncSession
):
    owner, _ = await _user(db_session)
    free_org = await _org(db_session, "free", owner)
    project = await _project(db_session, owner, free_org)
    _, outsider_key = await _user(db_session, tier="team")
    resp = await client.get(_tickets(project), headers=_hdrs(outsider_key))
    # The project's plan is never revealed to someone who cannot see it.
    assert resp.status_code in (403, 404)
    assert _error(resp.json()).get("error") != "upgrade_required"
    assert "plan_scope" not in resp.text


# ── service keys resolve the project's (= the key's org's) plan ──


async def _service_key(db: AsyncSession, org: Organization, minter: User) -> str:
    raw = generate_api_key()
    db.add(
        ApiKey(
            id=str(uuid.uuid4()),
            user_id=minter.id,
            key_hash=hash_api_key(raw),
            name="svc",
            is_active=True,
            key_kind="service",
            org_id=org.id,
            scopes=json.dumps(["tickets:read"]),
            created_by_user_id=minter.id,
            service_key_name="svc",
            created_at=datetime.now(timezone.utc),
        )
    )
    await db.commit()
    return raw


@pytest.mark.asyncio
async def test_service_key_of_free_org_gated_on_project_plan(
    client: AsyncClient, db_session: AsyncSession
):
    minter, _ = await _user(db_session)
    team_org = await _org(db_session, "team", minter)
    free_org = await _org(db_session, "free", minter)
    await _set_primary(db_session, minter, team_org)
    project = await _project(db_session, minter, free_org)
    key = await _service_key(db_session, free_org, minter)

    resp = await client.get(_tickets(project), headers=_hdrs(key))
    assert resp.status_code == 403
    await _grandfather(db_session, project, "agent_tickets")
    resp = await client.get(_tickets(project), headers=_hdrs(key))
    assert resp.status_code == 200, resp.text


@pytest.mark.asyncio
async def test_service_key_of_paid_org_allowed_when_minter_primary_is_free(
    client: AsyncClient, db_session: AsyncSession
):
    minter, _ = await _user(db_session)
    free_org = await _org(db_session, "free", minter)
    team_org = await _org(db_session, "team", minter)
    await _set_primary(db_session, minter, free_org)
    project = await _project(db_session, minter, team_org)
    key = await _service_key(db_session, team_org, minter)
    resp = await client.get(_tickets(project), headers=_hdrs(key))
    assert resp.status_code == 200, resp.text


# ── merges carry grandfathering and feedback to the target ──────


@pytest.mark.asyncio
async def test_merge_carries_grandfathered_features_to_target(db_session: AsyncSession):
    owner, _ = await _user(db_session)
    source = await _project(db_session, owner)
    target = await _project(db_session, owner)
    await _grandfather(db_session, source, "agent_tickets", "project_context")
    await _grandfather(db_session, target, "project_context")

    await _step_feature_grandfathers(db_session, source.id, target.id)
    await db_session.commit()

    rows = (
        await db_session.execute(
            select(ProjectFeatureGrandfather.feature).where(
                ProjectFeatureGrandfather.project_id == target.id
            )
        )
    ).scalars().all()
    assert sorted(rows) == ["agent_tickets", "project_context"]


@pytest.mark.asyncio
async def test_merge_moves_agent_feedback_with_its_runs(db_session: AsyncSession):
    owner, _ = await _user(db_session)
    source = await _project(db_session, owner)
    target = await _project(db_session, owner)
    run = AgentRun(id=f"run_{uuid.uuid4().hex[:16]}", project_id=source.id, persona_name="atlas")
    ticket = Ticket(
        id=f"tk_{uuid.uuid4().hex[:16]}", project_id=source.id, title="t",
        created_by_user_id=owner.id,
    )
    db_session.add_all([run, ticket])
    await db_session.flush()
    feedback = AgentFeedback(
        id=f"afb_{uuid.uuid4().hex[:16]}", project_id=source.id, agent_run_id=run.id,
        feedback_type="summary", verdict="positive",
    )
    db_session.add(feedback)
    await db_session.commit()

    await _step_straight_reassign(db_session, source.id, target.id)
    await db_session.commit()
    await db_session.refresh(feedback)
    assert feedback.project_id == target.id


# ── creating a project uses the target org's plan ───────────────


def _create_body(org: Organization | None) -> dict:
    body: dict = {
        "name": f"n-{uuid.uuid4().hex[:6]}",
        "git_remote_normalized": f"github.com/acme/{uuid.uuid4().hex[:8]}",
    }
    if org is not None:
        body["org_id"] = org.id
    return body


@pytest.mark.asyncio
async def test_create_project_in_paid_org_allowed_for_free_primary_member(
    client: AsyncClient, db_session: AsyncSession
):
    user, key = await _user(db_session)
    free_org = await _org(db_session, "free", user)
    team_org = await _org(db_session, "team", user)
    await _set_primary(db_session, user, free_org)
    resp = await client.post(
        "/api/v1/projects/", json=_create_body(team_org), headers=_hdrs(key)
    )
    assert resp.status_code == 201, resp.text


@pytest.mark.asyncio
async def test_create_project_in_free_org_denied_through_callers_plan(
    client: AsyncClient, db_session: AsyncSession
):
    user, key = await _user(db_session)
    team_org = await _org(db_session, "team", user)
    free_org = await _org(db_session, "free", user)
    await _set_primary(db_session, user, team_org)
    resp = await client.post(
        "/api/v1/projects/", json=_create_body(free_org), headers=_hdrs(key)
    )
    assert resp.status_code == 403
    err = _error(resp.json())
    assert err.get("plan_scope") == "project"
    assert err.get("current_tier") == "free"


@pytest.mark.asyncio
async def test_create_project_in_foreign_org_reveals_no_plan(
    client: AsyncClient, db_session: AsyncSession
):
    owner, _ = await _user(db_session)
    other_org = await _org(db_session, "free", owner)
    _, key = await _user(db_session, tier="team")
    resp = await client.post(
        "/api/v1/projects/", json=_create_body(other_org), headers=_hdrs(key)
    )
    assert resp.status_code == 403
    assert "plan_scope" not in resp.text


@pytest.mark.asyncio
async def test_create_personal_project_uses_callers_plan(
    client: AsyncClient, db_session: AsyncSession
):
    _, pro_key = await _user(db_session, tier="pro")
    resp = await client.post("/api/v1/projects/", json=_create_body(None), headers=_hdrs(pro_key))
    assert resp.status_code == 201, resp.text
    _, free_key = await _user(db_session, tier="free")
    resp = await client.post("/api/v1/projects/", json=_create_body(None), headers=_hdrs(free_key))
    assert resp.status_code == 403
