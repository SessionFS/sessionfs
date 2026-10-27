"""GH #98 — Jira integration routes (org connection/mapping/health + ticket
promotion).

The Jira client is monkeypatched with an in-memory fake (never a real Jira
call). Focus:
- org auth matrix: non-Enterprise 403 envelope, member vs admin, other-org
  and outsider 404, service-key scopes/org boundary/project allowlist;
- ticket routes: unmapped/not-connected 409, 1:1 link, publish create vs
  update, unlink audit, and the token never appearing in responses or audit.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server.auth.keys import generate_api_key, hash_api_key
from sessionfs.server.db.models import (
    ApiKey,
    OrgAuditEvent,
    OrgMember,
    OrgTrackerConnection,
    Organization,
    Project,
    Ticket,
    TicketComment,
    TicketTrackerLink,
    TrackerProjectMapping,
    User,
)
from sessionfs.server.integrations.trackers.base import (
    TrackerError,
    TrackerIssue,
    TrackerIssueType,
    TrackerProject,
    TrackerProjectPage,
)
from sessionfs.server.integrations.trackers.jira import validate_site_url

_TOKEN = "atlassian-secret-token-1234"


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


async def _org(db: AsyncSession, tier: str = "enterprise") -> Organization:
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


async def _ticket(db: AsyncSession, project: Project, user: User) -> Ticket:
    ticket = Ticket(
        id=f"tk_{uuid.uuid4().hex[:16]}",
        project_id=project.id,
        title="Ship the auth migration",
        description="line one\nline two",
        acceptance_criteria=json.dumps(["users can log in"]),
        created_by_user_id=user.id,
    )
    db.add(ticket)
    await db.commit()
    await db.refresh(ticket)
    return ticket


async def _insert_connection(db: AsyncSession, org: Organization, user: User) -> None:
    from sessionfs.server.llm_keys import encrypt_secret

    db.add(OrgTrackerConnection(
        org_id=org.id,
        site_url="https://acme.atlassian.net",
        account_email="jira@acme.com",
        encrypted_api_token=encrypt_secret(_TOKEN),
        default_template="agile",
        enabled=True,
        connected_by_user_id=user.id,
    ))
    await db.commit()


async def _insert_mapping(
    db: AsyncSession, org: Organization, project: Project, user: User
) -> None:
    db.add(TrackerProjectMapping(
        id=f"tpm_{uuid.uuid4().hex[:16]}",
        org_id=org.id,
        project_id=project.id,
        jira_project_key="ACME",
        issue_type="Story",
        created_by_user_id=user.id,
    ))
    await db.commit()


# ── fake Jira client ──


@pytest.fixture
def fake_jira(monkeypatch):
    captured: list = []
    state: dict = {"fail_update": False}

    class Fake:
        def __init__(self, site_url, account_email, api_token, *, transport=None):
            self.site_url = validate_site_url(site_url)
            self.account_email = account_email
            self.api_token = api_token
            self.calls = {"create_issue": 0, "update_issue": 0, "get_issue": 0}
            self.state = state
            captured.append(self)

        async def aclose(self) -> None:
            pass

        async def validate_connection(self) -> dict:
            return {"account_id": "a1", "display_name": "N", "email": self.account_email}

        async def list_projects(self) -> list[TrackerProject]:
            return [TrackerProject(key="ACME", name="Acme", issue_types=[])]

        async def search_projects(self, query, limit) -> TrackerProjectPage:
            self.state.setdefault("searches", []).append((query, limit))
            keys = self.state.get("project_keys", ["ACME"])
            names = {"ACME": "Acme"}
            matched = [
                k for k in keys
                if not query or query.lower() in k.lower()
                or query.lower() in names.get(k, k).lower()
            ]
            return TrackerProjectPage(
                projects=[
                    TrackerProject(key=k, name=names.get(k, k.title()), issue_types=[])
                    for k in matched[:limit]
                ],
                has_more=len(matched) > limit,
            )

        async def list_issue_types(self, project_key: str) -> list[TrackerIssueType]:
            return [TrackerIssueType(name="Story", id="10000")]

        async def create_issue(self, *a, **k) -> TrackerIssue:
            self.calls["create_issue"] += 1
            return TrackerIssue(key="ACME-1", url=f"{self.site_url}/browse/ACME-1", status="", id="10001")

        async def update_issue(self, issue_key, *a, **k) -> TrackerIssue:
            self.calls["update_issue"] += 1
            if self.state["fail_update"]:
                raise TrackerError("Jira API 500: boom")
            return TrackerIssue(key=issue_key, url=f"{self.site_url}/browse/{issue_key}", status="")

        async def get_issue(self, issue_key) -> TrackerIssue:
            self.calls["get_issue"] += 1
            return TrackerIssue(
                key=issue_key,
                url=f"{self.site_url}/browse/{issue_key}",
                status=self.state.get("status", "Done"),
            )

    monkeypatch.setattr("sessionfs.server.routes.jira.JiraClient", Fake)
    return captured


# ── org connection: auth matrix ──


@pytest.mark.asyncio
async def test_non_enterprise_org_is_gated_with_upgrade_envelope(
    client: AsyncClient, db_session: AsyncSession, fake_jira
):
    org = await _org(db_session, tier="free")
    _, key = await _member(db_session, org, "admin")
    resp = await client.get(f"/api/v1/orgs/{org.id}/integrations/jira", headers=_hdrs(key))
    assert resp.status_code == 403
    body = resp.json()["error"]
    assert body["code"] == "403"
    assert body["details"]["error"] == "upgrade_required"
    assert body["details"]["feature"] == "jira_integration"
    assert body["details"]["plan_scope"] == "org"
    assert body["details"]["required_tier"] == "enterprise"


@pytest.mark.asyncio
async def test_member_cannot_read_or_write(client: AsyncClient, db_session: AsyncSession, fake_jira):
    org = await _org(db_session)
    _, member_key = await _member(db_session, org, "member")

    # Reads expose connection config, so a plain member is rejected too (GH #98
    # review round 1 — every org Jira route is admin-only).
    resp = await client.get(f"/api/v1/orgs/{org.id}/integrations/jira", headers=_hdrs(member_key))
    assert resp.status_code == 403
    assert resp.json()["error"]["details"]["error"] == "insufficient_role"

    resp = await client.put(
        f"/api/v1/orgs/{org.id}/integrations/jira",
        headers=_hdrs(member_key),
        json={"site_url": "https://acme.atlassian.net", "account_email": "jira@acme.com",
              "api_token": _TOKEN, "default_template": "agile"},
    )
    assert resp.status_code == 403
    assert resp.json()["error"]["details"]["error"] == "insufficient_role"


@pytest.mark.asyncio
async def test_other_org_and_outsider_get_404(client: AsyncClient, db_session: AsyncSession, fake_jira):
    org = await _org(db_session)
    other = await _org(db_session)
    _, admin_key = await _member(db_session, org, "admin")
    _, other_admin_key = await _member(db_session, other, "admin")
    _, outsider_key = await _user(db_session)

    for key in (other_admin_key, outsider_key):
        for method, kwargs in (
            ("GET", {}),
            ("PUT", {"json": {"site_url": "https://acme.atlassian.net",
                              "account_email": "jira@acme.com", "api_token": _TOKEN,
                              "default_template": "agile"}}),
        ):
            resp = await client.request(
                method, f"/api/v1/orgs/{org.id}/integrations/jira", headers=_hdrs(key), **kwargs
            )
            assert resp.status_code == 404, (method, resp.text)


@pytest.mark.asyncio
async def test_service_keys_are_rejected_on_all_org_jira_routes(
    client: AsyncClient, db_session: AsyncSession, fake_jira
):
    org = await _org(db_session)
    other = await _org(db_session)
    admin, _ = await _member(db_session, org, "admin")
    other_admin, _ = await _member(db_session, other, "admin")
    write_key = await _service_key(db_session, org, admin, ["tickets:read", "tickets:write"])
    read_key = await _service_key(db_session, org, admin, ["tickets:read"])
    project = await _project(db_session, admin, org)
    restricted = await _service_key(
        db_session, org, admin, ["tickets:read", "tickets:write"], project_ids=[project.id]
    )
    foreign = await _service_key(db_session, other, other_admin, ["tickets:read", "tickets:write"])

    body = {"site_url": "https://acme.atlassian.net", "account_email": "jira@acme.com",
            "api_token": _TOKEN, "default_template": "agile"}

    # The org-level integration holds the Atlassian credential, so every
    # /orgs/{id}/integrations/jira* route accepts user keys only — a service
    # key (any scope, any org, project-restricted or not) is 403.
    for key in (write_key, read_key, restricted, foreign):
        assert (await client.put(
            f"/api/v1/orgs/{org.id}/integrations/jira", headers=_hdrs(key), json=body
        )).status_code == 403
        assert (await client.get(
            f"/api/v1/orgs/{org.id}/integrations/jira", headers=_hdrs(key)
        )).status_code == 403
        assert (await client.get(
            f"/api/v1/orgs/{org.id}/integrations/jira/mappings", headers=_hdrs(key)
        )).status_code == 403
        assert (await client.get(
            f"/api/v1/orgs/{org.id}/integrations/jira/health", headers=_hdrs(key)
        )).status_code == 403

    # The rejection is explicit: service_key_not_allowed, not a role/scope miss.
    resp = await client.get(f"/api/v1/orgs/{org.id}/integrations/jira", headers=_hdrs(write_key))
    assert resp.status_code == 403
    assert resp.json()["error"]["details"]["error"] == "service_key_not_allowed"


# ── connection lifecycle + token secrecy ──


@pytest.mark.asyncio
async def test_connection_put_returns_hint_not_token_and_audits(client: AsyncClient, db_session: AsyncSession, fake_jira):
    org = await _org(db_session)
    _, key = await _member(db_session, org, "admin")
    resp = await client.put(
        f"/api/v1/orgs/{org.id}/integrations/jira",
        headers=_hdrs(key),
        json={"site_url": "https://acme.atlassian.net", "account_email": "jira@acme.com",
              "api_token": _TOKEN, "default_template": "agile"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["connected"] is True
    assert body["token_hint"] == "••••1234"
    assert "has_token" in body and body["has_token"] is True
    assert _TOKEN not in resp.text
    assert "api_token" not in body

    # The fake received the real decrypted token (proves encryption round-trips)
    # but the token must never be written to the audit row.
    assert fake_jira and fake_jira[0].api_token == _TOKEN
    events = (
        await db_session.execute(select(OrgAuditEvent).where(OrgAuditEvent.org_id == org.id))
    ).scalars().all()
    assert len(events) == 1
    assert events[0].event_type == "jira_connection_updated"
    assert _TOKEN not in (events[0].after or "")
    assert _TOKEN not in (events[0].before or "")

    # GET never returns the token either.
    resp = await client.get(f"/api/v1/orgs/{org.id}/integrations/jira", headers=_hdrs(key))
    assert resp.status_code == 200
    assert _TOKEN not in resp.text


@pytest.mark.asyncio
async def test_connection_put_rejects_unknown_template_and_invalid_site(
    client: AsyncClient, db_session: AsyncSession, fake_jira
):
    org = await _org(db_session)
    _, key = await _member(db_session, org, "admin")
    base = {"site_url": "https://acme.atlassian.net", "account_email": "jira@acme.com",
            "api_token": _TOKEN, "default_template": "agile"}

    resp = await client.put(
        f"/api/v1/orgs/{org.id}/integrations/jira", headers=_hdrs(key),
        json={**base, "default_template": "nope"},
    )
    assert resp.status_code == 422

    resp = await client.put(
        f"/api/v1/orgs/{org.id}/integrations/jira", headers=_hdrs(key),
        json={**base, "site_url": "https://evil.example.com"},
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["details"]["error"] == "jira_connection_failed"


@pytest.mark.asyncio
async def test_connection_put_rejects_oversize_fields_with_422(
    client: AsyncClient, db_session: AsyncSession, fake_jira
):
    """Oversize connection fields are rejected with a 422 up front instead of
    overflowing the columns (and surfacing as a 500) on insert."""
    org = await _org(db_session)
    _, key = await _member(db_session, org, "admin")
    base = {"site_url": "https://acme.atlassian.net", "account_email": "jira@acme.com",
            "api_token": _TOKEN, "default_template": "agile"}

    resp = await client.put(
        f"/api/v1/orgs/{org.id}/integrations/jira", headers=_hdrs(key),
        json={**base, "api_token": "x" * 2049},
    )
    assert resp.status_code == 422, resp.text

    resp = await client.put(
        f"/api/v1/orgs/{org.id}/integrations/jira", headers=_hdrs(key),
        json={**base, "account_email": "a" * 256 + "@acme.com"},
    )
    assert resp.status_code == 422, resp.text

    resp = await client.put(
        f"/api/v1/orgs/{org.id}/integrations/jira", headers=_hdrs(key),
        json={**base, "site_url": "https://acme.atlassian.net/" + "x" * 255},
    )
    assert resp.status_code == 422, resp.text


@pytest.mark.asyncio
async def test_connection_delete_removes_and_audits(client: AsyncClient, db_session: AsyncSession, fake_jira):
    org = await _org(db_session)
    user, key = await _member(db_session, org, "admin")
    await _insert_connection(db_session, org, user)
    resp = await client.delete(f"/api/v1/orgs/{org.id}/integrations/jira", headers=_hdrs(key))
    assert resp.status_code == 204
    assert (await db_session.execute(
        select(OrgTrackerConnection).where(OrgTrackerConnection.org_id == org.id)
    )).scalar_one_or_none() is None
    events = (
        await db_session.execute(select(OrgAuditEvent).where(OrgAuditEvent.org_id == org.id))
    ).scalars().all()
    assert events[-1].event_type == "jira_connection_deleted"
    assert _TOKEN not in (events[-1].before or "")


# ── mappings + projects + health ──


@pytest.mark.asyncio
async def test_mapping_lifecycle_and_audit(client: AsyncClient, db_session: AsyncSession, fake_jira):
    org = await _org(db_session)
    user, key = await _member(db_session, org, "admin")
    await _insert_connection(db_session, org, user)
    project = await _project(db_session, user, org)

    resp = await client.put(
        f"/api/v1/orgs/{org.id}/integrations/jira/mappings/{project.id}",
        headers=_hdrs(key),
        json={"jira_project_key": "ACME", "issue_type": "Bug", "template": "bug", "done_status_name": "Done"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["jira_project_key"] == "ACME"
    assert resp.json()["issue_type"] == "Bug"

    listed = (await client.get(
        f"/api/v1/orgs/{org.id}/integrations/jira/mappings", headers=_hdrs(key)
    )).json()
    assert [m["project_id"] for m in listed] == [project.id]

    resp = await client.delete(
        f"/api/v1/orgs/{org.id}/integrations/jira/mappings/{project.id}", headers=_hdrs(key)
    )
    assert resp.status_code == 204
    types = {e.event_type for e in (
        await db_session.execute(select(OrgAuditEvent).where(OrgAuditEvent.org_id == org.id))
    ).scalars().all()}
    assert {"jira_mapping_updated", "jira_mapping_deleted"} <= types


@pytest.mark.asyncio
async def test_projects_list_and_health(client: AsyncClient, db_session: AsyncSession, fake_jira):
    org = await _org(db_session)
    user, key = await _member(db_session, org, "admin")
    await _insert_connection(db_session, org, user)
    mapped = await _project(db_session, user, org)
    unmapped = await _project(db_session, user, org)
    await _insert_mapping(db_session, org, mapped, user)

    projects = (await client.get(
        f"/api/v1/orgs/{org.id}/integrations/jira/projects", headers=_hdrs(key)
    )).json()
    # Issue types are no longer fetched eagerly — the list is cheap and the
    # per-project issue types are fetched lazily via the issue-types endpoint.
    assert projects == [{"key": "ACME", "name": "Acme", "issue_types": []}]

    issue_types = (await client.get(
        f"/api/v1/orgs/{org.id}/integrations/jira/projects/ACME/issue-types", headers=_hdrs(key)
    )).json()
    assert issue_types == [{"name": "Story", "id": "10000"}]

    health = (await client.get(
        f"/api/v1/orgs/{org.id}/integrations/jira/health", headers=_hdrs(key)
    )).json()
    assert health["unmapped_project_count"] == 1
    assert health["last_push_failures"] == []
    assert health["stale_links"] == []


@pytest.mark.asyncio
async def test_projects_search_passes_query_and_reports_has_more(
    client: AsyncClient, db_session: AsyncSession, fake_jira
):
    org = await _org(db_session)
    user, key = await _member(db_session, org, "admin")
    await _insert_connection(db_session, org, user)
    url = f"/api/v1/orgs/{org.id}/integrations/jira/projects"

    # Seed the fake site with many projects so paging matters.
    resp = await client.get(url, params={"limit": 5}, headers=_hdrs(key))
    state = fake_jira[-1].state
    state["project_keys"] = ["ACME", "AIOPS"] + [f"B{i:03d}" for i in range(120)]

    resp = await client.get(url, params={"q": "  aio  "}, headers=_hdrs(key))
    assert resp.status_code == 200
    body = resp.json()
    assert body == {
        "projects": [{"key": "AIOPS", "name": "Aiops", "issue_types": []}],
        "has_more": False,
        "query": "aio",
        "limit": 50,
    }
    # The trimmed query and the default page size reach the Jira client.
    assert state["searches"][-1] == ("aio", 50)

    resp = await client.get(url, params={"limit": 100}, headers=_hdrs(key))
    body = resp.json()
    assert len(body["projects"]) == 100
    assert body["has_more"] is True
    assert body["query"] is None
    assert state["searches"][-1] == (None, 100)

    # A blank query is the unfiltered first page, not a Jira search for "".
    resp = await client.get(url, params={"q": "   "}, headers=_hdrs(key))
    assert resp.json()["query"] is None
    assert state["searches"][-1] == (None, 50)


@pytest.mark.asyncio
async def test_projects_unpaged_list_keeps_bare_shape_and_flags_truncation(
    client: AsyncClient, db_session: AsyncSession, fake_jira
):
    org = await _org(db_session)
    user, key = await _member(db_session, org, "admin")
    await _insert_connection(db_session, org, user)
    url = f"/api/v1/orgs/{org.id}/integrations/jira/projects"

    resp = await client.get(url, headers=_hdrs(key))
    assert resp.json() == [{"key": "ACME", "name": "Acme", "issue_types": []}]
    assert resp.headers["X-Jira-Projects-Truncated"] == "false"
    fake_jira[-1].state["project_keys"] = [f"P{i:04d}" for i in range(600)]

    resp = await client.get(url, headers=_hdrs(key))
    assert isinstance(resp.json(), list)
    assert len(resp.json()) == 500
    assert resp.headers["X-Jira-Projects-Truncated"] == "true"
    assert fake_jira[-1].state["searches"][-1] == (None, 500)


@pytest.mark.asyncio
async def test_projects_search_validates_params(
    client: AsyncClient, db_session: AsyncSession, fake_jira
):
    org = await _org(db_session)
    user, key = await _member(db_session, org, "admin")
    await _insert_connection(db_session, org, user)
    url = f"/api/v1/orgs/{org.id}/integrations/jira/projects"

    for params in ({"q": "x" * 101}, {"limit": 0}, {"limit": 101}, {"limit": "many"}):
        resp = await client.get(url, params=params, headers=_hdrs(key))
        assert resp.status_code == 422, params
    assert fake_jira == [] or "searches" not in fake_jira[-1].state


@pytest.mark.asyncio
async def test_projects_search_is_admin_only_and_sanitizes_upstream_errors(
    client: AsyncClient, db_session: AsyncSession, fake_jira, monkeypatch
):
    org = await _org(db_session)
    admin, admin_key = await _member(db_session, org, "admin")
    _, member_key = await _member(db_session, org, "member")
    await _insert_connection(db_session, org, admin)
    url = f"/api/v1/orgs/{org.id}/integrations/jira/projects"

    resp = await client.get(url, params={"q": "acme"}, headers=_hdrs(member_key))
    assert resp.status_code == 403

    other = await _org(db_session)
    resp = await client.get(
        f"/api/v1/orgs/{other.id}/integrations/jira/projects",
        params={"q": "acme"},
        headers=_hdrs(admin_key),
    )
    assert resp.status_code == 404

    async def boom(self, query, limit):
        raise TrackerError("Jira API 500: upstream failure")

    monkeypatch.setattr(
        "sessionfs.server.routes.jira.JiraClient.search_projects", boom
    )
    resp = await client.get(url, params={"q": "acme"}, headers=_hdrs(admin_key))
    assert resp.status_code == 502
    assert _TOKEN not in resp.text


@pytest.mark.asyncio
async def test_project_listing_carries_org_id_for_the_mapping_picker(
    client: AsyncClient, db_session: AsyncSession, fake_jira
):
    """GH #103: the dashboard's SessionFS-project picker filters
    ``GET /api/v1/projects/`` by ``org_id``. The response model used to drop
    the column, so every project was filtered out and the picker was empty."""
    org = await _org(db_session)
    user, key = await _member(db_session, org, "admin")
    org_project = await _project(db_session, user, org)
    personal = await _project(db_session, user, None)

    listed = (await client.get("/api/v1/projects/", headers=_hdrs(key))).json()
    by_id = {p["id"]: p for p in listed}
    assert by_id[org_project.id]["org_id"] == org.id
    assert by_id[personal.id]["org_id"] is None


# ── ticket promotion ──


async def _ready(
    db: AsyncSession, *, tier: str = "enterprise"
) -> tuple[Organization, User, str, Project, Ticket]:
    org = await _org(db, tier=tier)
    user, key = await _member(db, org, "admin")
    await _insert_connection(db, org, user)
    project = await _project(db, user, org)
    await _insert_mapping(db, org, project, user)
    ticket = await _ticket(db, project, user)
    return org, user, key, project, ticket


@pytest.mark.asyncio
async def test_unmapped_project_409(client: AsyncClient, db_session: AsyncSession, fake_jira):
    org = await _org(db_session)
    user, key = await _member(db_session, org, "admin")
    await _insert_connection(db_session, org, user)
    project = await _project(db_session, user, org)
    ticket = await _ticket(db_session, project, user)

    resp = await client.post(
        f"/api/v1/projects/{project.id}/tickets/{ticket.id}/jira/preview", headers=_hdrs(key)
    )
    assert resp.status_code == 409
    assert resp.json()["error"]["details"]["error"] == "jira_project_unmapped"


@pytest.mark.asyncio
async def test_not_connected_409(client: AsyncClient, db_session: AsyncSession, fake_jira):
    org = await _org(db_session)
    user, key = await _member(db_session, org, "admin")
    project = await _project(db_session, user, org)
    ticket = await _ticket(db_session, project, user)

    resp = await client.post(
        f"/api/v1/projects/{project.id}/tickets/{ticket.id}/jira/preview", headers=_hdrs(key)
    )
    assert resp.status_code == 409
    assert resp.json()["error"]["details"]["error"] == "jira_not_connected"


@pytest.mark.asyncio
async def test_ticket_route_gates_on_project_org_tier(
    client: AsyncClient, db_session: AsyncSession, fake_jira
):
    org, user, key, project, ticket = await _ready(db_session, tier="free")
    resp = await client.post(
        f"/api/v1/projects/{project.id}/tickets/{ticket.id}/jira/preview", headers=_hdrs(key)
    )
    assert resp.status_code == 403
    assert resp.json()["error"]["details"]["error"] == "upgrade_required"
    assert resp.json()["error"]["details"]["plan_scope"] == "project"


@pytest.mark.asyncio
async def test_preview_returns_deterministic_draft(client: AsyncClient, db_session: AsyncSession, fake_jira):
    org, user, key, project, ticket = await _ready(db_session)
    resp = await client.post(
        f"/api/v1/projects/{project.id}/tickets/{ticket.id}/jira/preview", headers=_hdrs(key)
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["summary"] == "Ship the auth migration"
    assert body["jira_project_key"] == "ACME"
    assert body["issue_type"] == "Story"
    assert body["llm_used"] is False
    assert body["key_source"] == "none"
    assert "existing" not in body


@pytest.mark.asyncio
async def test_publish_creates_then_updates_with_1to1_link_and_audit(
    client: AsyncClient, db_session: AsyncSession, fake_jira
):
    org, user, key, project, ticket = await _ready(db_session)
    preview = (await client.post(
        f"/api/v1/projects/{project.id}/tickets/{ticket.id}/jira/preview", headers=_hdrs(key)
    )).json()
    publish_body = {"summary": preview["summary"], "sections": preview["sections"], "confirm": True}

    resp = await client.post(
        f"/api/v1/projects/{project.id}/tickets/{ticket.id}/jira/publish", headers=_hdrs(key),
        json=publish_body,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["issue_key"] == "ACME-1"
    assert resp.json()["issue_url"] == "https://acme.atlassian.net/browse/ACME-1"

    links = (
        await db_session.execute(select(TicketTrackerLink).where(TicketTrackerLink.ticket_id == ticket.id))
    ).scalars().all()
    assert len(links) == 1  # 1:1 — no duplicate row

    # Second publish updates, not creates.
    resp2 = await client.post(
        f"/api/v1/projects/{project.id}/tickets/{ticket.id}/jira/publish", headers=_hdrs(key),
        json=publish_body,
    )
    assert resp2.status_code == 200
    assert resp2.json()["issue_key"] == "ACME-1"
    links = (
        await db_session.execute(select(TicketTrackerLink).where(TicketTrackerLink.ticket_id == ticket.id))
    ).scalars().all()
    assert len(links) == 1

    types = [e.event_type for e in (
        await db_session.execute(select(OrgAuditEvent).where(OrgAuditEvent.org_id == org.id))
    ).scalars().all()]
    assert types == ["jira_issue_created", "jira_issue_updated"]

    comments = (
        await db_session.execute(select(TicketComment).where(TicketComment.ticket_id == ticket.id))
    ).scalars().all()
    assert [c.content for c in comments] == [
        "Promoted to Jira: https://acme.atlassian.net/browse/ACME-1",
        "Updated Jira issue: https://acme.atlassian.net/browse/ACME-1",
    ]
    # Token never leaks into audit.
    for e in (
        await db_session.execute(select(OrgAuditEvent).where(OrgAuditEvent.org_id == org.id))
    ).scalars().all():
        assert _TOKEN not in (e.after or "")
        assert _TOKEN not in (e.before or "")


@pytest.mark.asyncio
async def test_publish_requires_confirm_and_valid_body(client: AsyncClient, db_session: AsyncSession, fake_jira):
    org, user, key, project, ticket = await _ready(db_session)
    resp = await client.post(
        f"/api/v1/projects/{project.id}/tickets/{ticket.id}/jira/publish", headers=_hdrs(key),
        json={"summary": "s", "sections": [{"key": "x", "paragraphs": []}], "confirm": False},
    )
    assert resp.status_code == 400
    resp = await client.post(
        f"/api/v1/projects/{project.id}/tickets/{ticket.id}/jira/publish", headers=_hdrs(key),
        json={"summary": "", "sections": [{"key": "x", "paragraphs": []}], "confirm": True},
    )
    assert resp.status_code == 422
    resp = await client.post(
        f"/api/v1/projects/{project.id}/tickets/{ticket.id}/jira/publish", headers=_hdrs(key),
        json={"summary": "s", "sections": [], "confirm": True},
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_status_and_unlink_flow(client: AsyncClient, db_session: AsyncSession, fake_jira):
    org, user, key, project, ticket = await _ready(db_session)
    member, member_key = await _member(db_session, org, "member")

    # No link yet → 404.
    resp = await client.get(
        f"/api/v1/projects/{project.id}/tickets/{ticket.id}/jira", headers=_hdrs(key)
    )
    assert resp.status_code == 404

    preview = (await client.post(
        f"/api/v1/projects/{project.id}/tickets/{ticket.id}/jira/preview", headers=_hdrs(key)
    )).json()
    await client.post(
        f"/api/v1/projects/{project.id}/tickets/{ticket.id}/jira/publish", headers=_hdrs(key),
        json={"summary": preview["summary"], "sections": preview["sections"], "confirm": True},
    )

    resp = await client.get(
        f"/api/v1/projects/{project.id}/tickets/{ticket.id}/jira", headers=_hdrs(key)
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["issue_key"] == "ACME-1"
    assert body["last_known_status"] == "Done"

    # A plain member cannot unlink (project-admin standing required).
    resp = await client.delete(
        f"/api/v1/projects/{project.id}/tickets/{ticket.id}/jira", headers=_hdrs(member_key)
    )
    assert resp.status_code == 403
    assert resp.json()["error"]["details"]["error"] == "insufficient_role"

    resp = await client.delete(
        f"/api/v1/projects/{project.id}/tickets/{ticket.id}/jira", headers=_hdrs(key)
    )
    assert resp.status_code == 204
    assert (await db_session.execute(
        select(TicketTrackerLink).where(TicketTrackerLink.ticket_id == ticket.id)
    )).scalar_one_or_none() is None

    types = [e.event_type for e in (
        await db_session.execute(select(OrgAuditEvent).where(OrgAuditEvent.org_id == org.id))
    ).scalars().all()]
    assert "jira_unlinked" in types


@pytest.mark.asyncio
async def test_status_truncates_long_jira_status_to_column_width(
    client: AsyncClient, db_session: AsyncSession, fake_jira
):
    org, user, key, project, ticket = await _ready(db_session)

    preview = (await client.post(
        f"/api/v1/projects/{project.id}/tickets/{ticket.id}/jira/preview", headers=_hdrs(key)
    )).json()
    await client.post(
        f"/api/v1/projects/{project.id}/tickets/{ticket.id}/jira/publish", headers=_hdrs(key),
        json={"summary": preview["summary"], "sections": preview["sections"], "confirm": True},
    )

    fake_jira[0].state["status"] = "x" * 80
    resp = await client.get(
        f"/api/v1/projects/{project.id}/tickets/{ticket.id}/jira", headers=_hdrs(key)
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["last_known_status"] == "x" * 50


# ── typed publish models + mapping validation (GH #98 review round 1) ──


@pytest.mark.asyncio
async def test_publish_rejects_malformed_bodies_with_422(
    client: AsyncClient, db_session: AsyncSession, fake_jira
):
    org, user, key, project, ticket = await _ready(db_session)
    url = f"/api/v1/projects/{project.id}/tickets/{ticket.id}/jira/publish"

    async def post(payload):
        return await client.post(url, headers=_hdrs(key), json=payload)

    section = {"key": "k", "heading": "", "paragraphs": [{"text": "hi", "citations": []}]}
    cases = [
        # summary empty / too long
        {"summary": "", "sections": [section], "confirm": True},
        {"summary": "x" * 256, "sections": [section], "confirm": True},
        # no sections / too many sections
        {"summary": "s", "sections": [], "confirm": True},
        {"summary": "s", "sections": [section] * 21, "confirm": True},
        # section key too long
        {"summary": "s", "sections": [{**section, "key": "k" * 65}], "confirm": True},
        # heading too long
        {"summary": "s", "sections": [{**section, "heading": "h" * 201}], "confirm": True},
        # paragraph text too long
        {"summary": "s", "sections": [{"key": "k", "paragraphs": [{"text": "t" * 10001, "citations": []}]}], "confirm": True},
        # too many paragraphs
        {"summary": "s", "sections": [{"key": "k", "paragraphs": [{"text": "p", "citations": []}] * 51}], "confirm": True},
        # citation too long
        {"summary": "s", "sections": [{"key": "k", "paragraphs": [{"text": "p", "citations": ["c" * 65]}]}], "confirm": True},
        # too many citations
        {"summary": "s", "sections": [{"key": "k", "paragraphs": [{"text": "p", "citations": ["a"] * 21}]}], "confirm": True},
    ]
    for payload in cases:
        resp = await post(payload)
        assert resp.status_code == 422, resp.text

    # Malformed bodies never reach the Jira client.
    assert all(f.calls["create_issue"] == 0 for f in fake_jira)


@pytest.mark.asyncio
async def test_mapping_rejects_invalid_project_key_and_oversized_fields(
    client: AsyncClient, db_session: AsyncSession, fake_jira
):
    org = await _org(db_session)
    user, key = await _member(db_session, org, "admin")
    await _insert_connection(db_session, org, user)
    project = await _project(db_session, user, org)
    url = f"/api/v1/orgs/{org.id}/integrations/jira/mappings/{project.id}"
    base = {"jira_project_key": "ACME", "issue_type": "Story", "template": "agile", "done_status_name": "Done"}

    # Project keys must match ^[A-Z][A-Z0-9_]{1,9}$ and fit the 10-char column.
    for bad in ("acme", "A", "ACME-1", "ACME!", "ACME-Project", "ACME_X_Y_Z_1_2_3_4_5"):
        resp = await client.put(url, headers=_hdrs(key), json={**base, "jira_project_key": bad})
        assert resp.status_code == 422, (bad, resp.text)

    for field, value in (("issue_type", "x" * 51), ("done_status_name", "x" * 51)):
        resp = await client.put(url, headers=_hdrs(key), json={**base, field: value})
        assert resp.status_code == 422, (field, resp.text)

    resp = await client.put(url, headers=_hdrs(key), json=base)
    assert resp.status_code == 200, resp.text
    assert resp.json()["jira_project_key"] == "ACME"


# ── stale links after connection re-point / org change (GH #98 review round 1) ──


@pytest.mark.asyncio
async def test_stale_link_after_connection_repoint_blocks_outbound(
    client: AsyncClient, db_session: AsyncSession, fake_jira
):
    org, user, key, project, ticket = await _ready(db_session)
    preview = (await client.post(
        f"/api/v1/projects/{project.id}/tickets/{ticket.id}/jira/preview", headers=_hdrs(key)
    )).json()
    await client.post(
        f"/api/v1/projects/{project.id}/tickets/{ticket.id}/jira/publish", headers=_hdrs(key),
        json={"summary": preview["summary"], "sections": preview["sections"], "confirm": True},
    )
    assert fake_jira[0].calls["create_issue"] == 1

    # Re-point the org's Jira site so the stored link no longer matches.
    conn = (await db_session.execute(
        select(OrgTrackerConnection).where(OrgTrackerConnection.org_id == org.id)
    )).scalar_one()
    conn.site_url = "https://other.atlassian.net"
    await db_session.commit()

    # Status, preview and publish must refuse without an outbound call.
    resp = await client.get(
        f"/api/v1/projects/{project.id}/tickets/{ticket.id}/jira", headers=_hdrs(key)
    )
    assert resp.status_code == 409
    assert resp.json()["error"]["details"]["error"] == "jira_link_stale"

    resp = await client.post(
        f"/api/v1/projects/{project.id}/tickets/{ticket.id}/jira/preview", headers=_hdrs(key)
    )
    assert resp.status_code == 409
    assert resp.json()["error"]["details"]["error"] == "jira_link_stale"

    resp = await client.post(
        f"/api/v1/projects/{project.id}/tickets/{ticket.id}/jira/publish", headers=_hdrs(key),
        json={"summary": "changed", "sections": [{"key": "x", "paragraphs": []}], "confirm": True},
    )
    assert resp.status_code == 409
    assert resp.json()["error"]["details"]["error"] == "jira_link_stale"

    # No status refresh, no update, no second create ever happened.
    assert fake_jira[0].calls["get_issue"] == 0
    assert fake_jira[0].calls["update_issue"] == 0
    assert fake_jira[0].calls["create_issue"] == 1


@pytest.mark.asyncio
async def test_stale_link_after_org_change_blocks_outbound(
    client: AsyncClient, db_session: AsyncSession, fake_jira
):
    org, user, key, project, ticket = await _ready(db_session)
    preview = (await client.post(
        f"/api/v1/projects/{project.id}/tickets/{ticket.id}/jira/preview", headers=_hdrs(key)
    )).json()
    await client.post(
        f"/api/v1/projects/{project.id}/tickets/{ticket.id}/jira/publish", headers=_hdrs(key),
        json={"summary": preview["summary"], "sections": preview["sections"], "confirm": True},
    )

    # The project moved to another org (transfer/merge leaves the link behind).
    link = (await db_session.execute(
        select(TicketTrackerLink).where(TicketTrackerLink.ticket_id == ticket.id)
    )).scalar_one()
    link.org_id = "org_moved_away"
    await db_session.commit()

    resp = await client.get(
        f"/api/v1/projects/{project.id}/tickets/{ticket.id}/jira", headers=_hdrs(key)
    )
    assert resp.status_code == 409
    assert resp.json()["error"]["details"]["error"] == "jira_link_stale"
    # The message points a person at the exact unlink escape hatch.
    assert (
        "a project admin can unlink it (sfs ticket jira unlink) to promote again"
        in resp.json()["error"]["message"]
    )
    assert fake_jira[0].calls["get_issue"] == 0


@pytest.mark.asyncio
async def test_project_transfer_drops_mapping_and_leaves_link(
    client: AsyncClient, db_session: AsyncSession, fake_jira
):
    from sessionfs.server.services.tracker_scope import drop_project_integration_mappings

    org, user, key, project, ticket = await _ready(db_session)
    preview = (await client.post(
        f"/api/v1/projects/{project.id}/tickets/{ticket.id}/jira/preview", headers=_hdrs(key)
    )).json()
    await client.post(
        f"/api/v1/projects/{project.id}/tickets/{ticket.id}/jira/publish", headers=_hdrs(key),
        json={"summary": preview["summary"], "sections": preview["sections"], "confirm": True},
    )
    link_before = (await db_session.execute(
        select(TicketTrackerLink).where(TicketTrackerLink.ticket_id == ticket.id)
    )).scalar_one()
    assert link_before.status == "linked"

    # A transfer drops the tracker mapping but leaves the link row in place.
    await drop_project_integration_mappings(db_session, project.id)
    await db_session.commit()

    assert (await db_session.execute(
        select(TrackerProjectMapping).where(TrackerProjectMapping.project_id == project.id)
    )).scalar_one_or_none() is None

    link_after = (await db_session.execute(
        select(TicketTrackerLink).where(TicketTrackerLink.ticket_id == ticket.id)
    )).scalar_one()
    assert link_after.ticket_id == ticket.id  # not deleted, just stale
    assert link_after.status == "linked"

    # Without a mapping the project can no longer publish, and no update goes out.
    resp = await client.post(
        f"/api/v1/projects/{project.id}/tickets/{ticket.id}/jira/preview", headers=_hdrs(key)
    )
    assert resp.status_code == 409
    assert resp.json()["error"]["details"]["error"] == "jira_project_unmapped"
    assert fake_jira[0].calls["update_issue"] == 0


@pytest.mark.asyncio
async def test_transfer_lets_new_org_remap_and_stale_link_stays_blocked(
    client: AsyncClient, db_session: AsyncSession, fake_jira
):
    from sessionfs.server.services.tracker_scope import drop_project_integration_mappings

    # Org A owns the project and has a published Jira link.
    org_a, admin_a, key_a, project, ticket = await _ready(db_session)
    preview = (await client.post(
        f"/api/v1/projects/{project.id}/tickets/{ticket.id}/jira/preview", headers=_hdrs(key_a)
    )).json()
    await client.post(
        f"/api/v1/projects/{project.id}/tickets/{ticket.id}/jira/publish", headers=_hdrs(key_a),
        json={"summary": preview["summary"], "sections": preview["sections"], "confirm": True},
    )

    # Org B is the transfer target, with its own admin and Jira connection.
    org_b = await _org(db_session)
    admin_b, key_b = await _member(db_session, org_b, "admin")
    await _insert_connection(db_session, org_b, admin_b)

    # The accept path moves the project to org B and drops the source-org
    # mapping, leaving the old link in place (now stale).
    project.org_id = org_b.id
    await drop_project_integration_mappings(db_session, project.id)
    await db_session.commit()

    # The new org can map the transferred project: the dropped mapping must not
    # leave a unique-constraint collision on project_id.
    resp = await client.put(
        f"/api/v1/orgs/{org_b.id}/integrations/jira/mappings/{project.id}",
        headers=_hdrs(key_b),
        json={"jira_project_key": "OTHER", "issue_type": "Story", "template": "agile"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["project_id"] == project.id
    assert resp.json()["jira_project_key"] == "OTHER"

    # The old link points at org A while the project now lives in org B, so a
    # status read is blocked with no outbound Jira call.
    resp = await client.get(
        f"/api/v1/projects/{project.id}/tickets/{ticket.id}/jira", headers=_hdrs(key_b)
    )
    assert resp.status_code == 409
    assert resp.json()["error"]["details"]["error"] == "jira_link_stale"
    assert sum(f.calls["get_issue"] for f in fake_jira) == 0


# ── concurrent publish claims one slot (GH #98 review round 1) ──


@pytest.mark.asyncio
async def test_concurrent_publish_creates_exactly_one_issue(tmp_path, monkeypatch):
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    import sessionfs.server.db.engine as engine_mod
    from sessionfs.server.app import create_app
    from sessionfs.server.config import ServerConfig
    from sessionfs.server.db.engine import get_db, install_sqlite_unicode_lower
    from sessionfs.server.db.models import Base
    from sessionfs.server.integrations.trackers.base import TrackerIssue
    from httpx import ASGITransport

    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'race.db'}", connect_args={"timeout": 30}
    )
    install_sqlite_unicode_lower(engine)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(engine_mod, "_session_factory", factory)

    # Seed a file-backed DB (separate connections model separate workers).
    async with factory() as db:
        org = await _org(db)
        user, key = await _member(db, org, "admin")
        await _insert_connection(db, org, user)
        project = await _project(db, user, org)
        await _insert_mapping(db, org, project, user)
        ticket = await _ticket(db, project, user)
        project_id, ticket_id = project.id, ticket.id

    created = {"count": 0}

    class SlowFake:
        def __init__(self, site_url, account_email, api_token, *, transport=None):
            self.site_url = validate_site_url(site_url)

        async def aclose(self):
            pass

        async def create_issue(self, *a, **k):
            created["count"] += 1
            await asyncio.sleep(0.3)  # keep the claim in flight
            return TrackerIssue(key="ACME-1", url=f"{self.site_url}/browse/ACME-1", status="", id="10001")

        async def update_issue(self, issue_key, *a, **k):
            return TrackerIssue(key=issue_key, url=f"{self.site_url}/browse/{issue_key}", status="")

    monkeypatch.setattr("sessionfs.server.routes.jira.JiraClient", SlowFake)

    config = ServerConfig(database_url="sqlite+aiosqlite://")
    app = create_app(config)

    async def override_get_db():
        async with factory() as s:
            yield s

    app.dependency_overrides[get_db] = override_get_db
    app.state.config = config

    body = {
        "summary": "Ship the auth migration",
        "sections": [{"key": "summary", "heading": "", "mode": "verbatim",
                      "paragraphs": [{"text": "line one\nline two", "citations": []}]}],
        "confirm": True,
    }

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        r1, r2 = await asyncio.gather(
            c.post(f"/api/v1/projects/{project_id}/tickets/{ticket_id}/jira/publish",
                   headers=_hdrs(key), json=body),
            c.post(f"/api/v1/projects/{project_id}/tickets/{ticket_id}/jira/publish",
                   headers=_hdrs(key), json=body),
        )
    await engine.dispose()

    # One publish wins the claim, the other is told one is in progress.
    assert sorted([r1.status_code, r2.status_code]) == [200, 409]
    assert created["count"] == 1

    async with factory() as db:
        links = (await db.execute(
            select(TicketTrackerLink).where(TicketTrackerLink.ticket_id == ticket_id)
        )).scalars().all()
        assert len(links) == 1
        assert links[0].status == "linked"
        assert links[0].issue_key == "ACME-1"


async def _file_backed_app(tmp_path, monkeypatch, *, seed_pending: bool = False):
    """Spin up the app on a file-backed DB with ready Jira data.

    ``seed_pending`` also inserts a stale (>5 min) pending claim so a test can
    exercise the reclaim path. Separate sessions model separate workers.
    """
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    import sessionfs.server.db.engine as engine_mod
    from sessionfs.server.app import create_app
    from sessionfs.server.config import ServerConfig
    from sessionfs.server.db.engine import get_db, install_sqlite_unicode_lower
    from sessionfs.server.db.models import Base

    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'race.db'}", connect_args={"timeout": 30}
    )
    install_sqlite_unicode_lower(engine)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(engine_mod, "_session_factory", factory)

    async with factory() as db:
        org = await _org(db)
        user, key = await _member(db, org, "admin")
        await _insert_connection(db, org, user)
        project = await _project(db, user, org)
        await _insert_mapping(db, org, project, user)
        ticket = await _ticket(db, project, user)
        if seed_pending:
            db.add(TicketTrackerLink(
                ticket_id=ticket.id,
                org_id=org.id,
                provider="jira",
                status="pending",
                claim_token="stale-token",
                site_url="https://acme.atlassian.net",
                last_pushed_at=datetime.now(timezone.utc) - timedelta(minutes=10),
            ))
            await db.commit()
        project_id, ticket_id = project.id, ticket.id

    config = ServerConfig(database_url="sqlite+aiosqlite://")
    app = create_app(config)

    async def override_get_db():
        async with factory() as s:
            yield s

    app.dependency_overrides[get_db] = override_get_db
    app.state.config = config
    return engine, factory, app, project_id, ticket_id, key


_PUBLISH_BODY = {
    "summary": "Ship the auth migration",
    "sections": [{"key": "summary", "paragraphs": [{"text": "hi", "citations": []}]}],
    "confirm": True,
}


@pytest.mark.asyncio
async def test_two_concurrent_reclaims_of_stale_claim_create_one_issue(tmp_path, monkeypatch):
    from httpx import ASGITransport

    from sessionfs.server.integrations.trackers.base import TrackerIssue
    from sessionfs.server.integrations.trackers.jira import validate_site_url

    engine, factory, app, project_id, ticket_id, key = await _file_backed_app(
        tmp_path, monkeypatch, seed_pending=True
    )
    created = {"count": 0}

    class SlowFake:
        def __init__(self, site_url, account_email, api_token, *, transport=None):
            self.site_url = validate_site_url(site_url)

        async def aclose(self):
            pass

        async def create_issue(self, *a, **k):
            created["count"] += 1
            await asyncio.sleep(0.3)  # keep the reclaimed claim in flight
            return TrackerIssue(key="ACME-1", url=f"{self.site_url}/browse/ACME-1", status="", id="10001")

        async def update_issue(self, issue_key, *a, **k):
            return TrackerIssue(key=issue_key, url=f"{self.site_url}/browse/{issue_key}", status="")

    monkeypatch.setattr("sessionfs.server.routes.jira.JiraClient", SlowFake)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        r1, r2 = await asyncio.gather(
            c.post(f"/api/v1/projects/{project_id}/tickets/{ticket_id}/jira/publish",
                   headers=_hdrs(key), json=_PUBLISH_BODY),
            c.post(f"/api/v1/projects/{project_id}/tickets/{ticket_id}/jira/publish",
                   headers=_hdrs(key), json=_PUBLISH_BODY),
        )
    await engine.dispose()

    # One reclaim wins the stale claim, the other backs off — exactly one create.
    assert sorted([r1.status_code, r2.status_code]) == [200, 409]
    assert created["count"] == 1

    async with factory() as db:
        links = (await db.execute(
            select(TicketTrackerLink).where(TicketTrackerLink.ticket_id == ticket_id)
        )).scalars().all()
        assert len(links) == 1
        assert links[0].status == "linked"
        assert links[0].issue_key == "ACME-1"
        assert links[0].claim_token is None


@pytest.mark.asyncio
async def test_reclaimed_slow_create_cannot_overwrite_new_claim(tmp_path, monkeypatch):
    from httpx import ASGITransport
    from sqlalchemy import update

    from sessionfs.server.integrations.trackers.base import TrackerIssue
    from sessionfs.server.integrations.trackers.jira import validate_site_url

    engine, factory, app, project_id, ticket_id, key = await _file_backed_app(
        tmp_path, monkeypatch, seed_pending=True
    )
    entered = asyncio.Event()
    release = asyncio.Event()

    class SlowFake:
        def __init__(self, site_url, account_email, api_token, *, transport=None):
            self.site_url = validate_site_url(site_url)

        async def aclose(self):
            pass

        async def create_issue(self, *a, **k):
            entered.set()
            await release.wait()  # block A mid-create
            return TrackerIssue(key="ACME-1", url=f"{self.site_url}/browse/ACME-1", status="", id="10001")

        async def update_issue(self, issue_key, *a, **k):
            return TrackerIssue(key=issue_key, url=f"{self.site_url}/browse/{issue_key}", status="")

    monkeypatch.setattr("sessionfs.server.routes.jira.JiraClient", SlowFake)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        task = asyncio.create_task(
            c.post(f"/api/v1/projects/{project_id}/tickets/{ticket_id}/jira/publish",
                   headers=_hdrs(key), json=_PUBLISH_BODY)
        )
        await entered.wait()  # A has reclaimed the claim and is blocked creating

        # B reclaims the stale claim out from under A.
        async with factory() as db:
            res = await db.execute(
                update(TicketTrackerLink)
                .where(
                    TicketTrackerLink.ticket_id == ticket_id,
                    TicketTrackerLink.status == "pending",
                )
                .values(
                    claim_token="tokenB",
                    last_pushed_at=datetime.now(timezone.utc),
                )
                .execution_options(synchronize_session=False)
            )
            assert res.rowcount == 1
            await db.commit()

        release.set()
        resp = await task

    await engine.dispose()

    assert resp.status_code == 409
    assert resp.json()["error"]["details"]["error"] == "publish_superseded"
    assert resp.json()["error"]["details"]["issue_key"] == "ACME-1"

    async with factory() as db:
        link = (await db.execute(
            select(TicketTrackerLink).where(TicketTrackerLink.ticket_id == ticket_id)
        )).scalar_one()
        # A's fenced mark-linked did not overwrite B's claim.
        assert link.status == "pending"
        assert link.claim_token == "tokenB"
        assert link.issue_key is None


@pytest.mark.asyncio
async def test_reclaimed_slow_failed_create_cannot_delete_new_claim(tmp_path, monkeypatch):
    from httpx import ASGITransport
    from sqlalchemy import update

    from sessionfs.server.integrations.trackers.base import TrackerError
    from sessionfs.server.integrations.trackers.jira import validate_site_url

    engine, factory, app, project_id, ticket_id, key = await _file_backed_app(
        tmp_path, monkeypatch, seed_pending=True
    )
    entered = asyncio.Event()
    release = asyncio.Event()

    class FailingFake:
        def __init__(self, site_url, account_email, api_token, *, transport=None):
            self.site_url = validate_site_url(site_url)

        async def aclose(self):
            pass

        async def create_issue(self, *a, **k):
            entered.set()
            await release.wait()
            raise TrackerError("Jira API 500: boom")

        async def update_issue(self, issue_key, *a, **k):
            raise AssertionError("update should not be reached on the create path")

    monkeypatch.setattr("sessionfs.server.routes.jira.JiraClient", FailingFake)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        task = asyncio.create_task(
            c.post(f"/api/v1/projects/{project_id}/tickets/{ticket_id}/jira/publish",
                   headers=_hdrs(key), json=_PUBLISH_BODY)
        )
        await entered.wait()

        async with factory() as db:
            res = await db.execute(
                update(TicketTrackerLink)
                .where(
                    TicketTrackerLink.ticket_id == ticket_id,
                    TicketTrackerLink.status == "pending",
                )
                .values(
                    claim_token="tokenB",
                    last_pushed_at=datetime.now(timezone.utc),
                )
                .execution_options(synchronize_session=False)
            )
            assert res.rowcount == 1
            await db.commit()

        release.set()
        resp = await task

    await engine.dispose()

    assert resp.status_code == 409
    assert resp.json()["error"]["details"]["error"] == "publish_superseded"

    async with factory() as db:
        link = (await db.execute(
            select(TicketTrackerLink).where(TicketTrackerLink.ticket_id == ticket_id)
        )).scalar_one()
        # A's fenced release did not delete B's claim.
        assert link.status == "pending"
        assert link.claim_token == "tokenB"


# ── re-publish diff + health failures + service-key ticket routes ──


@pytest.mark.asyncio
async def test_republish_preview_returns_existing_and_diff(
    client: AsyncClient, db_session: AsyncSession, fake_jira
):
    org, user, key, project, ticket = await _ready(db_session)
    preview = (await client.post(
        f"/api/v1/projects/{project.id}/tickets/{ticket.id}/jira/preview", headers=_hdrs(key)
    )).json()
    publish_body = {"summary": preview["summary"], "sections": preview["sections"], "confirm": True}
    await client.post(
        f"/api/v1/projects/{project.id}/tickets/{ticket.id}/jira/publish", headers=_hdrs(key),
        json=publish_body,
    )

    # Unchanged re-preview: the existing link is surfaced and the diff is empty.
    resp = await client.post(
        f"/api/v1/projects/{project.id}/tickets/{ticket.id}/jira/preview", headers=_hdrs(key)
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["existing"]["issue_key"] == "ACME-1"
    assert body["diff"] == []

    # Publish a changed summary, then preview shows the unified delta.
    await client.post(
        f"/api/v1/projects/{project.id}/tickets/{ticket.id}/jira/publish", headers=_hdrs(key),
        json={**publish_body, "summary": "Ship the auth migration (v2)"},
    )
    body3 = (await client.post(
        f"/api/v1/projects/{project.id}/tickets/{ticket.id}/jira/preview", headers=_hdrs(key)
    )).json()
    assert body3["diff"]
    joined = "\n".join(body3["diff"])
    assert "Ship the auth migration (v2)" in joined
    assert "Ship the auth migration" in joined


@pytest.mark.asyncio
async def test_health_reports_persisted_push_failure(
    client: AsyncClient, db_session: AsyncSession, fake_jira
):
    org, user, key, project, ticket = await _ready(db_session)
    preview = (await client.post(
        f"/api/v1/projects/{project.id}/tickets/{ticket.id}/jira/preview", headers=_hdrs(key)
    )).json()
    publish_body = {"summary": preview["summary"], "sections": preview["sections"], "confirm": True}
    await client.post(
        f"/api/v1/projects/{project.id}/tickets/{ticket.id}/jira/publish", headers=_hdrs(key),
        json=publish_body,
    )

    # Fail the next update; the failure is persisted on the link.
    fake_jira[0].state["fail_update"] = True
    resp = await client.post(
        f"/api/v1/projects/{project.id}/tickets/{ticket.id}/jira/publish", headers=_hdrs(key),
        json={**publish_body, "summary": "Ship the auth migration (v2)"},
    )
    assert resp.status_code == 502
    assert resp.json()["error"]["details"]["error"] == "jira_upstream_error"

    health = (await client.get(
        f"/api/v1/orgs/{org.id}/integrations/jira/health", headers=_hdrs(key)
    )).json()
    assert len(health["last_push_failures"]) == 1
    failure = health["last_push_failures"][0]
    assert failure["ticket_id"] == ticket.id
    assert failure["issue_key"] == "ACME-1"
    assert "boom" in failure["reason"]
    assert failure["failed_at"]


@pytest.mark.asyncio
async def test_service_key_cannot_unlink(
    client: AsyncClient, db_session: AsyncSession, fake_jira
):
    org, user, key, project, ticket = await _ready(db_session)
    preview = (await client.post(
        f"/api/v1/projects/{project.id}/tickets/{ticket.id}/jira/preview", headers=_hdrs(key)
    )).json()
    await client.post(
        f"/api/v1/projects/{project.id}/tickets/{ticket.id}/jira/publish", headers=_hdrs(key),
        json={"summary": preview["summary"], "sections": preview["sections"], "confirm": True},
    )

    svc_key = await _service_key(
        db_session, org, user, ["tickets:read", "tickets:write"], project_ids=[project.id]
    )
    resp = await client.delete(
        f"/api/v1/projects/{project.id}/tickets/{ticket.id}/jira", headers=_hdrs(svc_key)
    )
    assert resp.status_code == 403
    assert resp.json()["error"]["details"]["error"] == "service_key_not_allowed"

    # The link survives an unlink attempt by a service key.
    assert (await db_session.execute(
        select(TicketTrackerLink).where(TicketTrackerLink.ticket_id == ticket.id)
    )).scalar_one_or_none() is not None


@pytest.mark.asyncio
async def test_service_key_can_preview_and_publish_without_personal_key(
    client: AsyncClient, db_session: AsyncSession, fake_jira
):
    org, user, key, project, ticket = await _ready(db_session)
    svc_key = await _service_key(
        db_session, org, user, ["tickets:read", "tickets:write"], project_ids=[project.id]
    )

    resp = await client.post(
        f"/api/v1/projects/{project.id}/tickets/{ticket.id}/jira/preview", headers=_hdrs(svc_key)
    )
    assert resp.status_code == 200, resp.text
    preview = resp.json()
    # A service key never spends the minter's personal LLM key on a draft.
    assert preview["llm_used"] is False
    assert preview["key_source"] == "none"

    resp2 = await client.post(
        f"/api/v1/projects/{project.id}/tickets/{ticket.id}/jira/publish", headers=_hdrs(svc_key),
        json={"summary": preview["summary"], "sections": preview["sections"], "confirm": True},
    )
    assert resp2.status_code == 200, resp2.text
    assert resp2.json()["issue_key"] == "ACME-1"


# ── GH #98 review round 2: claim fencing, publish caps, pending reads, L* ──


@pytest.mark.asyncio
async def test_publish_accepts_preview_of_huge_ticket_within_caps(
    client: AsyncClient, db_session: AsyncSession, fake_jira
):
    """The server's own preview of an extreme ticket always fits the publish caps.

    A ~12,000-char multi-line description + 40 acceptance criteria must publish
    unchanged: the preview's sections stay within the API caps (≤20 sections,
    ≤50 paragraphs, ≤10,000 chars per paragraph), so ``publish(preview)`` is
    accepted. The description appears in two sections, so its size stays under
    half the Jira 32,000-char pre-flight cap.
    """
    org, user, key, project, _ = await _ready(db_session)
    ticket = Ticket(
        id=f"tk_{uuid.uuid4().hex[:16]}",
        project_id=project.id,
        title="Huge ticket",
        # Line boundaries let the splitter keep every character without a hard
        # mid-line cut; a single unbroken line is split mid-line instead.
        description="\n".join("x" * 80 for _ in range(150)),
        acceptance_criteria=json.dumps([f"acceptance criterion {i}" for i in range(40)]),
        created_by_user_id=user.id,
    )
    db_session.add(ticket)
    await db_session.commit()
    await db_session.refresh(ticket)

    preview = (await client.post(
        f"/api/v1/projects/{project.id}/tickets/{ticket.id}/jira/preview", headers=_hdrs(key)
    )).json()
    assert preview["ticket_id"] == ticket.id
    assert preview["project_id"] == project.id

    sections = preview["sections"]
    assert 1 <= len(sections) <= 20
    for section in sections:
        assert len(section["paragraphs"]) <= 50
        for para in section["paragraphs"]:
            assert len(para["text"]) <= 10000

    resp = await client.post(
        f"/api/v1/projects/{project.id}/tickets/{ticket.id}/jira/publish", headers=_hdrs(key),
        json={"summary": preview["summary"], "sections": preview["sections"], "confirm": True},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["issue_key"] == "ACME-1"


@pytest.mark.asyncio
async def test_pending_claim_surfaces_publish_in_progress_on_get_ticket(
    client: AsyncClient, db_session: AsyncSession, fake_jira
):
    org, user, key, project, ticket = await _ready(db_session)
    db_session.add(TicketTrackerLink(
        ticket_id=ticket.id, org_id=org.id, provider="jira",
        status="pending", claim_token="tok", site_url="https://acme.atlassian.net",
        last_pushed_at=datetime.now(timezone.utc),
    ))
    await db_session.commit()

    resp = await client.get(
        f"/api/v1/projects/{project.id}/tickets/{ticket.id}", headers=_hdrs(key)
    )
    assert resp.status_code == 200
    body = resp.json()
    # A pending claim is not a link: `jira` stays None, but the in-flight flag
    # is surfaced so a read never shows a half-written issue.
    assert body["jira"] is None
    assert body["jira_publish_in_progress"] is True


@pytest.mark.asyncio
async def test_get_jira_returns_publish_in_progress_for_pending_row(
    client: AsyncClient, db_session: AsyncSession, fake_jira
):
    org, user, key, project, ticket = await _ready(db_session)
    db_session.add(TicketTrackerLink(
        ticket_id=ticket.id, org_id=org.id, provider="jira",
        status="pending", claim_token="tok", site_url="https://acme.atlassian.net",
        last_pushed_at=datetime.now(timezone.utc),
    ))
    await db_session.commit()

    resp = await client.get(
        f"/api/v1/projects/{project.id}/tickets/{ticket.id}/jira", headers=_hdrs(key)
    )
    assert resp.status_code == 409
    assert resp.json()["error"]["details"]["error"] == "publish_in_progress"
    # A pending row never triggers an outbound Jira status read.
    assert all(f.calls["get_issue"] == 0 for f in fake_jira)


@pytest.mark.asyncio
async def test_issue_types_rejects_invalid_project_key(
    client: AsyncClient, db_session: AsyncSession, fake_jira
):
    org = await _org(db_session)
    user, key = await _member(db_session, org, "admin")
    await _insert_connection(db_session, org, user)

    for bad in ("acme", "A", "ACME-1", "ACME!", "ACME_LONGER", "1ACME"):
        resp = await client.get(
            f"/api/v1/orgs/{org.id}/integrations/jira/projects/{bad}/issue-types",
            headers=_hdrs(key),
        )
        assert resp.status_code == 422, (bad, resp.text)

    resp = await client.get(
        f"/api/v1/orgs/{org.id}/integrations/jira/projects/ACME/issue-types", headers=_hdrs(key)
    )
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_publish_rejects_unknown_section_key(
    client: AsyncClient, db_session: AsyncSession, fake_jira
):
    org, user, key, project, ticket = await _ready(db_session)
    resp = await client.post(
        f"/api/v1/projects/{project.id}/tickets/{ticket.id}/jira/publish", headers=_hdrs(key),
        json={"summary": "s", "sections": [
            {"key": "not_in_template", "paragraphs": [{"text": "hi"}]},
        ], "confirm": True},
    )
    assert resp.status_code == 422
    assert all(f.calls["create_issue"] == 0 for f in fake_jira)


@pytest.mark.asyncio
async def test_publish_resolves_mode_from_template_not_body(
    client: AsyncClient, db_session: AsyncSession, monkeypatch, fake_jira
):
    org, user, key, project, ticket = await _ready(db_session)
    captured: dict = {}

    class CapturingFake:
        def __init__(self, site_url, account_email, api_token, *, transport=None):
            self.site_url = validate_site_url(site_url)

        async def aclose(self):
            pass

        async def create_issue(self, project_key, issue_type, summary, description_adf, *, labels=None):
            captured["adf"] = description_adf
            captured["labels"] = labels
            return TrackerIssue(key="ACME-1", url=f"{self.site_url}/browse/ACME-1", status="", id="10001")

        async def update_issue(self, issue_key, *a, **k):
            return TrackerIssue(key=issue_key, url=f"{self.site_url}/browse/{issue_key}", status="")

    monkeypatch.setattr("sessionfs.server.routes.jira.JiraClient", CapturingFake)

    resp = await client.post(
        f"/api/v1/projects/{project.id}/tickets/{ticket.id}/jira/publish", headers=_hdrs(key),
        json={
            "summary": "Ship it",
            "sections": [
                # The forged mode=rewrite must be ignored: the ticket's template
                # marks acceptance_criteria verbatim, so Markdown still renders.
                {"key": "acceptance_criteria", "heading": "AC", "mode": "rewrite",
                 "paragraphs": [{"text": "- one\n- two"}]},
            ],
            "confirm": True,
        },
    )
    assert resp.status_code == 200, resp.text
    types = [b["type"] for b in captured["adf"]["content"]]
    assert "bulletList" in types


# ── GH #98 review round 3: verbatim caps, unknown-adopt, split formatting, fenced update ──


def _adf_texts(node) -> list[str]:
    """Every ADF text node, in document order."""
    texts: list[str] = []
    if isinstance(node, dict):
        if node.get("type") == "text" and isinstance(node.get("text"), str):
            texts.append(node["text"])
        for child in node.get("content", []):
            texts.extend(_adf_texts(child))
    elif isinstance(node, list):
        for item in node:
            texts.extend(_adf_texts(item))
    return texts


@pytest.mark.parametrize("count", [60, 500])
@pytest.mark.asyncio
async def test_publish_acceptance_criteria_verbatim_each_once_in_order(
    client: AsyncClient, db_session: AsyncSession, monkeypatch, count: int
):
    """M1 — 60/500 acceptance criteria publish verbatim, each exactly once, in order.

    The old code silently dropped anything past 50 criteria; the packed
    Markdown-list paragraphs must carry every criterion into the ADF sent to
    Jira, in order, with no duplicates.
    """
    org, user, key, project, _ = await _ready(db_session)
    ticket = Ticket(
        id=f"tk_{uuid.uuid4().hex[:16]}",
        project_id=project.id,
        title="AC ticket",
        description="desc",
        acceptance_criteria=json.dumps([f"criterion {i}" for i in range(count)]),
        created_by_user_id=user.id,
    )
    db_session.add(ticket)
    await db_session.commit()
    await db_session.refresh(ticket)

    captured: dict = {}

    class CapturingFake:
        def __init__(self, site_url, account_email, api_token, *, transport=None):
            self.site_url = validate_site_url(site_url)

        async def aclose(self):
            pass

        async def create_issue(self, *a, **k):
            captured["adf"] = a[3]
            captured["labels"] = k.get("labels")
            return TrackerIssue(key="ACME-1", url=f"{self.site_url}/browse/ACME-1", status="", id="10001")

        async def update_issue(self, issue_key, *a, **k):
            return TrackerIssue(key=issue_key, url=f"{self.site_url}/browse/{issue_key}", status="")

    monkeypatch.setattr("sessionfs.server.routes.jira.JiraClient", CapturingFake)

    preview = (await client.post(
        f"/api/v1/projects/{project.id}/tickets/{ticket.id}/jira/preview", headers=_hdrs(key)
    )).json()
    resp = await client.post(
        f"/api/v1/projects/{project.id}/tickets/{ticket.id}/jira/publish", headers=_hdrs(key),
        json={"summary": preview["summary"], "sections": preview["sections"], "confirm": True},
    )
    assert resp.status_code == 200, resp.text

    criteria = [f"criterion {i}" for i in range(count)]
    texts = _adf_texts(captured["adf"])
    for c in criteria:
        assert texts.count(c) == 1, f"criterion {c!r} appears {texts.count(c)} times"
    assert [t for t in texts if t in criteria] == criteria
    assert captured["labels"] == [f"sessionfs-{ticket.id}"]


@pytest.mark.asyncio
async def test_preview_impossible_acceptance_criterion_returns_422(
    client: AsyncClient, db_session: AsyncSession, fake_jira
):
    """M1 — a single criterion that can't fit a paragraph is 422, never truncated."""
    org, user, key, project, _ = await _ready(db_session)
    ticket = Ticket(
        id=f"tk_{uuid.uuid4().hex[:16]}",
        project_id=project.id,
        title="Huge AC",
        description="desc",
        acceptance_criteria=json.dumps(["x" * 10001]),
        created_by_user_id=user.id,
    )
    db_session.add(ticket)
    await db_session.commit()
    await db_session.refresh(ticket)

    resp = await client.post(
        f"/api/v1/projects/{project.id}/tickets/{ticket.id}/jira/preview", headers=_hdrs(key)
    )
    assert resp.status_code == 422
    details = resp.json()["error"]["details"]
    assert details["error"] == "content_too_large"
    assert details["section"] == "acceptance_criteria"


@pytest.mark.asyncio
async def test_timeout_keeps_unknown_row_then_retry_adopts(
    client: AsyncClient, db_session: AsyncSession, monkeypatch
):
    """L1 — a timed-out create keeps the claim as ``unknown``; the retry adopts."""
    org, user, key, project, ticket = await _ready(db_session)
    calls = {"create": 0, "search": 0, "update": 0}

    class TimeoutThenFoundFake:
        def __init__(self, site_url, account_email, api_token, *, transport=None):
            self.site_url = validate_site_url(site_url)

        async def aclose(self):
            pass

        async def create_issue(self, *a, **k):
            calls["create"] += 1
            raise asyncio.TimeoutError

        async def myself_account_id(self):
            return "a1"

        async def search_issue_by_label(self, project_key, label):
            calls["search"] += 1
            return TrackerIssue(
                key="ACME-9",
                url=f"{self.site_url}/browse/ACME-9",
                status="",
                id="10009",
                project_key="ACME",
                creator_account_id="a1",
                reporter_account_id="a1",
                created_at=(datetime.now(timezone.utc) + timedelta(seconds=1)).isoformat(),
            )

        async def update_issue(self, issue_key, *a, **k):
            calls["update"] += 1
            return TrackerIssue(key=issue_key, url=f"{self.site_url}/browse/{issue_key}", status="")

    monkeypatch.setattr("sessionfs.server.routes.jira.JiraClient", TimeoutThenFoundFake)

    r1 = await client.post(
        f"/api/v1/projects/{project.id}/tickets/{ticket.id}/jira/publish", headers=_hdrs(key),
        json=_PUBLISH_BODY,
    )
    assert r1.status_code == 504
    assert r1.json()["error"]["details"]["error"] == "jira_timeout"

    link = (await db_session.execute(
        select(TicketTrackerLink).where(TicketTrackerLink.ticket_id == ticket.id)
    )).scalar_one()
    assert link.status == "unknown"
    assert link.claim_token is None
    await db_session.commit()  # release the read snapshot so the retry's commit is visible

    gt = (await client.get(
        f"/api/v1/projects/{project.id}/tickets/{ticket.id}", headers=_hdrs(key)
    )).json()
    assert gt["jira_publish_in_progress"] is False
    assert gt["jira_publish_unknown"] is True

    r2 = await client.post(
        f"/api/v1/projects/{project.id}/tickets/{ticket.id}/jira/publish", headers=_hdrs(key),
        json=_PUBLISH_BODY,
    )
    assert r2.status_code == 200, r2.text
    assert r2.json()["issue_key"] == "ACME-9"
    assert calls == {"create": 1, "search": 1, "update": 1}

    # Read the final state through the API (a fresh session) so the shared
    # in-memory connection can't serve a stale snapshot.
    gt2 = (await client.get(
        f"/api/v1/projects/{project.id}/tickets/{ticket.id}", headers=_hdrs(key)
    )).json()
    assert gt2["jira_publish_unknown"] is False
    assert gt2["jira"]["issue_key"] == "ACME-9"


@pytest.mark.asyncio
async def test_timeout_retry_with_no_match_creates_once(
    client: AsyncClient, db_session: AsyncSession, monkeypatch
):
    """L1 — a timed-out create with no matching issue is re-created exactly once."""
    monkeypatch.setattr("sessionfs.server.routes.jira._ADOPT_RETRY_DELAY_SECONDS", 0.0)
    org, user, key, project, ticket = await _ready(db_session)
    calls = {"create": 0, "search": 0}

    class TimeoutThenNoneFake:
        def __init__(self, site_url, account_email, api_token, *, transport=None):
            self.site_url = validate_site_url(site_url)

        async def aclose(self):
            pass

        async def create_issue(self, *a, **k):
            calls["create"] += 1
            if calls["create"] == 1:
                raise asyncio.TimeoutError
            return TrackerIssue(key="ACME-2", url=f"{self.site_url}/browse/ACME-2", status="", id="10002")

        async def search_issue_by_label(self, project_key, label):
            calls["search"] += 1
            return None

        async def update_issue(self, issue_key, *a, **k):
            raise AssertionError("no update expected when the search finds nothing")

    monkeypatch.setattr("sessionfs.server.routes.jira.JiraClient", TimeoutThenNoneFake)

    r1 = await client.post(
        f"/api/v1/projects/{project.id}/tickets/{ticket.id}/jira/publish", headers=_hdrs(key),
        json=_PUBLISH_BODY,
    )
    assert r1.status_code == 504

    r2 = await client.post(
        f"/api/v1/projects/{project.id}/tickets/{ticket.id}/jira/publish", headers=_hdrs(key),
        json=_PUBLISH_BODY,
    )
    assert r2.status_code == 200, r2.text
    assert r2.json()["issue_key"] == "ACME-2"
    # The empty label search is retried once (after the injectable delay) before
    # falling through to a create.
    assert calls == {"create": 2, "search": 2}

    link = (await db_session.execute(
        select(TicketTrackerLink).where(TicketTrackerLink.ticket_id == ticket.id)
    )).scalar_one()
    assert link.status == "linked"
    assert link.issue_key == "ACME-2"


@pytest.mark.asyncio
async def test_adopt_retries_label_search_once_before_creating(
    client: AsyncClient, db_session: AsyncSession, monkeypatch
):
    """An empty label search waits once and searches again before creating."""
    monkeypatch.setattr("sessionfs.server.routes.jira._ADOPT_RETRY_DELAY_SECONDS", 0.0)
    org, user, key, project, ticket = await _ready(db_session)
    calls = {"create": 0, "search": 0, "update": 0}

    class TimeoutThenLaggyFake:
        def __init__(self, site_url, account_email, api_token, *, transport=None):
            self.site_url = validate_site_url(site_url)

        async def aclose(self):
            pass

        async def create_issue(self, *a, **k):
            calls["create"] += 1
            raise asyncio.TimeoutError

        async def myself_account_id(self):
            return "a1"

        async def search_issue_by_label(self, project_key, label):
            calls["search"] += 1
            if calls["search"] == 1:
                return None  # not yet visible in Jira's search index
            return TrackerIssue(
                key="ACME-9",
                url=f"{self.site_url}/browse/ACME-9",
                status="",
                id="10009",
                project_key="ACME",
                creator_account_id="a1",
                reporter_account_id="a1",
                created_at=(datetime.now(timezone.utc) + timedelta(seconds=1)).isoformat(),
            )

        async def update_issue(self, issue_key, *a, **k):
            calls["update"] += 1
            return TrackerIssue(key=issue_key, url=f"{self.site_url}/browse/{issue_key}", status="")

    monkeypatch.setattr("sessionfs.server.routes.jira.JiraClient", TimeoutThenLaggyFake)

    r1 = await client.post(
        f"/api/v1/projects/{project.id}/tickets/{ticket.id}/jira/publish", headers=_hdrs(key),
        json=_PUBLISH_BODY,
    )
    assert r1.status_code == 504

    r2 = await client.post(
        f"/api/v1/projects/{project.id}/tickets/{ticket.id}/jira/publish", headers=_hdrs(key),
        json=_PUBLISH_BODY,
    )
    assert r2.status_code == 200, r2.text
    assert r2.json()["issue_key"] == "ACME-9"
    # The first empty search is retried once; the issue found on the retry is
    # adopted (updated), never re-created.
    assert calls == {"create": 1, "search": 2, "update": 1}


@pytest.mark.parametrize(
    "override, fragment",
    [
        (
            {"project_key": "OTHER"},
            "not the mapped project ACME",
        ),
        (
            {"creator_account_id": "someone-else", "reporter_account_id": "someone-else"},
            "not created by this connection",
        ),
        (
            {"created_at": "2020-01-01T00:00:00+00:00"},
            "created before the unknown publish began",
        ),
    ],
)
@pytest.mark.asyncio
async def test_adopt_rejects_unverified_issue(
    client, db_session, monkeypatch, override, fragment
):
    """A label match alone is not proof of ownership: a foreign-project /
    foreign-account / stale issue keeps the link ``unknown`` and 409s."""
    org, user, key, project, ticket = await _ready(db_session)
    calls = {"create": 0, "search": 0}

    base = dict(
        key="ACME-9",
        url="https://acme.atlassian.net/browse/ACME-9",
        status="",
        id="10009",
        project_key="ACME",
        creator_account_id="a1",
        reporter_account_id="a1",
        created_at=(datetime.now(timezone.utc) + timedelta(seconds=1)).isoformat(),
    )
    base.update(override)

    class RejectingFake:
        def __init__(self, site_url, account_email, api_token, *, transport=None):
            self.site_url = validate_site_url(site_url)

        async def aclose(self):
            pass

        async def create_issue(self, *a, **k):
            calls["create"] += 1
            raise asyncio.TimeoutError

        async def myself_account_id(self):
            return "a1"

        async def search_issue_by_label(self, project_key, label):
            calls["search"] += 1
            return TrackerIssue(**base)

        async def update_issue(self, issue_key, *a, **k):
            raise AssertionError("no update expected when adoption is refused")

    monkeypatch.setattr("sessionfs.server.routes.jira.JiraClient", RejectingFake)

    r1 = await client.post(
        f"/api/v1/projects/{project.id}/tickets/{ticket.id}/jira/publish", headers=_hdrs(key),
        json=_PUBLISH_BODY,
    )
    assert r1.status_code == 504

    link = (await db_session.execute(
        select(TicketTrackerLink).where(TicketTrackerLink.ticket_id == ticket.id)
    )).scalar_one()
    assert link.status == "unknown"
    await db_session.commit()

    r2 = await client.post(
        f"/api/v1/projects/{project.id}/tickets/{ticket.id}/jira/publish", headers=_hdrs(key),
        json=_PUBLISH_BODY,
    )
    assert r2.status_code == 409, r2.text
    assert r2.json()["error"]["details"]["error"] == "jira_adopt_rejected"
    assert fragment in r2.json()["error"]["message"]

    # The link stays 'unknown' so a corrected publish can still adopt/create.
    gt = (await client.get(
        f"/api/v1/projects/{project.id}/tickets/{ticket.id}", headers=_hdrs(key)
    )).json()
    assert gt["jira_publish_unknown"] is True
    assert gt["jira"] is None


@pytest.mark.asyncio
async def test_30k_single_line_preview_and_publish_preserve_every_char(
    client: AsyncClient, db_session: AsyncSession, monkeypatch, fake_jira
):
    """A single unbroken 30,000-char line splits (not 422) and survives intact."""
    org, user, key, project, _ = await _ready(db_session)
    note = "x" * 30000
    ticket = Ticket(
        id=f"tk_{uuid.uuid4().hex[:16]}",
        project_id=project.id,
        title="Huge note",
        description="line one\nline two",
        acceptance_criteria=json.dumps(["users can log in"]),
        completion_notes=note,
        created_by_user_id=user.id,
    )
    db_session.add(ticket)
    await db_session.commit()
    await db_session.refresh(ticket)

    preview = (await client.post(
        f"/api/v1/projects/{project.id}/tickets/{ticket.id}/jira/preview", headers=_hdrs(key)
    )).json()
    assert preview["ticket_id"] == ticket.id
    for section in preview["sections"]:
        for para in section["paragraphs"]:
            assert len(para["text"]) <= 10000

    captured: dict = {}

    class CapturingFake:
        def __init__(self, site_url, account_email, api_token, *, transport=None):
            self.site_url = validate_site_url(site_url)

        async def aclose(self):
            pass

        async def create_issue(self, project_key, issue_type, summary, description_adf, *, labels=None):
            captured["adf"] = description_adf
            return TrackerIssue(key="ACME-1", url=f"{self.site_url}/browse/ACME-1", status="", id="10001")

        async def update_issue(self, issue_key, *a, **k):
            return TrackerIssue(key=issue_key, url=f"{self.site_url}/browse/{issue_key}", status="")

    monkeypatch.setattr("sessionfs.server.routes.jira.JiraClient", CapturingFake)

    resp = await client.post(
        f"/api/v1/projects/{project.id}/tickets/{ticket.id}/jira/publish", headers=_hdrs(key),
        json={"summary": preview["summary"], "sections": preview["sections"], "confirm": True},
    )
    assert resp.status_code == 200, resp.text
    # The completion note appears once in the context section, intact.
    assert note in "".join(_adf_texts(captured["adf"]))


@pytest.mark.asyncio
async def test_40k_description_publish_422_with_no_outbound_call(
    client: AsyncClient, db_session: AsyncSession, fake_jira
):
    """A description whose rendered ADF would exceed Jira's cap is refused up front."""
    org, user, key, project, _ = await _ready(db_session)
    ticket = Ticket(
        id=f"tk_{uuid.uuid4().hex[:16]}",
        project_id=project.id,
        title="Too big",
        description="x" * 40000,
        acceptance_criteria=json.dumps(["users can log in"]),
        created_by_user_id=user.id,
    )
    db_session.add(ticket)
    await db_session.commit()
    await db_session.refresh(ticket)

    preview = (await client.post(
        f"/api/v1/projects/{project.id}/tickets/{ticket.id}/jira/preview", headers=_hdrs(key)
    )).json()
    assert preview["ticket_id"] == ticket.id

    resp = await client.post(
        f"/api/v1/projects/{project.id}/tickets/{ticket.id}/jira/publish", headers=_hdrs(key),
        json={"summary": preview["summary"], "sections": preview["sections"], "confirm": True},
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["details"]["error"] == "content_too_large_for_jira"
    # The pre-flight check fires before the Jira client is even constructed.
    assert fake_jira == []


@pytest.mark.asyncio
async def test_update_fenced_409s_link_changed_when_unlinked_during_call(tmp_path, monkeypatch):
    """L3 — an unlink during the outbound update is a 409 link_changed, not a 500."""
    from httpx import ASGITransport

    engine, factory, app, project_id, ticket_id, key = await _file_backed_app(
        tmp_path, monkeypatch
    )

    class SimpleFake:
        def __init__(self, site_url, account_email, api_token, *, transport=None):
            self.site_url = validate_site_url(site_url)

        async def aclose(self):
            pass

        async def create_issue(self, *a, **k):
            return TrackerIssue(key="ACME-1", url=f"{self.site_url}/browse/ACME-1", status="", id="10001")

        async def update_issue(self, issue_key, *a, **k):
            return TrackerIssue(key=issue_key, url=f"{self.site_url}/browse/{issue_key}", status="")

    monkeypatch.setattr("sessionfs.server.routes.jira.JiraClient", SimpleFake)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        r1 = await c.post(
            f"/api/v1/projects/{project_id}/tickets/{ticket_id}/jira/publish",
            headers=_hdrs(key), json=_PUBLISH_BODY,
        )
        assert r1.status_code == 200, r1.text

        entered = asyncio.Event()
        release = asyncio.Event()

        class BlockingFake:
            def __init__(self, site_url, account_email, api_token, *, transport=None):
                self.site_url = validate_site_url(site_url)

            async def aclose(self):
                pass

            async def create_issue(self, *a, **k):
                raise AssertionError("update path must not create")

            async def update_issue(self, issue_key, *a, **k):
                entered.set()
                await release.wait()
                return TrackerIssue(key=issue_key, url=f"{self.site_url}/browse/{issue_key}", status="")

        monkeypatch.setattr("sessionfs.server.routes.jira.JiraClient", BlockingFake)

        task = asyncio.create_task(
            c.post(
                f"/api/v1/projects/{project_id}/tickets/{ticket_id}/jira/publish",
                headers=_hdrs(key), json=_PUBLISH_BODY,
            )
        )
        await entered.wait()

        # Unlink while the update is outbound.
        async with factory() as db:
            link = (await db.execute(
                select(TicketTrackerLink).where(TicketTrackerLink.ticket_id == ticket_id)
            )).scalar_one()
            assert link.status == "linked"
            await db.delete(link)
            await db.commit()

        release.set()
        r2 = await task

    await engine.dispose()

    assert r2.status_code == 409
    assert r2.json()["error"]["details"]["error"] == "link_changed"


@pytest.mark.asyncio
async def test_non_member_with_captured_session_cannot_use_org_jira(
    client: AsyncClient, db_session: AsyncSession, fake_jira
):
    """Project read access via a captured session (no org membership) must not
    reach the org's Jira credential: preview and publish are refused."""
    from datetime import datetime, timezone

    from sessionfs.server.db.models import Session

    org, user, key, project, ticket = await _ready(db_session)
    outsider, outsider_key = await _user(db_session)
    now = datetime.now(timezone.utc)
    db_session.add(Session(
        id=f"ses_{uuid.uuid4().hex[:16]}",
        user_id=outsider.id,
        title="captured",
        source_tool="claude-code",
        blob_key=f"sessions/{outsider.id}/x.tar.gz",
        etag=uuid.uuid4().hex,
        created_at=now,
        updated_at=now,
        uploaded_at=now,
        git_remote_normalized=project.git_remote_normalized,
    ))
    await db_session.commit()

    resp = await client.post(
        f"/api/v1/projects/{project.id}/tickets/{ticket.id}/jira/preview",
        headers=_hdrs(outsider_key),
    )
    assert resp.status_code == 403, resp.text
    assert resp.json()["error"]["details"]["error"] == "org_membership_required"

    resp = await client.post(
        f"/api/v1/projects/{project.id}/tickets/{ticket.id}/jira/publish",
        headers=_hdrs(outsider_key),
        json={
            "summary": "x",
            "confirm": True,
            "sections": [{"key": "description", "paragraphs": [{"text": "hi"}]}],
        },
    )
    assert resp.status_code == 403, resp.text
    links = (
        await db_session.execute(
            select(TicketTrackerLink).where(TicketTrackerLink.ticket_id == ticket.id)
        )
    ).scalars().all()
    assert links == []


@pytest.mark.asyncio
async def test_restricted_org_jira_preview_and_publish_need_edit_standing(
    client: AsyncClient, db_session: AsyncSession, fake_jira
):
    """GitHub #101 — in an org that restricts project visibility, Jira
    preview/publish need edit standing: a group reader is refused, a group
    editor goes through. Unrestricted orgs keep member-level access."""
    from sessionfs.server.db.models import (
        ProjectGroup,
        ProjectGroupAccess,
        ProjectGroupMember,
    )

    org, admin, _key, project, ticket = await _ready(db_session)
    reader, reader_key = await _member(db_session, org, "member")
    editor, editor_key = await _member(db_session, org, "member")
    plain, plain_key = await _member(db_session, org, "member")
    group = ProjectGroup(
        id=f"pg_{uuid.uuid4().hex[:12]}", org_id=org.id, slug="g", name="G",
        created_at=datetime.now(timezone.utc), updated_at=datetime.now(timezone.utc),
    )
    db_session.add(group)
    await db_session.flush()
    db_session.add(ProjectGroupAccess(group_id=group.id, project_id=project.id))
    db_session.add(ProjectGroupMember(group_id=group.id, user_id=reader.id, role="reader"))
    db_session.add(ProjectGroupMember(group_id=group.id, user_id=editor.id, role="editor"))
    await db_session.commit()

    url = f"/api/v1/projects/{project.id}/tickets/{ticket.id}/jira"
    # Unrestricted: a plain member previews.
    resp = await client.post(f"{url}/preview", headers=_hdrs(plain_key))
    assert resp.status_code == 200, resp.text
    preview = resp.json()

    org.settings = json.dumps({"general": {"restrict_project_visibility": True}})
    await db_session.commit()

    resp = await client.post(f"{url}/preview", headers=_hdrs(reader_key))
    assert resp.status_code == 403, resp.text
    assert resp.json()["error"]["code"] == "project_edit_forbidden"
    resp = await client.post(
        f"{url}/publish", headers=_hdrs(reader_key),
        json={"summary": preview["summary"], "sections": preview["sections"], "confirm": True},
    )
    assert resp.status_code == 403, resp.text
    resp = await client.post(f"{url}/preview", headers=_hdrs(plain_key))
    assert resp.status_code == 403, resp.text
    resp = await client.post(f"{url}/preview", headers=_hdrs(editor_key))
    assert resp.status_code == 200, resp.text
