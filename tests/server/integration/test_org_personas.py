"""GH #70 — org-level personas.

Covers:
1. Org CRUD at /api/v1/orgs/{org_id}/personas — authz matrix (owner, admin,
   member, outsider, other-org admin, service keys incl. project-restricted
   and read-only keys), constant 404s, tier gate on the org plan, audit rows.
2. Project-side resolution: list returns project ∪ org personas with a
   `scope` flag (include_org toggle), project personas override org ones
   (list + get), deleting the override restores the org persona, other
   orgs / personal projects never see them.
3. Resolver precedence through the real consumers: ticket assignment
   normalization, start_ticket, agent runs.
4. Delete guards for org personas (stranded tickets, project override).
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from httpx import AsyncClient
from sqlalchemy import delete as sa_delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server.auth.keys import generate_api_key, hash_api_key
from sessionfs.server.db.models import (
    AgentPersona,
    ApiKey,
    OrgAuditEvent,
    OrgMember,
    Organization,
    Project,
    User,
)
from sessionfs.server.services.personas import resolve_persona


def _hdrs(raw: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {raw}"}


async def _user(db: AsyncSession, tier: str = "team") -> tuple[User, str]:
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


async def _org(db: AsyncSession, tier: str = "team") -> Organization:
    org = Organization(
        id=f"org_{uuid.uuid4().hex[:10]}",
        name=f"Org-{uuid.uuid4().hex[:6]}",
        slug=f"o-{uuid.uuid4().hex[:8]}",
        tier=tier,
        created_at=datetime.now(timezone.utc),
    )
    db.add(org)
    await db.commit()
    return org


async def _member(db: AsyncSession, org: Organization, role: str) -> tuple[User, str]:
    user, raw = await _user(db)
    db.add(
        OrgMember(
            org_id=org.id, user_id=user.id, role=role,
            joined_at=datetime.now(timezone.utc),
        )
    )
    await db.commit()
    return user, raw


async def _service_key(
    db: AsyncSession,
    org: Organization,
    minter: User,
    scopes: list[str],
    project_ids: list[str] | None = None,
) -> str:
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
            scopes=json.dumps(scopes),
            created_by_user_id=minter.id,
            service_key_name="svc",
            project_ids=json.dumps(project_ids) if project_ids else None,
            created_at=datetime.now(timezone.utc),
        )
    )
    await db.commit()
    return raw


async def _project(db: AsyncSession, owner: User, org: Organization | None) -> Project:
    project = Project(
        id=f"proj_{uuid.uuid4().hex[:12]}",
        name=f"p-{uuid.uuid4().hex[:6]}",
        git_remote_normalized=f"github.com/acme/{uuid.uuid4().hex[:10]}",
        context_document="",
        owner_id=owner.id,
        org_id=org.id if org else None,
    )
    db.add(project)
    await db.commit()
    return project


_BODY = {"name": "gcp-infra", "role": "Terraform engineer", "content": "# GCP", "specializations": ["gcp"]}


async def _create_org_persona(client: AsyncClient, org: Organization, key: str, **over) -> dict:
    resp = await client.post(
        f"/api/v1/orgs/{org.id}/personas", headers=_hdrs(key), json={**_BODY, **over}
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


# ── authz matrix ─────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["admin", "owner"])
async def test_admin_and_owner_can_create_org_persona(
    client: AsyncClient, db_session: AsyncSession, role: str
):
    org = await _org(db_session)
    _, key = await _member(db_session, org, role)
    body = await _create_org_persona(client, org, key)
    assert body["scope"] == "org"
    assert body["org_id"] == org.id
    assert body["project_id"] is None
    assert body["version"] == 1

    events = (
        await db_session.execute(
            select(OrgAuditEvent).where(
                OrgAuditEvent.org_id == org.id,
                OrgAuditEvent.event_type == "persona_created",
            )
        )
    ).scalars().all()
    assert len(events) == 1
    assert events[0].target_type == "persona"
    assert events[0].actor_role_at_time == role


@pytest.mark.asyncio
async def test_member_reads_but_cannot_write(client: AsyncClient, db_session: AsyncSession):
    org = await _org(db_session)
    _, admin_key = await _member(db_session, org, "admin")
    _, member_key = await _member(db_session, org, "member")
    await _create_org_persona(client, org, admin_key)

    resp = await client.get(f"/api/v1/orgs/{org.id}/personas", headers=_hdrs(member_key))
    assert resp.status_code == 200
    assert [p["name"] for p in resp.json()] == ["gcp-infra"]
    resp = await client.get(f"/api/v1/orgs/{org.id}/personas/gcp-infra", headers=_hdrs(member_key))
    assert resp.status_code == 200

    resp = await client.post(
        f"/api/v1/orgs/{org.id}/personas", headers=_hdrs(member_key), json={**_BODY, "name": "x"}
    )
    assert resp.status_code == 403
    resp = await client.put(
        f"/api/v1/orgs/{org.id}/personas/gcp-infra", headers=_hdrs(member_key), json={"role": "r"}
    )
    assert resp.status_code == 403
    resp = await client.delete(f"/api/v1/orgs/{org.id}/personas/gcp-infra", headers=_hdrs(member_key))
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_outsider_and_other_org_admin_get_constant_404(
    client: AsyncClient, db_session: AsyncSession
):
    org = await _org(db_session)
    other = await _org(db_session)
    _, admin_key = await _member(db_session, org, "admin")
    _, other_admin_key = await _member(db_session, other, "admin")
    _, outsider_key = await _user(db_session)
    await _create_org_persona(client, org, admin_key)

    for key in (other_admin_key, outsider_key):
        for method, url, kwargs in (
            ("GET", f"/api/v1/orgs/{org.id}/personas", {}),
            ("GET", f"/api/v1/orgs/{org.id}/personas/gcp-infra", {}),
            ("POST", f"/api/v1/orgs/{org.id}/personas", {"json": {**_BODY, "name": "y"}}),
            ("PUT", f"/api/v1/orgs/{org.id}/personas/gcp-infra", {"json": {"role": "r"}}),
            ("DELETE", f"/api/v1/orgs/{org.id}/personas/gcp-infra", {}),
        ):
            resp = await client.request(method, url, headers=_hdrs(key), **kwargs)
            assert resp.status_code == 404, (method, url, resp.text)
    # Unknown org looks the same as a foreign org.
    resp = await client.get("/api/v1/orgs/org_nope/personas", headers=_hdrs(admin_key))
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_service_keys_follow_scope_and_org(client: AsyncClient, db_session: AsyncSession):
    org = await _org(db_session)
    other = await _org(db_session)
    admin, _ = await _member(db_session, org, "admin")
    other_admin, _ = await _member(db_session, other, "admin")
    write_key = await _service_key(db_session, org, admin, ["personas:read", "personas:write"])
    read_key = await _service_key(db_session, org, admin, ["personas:read"])
    project = await _project(db_session, admin, org)
    restricted = await _service_key(
        db_session, org, admin, ["personas:read", "personas:write"], project_ids=[project.id]
    )
    foreign_key = await _service_key(
        db_session, other, other_admin, ["personas:read", "personas:write"]
    )

    created = await _create_org_persona(client, org, write_key)
    assert created["scope"] == "org"
    row = (
        await db_session.execute(select(AgentPersona).where(AgentPersona.id == created["id"]))
    ).scalar_one()
    assert row.actor_type == "service_key"

    resp = await client.post(
        f"/api/v1/orgs/{org.id}/personas", headers=_hdrs(read_key), json={**_BODY, "name": "z"}
    )
    assert resp.status_code == 403
    assert (await client.get(f"/api/v1/orgs/{org.id}/personas", headers=_hdrs(read_key))).status_code == 200

    resp = await client.post(
        f"/api/v1/orgs/{org.id}/personas", headers=_hdrs(restricted), json={**_BODY, "name": "z"}
    )
    assert resp.status_code == 403
    assert (await client.get(f"/api/v1/orgs/{org.id}/personas", headers=_hdrs(restricted))).status_code == 200

    for method, kwargs in (("GET", {}), ("POST", {"json": {**_BODY, "name": "w"}})):
        resp = await client.request(
            method, f"/api/v1/orgs/{org.id}/personas", headers=_hdrs(foreign_key), **kwargs
        )
        assert resp.status_code == 404


@pytest.mark.asyncio
async def test_org_name_is_case_insensitive_unique(client: AsyncClient, db_session: AsyncSession):
    org = await _org(db_session)
    _, key = await _member(db_session, org, "owner")
    await _create_org_persona(client, org, key)
    resp = await client.post(
        f"/api/v1/orgs/{org.id}/personas", headers=_hdrs(key), json={**_BODY, "name": "GCP-Infra"}
    )
    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_org_persona_name_is_unicode_case_insensitive_unique(
    db_session: AsyncSession
):
    """GH #70 — SQLite's built-in lower() folds ASCII only, so before the
    Unicode-aware override the (org_id, lower(name)) UNIQUE index let
    `Álex` and `álex` both insert (and func.lower() lookups missed the
    pair). The override must make the index reject the non-ASCII case
    collision at the DB layer, even for rows that bypass the API's
    ASCII-name validation."""
    from sqlalchemy.exc import IntegrityError

    org = await _org(db_session)
    org_id = org.id
    db_session.add(AgentPersona(
        id=f"per_{uuid.uuid4().hex[:12]}", org_id=org_id, name="Álex",
        role="Backend", created_by="someone",
    ))
    await db_session.commit()

    db_session.add(AgentPersona(
        id=f"per_{uuid.uuid4().hex[:12]}", org_id=org_id, name="álex",
        role="Backend", created_by="someone",
    ))
    with pytest.raises(IntegrityError):
        await db_session.flush()
    await db_session.rollback()

    # func.lower() lookups fold Unicode too: ÁLEX finds the stored Álex.
    rows = (
        await db_session.execute(
            select(AgentPersona.name).where(
                AgentPersona.org_id == org_id,
                func.lower(AgentPersona.name) == "ÁLEX".lower(),
            )
        )
    ).scalars().all()
    assert rows == ["Álex"]


@pytest.mark.asyncio
async def test_org_plan_without_personas_is_gated(client: AsyncClient, db_session: AsyncSession):
    org = await _org(db_session, tier="free")
    _, key = await _member(db_session, org, "owner")
    resp = await client.post(f"/api/v1/orgs/{org.id}/personas", headers=_hdrs(key), json=_BODY)
    assert resp.status_code == 403
    assert "upgrade_required" in resp.text


@pytest.mark.asyncio
async def test_update_reactivate_and_audit(client: AsyncClient, db_session: AsyncSession):
    org = await _org(db_session)
    _, key = await _member(db_session, org, "admin")
    await _create_org_persona(client, org, key)
    resp = await client.put(
        f"/api/v1/orgs/{org.id}/personas/gcp-infra", headers=_hdrs(key), json={"content": "# v2"}
    )
    assert resp.status_code == 200
    assert resp.json()["version"] == 2
    assert (await client.delete(f"/api/v1/orgs/{org.id}/personas/gcp-infra", headers=_hdrs(key))).status_code == 204
    assert (await client.get(f"/api/v1/orgs/{org.id}/personas/gcp-infra", headers=_hdrs(key))).status_code == 404
    resp = await client.put(
        f"/api/v1/orgs/{org.id}/personas/gcp-infra", headers=_hdrs(key), json={"is_active": True}
    )
    assert resp.status_code == 200 and resp.json()["is_active"] is True
    types = {
        e.event_type
        for e in (
            await db_session.execute(select(OrgAuditEvent).where(OrgAuditEvent.org_id == org.id))
        ).scalars().all()
    }
    assert types == {"persona_created", "persona_updated", "persona_deleted"}


# ── project-side resolution ──────────────────────────────────


@pytest.mark.asyncio
async def test_project_list_unions_org_personas_with_scope(
    client: AsyncClient, db_session: AsyncSession
):
    org = await _org(db_session)
    admin, key = await _member(db_session, org, "admin")
    project = await _project(db_session, admin, org)
    await _create_org_persona(client, org, key)
    resp = await client.post(
        f"/api/v1/projects/{project.id}/personas", headers=_hdrs(key),
        json={"name": "atlas", "role": "Backend"},
    )
    assert resp.status_code == 201
    assert resp.json()["scope"] == "project"

    resp = await client.get(f"/api/v1/projects/{project.id}/personas", headers=_hdrs(key))
    assert resp.status_code == 200
    assert [(p["name"], p["scope"]) for p in resp.json()] == [
        ("atlas", "project"), ("gcp-infra", "org"),
    ]
    resp = await client.get(
        f"/api/v1/projects/{project.id}/personas?include_org=false", headers=_hdrs(key)
    )
    assert [p["name"] for p in resp.json()] == ["atlas"]

    # get resolves the org persona from inside the project.
    resp = await client.get(f"/api/v1/projects/{project.id}/personas/gcp-infra", headers=_hdrs(key))
    assert resp.status_code == 200
    assert resp.json()["scope"] == "org"


@pytest.mark.asyncio
async def test_project_persona_overrides_org_persona(client: AsyncClient, db_session: AsyncSession):
    org = await _org(db_session)
    admin, key = await _member(db_session, org, "admin")
    project = await _project(db_session, admin, org)
    sibling = await _project(db_session, admin, org)
    await _create_org_persona(client, org, key)
    resp = await client.post(
        f"/api/v1/projects/{project.id}/personas", headers=_hdrs(key),
        json={"name": "gcp-infra", "role": "Repo-specific", "content": "# tuned"},
    )
    assert resp.status_code == 201

    listed = (await client.get(f"/api/v1/projects/{project.id}/personas", headers=_hdrs(key))).json()
    assert [(p["name"], p["scope"]) for p in listed] == [("gcp-infra", "project")]
    got = (await client.get(f"/api/v1/projects/{project.id}/personas/gcp-infra", headers=_hdrs(key))).json()
    assert got["scope"] == "project" and got["content"] == "# tuned"
    # Sibling project still sees the org persona.
    got = (await client.get(f"/api/v1/projects/{sibling.id}/personas/gcp-infra", headers=_hdrs(key))).json()
    assert got["scope"] == "org"

    # Deleting the override restores the org persona (no stranded-ticket guard
    # needed — tickets fall back to the org persona).
    assert (await client.delete(
        f"/api/v1/projects/{project.id}/personas/gcp-infra", headers=_hdrs(key)
    )).status_code == 204
    got = (await client.get(f"/api/v1/projects/{project.id}/personas/gcp-infra", headers=_hdrs(key))).json()
    assert got["scope"] == "org"


@pytest.mark.asyncio
async def test_project_write_on_org_persona_name_points_to_org_route(
    client: AsyncClient, db_session: AsyncSession
):
    org = await _org(db_session)
    admin, key = await _member(db_session, org, "admin")
    project = await _project(db_session, admin, org)
    await _create_org_persona(client, org, key)
    resp = await client.put(
        f"/api/v1/projects/{project.id}/personas/gcp-infra", headers=_hdrs(key), json={"role": "x"}
    )
    assert resp.status_code == 404
    assert "org-level persona" in resp.text
    resp = await client.delete(f"/api/v1/projects/{project.id}/personas/gcp-infra", headers=_hdrs(key))
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_other_org_and_personal_projects_never_see_org_personas(
    client: AsyncClient, db_session: AsyncSession
):
    org = await _org(db_session)
    other = await _org(db_session)
    admin, key = await _member(db_session, org, "admin")
    other_admin, other_key = await _member(db_session, other, "admin")
    await _create_org_persona(client, org, key)
    other_project = await _project(db_session, other_admin, other)
    personal = await _project(db_session, admin, None)

    for project, k in ((other_project, other_key), (personal, key)):
        listed = (await client.get(f"/api/v1/projects/{project.id}/personas", headers=_hdrs(k))).json()
        assert listed == []
        resp = await client.get(f"/api/v1/projects/{project.id}/personas/gcp-infra", headers=_hdrs(k))
        assert resp.status_code == 404


@pytest.mark.asyncio
async def test_resolver_precedence_and_inactive_fallthrough(
    client: AsyncClient, db_session: AsyncSession
):
    org = await _org(db_session)
    admin, key = await _member(db_session, org, "admin")
    project = await _project(db_session, admin, org)
    await _create_org_persona(client, org, key)

    hit = await resolve_persona(db_session, project.id, "gcp-infra", org_id=org.id)
    assert hit is not None and hit.org_id == org.id
    # Case-insensitive by default; canonical stored name comes back.
    hit = await resolve_persona(db_session, project.id, "GCP-INFRA", org_id=org.id)
    assert hit is not None and hit.name == "gcp-infra"
    assert await resolve_persona(
        db_session, project.id, "GCP-INFRA", org_id=org.id, case_insensitive=False
    ) is None
    # Without a visible org (non-member) the org persona never resolves.
    assert await resolve_persona(db_session, project.id, "gcp-infra", org_id=None) is None

    db_session.add(
        AgentPersona(
            id="per_override", project_id=project.id, name="gcp-infra", role="r",
            created_by=admin.id, is_active=False,
        )
    )
    await db_session.commit()
    # Inactive override: active-only callers fall through to the org persona;
    # inactive-tolerant callers get the project row.
    hit = await resolve_persona(db_session, project.id, "gcp-infra", org_id=org.id)
    assert hit is not None and hit.org_id == org.id
    hit = await resolve_persona(db_session, project.id, "gcp-infra", org_id=org.id, active_only=False)
    assert hit is not None and hit.id == "per_override"


# ── consumers: tickets, start_ticket, agent runs ─────────────


@pytest.mark.asyncio
async def test_ticket_assignment_and_start_resolve_org_persona(
    client: AsyncClient, db_session: AsyncSession
):
    org = await _org(db_session)
    admin, key = await _member(db_session, org, "admin")
    project = await _project(db_session, admin, org)
    await _create_org_persona(client, org, key)

    resp = await client.post(
        f"/api/v1/projects/{project.id}/tickets", headers=_hdrs(key),
        json={"title": "Write the TF module", "assigned_to": "GCP-Infra"},
    )
    assert resp.status_code == 201, resp.text
    ticket = resp.json()
    assert ticket["assigned_to"] == "gcp-infra"  # normalized to the org persona

    resp = await client.post(
        f"/api/v1/projects/{project.id}/tickets/{ticket['id']}/start", headers=_hdrs(key)
    )
    assert resp.status_code == 200, resp.text
    assert "gcp-infra" in resp.text and "Terraform engineer" in resp.text


@pytest.mark.asyncio
async def test_agent_run_accepts_org_persona(client: AsyncClient, db_session: AsyncSession):
    org = await _org(db_session)
    admin, key = await _member(db_session, org, "admin")
    project = await _project(db_session, admin, org)
    await _create_org_persona(client, org, key)
    resp = await client.post(
        f"/api/v1/projects/{project.id}/agent-runs", headers=_hdrs(key),
        json={"persona_name": "gcp-infra"},
    )
    assert resp.status_code == 201, resp.text

    other_project = await _project(db_session, admin, None)
    resp = await client.post(
        f"/api/v1/projects/{other_project.id}/agent-runs", headers=_hdrs(key),
        json={"persona_name": "gcp-infra"},
    )
    assert resp.status_code in (403, 422)


# ── delete guard ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_org_delete_guard_counts_only_unoverridden_projects(
    client: AsyncClient, db_session: AsyncSession
):
    org = await _org(db_session)
    admin, key = await _member(db_session, org, "admin")
    project = await _project(db_session, admin, org)
    await _create_org_persona(client, org, key)
    resp = await client.post(
        f"/api/v1/projects/{project.id}/tickets", headers=_hdrs(key),
        json={"title": "Infra work", "assigned_to": "gcp-infra"},
    )
    assert resp.status_code == 201

    resp = await client.delete(f"/api/v1/orgs/{org.id}/personas/gcp-infra", headers=_hdrs(key))
    assert resp.status_code == 409

    # A project-level override absorbs the ticket → no longer stranded.
    resp = await client.post(
        f"/api/v1/projects/{project.id}/personas", headers=_hdrs(key),
        json={"name": "gcp-infra", "role": "override"},
    )
    assert resp.status_code == 201
    resp = await client.delete(f"/api/v1/orgs/{org.id}/personas/gcp-infra", headers=_hdrs(key))
    assert resp.status_code == 204


@pytest.mark.asyncio
async def test_org_delete_force_overrides_guard(client: AsyncClient, db_session: AsyncSession):
    org = await _org(db_session)
    admin, key = await _member(db_session, org, "admin")
    project = await _project(db_session, admin, org)
    await _create_org_persona(client, org, key)
    await client.post(
        f"/api/v1/projects/{project.id}/tickets", headers=_hdrs(key),
        json={"title": "Infra work", "assigned_to": "gcp-infra"},
    )
    resp = await client.delete(
        f"/api/v1/orgs/{org.id}/personas/gcp-infra?force=true", headers=_hdrs(key)
    )
    assert resp.status_code == 204


# ── mixed-case names across scopes (Codex R1 MEDIUM 1) ───────


@pytest.mark.asyncio
async def test_mixed_case_cross_scope_resolves_like_the_list(
    client: AsyncClient, db_session: AsyncSession
):
    """Org `atlas` + project `Atlas`: the list shows the project persona,
    so every lookup by any spelling must resolve to it too, and stored
    names are the canonical `Atlas`."""
    org = await _org(db_session)
    admin, key = await _member(db_session, org, "admin")
    project = await _project(db_session, admin, org)
    await _create_org_persona(client, org, key, name="atlas", role="Org atlas")
    resp = await client.post(
        f"/api/v1/projects/{project.id}/personas", headers=_hdrs(key),
        json={"name": "Atlas", "role": "Project atlas"},
    )
    assert resp.status_code == 201

    listed = (await client.get(f"/api/v1/projects/{project.id}/personas", headers=_hdrs(key))).json()
    assert [(p["name"], p["scope"]) for p in listed] == [("Atlas", "project")]

    for spelling in ("atlas", "Atlas", "ATLAS"):
        got = await client.get(
            f"/api/v1/projects/{project.id}/personas/{spelling}", headers=_hdrs(key)
        )
        assert got.status_code == 200
        assert (got.json()["name"], got.json()["scope"]) == ("Atlas", "project"), spelling
        hit = await resolve_persona(db_session, project.id, spelling, org_id=org.id)
        assert hit is not None and hit.project_id == project.id

    run = await client.post(
        f"/api/v1/projects/{project.id}/agent-runs", headers=_hdrs(key),
        json={"persona_name": "atlas"},
    )
    assert run.status_code == 201, run.text
    assert run.json()["persona_name"] == "Atlas"

    ctx = await client.post(
        f"/api/v1/projects/{project.id}/retrieval-audit-contexts", headers=_hdrs(key),
        json={"persona_name": "ATLAS"},
    )
    assert ctx.status_code == 201, ctx.text
    assert ctx.json()["persona_name"] == "Atlas"

    ticket = await client.post(
        f"/api/v1/projects/{project.id}/tickets", headers=_hdrs(key),
        json={"title": "Mixed case", "assigned_to": "atlas"},
    )
    assert ticket.json()["assigned_to"] == "Atlas"
    started = await client.post(
        f"/api/v1/projects/{project.id}/tickets/{ticket.json()['id']}/start", headers=_hdrs(key)
    )
    assert started.status_code == 200
    assert "Project atlas" in started.text and "Org atlas" not in started.text
    # Canonical name in the response (for the client bundle) and in the
    # server-side retrieval audit context.
    assert started.json()["persona_name"] == "Atlas"
    from sessionfs.server.db.models import RetrievalAuditContext
    audit = (
        await db_session.execute(
            select(RetrievalAuditContext).where(
                RetrievalAuditContext.id == started.json()["retrieval_audit_id"]
            )
        )
    ).scalar_one()
    assert audit.persona_name == "Atlas"

    # Project writes match case-insensitively too (no misleading 404).
    upd = await client.put(
        f"/api/v1/projects/{project.id}/personas/atlas", headers=_hdrs(key),
        json={"content": "# tuned"},
    )
    assert upd.status_code == 200 and upd.json()["name"] == "Atlas"


@pytest.mark.asyncio
async def test_mixed_case_org_only_returns_canonical_org_name(
    client: AsyncClient, db_session: AsyncSession
):
    org = await _org(db_session)
    admin, key = await _member(db_session, org, "admin")
    project = await _project(db_session, admin, org)
    await _create_org_persona(client, org, key, name="Release-Gov", role="Gov")
    run = await client.post(
        f"/api/v1/projects/{project.id}/agent-runs", headers=_hdrs(key),
        json={"persona_name": "release-gov"},
    )
    assert run.status_code == 201
    assert run.json()["persona_name"] == "Release-Gov"
    got = await client.get(f"/api/v1/orgs/{org.id}/personas/RELEASE-GOV", headers=_hdrs(key))
    assert got.status_code == 200 and got.json()["name"] == "Release-Gov"


# ── project transfers vs org personas (Codex R1 MEDIUM 2) ────


async def _ticket(client: AsyncClient, project: Project, key: str, persona: str) -> str:
    resp = await client.post(
        f"/api/v1/projects/{project.id}/tickets", headers=_hdrs(key),
        json={"title": f"Work for {persona}", "assigned_to": persona},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


@pytest.mark.asyncio
async def test_transfer_out_of_org_refuses_stranded_org_personas(
    client: AsyncClient, db_session: AsyncSession
):
    """org → personal, auto-accepted (admin owns the project)."""
    org = await _org(db_session)
    admin, key = await _member(db_session, org, "admin")
    project = await _project(db_session, admin, org)
    await _create_org_persona(client, org, key)
    tid = await _ticket(client, project, key, "gcp-infra")

    resp = await client.post(
        f"/api/v1/projects/{project.id}/transfer", headers=_hdrs(key), json={"to": "personal"}
    )
    assert resp.status_code == 409, resp.text
    err = resp.json()["error"]
    assert err["code"] == "personas_stranded"
    assert err["details"]["personas"] == [{"persona": "gcp-infra", "ticket_ids": [tid], "run_ids": [], "handoff_ids": []}]
    await db_session.refresh(project)
    assert project.org_id == org.id  # nothing moved

    resp = await client.post(
        f"/api/v1/projects/{project.id}/transfer", headers=_hdrs(key),
        json={"to": "personal", "force": True},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["state"] == "accepted"
    await db_session.refresh(project)
    assert project.org_id is None
    # Not materialized: no project copy of the org persona was created.
    copies = (
        await db_session.execute(
            select(AgentPersona).where(AgentPersona.project_id == project.id)
        )
    ).scalars().all()
    assert copies == []


@pytest.mark.asyncio
async def test_transfer_out_of_org_allowed_when_project_override_exists(
    client: AsyncClient, db_session: AsyncSession
):
    org = await _org(db_session)
    admin, key = await _member(db_session, org, "admin")
    project = await _project(db_session, admin, org)
    await _create_org_persona(client, org, key)
    await client.post(
        f"/api/v1/projects/{project.id}/personas", headers=_hdrs(key),
        json={"name": "GCP-Infra", "role": "override"},
    )
    await _ticket(client, project, key, "gcp-infra")
    resp = await client.post(
        f"/api/v1/projects/{project.id}/transfer", headers=_hdrs(key), json={"to": "personal"}
    )
    assert resp.status_code == 200, resp.text


@pytest.mark.asyncio
async def test_transfer_into_other_org_accept_refuses_then_force(
    client: AsyncClient, db_session: AsyncSession
):
    """org A → org B (pending, accepted by B's admin). B lacks `gcp-infra`
    but has `release` — only gcp-infra is stranded."""
    org_a = await _org(db_session)
    org_b = await _org(db_session)
    admin_a, key_a = await _member(db_session, org_a, "admin")
    db_session.add(OrgMember(
        org_id=org_b.id, user_id=admin_a.id, role="member",
        joined_at=datetime.now(timezone.utc),
    ))
    await db_session.commit()
    _, key_b = await _member(db_session, org_b, "admin")
    project = await _project(db_session, admin_a, org_a)
    await _create_org_persona(client, org_a, key_a)
    await _create_org_persona(client, org_a, key_a, name="release", role="Gov")
    await _create_org_persona(client, org_b, key_b, name="Release", role="Gov B")
    t_infra = await _ticket(client, project, key_a, "gcp-infra")
    await _ticket(client, project, key_a, "release")

    resp = await client.post(
        f"/api/v1/projects/{project.id}/transfer", headers=_hdrs(key_a), json={"to": org_b.id}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["state"] == "pending"
    xfer = resp.json()["id"]

    resp = await client.post(f"/api/v1/transfers/{xfer}/accept", headers=_hdrs(key_b))
    assert resp.status_code == 409, resp.text
    details = resp.json()["error"]["details"]
    assert details["personas"] == [{"persona": "gcp-infra", "ticket_ids": [t_infra], "run_ids": [], "handoff_ids": []}]
    await db_session.refresh(project)
    assert project.org_id == org_a.id

    resp = await client.post(f"/api/v1/transfers/{xfer}/accept?force=true", headers=_hdrs(key_b))
    assert resp.status_code == 200, resp.text
    await db_session.refresh(project)
    assert project.org_id == org_b.id


@pytest.mark.asyncio
async def test_transfer_ignores_terminal_and_unresolved_assignments(
    client: AsyncClient, db_session: AsyncSession
):
    org = await _org(db_session)
    admin, key = await _member(db_session, org, "admin")
    project = await _project(db_session, admin, org)
    await _create_org_persona(client, org, key)
    tid = await _ticket(client, project, key, "gcp-infra")
    # Free-text owner that never resolved — not the transfer's doing.
    await _ticket(client, project, key, "some-human")
    from sessionfs.server.db.models import Ticket
    row = (await db_session.execute(select(Ticket).where(Ticket.id == tid))).scalar_one()
    row.status = "done"
    await db_session.commit()
    resp = await client.post(
        f"/api/v1/projects/{project.id}/transfer", headers=_hdrs(key), json={"to": "personal"}
    )
    assert resp.status_code == 200, resp.text



# ── Codex R2 HIGH — no org-persona disclosure after a transfer ───────


@pytest.mark.asyncio
async def test_non_member_keeps_project_access_but_not_new_orgs_personas(
    client: AsyncClient, db_session: AsyncSession
):
    """The project owner is a plain member of org A. The project moves to
    org B (where the owner is NOT a member). The owner still reaches the
    project, but must not see org B's personas through any path."""
    org_a = await _org(db_session)
    org_b = await _org(db_session)
    owner, owner_key = await _member(db_session, org_a, "member")
    admin_a, key_a = await _member(db_session, org_a, "admin")
    db_session.add(OrgMember(
        org_id=org_b.id, user_id=admin_a.id, role="member",
        joined_at=datetime.now(timezone.utc),
    ))
    await db_session.commit()
    _, key_b = await _member(db_session, org_b, "admin")
    project = await _project(db_session, owner, org_a)
    await _create_org_persona(client, org_b, key_b, name="secret-b", role="Org B only")

    resp = await client.post(
        f"/api/v1/projects/{project.id}/transfer", headers=_hdrs(key_a), json={"to": org_b.id}
    )
    assert resp.status_code == 200, resp.text
    resp = await client.post(f"/api/v1/transfers/{resp.json()['id']}/accept", headers=_hdrs(key_b))
    assert resp.status_code == 200, resp.text
    await db_session.refresh(project)
    assert project.org_id == org_b.id

    # A ticket assigned (by an org-B member) to the org-B persona.
    tk = await client.post(
        f"/api/v1/projects/{project.id}/tickets", headers=_hdrs(key_b),
        json={"title": "B work", "assigned_to": "secret-b"},
    )
    assert tk.status_code == 201, tk.text

    # Owner still has project access...
    listed = await client.get(f"/api/v1/projects/{project.id}/personas", headers=_hdrs(owner_key))
    assert listed.status_code == 200
    # ...but no org-B personas anywhere.
    assert listed.json() == []
    assert (await client.get(
        f"/api/v1/projects/{project.id}/personas/secret-b", headers=_hdrs(owner_key)
    )).status_code == 404
    start = await client.post(
        f"/api/v1/projects/{project.id}/tickets/{tk.json()['id']}/start", headers=_hdrs(owner_key)
    )
    assert start.status_code == 400
    assert "Org B only" not in start.text
    run = await client.post(
        f"/api/v1/projects/{project.id}/agent-runs", headers=_hdrs(owner_key),
        json={"persona_name": "secret-b"},
    )
    assert run.status_code == 422
    miss = await client.put(
        f"/api/v1/projects/{project.id}/personas/secret-b", headers=_hdrs(owner_key),
        json={"role": "x"},
    )
    assert miss.status_code == 404
    assert org_b.id not in miss.text and "org-level" not in miss.text
    ctx = await client.post(
        f"/api/v1/projects/{project.id}/retrieval-audit-contexts", headers=_hdrs(owner_key),
        json={"persona_name": "secret-b"},
    )
    assert ctx.status_code == 422

    # An org-B member does see it.
    listed_b = await client.get(f"/api/v1/projects/{project.id}/personas", headers=_hdrs(key_b))
    assert [p["name"] for p in listed_b.json()] == ["secret-b"]


# ── Codex R2 MEDIUM — locks precede the checks they protect ──────────


def _statement_log(db_session: AsyncSession) -> list[str]:
    from sqlalchemy import event

    log: list[str] = []
    engine = db_session.bind.sync_engine

    def _before(conn, cursor, statement, params, context, executemany):
        log.append(" ".join(statement.split()).lower())

    event.listen(engine, "before_cursor_execute", _before)
    log.append("__listening__")
    _statement_log.remove = lambda: event.remove(engine, "before_cursor_execute", _before)  # type: ignore[attr-defined]
    return log


def _index(log: list[str], needle: str, start: int = 0) -> int:
    for i in range(start, len(log)):
        if needle in log[i]:
            return i
    raise AssertionError(f"{needle!r} not found in statements: {log[start:]}")


@pytest.mark.asyncio
async def test_persona_create_takes_project_lock_before_uniqueness_check(
    client: AsyncClient, db_session: AsyncSession
):
    org = await _org(db_session)
    admin, key = await _member(db_session, org, "admin")
    project = await _project(db_session, admin, org)
    log = _statement_log(db_session)
    try:
        resp = await client.post(
            f"/api/v1/projects/{project.id}/personas", headers=_hdrs(key),
            json={"name": "Atlas", "role": "r"},
        )
    finally:
        _statement_log.remove()  # type: ignore[attr-defined]
    assert resp.status_code == 201
    lock_p = _index(log, "update projects set id = id")
    lock_o = _index(log, "update organizations set id = id", lock_p)
    check = _index(log, "lower(agent_personas.name)", lock_o)
    insert = _index(log, "insert into agent_personas", check)
    assert lock_p < lock_o < check < insert


@pytest.mark.asyncio
async def test_ticket_assign_and_transfer_take_locks_before_guard(
    client: AsyncClient, db_session: AsyncSession
):
    org = await _org(db_session)
    admin, key = await _member(db_session, org, "admin")
    project = await _project(db_session, admin, org)
    await _create_org_persona(client, org, key)

    log = _statement_log(db_session)
    try:
        tk = await client.post(
            f"/api/v1/projects/{project.id}/tickets", headers=_hdrs(key),
            json={"title": "Infra", "assigned_to": "gcp-infra"},
        )
        mark = len(log)
        xfer = await client.post(
            f"/api/v1/projects/{project.id}/transfer", headers=_hdrs(key), json={"to": "personal"}
        )
    finally:
        _statement_log.remove()  # type: ignore[attr-defined]
    assert tk.status_code == 201
    # ticket create: project lock, org lock, then persona resolution, insert.
    lp = _index(log, "update projects set id = id")
    lo = _index(log, "update organizations set id = id", lp)
    res = _index(log, "from agent_personas", lo)
    ins = _index(log, "insert into tickets", res)
    assert lp < lo < res < ins
    # transfer: project lock, org lock, then the stranded-ticket read.
    assert xfer.status_code == 409
    lp2 = _index(log, "update projects set id = id", mark)
    lo2 = _index(log, "update organizations set id = id", lp2)
    guard = _index(log, "from tickets", lo2)
    assert lp2 < lo2 < guard


@pytest.mark.asyncio
async def test_org_persona_delete_takes_org_lock_before_guard(
    client: AsyncClient, db_session: AsyncSession
):
    org = await _org(db_session)
    _, key = await _member(db_session, org, "admin")
    await _create_org_persona(client, org, key)
    log = _statement_log(db_session)
    try:
        resp = await client.delete(f"/api/v1/orgs/{org.id}/personas/gcp-infra", headers=_hdrs(key))
    finally:
        _statement_log.remove()  # type: ignore[attr-defined]
    assert resp.status_code == 204
    lo = _index(log, "update organizations set id = id")
    guard = _index(log, "from tickets", lo)
    assert lo < guard



@pytest.mark.asyncio
async def test_fenced_reassignment_takes_scope_lock_before_row_lock(
    client: AsyncClient, db_session: AsyncSession
):
    """Codex R3 MEDIUM — update_ticket with lease_epoch + assigned_to must
    take the project scope lock before the lease-epoch UPDATE (global
    order: scope locks, then row locks), else it can deadlock with merge."""
    org = await _org(db_session)
    admin, key = await _member(db_session, org, "admin")
    project = await _project(db_session, admin, org)
    await _create_org_persona(client, org, key)
    tk = await client.post(
        f"/api/v1/projects/{project.id}/tickets", headers=_hdrs(key),
        json={"title": "Fenced", "assigned_to": None},
    )
    assert tk.status_code == 201
    epoch = tk.json().get("lease_epoch", 0)

    log = _statement_log(db_session)
    try:
        resp = await client.put(
            f"/api/v1/projects/{project.id}/tickets/{tk.json()['id']}", headers=_hdrs(key),
            json={"assigned_to": "GCP-INFRA", "title": "Fenced v2", "lease_epoch": epoch},
        )
    finally:
        _statement_log.remove()  # type: ignore[attr-defined]
    assert resp.status_code == 200, resp.text
    assert resp.json()["assigned_to"] == "gcp-infra"
    lp = _index(log, "update projects set id = id")
    lo = _index(log, "update organizations set id = id", lp)
    fence = _index(log, "update tickets set lease_epoch", lo)
    assert lp < lo < fence



# ── Codex R4 — transfers re-check scope under the lock ───────────────


@pytest.mark.asyncio
async def test_accept_refuses_when_project_moved_since_initiation(
    client: AsyncClient, db_session: AsyncSession
):
    """A pending A→B transfer whose project has meanwhile moved to C (a
    racing auto-accepted move) must not pull the project out of C."""
    org_a = await _org(db_session)
    org_b = await _org(db_session)
    org_c = await _org(db_session)
    admin_a, key_a = await _member(db_session, org_a, "admin")
    db_session.add(OrgMember(
        org_id=org_b.id, user_id=admin_a.id, role="member",
        joined_at=datetime.now(timezone.utc),
    ))
    await db_session.commit()
    _, key_b = await _member(db_session, org_b, "admin")
    project = await _project(db_session, admin_a, org_a)
    resp = await client.post(
        f"/api/v1/projects/{project.id}/transfer", headers=_hdrs(key_a), json={"to": org_b.id}
    )
    assert resp.status_code == 200 and resp.json()["state"] == "pending"
    xfer = resp.json()["id"]

    # The racing move to C landed.
    project.org_id = org_c.id
    await db_session.commit()

    resp = await client.post(f"/api/v1/transfers/{xfer}/accept", headers=_hdrs(key_b))
    assert resp.status_code == 409, resp.text
    assert resp.json()["error"]["code"] == "transfer_scope_changed"
    await db_session.refresh(project)
    assert project.org_id == org_c.id


@pytest.mark.asyncio
async def test_every_initiation_locks_before_its_checks(
    client: AsyncClient, db_session: AsyncSession
):
    """Pending (non-auto-accepted) initiations take the project lock too,
    before the pending-transfer check."""
    org_a = await _org(db_session)
    org_b = await _org(db_session)
    admin_a, key_a = await _member(db_session, org_a, "admin")
    db_session.add(OrgMember(
        org_id=org_b.id, user_id=admin_a.id, role="member",
        joined_at=datetime.now(timezone.utc),
    ))
    await db_session.commit()
    await _member(db_session, org_b, "admin")
    project = await _project(db_session, admin_a, org_a)
    log = _statement_log(db_session)
    try:
        resp = await client.post(
            f"/api/v1/projects/{project.id}/transfer", headers=_hdrs(key_a), json={"to": org_b.id}
        )
    finally:
        _statement_log.remove()  # type: ignore[attr-defined]
    assert resp.status_code == 200 and resp.json()["state"] == "pending"
    lp = _index(log, "update projects set id = id")
    lo = _index(log, "update organizations set id = id", lp)
    pending = _index(log, "from project_transfers", lo)
    assert lp < lo < pending


# ── Codex R4 — writes to a merged project are refused ────────────────


@pytest.mark.asyncio
async def test_writes_to_merged_source_project_are_refused(
    client: AsyncClient, db_session: AsyncSession
):
    org = await _org(db_session)
    admin, key = await _member(db_session, org, "admin")
    source = await _project(db_session, admin, org)
    target = await _project(db_session, admin, org)
    source.merged_into_project_id = target.id
    await db_session.commit()

    wiki = await client.put(
        f"/api/v1/projects/{source.id}/pages/notes", headers=_hdrs(key),
        json={"content": "late page"},
    )
    entry = await client.post(
        f"/api/v1/projects/{source.id}/entries/add", headers=_hdrs(key),
        json={"content": "A late knowledge entry that must not land.", "entry_type": "discovery"},
    )
    run = await client.post(
        f"/api/v1/projects/{source.id}/agent-runs", headers=_hdrs(key),
        json={"persona_name": "atlas"},
    )
    for resp in (wiki, entry, run):
        assert resp.status_code == 409, resp.text
        assert "project_merged" in resp.text


@pytest.mark.asyncio
async def test_knowledge_pages_unique_per_project_slug(db_session: AsyncSession):
    from sqlalchemy.exc import IntegrityError

    from sessionfs.server.db.models import KnowledgePage

    owner, _ = await _user(db_session)
    project = await _project(db_session, owner, None)
    for i in range(2):
        db_session.add(KnowledgePage(
            id=f"kp_{i}_{uuid.uuid4().hex[:6]}", project_id=project.id, slug="dup",
            title="t", page_type="concept", content="",
        ))
    with pytest.raises(IntegrityError):
        await db_session.commit()
    await db_session.rollback()


# ── Codex R4 — agent runs vs stranded personas ───────────────────────


@pytest.mark.asyncio
async def test_agent_run_start_with_unresolvable_persona_stays_queued(
    client: AsyncClient, db_session: AsyncSession
):
    from sessionfs.server.db.models import AgentRun

    org = await _org(db_session)
    admin, key = await _member(db_session, org, "admin")
    project = await _project(db_session, admin, org)
    await _create_org_persona(client, org, key)
    run = await client.post(
        f"/api/v1/projects/{project.id}/agent-runs", headers=_hdrs(key),
        json={"persona_name": "gcp-infra"},
    )
    assert run.status_code == 201
    run_id = run.json()["id"]
    assert (await client.delete(
        f"/api/v1/orgs/{org.id}/personas/gcp-infra?force=true", headers=_hdrs(key)
    )).status_code == 204

    resp = await client.post(
        f"/api/v1/projects/{project.id}/agent-runs/{run_id}/start", headers=_hdrs(key)
    )
    assert resp.status_code == 422, resp.text
    row = (await db_session.execute(select(AgentRun).where(AgentRun.id == run_id))).scalar_one()
    await db_session.refresh(row)
    assert row.status == "queued"


@pytest.mark.asyncio
async def test_queued_agent_runs_count_in_transfer_and_delete_guards(
    client: AsyncClient, db_session: AsyncSession
):
    org = await _org(db_session)
    admin, key = await _member(db_session, org, "admin")
    project = await _project(db_session, admin, org)
    await _create_org_persona(client, org, key)
    run = await client.post(
        f"/api/v1/projects/{project.id}/agent-runs", headers=_hdrs(key),
        json={"persona_name": "gcp-infra"},
    )
    run_id = run.json()["id"]

    resp = await client.post(
        f"/api/v1/projects/{project.id}/transfer", headers=_hdrs(key), json={"to": "personal"}
    )
    assert resp.status_code == 409, resp.text
    assert resp.json()["error"]["details"]["personas"] == [
        {"persona": "gcp-infra", "ticket_ids": [], "run_ids": [run_id], "handoff_ids": []}
    ]
    resp = await client.delete(f"/api/v1/orgs/{org.id}/personas/gcp-infra", headers=_hdrs(key))
    assert resp.status_code == 409
    assert "agent run" in resp.text

    # Project persona delete guard counts runs too.
    await client.post(
        f"/api/v1/projects/{project.id}/personas", headers=_hdrs(key),
        json={"name": "local", "role": "r"},
    )
    await client.post(
        f"/api/v1/projects/{project.id}/agent-runs", headers=_hdrs(key),
        json={"persona_name": "local"},
    )
    resp = await client.delete(f"/api/v1/projects/{project.id}/personas/local", headers=_hdrs(key))
    assert resp.status_code == 409
    assert "agent run" in resp.text



# ── Codex R5 — every writer of merge-relocated tables locks first ────


@pytest.mark.asyncio
async def test_more_writers_refuse_a_merged_project(
    client: AsyncClient, db_session: AsyncSession
):
    """Compile, rebuild, wiki delete/regenerate, knowledge supersede,
    unassigned ticket create and retrieval-audit context create all take
    the project scope lock and refuse a merged-away project."""
    org = await _org(db_session)
    admin, key = await _member(db_session, org, "admin")
    source = await _project(db_session, admin, org)
    target = await _project(db_session, admin, org)
    source.merged_into_project_id = target.id
    await db_session.commit()

    calls = [
        ("POST", f"/api/v1/projects/{source.id}/compile", {"json": {}}),
        ("POST", f"/api/v1/projects/{source.id}/rebuild", {"json": {}}),
        ("DELETE", f"/api/v1/projects/{source.id}/pages/notes", {}),
        ("POST", f"/api/v1/projects/{source.id}/pages/notes/regenerate", {"json": {}}),
        ("PUT", f"/api/v1/projects/{source.id}/entries/1/supersede",
         {"json": {"superseding_id": 2, "reason": "newer"}}),
        ("POST", f"/api/v1/projects/{source.id}/tickets", {"json": {"title": "Unassigned"}}),
        ("POST", f"/api/v1/projects/{source.id}/retrieval-audit-contexts", {"json": {}}),
    ]
    for method, url, kwargs in calls:
        resp = await client.request(method, url, headers=_hdrs(key), **kwargs)
        assert resp.status_code == 409, (method, url, resp.status_code, resp.text)
        assert "project_merged" in resp.text, (method, url)


@pytest.mark.asyncio
async def test_compile_takes_scope_lock_before_writes(
    client: AsyncClient, db_session: AsyncSession
):
    org = await _org(db_session)
    admin, key = await _member(db_session, org, "admin")
    project = await _project(db_session, admin, org)
    resp = await client.post(
        f"/api/v1/projects/{project.id}/entries/add", headers=_hdrs(key),
        json={"content": "The compile path must lock before writing anything.",
              "entry_type": "decision", "confidence": 0.9},
    )
    assert resp.status_code == 201, resp.text
    log = _statement_log(db_session)
    try:
        resp = await client.post(
            f"/api/v1/projects/{project.id}/rebuild", headers=_hdrs(key), json={}
        )
    finally:
        _statement_log.remove()  # type: ignore[attr-defined]
    assert resp.status_code == 200, resp.text
    lock = _index(log, "update projects set id = id")
    writes = [
        i for i, stmt in enumerate(log)
        if stmt.startswith(("update knowledge", "insert into", "delete from"))
    ]
    assert writes and min(writes) > lock


# ── Codex R5 — starts lock first and keep one transaction ────────────


@pytest.mark.asyncio
async def test_start_ticket_locks_before_reads_and_is_one_transaction(
    client: AsyncClient, db_session: AsyncSession, monkeypatch
):
    from sessionfs.server.db.models import RetrievalAuditContext, Ticket
    from sessionfs.server.routes import tickets as tickets_mod

    org = await _org(db_session)
    admin, key = await _member(db_session, org, "admin")
    project = await _project(db_session, admin, org)
    await _create_org_persona(client, org, key)
    tid = await _ticket(client, project, key, "gcp-infra")

    log = _statement_log(db_session)
    try:
        ok = await client.post(
            f"/api/v1/projects/{project.id}/tickets/{tid}/start", headers=_hdrs(key)
        )
    finally:
        _statement_log.remove()  # type: ignore[attr-defined]
    assert ok.status_code == 200
    lp = _index(log, "update projects set id = id")
    lo = _index(log, "update organizations set id = id", lp)
    read_ticket = _index(log, "from tickets", lo)
    read_persona = _index(log, "from agent_personas", lo)
    assert lp < lo < read_ticket and lo < read_persona

    # A failure after the transition rolls the transition back too.
    tid2 = await _ticket(client, project, key, "gcp-infra")

    async def _boom(*_a, **_k):
        raise RuntimeError("compile failed")

    monkeypatch.setattr(tickets_mod, "_compile_persona_context", _boom)
    with pytest.raises(RuntimeError):
        await client.post(
            f"/api/v1/projects/{project.id}/tickets/{tid2}/start", headers=_hdrs(key)
        )
    await db_session.rollback()
    status = (await db_session.execute(select(Ticket.status).where(Ticket.id == tid2))).scalar_one()
    assert status == "open"
    audits = (await db_session.execute(
        select(RetrievalAuditContext).where(RetrievalAuditContext.ticket_id == tid2)
    )).scalars().all()
    assert audits == []


@pytest.mark.asyncio
async def test_start_agent_run_locks_before_reading_run_and_persona(
    client: AsyncClient, db_session: AsyncSession
):
    org = await _org(db_session)
    admin, key = await _member(db_session, org, "admin")
    project = await _project(db_session, admin, org)
    await _create_org_persona(client, org, key)
    run = await client.post(
        f"/api/v1/projects/{project.id}/agent-runs", headers=_hdrs(key),
        json={"persona_name": "gcp-infra"},
    )
    log = _statement_log(db_session)
    try:
        resp = await client.post(
            f"/api/v1/projects/{project.id}/agent-runs/{run.json()['id']}/start",
            headers=_hdrs(key),
        )
    finally:
        _statement_log.remove()  # type: ignore[attr-defined]
    assert resp.status_code == 200, resp.text
    lp = _index(log, "update projects set id = id")
    lo = _index(log, "update organizations set id = id", lp)
    assert _index(log, "from agent_runs", lo) > lo
    assert _index(log, "from agent_personas", lo) > lo
    assert _index(log, "update agent_runs set status", lo) > lo


# ── Codex R5 — membership changes follow the lock protocol ───────────


@pytest.mark.asyncio
async def test_role_change_and_removal_take_org_lock_before_row_changes(
    client: AsyncClient, db_session: AsyncSession
):
    org = await _org(db_session)
    _, owner_key = await _member(db_session, org, "owner")
    target, _ = await _member(db_session, org, "admin")
    other, _ = await _member(db_session, org, "member")
    await _project(db_session, other, org)  # removal auto-transfers it

    log = _statement_log(db_session)
    try:
        role = await client.put(
            f"/api/v1/orgs/{org.id}/members/{target.id}/role", headers=_hdrs(owner_key),
            json={"role": "member"},
        )
        mark = len(log)
        removed = await client.delete(
            f"/api/v1/orgs/{org.id}/members/{other.id}", headers=_hdrs(owner_key)
        )
    finally:
        _statement_log.remove()  # type: ignore[attr-defined]
    assert role.status_code == 200, role.text
    assert removed.status_code == 200, removed.text
    lo = _index(log, "update organizations set id = id")
    assert _index(log, "update org_members", lo) > lo
    lp = _index(log, "update projects set id = id", mark)
    lo2 = _index(log, "update organizations set id = id", lp)
    assert _index(log, "delete from org_members", lo2) > lo2


# ── Codex R5 — PUT is_active=false runs the stranding guard ──────────


@pytest.mark.asyncio
async def test_put_deactivation_runs_stranding_guard(
    client: AsyncClient, db_session: AsyncSession
):
    org = await _org(db_session)
    admin, key = await _member(db_session, org, "admin")
    project = await _project(db_session, admin, org)
    await _create_org_persona(client, org, key)
    await client.post(
        f"/api/v1/projects/{project.id}/personas", headers=_hdrs(key),
        json={"name": "local", "role": "r"},
    )
    personal = await _project(db_session, admin, None)
    await client.post(
        f"/api/v1/projects/{personal.id}/personas", headers=_hdrs(key),
        json={"name": "solo", "role": "r"},
    )
    await _ticket(client, project, key, "gcp-infra")
    await _ticket(client, personal, key, "solo")

    # Org persona: PUT is_active=false refused, then forced (audited as delete).
    resp = await client.put(
        f"/api/v1/orgs/{org.id}/personas/gcp-infra", headers=_hdrs(key),
        json={"is_active": False},
    )
    assert resp.status_code == 409, resp.text
    resp = await client.put(
        f"/api/v1/orgs/{org.id}/personas/gcp-infra?force=true", headers=_hdrs(key),
        json={"is_active": False},
    )
    assert resp.status_code == 200 and resp.json()["is_active"] is False
    events = {
        e.event_type
        for e in (
            await db_session.execute(select(OrgAuditEvent).where(OrgAuditEvent.org_id == org.id))
        ).scalars().all()
    }
    assert "persona_deleted" in events

    # Project persona: same.
    resp = await client.put(
        f"/api/v1/projects/{personal.id}/personas/solo", headers=_hdrs(key),
        json={"is_active": False},
    )
    assert resp.status_code == 409, resp.text
    resp = await client.put(
        f"/api/v1/projects/{personal.id}/personas/solo?force=true", headers=_hdrs(key),
        json={"is_active": False},
    )
    assert resp.status_code == 200 and resp.json()["is_active"] is False
    # Non-deactivating PUTs are unaffected.
    resp = await client.put(
        f"/api/v1/projects/{project.id}/personas/local", headers=_hdrs(key),
        json={"content": "# v2"},
    )
    assert resp.status_code == 200


# ── Codex R5 LOW — delete guard does not reveal hidden org personas ──


@pytest.mark.asyncio
async def test_project_persona_delete_guard_ignores_hidden_org_fallback(
    client: AsyncClient, db_session: AsyncSession
):
    """A non-member owner (project moved into an org they don't belong to)
    deleting a project persona that shadows a private org persona gets the
    normal guard (409), exactly as if no org persona existed. An org member
    gets the fallback (204)."""
    org = await _org(db_session)
    _, member_key = await _member(db_session, org, "admin")
    outsider, outsider_key = await _user(db_session)
    project = await _project(db_session, outsider, org)  # owner, not a member
    await _create_org_persona(client, org, member_key, name="secret", role="Org only")
    resp = await client.post(
        f"/api/v1/projects/{project.id}/personas", headers=_hdrs(outsider_key),
        json={"name": "secret", "role": "project"},
    )
    assert resp.status_code == 201, resp.text
    await _ticket(client, project, outsider_key, "secret")

    resp = await client.delete(
        f"/api/v1/projects/{project.id}/personas/secret", headers=_hdrs(outsider_key)
    )
    assert resp.status_code == 409, resp.text
    resp = await client.delete(
        f"/api/v1/projects/{project.id}/personas/secret", headers=_hdrs(member_key)
    )
    assert resp.status_code == 204, resp.text



# ── Codex R6 — re-authorize after waiting for the scope lock ─────────


def _land_transfer_while_waiting(monkeypatch, project_id: str, new_org_id: str | None):
    """Make the first lock_project_scope() call behave as if a transfer of
    the project committed while this request waited for the lock."""
    from sqlalchemy import update as sa_update

    from sessionfs.server.services import scope_locks

    real = scope_locks.lock_project_scope
    state = {"fired": False}

    async def racing(db, project, **kw):
        if not state["fired"] and project.id == project_id:
            state["fired"] = True
            await db.execute(
                sa_update(Project).where(Project.id == project_id).values(org_id=new_org_id)
            )
        await real(db, project, **kw)

    monkeypatch.setattr(scope_locks, "lock_project_scope", racing)
    return state


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["ticket", "persona", "knowledge", "agent_run"])
async def test_transfer_landing_while_waiting_revokes_access(
    client: AsyncClient, db_session: AsyncSession, monkeypatch, route: str
):
    """A member of org A (not the owner) writes to a project that moves to
    org B (where they are not a member) while the request waits for the
    lock: the re-check under the lock refuses it (403/404)."""
    org_a = await _org(db_session)
    org_b = await _org(db_session)
    owner, _ = await _member(db_session, org_a, "admin")
    _, member_key = await _member(db_session, org_a, "member")
    project = await _project(db_session, owner, org_a)
    if route == "agent_run":
        await client.post(
            f"/api/v1/projects/{project.id}/personas", headers=_hdrs(member_key),
            json={"name": "runner", "role": "r"},
        )
    state = _land_transfer_while_waiting(monkeypatch, project.id, org_b.id)

    if route == "ticket":
        resp = await client.post(
            f"/api/v1/projects/{project.id}/tickets", headers=_hdrs(member_key),
            json={"title": "Late"},
        )
    elif route == "persona":
        resp = await client.post(
            f"/api/v1/projects/{project.id}/personas", headers=_hdrs(member_key),
            json={"name": "late", "role": "r"},
        )
    elif route == "knowledge":
        resp = await client.post(
            f"/api/v1/projects/{project.id}/entries/add", headers=_hdrs(member_key),
            json={"content": "A late entry after the project moved away.",
                  "entry_type": "discovery"},
        )
    else:
        resp = await client.post(
            f"/api/v1/projects/{project.id}/agent-runs", headers=_hdrs(member_key),
            json={"persona_name": "runner"},
        )
    assert state["fired"]
    assert resp.status_code in (403, 404), (route, resp.status_code, resp.text)


@pytest.mark.asyncio
async def test_transfer_to_lower_plan_while_waiting_hits_tier_gate(
    client: AsyncClient, db_session: AsyncSession, monkeypatch
):
    """The caller is a member of both orgs, but the destination org's plan
    lacks agent_tickets: the feature gate re-runs under the lock."""
    org_a = await _org(db_session)
    org_free = await _org(db_session, tier="free")
    owner, _ = await _member(db_session, org_a, "admin")
    member, member_key = await _member(db_session, org_a, "member")
    db_session.add(OrgMember(
        org_id=org_free.id, user_id=member.id, role="member",
        joined_at=datetime.now(timezone.utc),
    ))
    await db_session.commit()
    project = await _project(db_session, owner, org_a)
    _land_transfer_while_waiting(monkeypatch, project.id, org_free.id)
    resp = await client.post(
        f"/api/v1/projects/{project.id}/tickets", headers=_hdrs(member_key),
        json={"title": "Late"},
    )
    assert resp.status_code == 403, resp.text
    assert "upgrade_required" in resp.text


@pytest.mark.asyncio
async def test_service_key_boundary_rechecked_under_lock(
    client: AsyncClient, db_session: AsyncSession, monkeypatch
):
    org_a = await _org(db_session)
    org_b = await _org(db_session)
    admin, _ = await _member(db_session, org_a, "admin")
    key = await _service_key(db_session, org_a, admin, ["tickets:write", "tickets:read"])
    project = await _project(db_session, admin, org_a)
    _land_transfer_while_waiting(monkeypatch, project.id, org_b.id)
    resp = await client.post(
        f"/api/v1/projects/{project.id}/tickets", headers=_hdrs(key), json={"title": "Late"},
    )
    assert resp.status_code == 403, resp.text
    assert "cross_org_denied" in resp.text


# ── Codex R6 MEDIUM — org persona create re-authorizes under the lock ─


@pytest.mark.asyncio
async def test_org_persona_create_reauthorizes_after_org_lock(
    client: AsyncClient, db_session: AsyncSession
):
    org = await _org(db_session)
    _, key = await _member(db_session, org, "admin")
    log = _statement_log(db_session)
    try:
        resp = await client.post(
            f"/api/v1/orgs/{org.id}/personas", headers=_hdrs(key), json=_BODY
        )
    finally:
        _statement_log.remove()  # type: ignore[attr-defined]
    assert resp.status_code == 201
    lo = _index(log, "update organizations set id = id")
    assert _index(log, "from org_members", lo) > lo



# ── Codex R6 HIGH — concept generation commits exactly once ──────────


@pytest.mark.asyncio
async def test_auto_generate_concepts_prunes_inside_its_single_commit(
    db_session: AsyncSession,
):
    """_prune_dead_concept_pages no longer commits mid-phase (that released
    the scope and row locks early); the pruning lands in the one commit at
    the end of auto_generate_concepts."""
    from sqlalchemy import event

    from sessionfs.server.db.models import KnowledgePage
    from sessionfs.server.services.compiler import auto_generate_concepts

    owner, _ = await _user(db_session)
    project = await _project(db_session, owner, None)
    page_id = f"kp_{uuid.uuid4().hex[:8]}"
    db_session.add(KnowledgePage(
        id=page_id, project_id=project.id, slug="concept/orphan",
        title="Orphan", page_type="concept", content="",
    ))
    await db_session.commit()

    commits: list[int] = []
    sync_session = db_session.sync_session

    def _after_commit(_session):
        commits.append(1)

    event.listen(sync_session, "after_commit", _after_commit)
    try:
        await auto_generate_concepts(project.id, owner.id, db_session)
    finally:
        event.remove(sync_session, "after_commit", _after_commit)
    assert len(commits) == 1
    gone = (await db_session.execute(
        select(KnowledgePage.id).where(KnowledgePage.id == page_id)
    )).scalar_one_or_none()
    assert gone is None

    # The pruning helper itself never commits (it used to, which released
    # the locks before concept creation in the same phase).
    from sessionfs.server.services.compiler import _prune_dead_concept_pages

    db_session.add(KnowledgePage(
        id=f"kp_{uuid.uuid4().hex[:8]}", project_id=project.id, slug="concept/orphan2",
        title="Orphan 2", page_type="concept", content="",
    ))
    await db_session.commit()
    commits.clear()
    event.listen(sync_session, "after_commit", _after_commit)
    try:
        deleted = await _prune_dead_concept_pages(project.id, db_session)
    finally:
        event.remove(sync_session, "after_commit", _after_commit)
    assert deleted == 1
    assert commits == []
    await db_session.rollback()



# ── Codex R7 — reassignment re-authorizes under the scope lock ───────


@pytest.mark.asyncio
async def test_reassignment_reauthorizes_after_waiting_for_lock(
    client: AsyncClient, db_session: AsyncSession, monkeypatch
):
    """An org admin (not the ticket's creator) is demoted while their
    reassignment waits for the project lock: the creator/admin check
    re-runs under the lock and refuses (403)."""
    from sqlalchemy import update as sa_update

    from sessionfs.server.services import scope_locks

    org = await _org(db_session)
    owner, owner_key = await _member(db_session, org, "owner")
    admin, admin_key = await _member(db_session, org, "admin")
    project = await _project(db_session, owner, org)
    tk = await client.post(
        f"/api/v1/projects/{project.id}/tickets", headers=_hdrs(owner_key),
        json={"title": "Owned by the owner"},
    )
    assert tk.status_code == 201

    real = scope_locks.lock_project_scope
    fired = {"n": 0}

    async def demote_while_waiting(db, proj, **kw):
        if fired["n"] == 0:
            fired["n"] += 1
            await db.execute(
                sa_update(OrgMember)
                .where(OrgMember.org_id == org.id, OrgMember.user_id == admin.id)
                .values(role="member")
            )
        await real(db, proj, **kw)

    monkeypatch.setattr(scope_locks, "lock_project_scope", demote_while_waiting)
    resp = await client.put(
        f"/api/v1/projects/{project.id}/tickets/{tk.json()['id']}", headers=_hdrs(admin_key),
        json={"assigned_to": "someone"},
    )
    assert fired["n"] == 1
    assert resp.status_code == 403, resp.text


@pytest.mark.asyncio
async def test_title_edit_reauthorizes_after_waiting_for_lock(
    client: AsyncClient, db_session: AsyncSession, monkeypatch
):
    """A non-reassignment edit (title) must take the same scope lock and
    re-run the creator/admin check under it. Before GH #70 the lock and
    re-auth were gated on `body.assigned_to is not None`, so a title edit
    that passed its pre-lock check would commit against a ticket a
    concurrent merge relocated to another project (the flush UPDATE is
    PK-scoped). Here: the admin is demoted while their title edit waits,
    and the under-lock re-auth refuses (403)."""
    from sqlalchemy import update as sa_update

    from sessionfs.server.services import scope_locks

    org = await _org(db_session)
    owner, owner_key = await _member(db_session, org, "owner")
    admin, admin_key = await _member(db_session, org, "admin")
    project = await _project(db_session, owner, org)
    tk = await client.post(
        f"/api/v1/projects/{project.id}/tickets", headers=_hdrs(owner_key),
        json={"title": "Owned by the owner"},
    )
    assert tk.status_code == 201

    real = scope_locks.lock_project_scope
    fired = {"n": 0}

    async def demote_while_waiting(db, proj, **kw):
        if fired["n"] == 0:
            fired["n"] += 1
            await db.execute(
                sa_update(OrgMember)
                .where(OrgMember.org_id == org.id, OrgMember.user_id == admin.id)
                .values(role="member")
            )
        await real(db, proj, **kw)

    monkeypatch.setattr(scope_locks, "lock_project_scope", demote_while_waiting)
    resp = await client.put(
        f"/api/v1/projects/{project.id}/tickets/{tk.json()['id']}", headers=_hdrs(admin_key),
        json={"title": "Renamed by the admin"},
    )
    assert fired["n"] == 1
    assert resp.status_code == 403, resp.text


# ── Codex R7 — platform-admin membership changes take org locks ──────


@pytest.mark.asyncio
async def test_admin_delete_user_and_force_transfer_lock_orgs_first(
    client: AsyncClient, db_session: AsyncSession
):
    platform_admin, admin_key = await _user(db_session, tier="admin")
    org_a = await _org(db_session)
    org_b = await _org(db_session)
    victim, _ = await _member(db_session, org_a, "member")
    db_session.add(OrgMember(
        org_id=org_b.id, user_id=victim.id, role="member",
        joined_at=datetime.now(timezone.utc),
    ))
    await db_session.commit()
    owner_c_org = await _org(db_session)
    old_owner, _ = await _member(db_session, owner_c_org, "owner")
    new_owner, _ = await _member(db_session, owner_c_org, "admin")

    log = _statement_log(db_session)
    try:
        deleted = await client.delete(
            f"/api/v1/admin/users/{victim.id}", headers=_hdrs(admin_key)
        )
        mark = len(log)
        moved = await client.post(
            f"/api/v1/admin/orgs/{owner_c_org.id}/force-transfer-owner",
            headers=_hdrs(admin_key), json={"to_user_id": new_owner.id},
        )
    finally:
        _statement_log.remove()  # type: ignore[attr-defined]
    assert deleted.status_code == 204, deleted.text
    assert moved.status_code == 200, moved.text

    org_locks = [
        i for i, s in enumerate(log[:mark]) if "update organizations set id = id" in s
    ]
    assert len(org_locks) >= 1
    assert _index(log, "delete from org_members") > max(org_locks)
    lo = _index(log, "update organizations set id = id", mark)
    assert _index(log, "update org_members", lo) > lo



# ── Codex R8 — owner transfers and membership add/remove lock first ──


@pytest.mark.asyncio
async def test_owner_transfer_steps_take_org_lock_before_membership_reads(
    client: AsyncClient, db_session: AsyncSession
):
    org = await _org(db_session)
    owner, owner_key = await _member(db_session, org, "owner")
    admin, admin_key = await _member(db_session, org, "admin")
    log = _statement_log(db_session)
    try:
        init = await client.post(
            f"/api/v1/orgs/{org.id}/owner/transfer", headers=_hdrs(owner_key),
            json={"to_user_id": admin.id},
        )
        mark = len(log)
        acc = await client.post(
            f"/api/v1/orgs/{org.id}/owner/transfer/{init.json()['transfer_id']}/accept",
            headers=_hdrs(admin_key),
        )
    finally:
        _statement_log.remove()  # type: ignore[attr-defined]
    assert init.status_code == 200, init.text
    assert acc.status_code == 200, acc.text
    lo = _index(log, "update organizations set id = id")
    assert _index(log, "from org_members", lo) > lo
    assert lo < mark
    lo2 = _index(log, "update organizations set id = id", mark)
    first_member_read = _index(log, "from org_members", mark)
    assert lo2 < first_member_read


@pytest.mark.asyncio
async def test_membership_add_and_user_delete_take_user_lock_first(
    client: AsyncClient, db_session: AsyncSession
):
    platform_admin, admin_key = await _user(db_session, tier="admin")
    org = await _org(db_session)
    _, owner_key = await _member(db_session, org, "owner")
    joiner, joiner_key = await _user(db_session)

    inv = await client.post(
        "/api/v1/org/invite", headers=_hdrs(owner_key),
        json={"email": joiner.email, "role": "member"},
    )
    assert inv.status_code in (200, 201), inv.text
    log = _statement_log(db_session)
    try:
        acc = await client.post(
            f"/api/v1/org/invite/{inv.json()['invite_id']}/accept", headers=_hdrs(joiner_key)
        )
        mark = len(log)
        gone = await client.delete(f"/api/v1/admin/users/{joiner.id}", headers=_hdrs(admin_key))
    finally:
        _statement_log.remove()  # type: ignore[attr-defined]
    assert acc.status_code == 200, acc.text
    assert gone.status_code == 204, gone.text

    lu = _index(log, "update users set id = id")
    lo = _index(log, "update organizations set id = id", lu)
    assert _index(log, "insert into org_members", lo) > lo
    lu2 = _index(log, "update users set id = id", mark)
    lo2 = _index(log, "update organizations set id = id", lu2)
    assert _index(log, "delete from org_members", lo2) > lo2



# ── Codex R9 — inactive users are never added to an org ──────────────


def _deactivate_on_user_lock(monkeypatch, user_id: str):
    """Simulate a concurrent user delete committing while the request
    waits for the user scope lock."""
    from sqlalchemy import update as sa_update

    from sessionfs.server.services import scope_locks

    real = scope_locks.lock_scopes
    state = {"fired": False}

    async def racing(db, *, project_ids=(), user_ids=(), org_ids=()):
        if not state["fired"] and user_id in list(user_ids):
            state["fired"] = True
            await db.execute(
                sa_update(User).where(User.id == user_id).values(is_active=False)
            )
        await real(db, project_ids=project_ids, user_ids=user_ids, org_ids=org_ids)

    monkeypatch.setattr(scope_locks, "lock_scopes", racing)
    return state


@pytest.mark.asyncio
async def test_invite_accept_refuses_user_deactivated_while_waiting(
    client: AsyncClient, db_session: AsyncSession, monkeypatch
):
    org = await _org(db_session)
    _, owner_key = await _member(db_session, org, "owner")
    joiner, joiner_key = await _user(db_session)
    inv = await client.post(
        "/api/v1/org/invite", headers=_hdrs(owner_key),
        json={"email": joiner.email, "role": "member"},
    )
    assert inv.status_code in (200, 201), inv.text
    state = _deactivate_on_user_lock(monkeypatch, joiner.id)
    resp = await client.post(
        f"/api/v1/org/invite/{inv.json()['invite_id']}/accept", headers=_hdrs(joiner_key)
    )
    assert state["fired"]
    assert resp.status_code == 403, resp.text
    count = (await db_session.execute(
        select(OrgMember).where(OrgMember.user_id == joiner.id)
    )).scalars().all()
    assert count == []


@pytest.mark.asyncio
async def test_org_create_refuses_user_deactivated_while_waiting(
    client: AsyncClient, db_session: AsyncSession, monkeypatch
):
    creator, key = await _user(db_session, tier="team")
    state = _deactivate_on_user_lock(monkeypatch, creator.id)
    resp = await client.post(
        "/api/v1/org", headers=_hdrs(key),
        json={"name": "Late Org", "slug": f"late-{uuid.uuid4().hex[:6]}"},
    )
    assert state["fired"]
    assert resp.status_code == 403, resp.text


@pytest.mark.asyncio
async def test_admin_org_create_refuses_inactive_owner(
    client: AsyncClient, db_session: AsyncSession
):
    _, admin_key = await _user(db_session, tier="admin")
    owner, _ = await _user(db_session)
    owner.is_active = False
    await db_session.commit()
    resp = await client.post(
        "/api/v1/admin/orgs", headers=_hdrs(admin_key),
        json={"name": "X", "slug": f"x-{uuid.uuid4().hex[:6]}", "owner_user_id": owner.id},
    )
    # Refused from the owner row re-read under the scope lock.
    assert resp.status_code == 422, resp.text


# ── Codex R9 — pending handoffs count in stranding guards ────────────


async def _pending_handoff(db: AsyncSession, project: Project, sender: User, persona: str) -> str:
    from sessionfs.server.db.models import Handoff, Session

    sid = f"ses_{uuid.uuid4().hex[:12]}"
    db.add(Session(
        id=sid, user_id=sender.id, source_tool="claude-code", blob_key=f"b/{sid}",
        etag="e", project_id=project.id,
    ))
    await db.flush()
    hid = f"hf_{uuid.uuid4().hex[:12]}"
    db.add(Handoff(
        id=hid, session_id=sid, sender_id=sender.id, recipient_email="r@example.com",
        status="pending", persona_name=persona,
        expires_at=datetime.now(timezone.utc) + timedelta(days=1),
    ))
    await db.commit()
    return hid


@pytest.mark.asyncio
async def test_stranding_guards_see_anchored_handoff_on_legacy_session(
    client: AsyncClient, db_session: AsyncSession
):
    """GH #70 R10 — a pending handoff whose durable anchor names the
    project must block the org persona delete even when the source
    session is LEGACY (project_id NULL): the guards use the anchor, not
    the session's (missing) project link."""
    from sessionfs.server.db.models import Handoff, Session

    org = await _org(db_session)
    admin, key = await _member(db_session, org, "admin")
    project = await _project(db_session, admin, org)
    await _create_org_persona(client, org, key)

    sid = f"ses_{uuid.uuid4().hex[:12]}"
    db_session.add(Session(
        id=sid, user_id=admin.id, source_tool="claude-code", blob_key=f"b/{sid}",
        etag="e", project_id=None,
        git_remote_normalized=project.git_remote_normalized,
    ))
    await db_session.flush()
    hid = f"hf_{uuid.uuid4().hex[:12]}"
    db_session.add(Handoff(
        id=hid, session_id=sid, sender_id=admin.id, recipient_email="r@example.com",
        status="pending", persona_name="gcp-infra", source_project_id=project.id,
        expires_at=datetime.now(timezone.utc) + timedelta(days=1),
    ))
    await db_session.commit()

    resp = await client.delete(f"/api/v1/orgs/{org.id}/personas/gcp-infra", headers=_hdrs(key))
    assert resp.status_code == 409 and "handoff" in resp.text


@pytest.mark.asyncio
async def test_org_delete_guard_sees_handoff_on_legacy_remote_only_session(
    client: AsyncClient, db_session: AsyncSession
):
    """GH #70 — a pending handoff whose source session predates the project
    link (project_id NULL) AND whose durable anchor is unset
    (source_project_id NULL) still strands the org persona. The guard must
    resolve the session's git_remote_normalized to the org's project
    (project_repos first, then projects.git_remote_normalized) instead of
    dropping the handoff and letting the delete through."""
    from sessionfs.server.db.models import Handoff, ProjectRepo, Session

    org = await _org(db_session)
    admin, key = await _member(db_session, org, "admin")
    project = await _project(db_session, admin, org)
    await _create_org_persona(client, org, key)

    # The session's remote belongs to the project only via ProjectRepo —
    # project.git_remote_normalized differs, so this exercises the repo-first
    # branch of the legacy remote resolution.
    remote = f"github.com/legacy/{uuid.uuid4().hex[:10]}"
    db_session.add(ProjectRepo(
        id=f"repo_{uuid.uuid4().hex[:12]}", project_id=project.id,
        git_remote_normalized=remote, is_primary=True,
    ))
    await db_session.flush()
    sid = f"ses_{uuid.uuid4().hex[:12]}"
    db_session.add(Session(
        id=sid, user_id=admin.id, source_tool="claude-code", blob_key=f"b/{sid}",
        etag="e", project_id=None, git_remote_normalized=remote,
    ))
    await db_session.flush()
    db_session.add(Handoff(
        id=f"hf_{uuid.uuid4().hex[:12]}", session_id=sid, sender_id=admin.id,
        recipient_email="r@example.com", status="pending", persona_name="gcp-infra",
        source_project_id=None,
        expires_at=datetime.now(timezone.utc) + timedelta(days=1),
    ))
    await db_session.commit()

    resp = await client.delete(f"/api/v1/orgs/{org.id}/personas/gcp-infra", headers=_hdrs(key))
    assert resp.status_code == 409 and "handoff" in resp.text


@pytest.mark.asyncio
async def test_pending_handoffs_count_in_stranding_guards(
    client: AsyncClient, db_session: AsyncSession
):
    org = await _org(db_session)
    admin, key = await _member(db_session, org, "admin")
    project = await _project(db_session, admin, org)
    await _create_org_persona(client, org, key)
    hid = await _pending_handoff(db_session, project, admin, "gcp-infra")

    resp = await client.post(
        f"/api/v1/projects/{project.id}/transfer", headers=_hdrs(key), json={"to": "personal"}
    )
    assert resp.status_code == 409, resp.text
    assert resp.json()["error"]["details"]["personas"] == [
        {"persona": "gcp-infra", "ticket_ids": [], "run_ids": [], "handoff_ids": [hid]}
    ]
    resp = await client.delete(f"/api/v1/orgs/{org.id}/personas/gcp-infra", headers=_hdrs(key))
    assert resp.status_code == 409 and "handoff" in resp.text
    resp = await client.put(
        f"/api/v1/orgs/{org.id}/personas/gcp-infra", headers=_hdrs(key), json={"is_active": False}
    )
    assert resp.status_code == 409

    await client.post(
        f"/api/v1/projects/{project.id}/personas", headers=_hdrs(key),
        json={"name": "local", "role": "r"},
    )
    await _pending_handoff(db_session, project, admin, "LOCAL")
    resp = await client.delete(f"/api/v1/projects/{project.id}/personas/local", headers=_hdrs(key))
    assert resp.status_code == 409 and "handoff" in resp.text
    resp = await client.delete(
        f"/api/v1/projects/{project.id}/personas/local?force=true", headers=_hdrs(key)
    )
    assert resp.status_code == 204


# ── Codex R9 — agent run start response is built under the lock ──────


@pytest.mark.asyncio
async def test_agent_run_start_does_not_requery_after_commit(
    client: AsyncClient, db_session: AsyncSession, monkeypatch
):
    """After the transition commits a concurrent merge may move the run;
    the route must not re-query it by the (now old) project."""
    from sessionfs.server.routes import agent_runs as runs_mod

    org = await _org(db_session)
    admin, key = await _member(db_session, org, "admin")
    project = await _project(db_session, admin, org)
    await client.post(
        f"/api/v1/projects/{project.id}/personas", headers=_hdrs(key),
        json={"name": "runner", "role": "r"},
    )
    run = await client.post(
        f"/api/v1/projects/{project.id}/agent-runs", headers=_hdrs(key),
        json={"persona_name": "runner"},
    )
    real = runs_mod._get_run_or_404
    calls = {"n": 0}

    async def only_once(*a, **k):
        calls["n"] += 1
        if calls["n"] > 1:
            from fastapi import HTTPException

            raise HTTPException(404, "Agent run not found (moved by a merge)")
        return await real(*a, **k)

    monkeypatch.setattr(runs_mod, "_get_run_or_404", only_once)
    resp = await client.post(
        f"/api/v1/projects/{project.id}/agent-runs/{run.json()['id']}/start", headers=_hdrs(key)
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["run"]["status"] == "running"
    assert calls["n"] == 1


# ── Codex R10 — finding 4: service-key provenance uses the key's org ──


@pytest.mark.asyncio
async def test_service_key_handoff_provenance_uses_key_org_not_membership(
    client: AsyncClient, db_session: AsyncSession
):
    """GH #70 R10 — a service key's org-persona visibility comes from the
    KEY's org, not the backing user's membership: after the minter is
    removed from the org, the key can still attach an org persona to a
    handoff (before the fix the provenance check resolved visibility from
    the user's membership and 422'd)."""
    from sessionfs.server.db.models import Session

    org = await _org(db_session)
    minter, _ = await _member(db_session, org, "admin")
    project = await _project(db_session, minter, org)
    await _create_org_persona(client, org, await _service_key(
        db_session, org, minter, ["personas:write"]
    ))
    handoff_key = await _service_key(db_session, org, minter, ["handoffs:write"])

    sid = f"ses_{uuid.uuid4().hex[:12]}"
    db_session.add(Session(
        id=sid, user_id=minter.id, source_tool="claude-code",
        blob_key=f"b/{sid}", etag="e", project_id=project.id,
    ))
    # The minter leaves the org AFTER minting the key.
    await db_session.execute(
        sa_delete(OrgMember).where(
            OrgMember.org_id == org.id, OrgMember.user_id == minter.id
        )
    )
    await db_session.commit()

    resp = await client.post(
        "/api/v1/handoffs", headers=_hdrs(handoff_key),
        json={
            "session_id": sid,
            "recipient_email": "r@example.com",
            "persona_name": "gcp-infra",
        },
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["persona_name"] == "gcp-infra"
    assert body["source_project_id"] == project.id
