"""GitHub #144 — Jira search (route, helper tool, JQL safety).

The Jira client is replaced with an in-memory fake that records the JQL it
was sent and returns canned issues — never a real Jira call. Focus: the
visibility rule (only Jira projects mapped to readable SessionFS projects),
JQL injection refusal, the post-filter, service-key scope/allowlist, the plan
gate, the per-org limit, the cache, the audit row and DLP on the results.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.judge.agent_stream import ToolCall
from sessionfs.server.auth.keys import generate_api_key, hash_api_key
from sessionfs.server.auth.rate_limit import SlidingWindowRateLimiter
from sessionfs.server.db.models import (
    ApiKey,
    OrgAuditEvent,
    OrgMember,
    OrgTrackerConnection,
    Organization,
    Project,
    TrackerProjectMapping,
    User,
)
from sessionfs.server.helper.context import HelperContext, HelperScope, RetrievalBudget
from sessionfs.server.helper.tools import TOOLS, ToolArgumentError, execute
from sessionfs.server.integrations.trackers.base import TrackerError
from sessionfs.server.llm_keys import encrypt_secret
from sessionfs.server.services import jira_search

_TOKEN = "atlassian-secret-token-1234"
_AWS = "AKIAIOSFODNN7EXAMPLE"


def _hdrs(raw: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {raw}"}


async def _user(db: AsyncSession) -> tuple[User, str]:
    user = User(
        id=str(uuid.uuid4()),
        email=f"u-{uuid.uuid4().hex[:8]}@example.com",
        display_name="U",
        tier="team",
        email_verified=True,
        is_active=True,
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


async def _member(db: AsyncSession, org: Organization, role: str = "member") -> tuple[User, str]:
    user, raw = await _user(db)
    db.add(OrgMember(org_id=org.id, user_id=user.id, role=role, joined_at=datetime.now(timezone.utc)))
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


async def _project(db: AsyncSession, owner: User, org: Organization) -> Project:
    project = Project(
        id=f"proj_{uuid.uuid4().hex[:12]}",
        name=f"p-{uuid.uuid4().hex[:6]}",
        git_remote_normalized=f"github.com/acme/{uuid.uuid4().hex[:10]}",
        context_document="",
        owner_id=owner.id,
        org_id=org.id,
    )
    db.add(project)
    await db.commit()
    return project


async def _connection(db: AsyncSession, org: Organization, user: User, enabled: bool = True) -> None:
    db.add(
        OrgTrackerConnection(
            org_id=org.id,
            site_url="https://acme.atlassian.net",
            account_email="jira@acme.com",
            encrypted_api_token=encrypt_secret(_TOKEN),
            default_template="agile",
            enabled=enabled,
            connected_by_user_id=user.id,
        )
    )
    await db.commit()


async def _mapping(db: AsyncSession, org: Organization, project: Project, user: User, key: str) -> None:
    db.add(
        TrackerProjectMapping(
            id=f"tpm_{uuid.uuid4().hex[:16]}",
            org_id=org.id,
            project_id=project.id,
            jira_project_key=key,
            issue_type="Story",
            created_by_user_id=user.id,
        )
    )
    await db.commit()


def _issue(key: str, project_key: str | None = None, **fields) -> dict:
    base = {
        "project": {"key": project_key or key.split("-")[0]},
        "summary": f"Summary of {key}",
        "status": {"name": "In Progress"},
        "assignee": {"displayName": "Kelvin", "emailAddress": "kelvin@acme.com"},
        "priority": {"name": "High"},
        "issuetype": {"name": "Story"},
        "updated": "2026-11-20T10:00:00.000+0000",
    }
    base.update(fields)
    return {"id": "1", "key": key, "fields": base}


@pytest.fixture(autouse=True)
def _reset_search_state():
    jira_search.reset_state()
    yield
    jira_search.reset_state()


@pytest.fixture
def fake_jira(monkeypatch):
    state: dict = {"calls": [], "issues": [], "is_last": True, "error": None}

    class Fake:
        async def search_issues(self, jql, fields, max_results):
            state["calls"].append({"jql": jql, "fields": fields, "max_results": max_results})
            if state["error"] is not None:
                raise state["error"]
            return list(state["issues"]), state["is_last"]

        async def aclose(self):
            pass

    monkeypatch.setattr(jira_search, "_default_client", lambda conn: Fake())
    return state


async def _setup(db: AsyncSession):
    """Org with a connection; the member can read P1 (ACME) and P2 (BETA);
    P3 (SECRET) belongs to a restricted owner the member can't read."""
    org = await _org(db)
    admin, admin_key = await _member(db, org, "admin")
    member, member_key = await _member(db, org, "member")
    await _connection(db, org, admin)
    p1 = await _project(db, member, org)
    p2 = await _project(db, member, org)
    await _mapping(db, org, p1, admin, "ACME")
    await _mapping(db, org, p2, admin, "BETA")
    return org, admin, admin_key, member, member_key, p1, p2


def _detail(resp) -> dict:
    """The route's detail dict, rebuilt from the error envelope."""
    err = resp.json()["error"]
    return {**(err.get("details") or {}), "code": err["code"], "message": err["message"]}


def _url(org_id: str) -> str:
    return f"/api/v1/orgs/{org_id}/integrations/jira/search"


# ── JQL scanner (unit) ──


@pytest.mark.parametrize(
    "jql",
    [
        ") OR project = SECRET",
        "assignee = x) OR (project = SECRET",
        'summary ~ "unclosed',
        "summary ~ 'unclosed",
        "(status = Done",
        "status = Done)",
        "(a = 1 ORDER BY key)",
        "a = 1 ORDER BY key, (x)",
        "a = 1 ORDER BY key; project = SECRET",
        "a = 1 ORDER BY key OR project = SECRET",
        "a = \\) OR project = SECRET",
        "a = 1\x00",
        "x" * (jira_search.JQL_MAX_CHARS + 1),
    ],
)
def test_split_jql_refuses_escapes(jql):
    with pytest.raises(jira_search.JqlError):
        jira_search.split_jql(jql)


def test_split_jql_accepts_strings_and_trailing_order_by():
    assert jira_search.split_jql('summary ~ "a) OR (b" ORDER BY updated DESC, key') == (
        'summary ~ "a) OR (b"',
        "updated DESC, key",
    )
    assert jira_search.split_jql('summary ~ "a\\" ) x"') == ('summary ~ "a\\" ) x"', None)
    assert jira_search.split_jql("summary ~ reorder") == ("summary ~ reorder", None)
    assert jira_search.split_jql("ORDER BY created") == ("", "created")
    assert jira_search.build_jql({"BETA", "ACME", "bad key"}, "a = 1", "key") == (
        'project in ("ACME", "BETA") AND (a = 1) ORDER BY key'
    )


# ── Route ──


@pytest.mark.asyncio
async def test_member_search_wraps_jql_in_allowed_projects(
    client: AsyncClient, db_session: AsyncSession, fake_jira
):
    org, admin, _, member, member_key, p1, p2 = await _setup(db_session)
    # A third project the member can't read, mapped to SECRET.
    outsider, _ = await _user(db_session)
    hidden = await _project(db_session, outsider, org)
    hidden.org_id = org.id
    await db_session.commit()
    await _mapping(db_session, org, hidden, admin, "SECRET")
    # Restrict visibility so org membership alone doesn't grant read access.
    org.settings = json.dumps({"general": {"restrict_project_visibility": True}})
    await db_session.commit()

    fake_jira["issues"] = [_issue("ACME-1"), _issue("BETA-2"), _issue("SECRET-3")]
    resp = await client.post(
        _url(org.id),
        json={"jql": "assignee = Kelvin ORDER BY updated DESC"},
        headers=_hdrs(member_key),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    sent = fake_jira["calls"][0]["jql"]
    assert sent == (
        'project in ("ACME", "BETA") AND (assignee = Kelvin) ORDER BY updated DESC'
    )
    assert "project" in fake_jira["calls"][0]["fields"]
    # The post-filter drops the out-of-scope issue Jira (wrongly) returned.
    assert [i["key"] for i in body["issues"]] == ["ACME-1", "BETA-2"]
    assert body["allowed_jira_projects"] == ["ACME", "BETA"]
    assert body["issues"][0]["project_ids"] == [p1.id]
    assert body["issues"][0]["assignee"] == "Kelvin"
    assert "kelvin@acme.com" not in resp.text
    assert body["issues"][0]["url"] == "https://acme.atlassian.net/browse/ACME-1"
    assert body["reason"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "jql",
    [") OR project = SECRET", '"', "(status = Done", "(a = 1 ORDER BY key)"],
)
async def test_injection_attempts_are_refused_before_jira(
    client: AsyncClient, db_session: AsyncSession, fake_jira, jql
):
    org, _, _, _, member_key, _, _ = await _setup(db_session)
    resp = await client.post(_url(org.id), json={"jql": jql}, headers=_hdrs(member_key))
    assert resp.status_code == 422
    assert _detail(resp)["code"] == "invalid_jql"
    assert fake_jira["calls"] == []


@pytest.mark.asyncio
async def test_no_allowed_projects_is_empty_with_reason(
    client: AsyncClient, db_session: AsyncSession, fake_jira
):
    org = await _org(db_session)
    admin, _ = await _member(db_session, org, "admin")
    _, member_key = await _member(db_session, org, "member")
    await _connection(db_session, org, admin)
    resp = await client.post(_url(org.id), json={"jql": ""}, headers=_hdrs(member_key))
    assert resp.status_code == 200
    body = resp.json()
    assert body["issues"] == [] and body["reason"] == "no_mapped_projects"
    assert body["message"]
    assert fake_jira["calls"] == []


@pytest.mark.asyncio
async def test_outsider_unknown_org_and_plan_gate(
    client: AsyncClient, db_session: AsyncSession, fake_jira
):
    org, *_ = await _setup(db_session)
    _, outsider_key = await _user(db_session)
    resp = await client.post(_url(org.id), json={"jql": ""}, headers=_hdrs(outsider_key))
    assert resp.status_code == 404
    resp = await client.post(_url("org_nope"), json={"jql": ""}, headers=_hdrs(outsider_key))
    assert resp.status_code == 404

    team = await _org(db_session, tier="team")
    admin, _ = await _member(db_session, team, "admin")
    _, team_key = await _member(db_session, team, "member")
    await _connection(db_session, team, admin)
    resp = await client.post(_url(team.id), json={"jql": ""}, headers=_hdrs(team_key))
    assert resp.status_code == 403
    assert _detail(resp)["error"] == "upgrade_required"
    assert fake_jira["calls"] == []


@pytest.mark.asyncio
async def test_not_connected_bad_fields_and_unknown_body_keys(
    client: AsyncClient, db_session: AsyncSession, fake_jira
):
    org = await _org(db_session)
    _, member_key = await _member(db_session, org, "member")
    resp = await client.post(_url(org.id), json={"jql": ""}, headers=_hdrs(member_key))
    assert resp.status_code == 404
    assert _detail(resp)["code"] == "jira_not_connected"
    resp = await client.post(
        _url(org.id), json={"jql": "", "fields": ["customfield_1"]}, headers=_hdrs(member_key)
    )
    assert resp.status_code == 422
    resp = await client.post(
        _url(org.id), json={"jql": "", "max_results": 51}, headers=_hdrs(member_key)
    )
    assert resp.status_code == 422
    resp = await client.post(_url(org.id), json={"jql": "", "x": 1}, headers=_hdrs(member_key))
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_service_key_scope_org_and_allowlist(
    client: AsyncClient, db_session: AsyncSession, fake_jira
):
    org, admin, _, member, _, p1, p2 = await _setup(db_session)
    no_scope = await _service_key(db_session, org, member, ["tickets:read"])
    resp = await client.post(_url(org.id), json={"jql": ""}, headers=_hdrs(no_scope))
    assert resp.status_code == 403
    assert _detail(resp)["error"] == "insufficient_scope"

    allowlisted = await _service_key(db_session, org, member, ["jira:read"], [p2.id])
    fake_jira["issues"] = [_issue("ACME-1"), _issue("BETA-2")]
    resp = await client.post(_url(org.id), json={"jql": ""}, headers=_hdrs(allowlisted))
    assert resp.status_code == 200, resp.text
    assert fake_jira["calls"][-1]["jql"] == 'project in ("BETA")'
    assert [i["key"] for i in resp.json()["issues"]] == ["BETA-2"]

    other = await _org(db_session)
    foreign = await _service_key(db_session, other, member, ["jira:read"])
    resp = await client.post(_url(org.id), json={"jql": ""}, headers=_hdrs(foreign))
    assert resp.status_code == 404

    audit = (
        await db_session.execute(
            select(OrgAuditEvent).where(
                OrgAuditEvent.org_id == org.id, OrgAuditEvent.event_type == "jira_search"
            )
        )
    ).scalars().all()
    assert any(json.loads(a.after).get("service_key_id") for a in audit)
    assert all(a.actor_role_at_time == "service_key" for a in audit if json.loads(a.after).get("service_key_id"))


@pytest.mark.asyncio
async def test_cache_and_rate_limit(
    client: AsyncClient, db_session: AsyncSession, fake_jira, monkeypatch
):
    org, _, _, _, member_key, _, _ = await _setup(db_session)
    fake_jira["issues"] = [_issue("ACME-1")]
    first = await client.post(_url(org.id), json={"jql": "a = 1"}, headers=_hdrs(member_key))
    second = await client.post(_url(org.id), json={"jql": "a = 1"}, headers=_hdrs(member_key))
    assert first.status_code == second.status_code == 200
    assert first.json()["cached"] is False and second.json()["cached"] is True
    assert len(fake_jira["calls"]) == 1

    monkeypatch.setattr(jira_search, "_limiter", SlidingWindowRateLimiter(max_requests=1, window_seconds=60))
    ok = await client.post(_url(org.id), json={"jql": "a = 2"}, headers=_hdrs(member_key))
    limited = await client.post(_url(org.id), json={"jql": "a = 3"}, headers=_hdrs(member_key))
    assert ok.status_code == 200
    assert limited.status_code == 429
    assert _detail(limited)["code"] == "jira_search_rate_limited"
    assert limited.headers.get("retry-after") == "60"
    # A cached query still answers while the org is limited.
    cached = await client.post(_url(org.id), json={"jql": "a = 1"}, headers=_hdrs(member_key))
    assert cached.status_code == 200


@pytest.mark.asyncio
async def test_audit_row_has_redacted_jql_and_no_issue_content(
    client: AsyncClient, db_session: AsyncSession, fake_jira
):
    org, _, _, member, member_key, _, _ = await _setup(db_session)
    fake_jira["issues"] = [_issue("ACME-1", summary="Top secret plan")]
    resp = await client.post(
        _url(org.id), json={"jql": f'text ~ "{_AWS}"'}, headers=_hdrs(member_key)
    )
    assert resp.status_code == 200, resp.text
    row = (
        await db_session.execute(
            select(OrgAuditEvent).where(
                OrgAuditEvent.org_id == org.id, OrgAuditEvent.event_type == "jira_search"
            )
        )
    ).scalars().one()
    assert row.actor_user_id == member.id
    assert _AWS not in (row.after or "") and _AWS not in (row.target_id or "")
    after = json.loads(row.after)
    assert "REDACTED" in after["jql"]
    assert after["issue_keys"] == ["ACME-1"]
    assert after["result"] == "ok"
    assert "Top secret plan" not in row.after


@pytest.mark.asyncio
async def test_results_are_dlp_redacted(
    client: AsyncClient, db_session: AsyncSession, fake_jira
):
    org, _, _, _, member_key, _, _ = await _setup(db_session)
    fake_jira["issues"] = [
        _issue(
            "ACME-1",
            summary=f"rotate {_AWS} today",
            description={
                "type": "doc",
                "content": [{"type": "paragraph", "content": [{"type": "text", "text": f"key {_AWS}"}]}],
            },
        )
    ]
    resp = await client.post(
        _url(org.id),
        json={"jql": "", "fields": ["summary", "description"]},
        headers=_hdrs(member_key),
    )
    assert resp.status_code == 200, resp.text
    assert _AWS not in resp.text
    issue = resp.json()["issues"][0]
    assert "REDACTED" in issue["summary"] and "REDACTED" in issue["description"]
    assert set(issue) >= {"key", "url", "project_key", "summary", "description"}
    assert "status" not in issue


@pytest.mark.asyncio
async def test_jira_rejection_is_generic(
    client: AsyncClient, db_session: AsyncSession, fake_jira
):
    org, _, _, _, member_key, _, _ = await _setup(db_session)
    fake_jira["error"] = TrackerError(
        "Jira API 400: The value 'SECRET' does not exist for the field 'project'.",
        status_code=400,
    )
    resp = await client.post(_url(org.id), json={"jql": "a = 1"}, headers=_hdrs(member_key))
    assert resp.status_code == 422
    assert _detail(resp)["code"] == "jira_query_rejected"
    assert "SECRET" not in resp.text
    fake_jira["error"] = TrackerError("Could not reach Jira Cloud", status_code=None)
    resp = await client.post(_url(org.id), json={"jql": "a = 2"}, headers=_hdrs(member_key))
    assert resp.status_code == 502


# ── Helper tool ──


class _Sink:
    def __init__(self):
        self.events: list = []

    async def record(self, tool_name, sanitized_args, returned_refs):
        self.events.append((tool_name, sanitized_args, returned_refs))


def _ctx(db: AsyncSession, user: User, scope: HelperScope, org_id: str | None, audit=None) -> HelperContext:
    return HelperContext(
        user=user, db=db, org_id=org_id, scope=scope, budget=RetrievalBudget(), audit=audit
    )


@pytest.mark.asyncio
async def test_helper_tool_respects_conversation_scope(db_session: AsyncSession, fake_jira):
    org, _, _, member, _, p1, _ = await _setup(db_session)
    fake_jira["issues"] = [_issue("ACME-1"), _issue("BETA-2")]
    ctx = _ctx(db_session, member, HelperScope(project_id=p1.id), org.id)
    out = await TOOLS["search_jira_issues"].handler(ctx, {"jql": "assignee = Kelvin"})
    assert fake_jira["calls"][0]["jql"] == 'project in ("ACME") AND (assignee = Kelvin)'
    assert "ACME-1" in out.text and "BETA-2" not in out.text
    assert "Searched only these Jira projects" in out.text
    assert out.refs["jira_issue_keys"] == ["ACME-1"]
    assert out.refs["project_ids"] == [p1.id]

    # Unscoped turn bounded to the org: both mapped projects.
    ctx = _ctx(db_session, member, HelperScope(org_boundary=org.id), None)
    out = await TOOLS["search_jira_issues"].handler(ctx, {"jql": "assignee = Kelvin "})
    assert fake_jira["calls"][-1]["jql"].startswith('project in ("ACME", "BETA")')
    assert "ACME-1" in out.text and "BETA-2" in out.text


@pytest.mark.asyncio
async def test_helper_tool_refusals(db_session: AsyncSession, fake_jira):
    org, *_ = await _setup(db_session)
    outsider, _ = await _user(db_session)
    ctx = _ctx(db_session, outsider, HelperScope(org_boundary=org.id), None)
    out = await TOOLS["search_jira_issues"].handler(ctx, {"jql": ""})
    assert "Only members" in out.text
    with pytest.raises(ToolArgumentError):
        await TOOLS["search_jira_issues"].handler(ctx, {"jql": ") OR project = SECRET"})
    ctx = _ctx(db_session, outsider, HelperScope(), None)
    out = await TOOLS["search_jira_issues"].handler(ctx, {"jql": ""})
    assert "needs an organization" in out.text
    assert fake_jira["calls"] == []


@pytest.mark.asyncio
async def test_helper_execute_redacts_jql_in_retrieval_audit(db_session: AsyncSession, fake_jira):
    org, _, _, member, _, p1, _ = await _setup(db_session)
    fake_jira["issues"] = [_issue("ACME-1", summary=f"leak {_AWS}")]
    sink = _Sink()
    ctx = _ctx(db_session, member, HelperScope(project_id=p1.id), org.id, audit=sink)
    result = await execute(
        ctx,
        ToolCall(id="c1", name="search_jira_issues", arguments={"jql": f'text ~ "{_AWS}"'}),
    )
    assert not result.is_error
    assert _AWS not in result.content
    name, args, refs = sink.events[-1]
    assert name == "search_jira_issues"
    assert _AWS not in args["jql"]
    assert refs["jira_issue_keys"] == ["ACME-1"]
