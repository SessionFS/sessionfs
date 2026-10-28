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
import hashlib
import hmac
import json
import uuid
from datetime import date, datetime, timedelta, timezone

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
    TicketEvent,
    TicketTrackerLink,
    TrackerProjectMapping,
    TrackerSyncEvent,
    User,
)
from sessionfs.server.integrations.trackers.base import (
    TrackerBoard,
    TrackerError,
    TrackerIssue,
    TrackerIssueType,
    TrackerProject,
    TrackerProjectPage,
    TrackerSprint,
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


_WEBHOOK_SECRET = "whsec_test_secret_value_1234"


def _sign(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def _webhook_payload(event: str, **overrides: object) -> dict:
    base: dict = {"webhookEvent": event, "timestamp": 1700000000000}
    base.update(overrides)
    return base


async def _post_webhook(
    client: AsyncClient,
    org_id: str,
    payload: dict,
    *,
    secret: str = _WEBHOOK_SECRET,
) -> tuple[int, dict]:
    body = json.dumps(payload).encode()
    headers = {
        "content-type": "application/json",
        "x-hub-signature": _sign(secret, body),
    }
    resp = await client.post(
        f"/api/v1/webhooks/jira/{org_id}", content=body, headers=headers
    )
    try:
        return resp.status_code, resp.json()
    except Exception:
        return resp.status_code, {}


# ── fake Jira client ──


@pytest.fixture
def fake_jira(monkeypatch):
    class Captured(list):
        """The captured Fake list, with the shared mutable ``state`` attached."""

    captured = Captured()
    state: dict = {"fail_update": False}

    class Fake:
        def __init__(self, site_url, account_email, api_token, *, transport=None):
            self.site_url = validate_site_url(site_url)
            self.account_email = account_email
            self.api_token = api_token
            self.calls = {
                "create_issue": 0,
                "update_issue": 0,
                "get_issue": 0,
                "add_comment": 0,
                "transition_issue": 0,
            }
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
            self.state.setdefault("create_extra_fields", []).append(k.get("extra_fields"))
            self.state.setdefault("create_scheduling", []).append(
                {key: k.get(key) for key in ("due_date", "original_estimate")}
            )
            # A per-call script lets a test fail one attempt then succeed the
            # next (sprint field rejected / parent rejected → retry without it).
            script = self.state.get("create_script") or []
            if script:
                step = script.pop(0)
                if "error" in step:
                    raise TrackerError(step["error"][1], status_code=step["error"][0])
            err = self.state.get("create_error")
            if err is not None:
                status_code, message = err
                raise TrackerError(message, status_code=status_code)
            if self.state.get("scheduling_400"):
                self.state["scheduling_400"] = False
                raise TrackerError(
                    self.state.get(
                        "scheduling_400_message",
                        "Field 'duedate' cannot be set. It is not on the appropriate screen, or unknown.",
                    ),
                    status_code=400,
                )
            key = self.state.get("create_issue_key", "ACME-1")
            return TrackerIssue(key=key, url=f"{self.site_url}/browse/{key}", status="", id="10001")

        async def update_issue(self, issue_key, *a, **k) -> TrackerIssue:
            self.calls["update_issue"] += 1
            self.state.setdefault("update_extra_fields", []).append(k.get("extra_fields"))
            self.state.setdefault("update_scheduling", []).append(
                {key: k.get(key) for key in ("due_date", "original_estimate")}
            )
            # A per-call script (parallel to ``create_script``) lets a test fail
            # one attempt then succeed the next (parent rejected → retry).
            script = self.state.get("update_script") or []
            if script:
                step = script.pop(0)
                if "error" in step:
                    raise TrackerError(step["error"][1], status_code=step["error"][0])
            if self.state["fail_update"]:
                raise TrackerError("Jira API 500: boom")
            if self.state.get("scheduling_400"):
                self.state["scheduling_400"] = False
                raise TrackerError(
                    self.state.get(
                        "scheduling_400_message",
                        "Field 'timetracking' cannot be set. It is not on the appropriate screen, or unknown.",
                    ),
                    status_code=400,
                )
            return TrackerIssue(key=issue_key, url=f"{self.site_url}/browse/{issue_key}", status="")

        async def get_issue(self, issue_key, sprint_field_id=None, epic_link_field_id=None) -> TrackerIssue:
            self.calls["get_issue"] += 1
            self.state.setdefault("get_issue_fields", []).append(
                (sprint_field_id, epic_link_field_id)
            )
            if self.state.get("get_issue_timeout"):
                raise asyncio.TimeoutError()
            err = self.state.get("get_issue_error")
            if err is not None:
                status_code, message = err
                raise TrackerError(message, status_code=status_code)
            return TrackerIssue(
                key=issue_key,
                url=f"{self.site_url}/browse/{issue_key}",
                status=self.state.get("status", "Done"),
                id=self.state.get("issue_id", "10001"),
                project_key=self.state.get("issue_project_key", "ACME"),
                sprint_id=self.state.get("issue_sprint_id"),
                sprint_name=self.state.get("issue_sprint_name"),
                sprint_state=self.state.get("issue_sprint_state"),
                parent_key=self.state.get("issue_parent_key"),
                due_date=self.state.get("due_date"),
                original_estimate=self.state.get("original_estimate"),
            )

        async def detect_fields(self) -> dict:
            err = self.state.get("detect_fields_error")
            if err is not None:
                status_code, message = err
                raise TrackerError(message, status_code=status_code)
            return self.state.get(
                "detected_fields",
                {"sprint_field_id": None, "epic_link_field_id": None},
            )

        async def list_boards(self, project_key, *, limit=10) -> list[TrackerBoard]:
            if self.state.get("list_boards_timeout"):
                raise asyncio.TimeoutError()
            err = self.state.get("list_boards_error")
            if err is not None:
                status_code, message = err
                raise TrackerError(message, status_code=status_code)
            return list(self.state.get("boards", [TrackerBoard(id="1", name="Board 1")]))

        async def list_sprints(self, board) -> list[TrackerSprint]:
            if self.state.get("list_sprints_timeout"):
                raise asyncio.TimeoutError()
            err = self.state.get("list_sprints_error")
            if err is not None:
                status_code, message = err
                raise TrackerError(message, status_code=status_code)
            sprints = self.state.get("sprints", [])
            if isinstance(sprints, dict):
                # Keyed by board id, so different boards expose different sets.
                sprints = sprints.get(board.id, [])
            return list(sprints)

        async def move_issue_to_sprint(self, issue_key, sprint_id) -> None:
            self.state.setdefault("sprint_moves", []).append((issue_key, sprint_id))
            err = self.state.get("move_sprint_error")
            if err is not None:
                status_code, message = err
                raise TrackerError(message, status_code=status_code)

        async def get_sprint(self, sprint_id) -> TrackerSprint:
            self.state.setdefault("get_sprint_calls", []).append(sprint_id)
            if self.state.get("get_sprint_timeout"):
                raise asyncio.TimeoutError()
            err = self.state.get("get_sprint_error")
            if err is not None:
                status_code, message = err
                raise TrackerError(message, status_code=status_code)
            return TrackerSprint(
                id=str(sprint_id),
                name=self.state.get("sprint_name", "Sprint 42"),
                state=self.state.get("sprint_state", "active"),
                board_name="",
            )

        async def get_issue_summary_and_description(self, issue_key):
            self.calls.setdefault("get_issue_summary_and_description", 0)
            self.calls["get_issue_summary_and_description"] += 1
            if self.state.get("pull_timeout"):
                raise asyncio.TimeoutError()
            err = self.state.get("pull_error")
            if err is not None:
                status_code, message = err
                raise TrackerError(message, status_code=status_code)
            return (
                self.state.get("pull_summary", "Pulled summary"),
                self.state.get(
                    "pull_description",
                    {"type": "doc", "version": 1, "content": [{"type": "paragraph", "content": [{"type": "text", "text": "Pulled body"}]}]},
                ),
            )

        async def add_comment(self, issue_key, adf_body) -> str:
            self.calls["add_comment"] += 1
            return "20001"

        async def transition_issue(self, issue_key, transition_name) -> dict:
            self.calls["transition_issue"] += 1
            return {"id": "31", "name": transition_name}

    monkeypatch.setattr("sessionfs.server.routes.jira.JiraClient", Fake)
    # The link service imports the client from its own module namespace.
    monkeypatch.setattr("sessionfs.server.services.jira_links.JiraClient", Fake)
    # Expose the shared mutable state so a test can script the next call before
    # any client is constructed.
    captured.state = state
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


async def _put_connection(client: AsyncClient, org: Organization, key: str, **overrides) -> dict:
    base = {"site_url": "https://acme.atlassian.net", "account_email": "jira@acme.com",
            "api_token": _TOKEN, "default_template": "agile"}
    resp = await client.put(
        f"/api/v1/orgs/{org.id}/integrations/jira", headers=_hdrs(key), json={**base, **overrides}
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


@pytest.mark.asyncio
async def test_connection_put_detects_and_overrides_sprint_and_epic_fields(
    client: AsyncClient, db_session: AsyncSession, fake_jira
):
    """Save discovers the custom fields; an explicit override wins over detection."""
    org = await _org(db_session)
    _, key = await _member(db_session, org, "admin")
    fake_jira.state["detected_fields"] = {
        "sprint_field_id": "customfield_10010",
        "epic_link_field_id": "customfield_10011",
    }

    body = await _put_connection(client, org, key)
    assert body["sprint_field_id"] == "customfield_10010"
    assert body["epic_link_field_id"] == "customfield_10011"

    # Override the sprint field (epic-link stays detected on the re-save).
    body = await _put_connection(client, org, key, sprint_field_id="customfield_99999")
    assert body["sprint_field_id"] == "customfield_99999"
    assert body["epic_link_field_id"] == "customfield_10011"

    conn = (await db_session.execute(
        select(OrgTrackerConnection).where(OrgTrackerConnection.org_id == org.id)
    )).scalar_one()
    assert conn.sprint_field_id == "customfield_99999"
    assert conn.epic_link_field_id == "customfield_10011"


@pytest.mark.asyncio
async def test_connection_put_explicit_null_clears_sprint_field(
    client: AsyncClient, db_session: AsyncSession, fake_jira
):
    """An explicit ``null`` clears a detected field (no field on the site)."""
    org = await _org(db_session)
    _, key = await _member(db_session, org, "admin")
    fake_jira.state["detected_fields"] = {
        "sprint_field_id": "customfield_10010",
        "epic_link_field_id": None,
    }

    body = await _put_connection(client, org, key)
    assert body["sprint_field_id"] == "customfield_10010"

    body = await _put_connection(client, org, key, sprint_field_id=None)
    assert body["sprint_field_id"] is None
    conn = (await db_session.execute(
        select(OrgTrackerConnection).where(OrgTrackerConnection.org_id == org.id)
    )).scalar_one()
    assert conn.sprint_field_id is None


@pytest.mark.asyncio
async def test_connection_put_omitted_override_keeps_existing_override(
    client: AsyncClient, db_session: AsyncSession, fake_jira
):
    """L3 — re-saving with the override omitted keeps the stored value, even
    when detection would now report a different id."""
    org = await _org(db_session)
    _, key = await _member(db_session, org, "admin")
    fake_jira.state["detected_fields"] = {
        "sprint_field_id": "customfield_10010",
        "epic_link_field_id": "customfield_10011",
    }

    body = await _put_connection(client, org, key)
    assert body["sprint_field_id"] == "customfield_10010"
    assert body["epic_link_field_id"] == "customfield_10011"

    # Manually override the sprint field.
    await _put_connection(client, org, key, sprint_field_id="customfield_99999")

    # Detection now reports different ids; an omitted override must keep the
    # manual value rather than silently reverting to the detected id.
    fake_jira.state["detected_fields"] = {
        "sprint_field_id": "customfield_20020",
        "epic_link_field_id": "customfield_20021",
    }
    body = await _put_connection(client, org, key)
    assert body["sprint_field_id"] == "customfield_99999"
    assert body["epic_link_field_id"] == "customfield_10011"

    conn = (await db_session.execute(
        select(OrgTrackerConnection).where(OrgTrackerConnection.org_id == org.id)
    )).scalar_one()
    assert conn.sprint_field_id == "customfield_99999"
    assert conn.epic_link_field_id == "customfield_10011"


@pytest.mark.asyncio
async def test_connection_put_site_change_redetects_field_ids(
    client: AsyncClient, db_session: AsyncSession, fake_jira
):
    """Custom field ids belong to one Jira site: moving the connection to a
    different site re-detects them instead of keeping the old site's ids."""
    org = await _org(db_session)
    _, key = await _member(db_session, org, "admin")
    fake_jira.state["detected_fields"] = {
        "sprint_field_id": "customfield_10010",
        "epic_link_field_id": "customfield_10011",
    }
    await _put_connection(client, org, key, sprint_field_id="customfield_99999")

    fake_jira.state["detected_fields"] = {
        "sprint_field_id": "customfield_20020",
        "epic_link_field_id": None,
    }
    body = await _put_connection(client, org, key, site_url="https://other.atlassian.net")
    assert body["sprint_field_id"] == "customfield_20020"
    assert body["epic_link_field_id"] is None

    # An explicit value on the same request still wins on a site change.
    body = await _put_connection(
        client, org, key, site_url="https://third.atlassian.net",
        sprint_field_id="customfield_30030",
    )
    assert body["sprint_field_id"] == "customfield_30030"


@pytest.mark.asyncio
async def test_connection_put_rejects_bad_detected_field_id(
    client: AsyncClient, db_session: AsyncSession, fake_jira
):
    """L3 — a detected id that isn't ``customfield_<n>`` falls back to None."""
    org = await _org(db_session)
    _, key = await _member(db_session, org, "admin")
    fake_jira.state["detected_fields"] = {
        "sprint_field_id": "not_a_customfield",
        "epic_link_field_id": "customfield_10011",
    }

    body = await _put_connection(client, org, key)
    assert body["sprint_field_id"] is None
    assert body["epic_link_field_id"] == "customfield_10011"

    conn = (await db_session.execute(
        select(OrgTrackerConnection).where(OrgTrackerConnection.org_id == org.id)
    )).scalar_one()
    assert conn.sprint_field_id is None


@pytest.mark.asyncio
async def test_connection_put_detection_failure_does_not_fail_save(
    client: AsyncClient, db_session: AsyncSession, fake_jira
):
    """L3 — field detection is best-effort; a detection failure must not fail
    the connection save."""
    org = await _org(db_session)
    _, key = await _member(db_session, org, "admin")
    fake_jira.state["detect_fields_error"] = (500, "upstream boom")

    body = await _put_connection(client, org, key)
    assert body["sprint_field_id"] is None
    assert body["epic_link_field_id"] is None

    conn = (await db_session.execute(
        select(OrgTrackerConnection).where(OrgTrackerConnection.org_id == org.id)
    )).scalar_one()
    assert conn.sprint_field_id is None
    assert conn.epic_link_field_id is None


@pytest.mark.asyncio
async def test_connection_put_rejects_bad_sprint_field_id(
    client: AsyncClient, db_session: AsyncSession, fake_jira
):
    org = await _org(db_session)
    _, key = await _member(db_session, org, "admin")
    resp = await client.put(
        f"/api/v1/orgs/{org.id}/integrations/jira", headers=_hdrs(key),
        json={"site_url": "https://acme.atlassian.net", "account_email": "jira@acme.com",
              "api_token": _TOKEN, "default_template": "agile", "sprint_field_id": "not_a_field"},
    )
    assert resp.status_code == 422, resp.text


@pytest.mark.asyncio
async def test_detect_fields_endpoint_returns_both_fields(
    client: AsyncClient, db_session: AsyncSession, fake_jira
):
    org = await _org(db_session)
    user, key = await _member(db_session, org, "admin")
    await _insert_connection(db_session, org, user)
    fake_jira.state["detected_fields"] = {
        "sprint_field_id": "customfield_10010",
        "epic_link_field_id": "customfield_10011",
    }
    resp = await client.post(f"/api/v1/orgs/{org.id}/jira/fields/detect", headers=_hdrs(key))
    assert resp.status_code == 200, resp.text
    assert resp.json() == {
        "sprint_field_id": "customfield_10010",
        "epic_link_field_id": "customfield_10011",
    }


@pytest.mark.asyncio
async def test_detect_fields_endpoint_epic_missing(
    client: AsyncClient, db_session: AsyncSession, fake_jira
):
    org = await _org(db_session)
    user, key = await _member(db_session, org, "admin")
    await _insert_connection(db_session, org, user)
    fake_jira.state["detected_fields"] = {
        "sprint_field_id": "customfield_10010",
        "epic_link_field_id": None,
    }
    resp = await client.post(f"/api/v1/orgs/{org.id}/jira/fields/detect", headers=_hdrs(key))
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"sprint_field_id": "customfield_10010", "epic_link_field_id": None}


@pytest.mark.asyncio
async def test_detect_fields_endpoint_validates_ids(
    client: AsyncClient, db_session: AsyncSession, fake_jira
):
    """L3 — the on-demand detect endpoint falls back to None for a non-standard id."""
    org = await _org(db_session)
    user, key = await _member(db_session, org, "admin")
    await _insert_connection(db_session, org, user)
    fake_jira.state["detected_fields"] = {
        "sprint_field_id": "gh-sprint",
        "epic_link_field_id": "customfield_10011",
    }
    resp = await client.post(f"/api/v1/orgs/{org.id}/jira/fields/detect", headers=_hdrs(key))
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"sprint_field_id": None, "epic_link_field_id": "customfield_10011"}


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
    _unmapped = await _project(db_session, user, org)
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


# ── sprints (GitHub #113 C7) ──


@pytest.mark.asyncio
async def test_list_sprints_dedupes_and_sorts_active_first(
    client: AsyncClient, db_session: AsyncSession, fake_jira
):
    org, _user, key, project, _ticket = await _ready(db_session)
    await _set_conn_fields(db_session, org, sprint="customfield_10010", epic=None)
    fake_jira.state["boards"] = [
        TrackerBoard(id="1", name="Board A"),
        TrackerBoard(id="2", name="Board B"),
    ]
    fake_jira.state["sprints"] = {
        "1": [
            TrackerSprint(id="10", name="Active", state="active", board_name="Board A",
                          start_date="2026-01-01", end_date="2026-01-14"),
            TrackerSprint(id="11", name="Future B", state="future", board_name="Board A",
                          start_date="2026-02-01", end_date="2026-02-14"),
        ],
        "2": [
            # Same sprint id on a second board: keep the first board's name.
            TrackerSprint(id="10", name="Active (dup)", state="active", board_name="Board B",
                          start_date="2026-01-01", end_date="2026-01-14"),
            TrackerSprint(id="12", name="Future A", state="future", board_name="Board B",
                          start_date="2026-01-15", end_date="2026-01-28"),
        ],
    }
    resp = await client.get(f"/api/v1/projects/{project.id}/jira/sprints", headers=_hdrs(key))
    assert resp.status_code == 200, resp.text
    sprints = resp.json()["sprints"]
    assert [s["id"] for s in sprints] == [10, 12, 11]
    assert sprints[0]["name"] == "Active"
    assert sprints[0]["board_name"] == "Board A"
    assert sprints[1]["state"] == "future"


@pytest.mark.asyncio
async def test_list_sprints_unavailable_when_jira_software_missing(
    client: AsyncClient, db_session: AsyncSession, fake_jira
):
    org, _user, key, project, _ticket = await _ready(db_session)
    await _set_conn_fields(db_session, org, sprint="customfield_10010", epic=None)
    fake_jira.state["list_boards_error"] = (404, "no agile boards")
    resp = await client.get(f"/api/v1/projects/{project.id}/jira/sprints", headers=_hdrs(key))
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"sprints": [], "unavailable": "jira_software_not_available"}


@pytest.mark.asyncio
async def test_list_sprints_unavailable_when_sprint_field_unknown(
    client: AsyncClient, db_session: AsyncSession, fake_jira
):
    org, _user, key, project, _ticket = await _ready(db_session)
    # The connection has no sprint field, so the endpoint must short-circuit
    # without calling Jira — a Jira call would trip the scripted timeout below.
    fake_jira.state["list_boards_timeout"] = True
    resp = await client.get(f"/api/v1/projects/{project.id}/jira/sprints", headers=_hdrs(key))
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"sprints": [], "unavailable": "jira_sprint_field_unknown"}


@pytest.mark.asyncio
async def test_list_sprints_timeout_and_upstream_error_map_to_504_and_502(
    client: AsyncClient, db_session: AsyncSession, fake_jira
):
    org, _user, key, project, _ticket = await _ready(db_session)
    await _set_conn_fields(db_session, org, sprint="customfield_10010", epic=None)
    fake_jira.state["list_boards_timeout"] = True
    resp = await client.get(f"/api/v1/projects/{project.id}/jira/sprints", headers=_hdrs(key))
    assert resp.status_code == 504
    assert resp.json()["error"]["details"]["error"] == "jira_timeout"

    del fake_jira.state["list_boards_timeout"]
    fake_jira.state["list_boards_error"] = (500, "upstream boom")
    resp = await client.get(f"/api/v1/projects/{project.id}/jira/sprints", headers=_hdrs(key))
    assert resp.status_code == 502
    assert resp.json()["error"]["details"]["error"] == "jira_upstream_error"


@pytest.mark.asyncio
async def test_list_sprints_reader_403_in_restricted_org(
    client: AsyncClient, db_session: AsyncSession, fake_jira
):
    from sessionfs.server.db.models import (
        ProjectGroup,
        ProjectGroupAccess,
        ProjectGroupMember,
    )

    org, _user, _key, project, _ticket = await _ready(db_session)
    reader, reader_key = await _member(db_session, org, "member")
    group = ProjectGroup(
        id=f"pg_{uuid.uuid4().hex[:12]}", org_id=org.id, slug="g", name="G",
        created_at=datetime.now(timezone.utc), updated_at=datetime.now(timezone.utc),
    )
    db_session.add(group)
    await db_session.flush()
    db_session.add(ProjectGroupAccess(group_id=group.id, project_id=project.id))
    db_session.add(ProjectGroupMember(group_id=group.id, user_id=reader.id, role="reader"))
    org.settings = json.dumps({"general": {"restrict_project_visibility": True}})
    await db_session.commit()
    resp = await client.get(f"/api/v1/projects/{project.id}/jira/sprints", headers=_hdrs(reader_key))
    assert resp.status_code == 403, resp.text
    assert resp.json()["error"]["code"] == "project_edit_forbidden"


@pytest.mark.asyncio
async def test_list_sprints_gated_on_enterprise(
    client: AsyncClient, db_session: AsyncSession, fake_jira
):
    org, _user, key, project, _ticket = await _ready(db_session, tier="free")
    resp = await client.get(f"/api/v1/projects/{project.id}/jira/sprints", headers=_hdrs(key))
    assert resp.status_code == 403
    assert resp.json()["error"]["details"]["error"] == "upgrade_required"


@pytest.mark.asyncio
async def test_list_sprints_unmapped_409(
    client: AsyncClient, db_session: AsyncSession, fake_jira
):
    org = await _org(db_session)
    user, key = await _member(db_session, org, "admin")
    await _insert_connection(db_session, org, user)
    project = await _project(db_session, user, org)
    resp = await client.get(f"/api/v1/projects/{project.id}/jira/sprints", headers=_hdrs(key))
    assert resp.status_code == 409
    assert resp.json()["error"]["details"]["error"] == "jira_project_unmapped"


# ── publish sprint + parent/epic (GitHub #113 C7/C8) ──


async def _set_conn_fields(
    db: AsyncSession, org: Organization, *, sprint: str | None, epic: str | None
) -> None:
    conn = (await db.execute(
        select(OrgTrackerConnection).where(OrgTrackerConnection.org_id == org.id)
    )).scalar_one()
    conn.sprint_field_id = sprint
    conn.epic_link_field_id = epic
    await db.commit()


async def _preview_publish_body(
    client: AsyncClient, project: Project, ticket: Ticket, key: str, **extra: object
) -> dict:
    preview = (await client.post(
        f"/api/v1/projects/{project.id}/tickets/{ticket.id}/jira/preview", headers=_hdrs(key)
    )).json()
    body: dict = {
        "summary": preview["summary"],
        "sections": preview["sections"],
        "confirm": True,
    }
    body.update(extra)
    return body


async def _link_parent(
    db: AsyncSession,
    org: Organization,
    project: Project,
    user: User,
    *,
    parent_key: str,
    parent_project: Project | None = None,
) -> tuple[Ticket, Ticket]:
    """Create a parent ticket (linked to ``parent_key``) + a child referencing it."""
    parent = await _ticket(db, parent_project or project, user)
    db.add(TicketTrackerLink(
        ticket_id=parent.id, org_id=org.id, provider="jira",
        status="linked", site_url="https://acme.atlassian.net",
        issue_key=parent_key, issue_url=f"https://acme.atlassian.net/browse/{parent_key}",
    ))
    await db.commit()
    child = await _ticket(db, project, user)
    child.parent_ticket_id = parent.id
    await db.commit()
    return child, parent


@pytest.mark.asyncio
async def test_publish_create_sets_sprint_field_inline(
    client: AsyncClient, db_session: AsyncSession, fake_jira
):
    org, user, key, project, ticket = await _ready(db_session)
    await _set_conn_fields(db_session, org, sprint="customfield_10010", epic=None)

    body = await _preview_publish_body(client, project, ticket, key, sprint_id=42)
    resp = await client.post(
        f"/api/v1/projects/{project.id}/tickets/{ticket.id}/jira/publish",
        headers=_hdrs(key), json=body,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["warnings"] == []
    # The create path sets the sprint inline via the discovered field.
    assert fake_jira.state["create_extra_fields"][0] == {"customfield_10010": 42}
    assert fake_jira.state.get("sprint_moves", []) == []


@pytest.mark.asyncio
async def test_publish_update_moves_issue_to_sprint(
    client: AsyncClient, db_session: AsyncSession, fake_jira
):
    org, user, key, project, ticket = await _ready(db_session)
    await _set_conn_fields(db_session, org, sprint="customfield_10010", epic=None)

    body = await _preview_publish_body(client, project, ticket, key)
    resp = await client.post(
        f"/api/v1/projects/{project.id}/tickets/{ticket.id}/jira/publish",
        headers=_hdrs(key), json={**body, "sprint_id": 42},
    )
    assert resp.status_code == 200, resp.text

    resp2 = await client.post(
        f"/api/v1/projects/{project.id}/tickets/{ticket.id}/jira/publish",
        headers=_hdrs(key), json={**body, "sprint_id": 43},
    )
    assert resp2.status_code == 200, resp2.text
    assert resp2.json()["warnings"] == []
    # The update path uses the Agile move API, not the inline sprint field.
    assert fake_jira.state["sprint_moves"] == [("ACME-1", 43)]


@pytest.mark.asyncio
async def test_publish_update_sprint_move_failure_warns(
    client: AsyncClient, db_session: AsyncSession, fake_jira
):
    org, user, key, project, ticket = await _ready(db_session)
    await _set_conn_fields(db_session, org, sprint="customfield_10010", epic=None)

    body = await _preview_publish_body(client, project, ticket, key)
    resp = await client.post(
        f"/api/v1/projects/{project.id}/tickets/{ticket.id}/jira/publish",
        headers=_hdrs(key), json={**body, "sprint_id": 42},
    )
    assert resp.status_code == 200, resp.text

    fake_jira.state["move_sprint_error"] = (500, "boom")
    resp2 = await client.post(
        f"/api/v1/projects/{project.id}/tickets/{ticket.id}/jira/publish",
        headers=_hdrs(key), json={**body, "sprint_id": 43},
    )
    assert resp2.status_code == 200, resp2.text
    assert "jira_sprint_not_set:move_failed" in resp2.json()["warnings"]


@pytest.mark.asyncio
async def test_publish_with_sprint_requires_sprint_field(
    client: AsyncClient, db_session: AsyncSession, fake_jira
):
    org, user, key, project, ticket = await _ready(db_session)
    body = await _preview_publish_body(client, project, ticket, key, sprint_id=42)
    resp = await client.post(
        f"/api/v1/projects/{project.id}/tickets/{ticket.id}/jira/publish",
        headers=_hdrs(key), json=body,
    )
    assert resp.status_code == 422, resp.text
    assert resp.json()["error"]["details"]["error"] == "jira_sprint_field_unknown"


@pytest.mark.asyncio
async def test_publish_create_sends_linked_parent(
    client: AsyncClient, db_session: AsyncSession, fake_jira
):
    org, user, key, project, ticket = await _ready(db_session)
    ticket, parent = await _link_parent(db_session, org, project, user, parent_key="PARENT-1")

    body = await _preview_publish_body(client, project, ticket, key)
    resp = await client.post(
        f"/api/v1/projects/{project.id}/tickets/{ticket.id}/jira/publish",
        headers=_hdrs(key), json=body,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["warnings"] == []
    assert fake_jira.state["create_extra_fields"][0] == {"parent": {"key": "PARENT-1"}}


@pytest.mark.asyncio
async def test_publish_create_retries_parent_via_epic_link_field(
    client: AsyncClient, db_session: AsyncSession, fake_jira
):
    org, user, key, project, ticket = await _ready(db_session)
    await _set_conn_fields(db_session, org, sprint=None, epic="customfield_10011")
    ticket, parent = await _link_parent(db_session, org, project, user, parent_key="PARENT-1")

    fake_jira.state["create_script"] = [
        {"error": (400, "parent field is not available on this issue type")},
    ]

    body = await _preview_publish_body(client, project, ticket, key)
    resp = await client.post(
        f"/api/v1/projects/{project.id}/tickets/{ticket.id}/jira/publish",
        headers=_hdrs(key), json=body,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["warnings"] == []
    # The standard parent was rejected; the retry used the epic-link field with
    # the bare key.
    assert fake_jira.state["create_extra_fields"] == [
        {"parent": {"key": "PARENT-1"}},
        {"customfield_10011": "PARENT-1"},
    ]


@pytest.mark.asyncio
async def test_publish_create_parent_both_attempts_fail_warns(
    client: AsyncClient, db_session: AsyncSession, fake_jira
):
    org, user, key, project, ticket = await _ready(db_session)
    await _set_conn_fields(db_session, org, sprint=None, epic="customfield_10011")
    ticket, parent = await _link_parent(db_session, org, project, user, parent_key="PARENT-1")

    fake_jira.state["create_script"] = [
        {"error": (400, "parent field is not available on this issue type")},
        {"error": (400, "Field 'customfield_10011' cannot be set. It is not on the appropriate screen, or unknown.")},
    ]

    body = await _preview_publish_body(client, project, ticket, key)
    resp = await client.post(
        f"/api/v1/projects/{project.id}/tickets/{ticket.id}/jira/publish",
        headers=_hdrs(key), json=body,
    )
    assert resp.status_code == 200, resp.text
    assert "jira_parent_not_set" in resp.json()["warnings"]


@pytest.mark.asyncio
async def test_publish_update_retries_parent_via_epic_link_then_drops(
    client: AsyncClient, db_session: AsyncSession, fake_jira
):
    """M3 — the update path uses the same parent → epic-link → drop ladder and
    warns ``jira_parent_not_set`` when both are refused."""
    org, user, key, project, child = await _ready(db_session)
    await _set_conn_fields(db_session, org, sprint=None, epic="customfield_10011")
    child, parent = await _link_parent(db_session, org, project, user, parent_key="PARENT-1")
    # Link the child so the publish takes the update path; its cached parent is
    # None, so the (changed) parent is sent.
    db_session.add(TicketTrackerLink(
        ticket_id=child.id, org_id=org.id, provider="jira",
        status="linked", site_url="https://acme.atlassian.net",
        issue_key="CHILD-1", issue_url="https://acme.atlassian.net/browse/CHILD-1",
    ))
    await db_session.commit()

    fake_jira.state["update_script"] = [
        {"error": (400, "parent field is not available on this issue type")},
        {"error": (400, "Field 'customfield_10011' cannot be set. It is not on the appropriate screen, or unknown.")},
    ]

    body = await _preview_publish_body(client, project, child, key)
    resp = await client.post(
        f"/api/v1/projects/{project.id}/tickets/{child.id}/jira/publish",
        headers=_hdrs(key), json={**body, "overwrite_existing": True},
    )
    assert resp.status_code == 200, resp.text
    assert "jira_parent_not_set" in resp.json()["warnings"]
    # parent → epic-link → drop, matching the create-path ladder.
    assert fake_jira.state["update_extra_fields"] == [
        {"parent": {"key": "PARENT-1"}},
        {"customfield_10011": "PARENT-1"},
        None,
    ]


@pytest.mark.asyncio
async def test_publish_create_500_with_sprint_and_parent_is_not_retried(
    client: AsyncClient, db_session: AsyncSession, fake_jira
):
    """H1 — a non-400 failure is never retried, even when droppable fields
    (sprint + parent) are set on the create."""
    org, user, key, project, ticket = await _ready(db_session)
    await _set_conn_fields(db_session, org, sprint="customfield_10010", epic="customfield_10011")
    ticket, parent = await _link_parent(db_session, org, project, user, parent_key="PARENT-1")

    fake_jira.state["create_error"] = (500, "upstream boom")

    body = await _preview_publish_body(client, project, ticket, key, sprint_id=42)
    resp = await client.post(
        f"/api/v1/projects/{project.id}/tickets/{ticket.id}/jira/publish",
        headers=_hdrs(key), json=body,
    )
    assert resp.status_code == 502
    assert resp.json()["error"]["details"]["error"] == "jira_upstream_error"
    assert fake_jira[0].calls["create_issue"] == 1


@pytest.mark.asyncio
async def test_publish_create_transport_error_with_sprint_and_parent_is_not_retried(
    client: AsyncClient, db_session: AsyncSession, fake_jira
):
    """H1 — a transport error (status None) is also not retried."""
    org, user, key, project, ticket = await _ready(db_session)
    await _set_conn_fields(db_session, org, sprint="customfield_10010", epic="customfield_10011")
    ticket, parent = await _link_parent(db_session, org, project, user, parent_key="PARENT-1")

    fake_jira.state["create_error"] = (None, "connection reset")

    body = await _preview_publish_body(client, project, ticket, key, sprint_id=42)
    resp = await client.post(
        f"/api/v1/projects/{project.id}/tickets/{ticket.id}/jira/publish",
        headers=_hdrs(key), json=body,
    )
    assert resp.status_code == 502
    assert resp.json()["error"]["details"]["error"] == "jira_upstream_error"
    assert fake_jira[0].calls["create_issue"] == 1


@pytest.mark.asyncio
async def test_publish_create_400_parent_then_502_fails_after_one_retry(
    client: AsyncClient, db_session: AsyncSession, fake_jira
):
    """H1 — a 400 naming parent falls back to the epic-link field exactly once;
    the follow-up 502 is a non-400, so it re-raises and the publish fails."""
    org, user, key, project, ticket = await _ready(db_session)
    await _set_conn_fields(db_session, org, sprint=None, epic="customfield_10011")
    ticket, parent = await _link_parent(db_session, org, project, user, parent_key="PARENT-1")

    fake_jira.state["create_script"] = [
        {"error": (400, "parent field is not available on this issue type")},
        {"error": (502, "bad gateway")},
    ]

    body = await _preview_publish_body(client, project, ticket, key)
    resp = await client.post(
        f"/api/v1/projects/{project.id}/tickets/{ticket.id}/jira/publish",
        headers=_hdrs(key), json=body,
    )
    assert resp.status_code == 502
    assert resp.json()["error"]["details"]["error"] == "jira_upstream_error"
    # parent → epic-link fallback, then the non-400 stops the ladder.
    assert fake_jira[0].calls["create_issue"] == 2
    assert fake_jira.state["create_extra_fields"] == [
        {"parent": {"key": "PARENT-1"}},
        {"customfield_10011": "PARENT-1"},
    ]


@pytest.mark.asyncio
async def test_publish_create_400_naming_non_droppable_field_is_not_retried(
    client: AsyncClient, db_session: AsyncSession, fake_jira
):
    """H1 — a 400 that names no droppable field raises without any retry."""
    org, user, key, project, ticket = await _ready(db_session)
    ticket.due_date = date(2026, 11, 15)
    await db_session.commit()

    fake_jira.state["create_script"] = [
        {"error": (400, "Field 'labels' cannot be set. It is not on the appropriate screen.")},
    ]

    body = await _preview_publish_body(client, project, ticket, key)
    resp = await client.post(
        f"/api/v1/projects/{project.id}/tickets/{ticket.id}/jira/publish",
        headers=_hdrs(key), json=body,
    )
    assert resp.status_code == 502
    assert resp.json()["error"]["details"]["error"] == "jira_upstream_error"
    assert fake_jira[0].calls["create_issue"] == 1


@pytest.mark.asyncio
async def test_publish_create_sequential_scheduling_rejection_drops_both(
    client: AsyncClient, db_session: AsyncSession, fake_jira
):
    """H1 — sequential 400s rejecting each scheduling field in turn drop both."""
    org, user, key, project, ticket = await _ready(db_session)
    ticket.due_date = date(2026, 11, 15)
    ticket.estimated_effort = "2d 4h"
    await db_session.commit()

    fake_jira.state["create_script"] = [
        {"error": (400, "Field 'duedate' cannot be set. It is not on the appropriate screen, or unknown.")},
        {"error": (400, "Field 'timetracking' cannot be set. It is not on the appropriate screen, or unknown.")},
    ]

    resp = await client.post(
        f"/api/v1/projects/{project.id}/tickets/{ticket.id}/jira/publish",
        headers=_hdrs(key), json=_PUBLISH_BODY,
    )
    assert resp.status_code == 200, resp.text
    assert set(resp.json()["warnings"]) == {
        "jira_field_not_on_screen:duedate",
        "jira_field_not_on_screen:timetracking",
    }
    # duedate rejected → dropped; timetracking rejected → dropped; succeeds empty.
    assert fake_jira.state["create_scheduling"] == [
        {"due_date": "2026-11-15", "original_estimate": "2d 4h"},
        {"due_date": None, "original_estimate": "2d 4h"},
        {"due_date": None, "original_estimate": None},
    ]


@pytest.mark.asyncio
async def test_publish_update_500_is_not_retried(
    client: AsyncClient, db_session: AsyncSession, fake_jira
):
    """H1 — the update path stops on a non-400 too, without any retry."""
    org, user, key, project, ticket = await _ready(db_session)
    await _set_conn_fields(db_session, org, sprint=None, epic="customfield_10011")
    ticket, parent = await _link_parent(db_session, org, project, user, parent_key="PARENT-1")
    db_session.add(TicketTrackerLink(
        ticket_id=ticket.id, org_id=org.id, provider="jira",
        status="linked", site_url="https://acme.atlassian.net",
        issue_key="ACME-1", issue_url="https://acme.atlassian.net/browse/ACME-1",
    ))
    await db_session.commit()

    fake_jira.state["update_script"] = [
        {"error": (500, "upstream boom")},
    ]

    body = await _preview_publish_body(client, project, ticket, key)
    resp = await client.post(
        f"/api/v1/projects/{project.id}/tickets/{ticket.id}/jira/publish",
        headers=_hdrs(key), json={**body, "overwrite_existing": True},
    )
    assert resp.status_code == 502
    assert resp.json()["error"]["details"]["error"] == "jira_upstream_error"
    assert fake_jira[0].calls["update_issue"] == 1


@pytest.mark.asyncio
async def test_adopt_retries_parent_via_epic_link_then_drops(
    client: AsyncClient, db_session: AsyncSession, monkeypatch
):
    """M3 — a timed-out create that is later adopted runs the same
    parent → epic-link → drop ladder on the adopt-path update and warns
    ``jira_parent_not_set`` when both are refused."""
    monkeypatch.setattr("sessionfs.server.routes.jira._ADOPT_RETRY_DELAY_SECONDS", 0.0)
    org, user, key, project, ticket = await _ready(db_session)
    await _set_conn_fields(db_session, org, sprint=None, epic="customfield_10011")
    ticket, parent = await _link_parent(db_session, org, project, user, parent_key="PARENT-1")
    calls = {"create": 0, "search": 0, "update": 0}
    update_fields: list[dict | None] = []
    update_script = [
        (400, "parent field is not available on this issue type"),
        (400, "Field 'customfield_10011' cannot be set. It is not on the appropriate screen, or unknown."),
    ]

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
            update_fields.append(k.get("extra_fields"))
            if update_script:
                status_code, message = update_script.pop(0)
                raise TrackerError(message, status_code=status_code)
            return TrackerIssue(
                key=issue_key, url=f"{self.site_url}/browse/{issue_key}", status=""
            )

    monkeypatch.setattr("sessionfs.server.routes.jira.JiraClient", TimeoutThenFoundFake)

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
    assert r2.status_code == 200, r2.text
    assert r2.json()["issue_key"] == "ACME-9"
    assert "jira_parent_not_set" in r2.json()["warnings"]
    # The first timed-out create, one label search, then parent → epic-link →
    # drop on the adopt-path update.
    assert calls == {"create": 1, "search": 1, "update": 3}
    assert update_fields == [
        {"parent": {"key": "PARENT-1"}},
        {"customfield_10011": "PARENT-1"},
        None,
    ]


@pytest.mark.asyncio
async def test_publish_response_returns_sprint_and_parent(
    client: AsyncClient, db_session: AsyncSession, fake_jira
):
    """M4 — the publish response carries the cached sprint + parent in the same
    shape as GET /jira, and the cache is persisted on the link row."""
    org, user, key, project, ticket = await _ready(db_session)
    await _set_conn_fields(db_session, org, sprint="customfield_10010", epic=None)
    ticket, parent = await _link_parent(db_session, org, project, user, parent_key="PARENT-1")

    body = await _preview_publish_body(client, project, ticket, key, sprint_id=42)
    resp = await client.post(
        f"/api/v1/projects/{project.id}/tickets/{ticket.id}/jira/publish",
        headers=_hdrs(key), json=body,
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["warnings"] == []
    # The sprint name/state are fetched from the Agile API right away, not left
    # null until a later pull.
    assert data["sprint"] == {"id": 42, "name": "Sprint 42", "state": "active"}
    assert data["parent"] == {"key": "PARENT-1", "ticket_id": parent.id}

    link = (await db_session.execute(
        select(TicketTrackerLink)
        .where(TicketTrackerLink.ticket_id == ticket.id)
        .execution_options(populate_existing=True)
    )).scalar_one()
    assert link.jira_sprint_id == "42"
    assert link.jira_sprint_name == "Sprint 42"
    assert link.jira_sprint_state == "active"
    assert link.jira_parent_key == "PARENT-1"


@pytest.mark.asyncio
async def test_publish_sprint_detail_failure_leaves_name_state_null(
    client: AsyncClient, db_session: AsyncSession, fake_jira
):
    """M4b — a failed Agile sprint read after a successful sprint set is
    swallowed: the publish still succeeds, no warning is added, and the cached
    name/state stay null (they'll be filled by a later pull)."""
    org, user, key, project, ticket = await _ready(db_session)
    await _set_conn_fields(db_session, org, sprint="customfield_10010", epic=None)

    fake_jira.state["get_sprint_error"] = (500, "sprint read boom")
    body = await _preview_publish_body(client, project, ticket, key, sprint_id=42)
    resp = await client.post(
        f"/api/v1/projects/{project.id}/tickets/{ticket.id}/jira/publish",
        headers=_hdrs(key), json=body,
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["warnings"] == []
    assert data["sprint"] == {"id": 42, "name": None, "state": None}

    link = (await db_session.execute(
        select(TicketTrackerLink)
        .where(TicketTrackerLink.ticket_id == ticket.id)
        .execution_options(populate_existing=True)
    )).scalar_one()
    assert link.jira_sprint_id == "42"
    assert link.jira_sprint_name is None
    assert link.jira_sprint_state is None


@pytest.mark.asyncio
async def test_publish_create_unlinked_parent_warns(
    client: AsyncClient, db_session: AsyncSession, fake_jira
):
    org, user, key, project, ticket = await _ready(db_session)
    parent = await _ticket(db_session, project, user)
    ticket.parent_ticket_id = parent.id
    await db_session.commit()

    body = await _preview_publish_body(client, project, ticket, key)
    resp = await client.post(
        f"/api/v1/projects/{project.id}/tickets/{ticket.id}/jira/publish",
        headers=_hdrs(key), json=body,
    )
    assert resp.status_code == 200, resp.text
    assert "jira_parent_not_linked" in resp.json()["warnings"]
    # No parent was sent to Jira at all.
    assert fake_jira.state["create_extra_fields"][0] is None


@pytest.mark.asyncio
async def test_get_link_surfaces_parent_key_and_ticket_id_when_readable(
    client: AsyncClient, db_session: AsyncSession, fake_jira
):
    org, user, key, project, ticket = await _ready(db_session)
    parent = await _ticket(db_session, project, user)
    db_session.add(TicketTrackerLink(
        ticket_id=parent.id, org_id=org.id, provider="jira",
        status="linked", site_url="https://acme.atlassian.net",
        issue_key="PARENT-1", issue_url="https://acme.atlassian.net/browse/PARENT-1",
    ))
    await db_session.commit()

    fake_jira.state["issue_parent_key"] = "PARENT-1"
    resp = await client.post(
        f"/api/v1/projects/{project.id}/tickets/{ticket.id}/jira/link",
        headers=_hdrs(key), json={"issue_key": "CHILD-1"},
    )
    assert resp.status_code == 200, resp.text

    resp = await client.get(
        f"/api/v1/projects/{project.id}/tickets/{ticket.id}/jira", headers=_hdrs(key)
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["parent"] == {"key": "PARENT-1", "ticket_id": parent.id}


@pytest.mark.asyncio
async def test_get_link_parent_ticket_id_null_when_not_readable(
    client: AsyncClient, db_session: AsyncSession, fake_jira
):
    org, admin, key, project, ticket = await _ready(db_session)
    other = await _project(db_session, admin, org)
    parent = await _ticket(db_session, other, admin)
    db_session.add(TicketTrackerLink(
        ticket_id=parent.id, org_id=org.id, provider="jira",
        status="linked", site_url="https://acme.atlassian.net",
        issue_key="PARENT-1", issue_url="https://acme.atlassian.net/browse/PARENT-1",
    ))
    await db_session.commit()

    fake_jira.state["issue_parent_key"] = "PARENT-1"
    resp = await client.post(
        f"/api/v1/projects/{project.id}/tickets/{ticket.id}/jira/link",
        headers=_hdrs(key), json={"issue_key": "CHILD-1"},
    )
    assert resp.status_code == 200, resp.text

    # A service key scoped to the child project can read the child link but not
    # the parent's project, so the parent key is surfaced without a ticket id.
    svc_key = await _service_key(
        db_session, org, admin, ["tickets:read"], project_ids=[project.id]
    )
    resp = await client.get(
        f"/api/v1/projects/{project.id}/tickets/{ticket.id}/jira", headers=_hdrs(svc_key)
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["parent"] == {"key": "PARENT-1", "ticket_id": None}


@pytest.mark.asyncio
async def test_inbound_webhook_sprint_changelog_refreshes_cache(
    client: AsyncClient, db_session: AsyncSession, fake_jira
):
    from sessionfs.server.llm_keys import encrypt_secret

    org, user, key, project, ticket = await _ready(db_session)
    conn = (await db_session.execute(
        select(OrgTrackerConnection).where(OrgTrackerConnection.org_id == org.id)
    )).scalar_one()
    conn.sprint_field_id = "customfield_10010"
    conn.sync_status = True
    conn.encrypted_webhook_secret = encrypt_secret(_WEBHOOK_SECRET)
    conn.webhook_secret_hash = hashlib.sha256(_WEBHOOK_SECRET.encode()).hexdigest()
    conn.webhook_secret_last4 = _WEBHOOK_SECRET[-4:]
    await db_session.commit()

    resp = await client.post(
        f"/api/v1/projects/{project.id}/tickets/{ticket.id}/jira/link",
        headers=_hdrs(key), json={"issue_key": "ACME-1"},
    )
    assert resp.status_code == 200, resp.text

    payload = _webhook_payload(
        "jira:issue_updated",
        user={"displayName": "Bob", "accountId": "acc-bob"},
        changelog={
            "id": "20002",
            "items": [{"field": "Sprint", "fieldId": "customfield_10010",
                       "fromString": "", "toString": "Sprint 1"}],
        },
        issue={
            "key": "ACME-1",
            "id": "10001",
            "fields": {
                "customfield_10010": [
                    {"id": "10", "name": "Sprint 1", "state": "active"},
                ],
            },
        },
    )
    status, _body = await _post_webhook(client, org.id, payload)
    assert status == 200

    link = (await db_session.execute(
        select(TicketTrackerLink).where(TicketTrackerLink.ticket_id == ticket.id)
    )).scalar_one()
    assert link.jira_sprint_id == "10"
    assert link.jira_sprint_name == "Sprint 1"
    assert link.jira_sprint_state == "active"


@pytest.mark.asyncio
async def test_inbound_webhook_sprint_removal_clears_cache(
    client: AsyncClient, db_session: AsyncSession, fake_jira
):
    """L6 — a sprint changelog removing the issue from all sprints clears the
    cached sprint on the link."""
    from sessionfs.server.llm_keys import encrypt_secret

    org, user, key, project, ticket = await _ready(db_session)
    conn = (await db_session.execute(
        select(OrgTrackerConnection).where(OrgTrackerConnection.org_id == org.id)
    )).scalar_one()
    conn.sprint_field_id = "customfield_10010"
    conn.sync_status = True
    conn.encrypted_webhook_secret = encrypt_secret(_WEBHOOK_SECRET)
    conn.webhook_secret_hash = hashlib.sha256(_WEBHOOK_SECRET.encode()).hexdigest()
    conn.webhook_secret_last4 = _WEBHOOK_SECRET[-4:]
    await db_session.commit()

    resp = await client.post(
        f"/api/v1/projects/{project.id}/tickets/{ticket.id}/jira/link",
        headers=_hdrs(key), json={"issue_key": "ACME-1"},
    )
    assert resp.status_code == 200, resp.text

    link = (await db_session.execute(
        select(TicketTrackerLink).where(TicketTrackerLink.ticket_id == ticket.id)
    )).scalar_one()
    link.jira_sprint_id = "10"
    link.jira_sprint_name = "Sprint 1"
    link.jira_sprint_state = "active"
    await db_session.commit()

    payload = _webhook_payload(
        "jira:issue_updated",
        user={"displayName": "Bob", "accountId": "acc-bob"},
        changelog={
            "id": "20003",
            "items": [{"field": "Sprint", "fieldId": "customfield_10010",
                       "fromString": "Sprint 1", "toString": ""}],
        },
        issue={
            "key": "ACME-1",
            "id": "10001",
            "fields": {"customfield_10010": []},
        },
    )
    status, _body = await _post_webhook(client, org.id, payload)
    assert status == 200

    link = (await db_session.execute(
        select(TicketTrackerLink)
        .where(TicketTrackerLink.ticket_id == ticket.id)
        .execution_options(populate_existing=True)
    )).scalar_one()
    assert link.jira_sprint_id is None
    assert link.jira_sprint_name is None
    assert link.jira_sprint_state is None


@pytest.mark.asyncio
async def test_inbound_webhook_sprint_changelog_matches_by_field_name(
    client: AsyncClient, db_session: AsyncSession, fake_jira
):
    """L6 — a changelog item carrying only ``field == "Sprint"`` (no fieldId)
    still refreshes the cached sprint."""
    from sessionfs.server.llm_keys import encrypt_secret

    org, user, key, project, ticket = await _ready(db_session)
    conn = (await db_session.execute(
        select(OrgTrackerConnection).where(OrgTrackerConnection.org_id == org.id)
    )).scalar_one()
    conn.sprint_field_id = "customfield_10010"
    conn.sync_status = True
    conn.encrypted_webhook_secret = encrypt_secret(_WEBHOOK_SECRET)
    conn.webhook_secret_hash = hashlib.sha256(_WEBHOOK_SECRET.encode()).hexdigest()
    conn.webhook_secret_last4 = _WEBHOOK_SECRET[-4:]
    await db_session.commit()

    resp = await client.post(
        f"/api/v1/projects/{project.id}/tickets/{ticket.id}/jira/link",
        headers=_hdrs(key), json={"issue_key": "ACME-1"},
    )
    assert resp.status_code == 200, resp.text

    payload = _webhook_payload(
        "jira:issue_updated",
        user={"displayName": "Bob", "accountId": "acc-bob"},
        changelog={
            "id": "20004",
            "items": [{"field": "Sprint", "fromString": "", "toString": "Sprint 2"}],
        },
        issue={
            "key": "ACME-1",
            "id": "10001",
            "fields": {
                "customfield_10010": [
                    {"id": "20", "name": "Sprint 2", "state": "future"},
                ],
            },
        },
    )
    status, _body = await _post_webhook(client, org.id, payload)
    assert status == 200

    link = (await db_session.execute(
        select(TicketTrackerLink).where(TicketTrackerLink.ticket_id == ticket.id)
    )).scalar_one()
    assert link.jira_sprint_id == "20"
    assert link.jira_sprint_name == "Sprint 2"
    assert link.jira_sprint_state == "future"


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

    # Each publish records one timeline event, attributed to the clicking user.
    published = (await db_session.execute(
        select(TicketEvent).where(TicketEvent.ticket_id == ticket.id)
    )).scalars().all()
    assert [e.action for e in published] == ["jira_published", "jira_published"]
    assert {e.actor_type for e in published} == {"user"}
    assert {e.actor_user_id for e in published} == {user.id}
    assert {json.loads(e.after_value)["issue_key"] for e in published} == {"ACME-1"}
    # Token never leaks into audit.
    for e in (
        await db_session.execute(select(OrgAuditEvent).where(OrgAuditEvent.org_id == org.id))
    ).scalars().all():
        assert _TOKEN not in (e.after or "")
        assert _TOKEN not in (e.before or "")


@pytest.mark.asyncio
async def test_publish_sends_due_date_and_estimate(
    client: AsyncClient, db_session: AsyncSession, fake_jira
):
    org, user, key, project, ticket = await _ready(db_session)
    ticket.due_date = date(2026, 11, 15)
    ticket.estimated_effort = "2d 4h"
    await db_session.commit()

    resp = await client.post(
        f"/api/v1/projects/{project.id}/tickets/{ticket.id}/jira/publish",
        headers=_hdrs(key), json=_PUBLISH_BODY,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["issue_key"] == "ACME-1"

    # The create pushed the ticket's scheduling fields to Jira.
    assert fake_jira.state["create_scheduling"] == [
        {"due_date": "2026-11-15", "original_estimate": "2d 4h"}
    ]


@pytest.mark.asyncio
async def test_publish_retries_without_scheduling_fields_when_off_screen(
    client: AsyncClient, db_session: AsyncSession, fake_jira
):
    org, user, key, project, ticket = await _ready(db_session)
    ticket.due_date = date(2026, 11, 15)
    ticket.estimated_effort = "2d 4h"
    await db_session.commit()

    # First outbound call is rejected with the classic field-not-on-screen 400
    # naming only duedate, so only duedate is warned about.
    fake_jira.state["scheduling_400"] = True

    resp = await client.post(
        f"/api/v1/projects/{project.id}/tickets/{ticket.id}/jira/publish",
        headers=_hdrs(key), json=_PUBLISH_BODY,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["warnings"] == ["jira_field_not_on_screen:duedate"]

    # The retry dropped only the refused duedate; the estimate still went out.
    assert fake_jira.state["create_scheduling"] == [
        {"due_date": "2026-11-15", "original_estimate": "2d 4h"},
        {"due_date": None, "original_estimate": "2d 4h"},
    ]


@pytest.mark.asyncio
async def test_publish_scheduling_warning_names_only_rejected_field(
    client: AsyncClient, db_session: AsyncSession, fake_jira
):
    org, user, key, project, ticket = await _ready(db_session)
    ticket.due_date = date(2026, 11, 15)
    ticket.estimated_effort = "2d 4h"
    await db_session.commit()

    # The 400 names only timetracking, so duedate is not warned about.
    fake_jira.state["scheduling_400"] = True
    fake_jira.state["scheduling_400_message"] = (
        "Field 'timetracking' cannot be set. It is not on the appropriate screen, or unknown."
    )

    resp = await client.post(
        f"/api/v1/projects/{project.id}/tickets/{ticket.id}/jira/publish",
        headers=_hdrs(key), json=_PUBLISH_BODY,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["warnings"] == ["jira_field_not_on_screen:timetracking"]
    # Only the refused estimate is dropped: the due date still reaches Jira.
    assert fake_jira.state["create_scheduling"] == [
        {"due_date": "2026-11-15", "original_estimate": "2d 4h"},
        {"due_date": "2026-11-15", "original_estimate": None},
    ]


@pytest.mark.asyncio
async def test_publish_scheduling_warning_labels_invalid_value(
    client: AsyncClient, db_session: AsyncSession, fake_jira
):
    org, user, key, project, ticket = await _ready(db_session)
    ticket.due_date = date(2026, 11, 15)
    ticket.estimated_effort = "2d 4h"
    await db_session.commit()

    # A 400 rejecting the estimate's value (not the field being off-screen)
    # must not be mislabelled as "not on screen".
    fake_jira.state["scheduling_400"] = True
    fake_jira.state["scheduling_400_message"] = (
        "Jira API 400: timetracking: Operation value must be a number"
    )

    resp = await client.post(
        f"/api/v1/projects/{project.id}/tickets/{ticket.id}/jira/publish",
        headers=_hdrs(key), json=_PUBLISH_BODY,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["warnings"] == ["jira_invalid_value:timetracking"]


@pytest.mark.asyncio
async def test_pull_imports_due_date_and_estimate(
    client: AsyncClient, db_session: AsyncSession, fake_jira
):
    org, user, key, project, ticket = await _ready(db_session)
    resp = await client.post(
        f"/api/v1/projects/{project.id}/tickets/{ticket.id}/jira/link",
        headers=_hdrs(key), json={"issue_key": "ACME-1"},
    )
    assert resp.status_code == 200, resp.text

    fake_jira.state["due_date"] = "2026-11-20"
    fake_jira.state["original_estimate"] = "1w 2d"

    resp = await client.post(
        f"/api/v1/projects/{project.id}/tickets/{ticket.id}/jira/pull",
        headers=_hdrs(key), json={"confirm": True},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert set(body["updated_fields"]) == {
        "title", "description", "due_date", "estimated_effort",
    }

    ticket = await db_session.get(Ticket, ticket.id, populate_existing=True)
    assert ticket.due_date == date(2026, 11, 20)
    assert ticket.estimated_effort == "1w 2d"

    audits = (await db_session.execute(
        select(OrgAuditEvent).where(OrgAuditEvent.org_id == org.id)
    )).scalars().all()
    pulled = [e for e in audits if e.event_type == "jira_pulled"]
    assert len(pulled) == 1
    assert set(json.loads(pulled[0].after)["updated_fields"]) == {
        "title", "description", "due_date", "estimated_effort",
    }

    # The pull attributes every change to Jira, with the clicking user kept as
    # provenance in actor_user_id (L4).
    events = (await db_session.execute(
        select(TicketEvent).where(TicketEvent.ticket_id == ticket.id)
    )).scalars().all()
    by_field = {e.field: e for e in events}
    due = by_field["due_date"]
    assert due.action == "due_date_set"
    assert due.actor_type == "jira"
    assert due.actor_user_id == user.id
    assert json.loads(due.after_value) == "2026-11-20"
    estimate = by_field["estimated_effort"]
    assert estimate.action == "estimate_set"
    assert estimate.actor_type == "jira"
    assert estimate.actor_user_id == user.id
    assert json.loads(estimate.after_value) == "1w 2d"
    for field in ("title", "description"):
        edited = by_field[field]
        assert edited.action == "field_edited"
        assert edited.actor_type == "jira"
        assert edited.actor_user_id == user.id
    pulled_events = [e for e in events if e.action == "jira_pulled"]
    assert len(pulled_events) == 1
    assert pulled_events[0].actor_type == "jira"
    assert json.loads(pulled_events[0].after_value) == {"issue_key": "ACME-1"}


@pytest.mark.asyncio
async def test_pull_scheduling_only_bumps_updated_at(
    client: AsyncClient, db_session: AsyncSession, fake_jira
):
    org, user, key, project, ticket = await _ready(db_session)
    resp = await client.post(
        f"/api/v1/projects/{project.id}/tickets/{ticket.id}/jira/link",
        headers=_hdrs(key), json={"issue_key": "ACME-1"},
    )
    assert resp.status_code == 200, resp.text

    # Match the remote text so the pull changes only the due date. A
    # scheduling-only pull must still bump updated_at (L4).
    ticket.title = "Pulled summary"
    ticket.description = "Pulled body"
    await db_session.commit()
    before = (await db_session.get(Ticket, ticket.id, populate_existing=True)).updated_at

    fake_jira.state["due_date"] = "2026-11-20"
    resp = await client.post(
        f"/api/v1/projects/{project.id}/tickets/{ticket.id}/jira/pull",
        headers=_hdrs(key), json={"confirm": True},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["updated_fields"] == ["due_date"]

    ticket = await db_session.get(Ticket, ticket.id, populate_existing=True)
    assert ticket.due_date == date(2026, 11, 20)
    assert ticket.updated_at >= before


@pytest.mark.asyncio
async def test_webhook_duedate_applies(
    client: AsyncClient, db_session: AsyncSession, fake_jira
):
    from sessionfs.server.llm_keys import encrypt_secret

    org, user, key, project, ticket = await _ready(db_session)
    conn = (await db_session.execute(
        select(OrgTrackerConnection).where(OrgTrackerConnection.org_id == org.id)
    )).scalar_one()
    conn.sync_status = True
    conn.encrypted_webhook_secret = encrypt_secret(_WEBHOOK_SECRET)
    conn.webhook_secret_hash = hashlib.sha256(_WEBHOOK_SECRET.encode()).hexdigest()
    conn.webhook_secret_last4 = _WEBHOOK_SECRET[-4:]
    await db_session.commit()

    resp = await client.post(
        f"/api/v1/projects/{project.id}/tickets/{ticket.id}/jira/link",
        headers=_hdrs(key), json={"issue_key": "ACME-1"},
    )
    assert resp.status_code == 200, resp.text
    before = (await db_session.get(Ticket, ticket.id, populate_existing=True)).updated_at

    payload = _webhook_payload(
        "jira:issue_updated",
        user={"displayName": "Bob", "accountId": "acc-bob"},
        changelog={
            "id": "20003",
            "items": [
                {"field": "duedate", "fieldId": "duedate",
                 "from": "2026-11-24", "fromString": "2026-11-24 00:00:00.0",
                 "to": "2026-11-25", "toString": "2026-11-25 00:00:00.0"},
            ],
        },
        issue={"key": "ACME-1", "id": "10001"},
    )
    status, _body = await _post_webhook(client, org.id, payload)
    assert status == 200

    ticket = await db_session.get(Ticket, ticket.id, populate_existing=True)
    assert ticket.due_date == date(2026, 11, 25)
    assert ticket.updated_at >= before

    due_events = (await db_session.execute(
        select(TicketEvent).where(
            TicketEvent.ticket_id == ticket.id, TicketEvent.field == "due_date"
        )
    )).scalars().all()
    assert len(due_events) == 1
    assert due_events[0].action == "due_date_set"
    assert due_events[0].actor_type == "jira"
    assert due_events[0].actor_user_id is None
    assert json.loads(due_events[0].after_value) == "2026-11-25"


@pytest.mark.asyncio
async def test_webhook_duedate_null_clears(
    client: AsyncClient, db_session: AsyncSession, fake_jira
):
    from sessionfs.server.llm_keys import encrypt_secret

    org, user, key, project, ticket = await _ready(db_session)
    ticket.due_date = date(2026, 11, 25)
    await db_session.commit()
    conn = (await db_session.execute(
        select(OrgTrackerConnection).where(OrgTrackerConnection.org_id == org.id)
    )).scalar_one()
    conn.sync_status = True
    conn.encrypted_webhook_secret = encrypt_secret(_WEBHOOK_SECRET)
    conn.webhook_secret_hash = hashlib.sha256(_WEBHOOK_SECRET.encode()).hexdigest()
    conn.webhook_secret_last4 = _WEBHOOK_SECRET[-4:]
    await db_session.commit()

    resp = await client.post(
        f"/api/v1/projects/{project.id}/tickets/{ticket.id}/jira/link",
        headers=_hdrs(key), json={"issue_key": "ACME-1"},
    )
    assert resp.status_code == 200, resp.text

    # Real Jira clears the field with a null ``to``/``toString``.
    payload = _webhook_payload(
        "jira:issue_updated",
        user={"displayName": "Bob", "accountId": "acc-bob"},
        changelog={
            "id": "20004",
            "items": [
                {"field": "duedate", "fieldId": "duedate",
                 "from": "2026-11-25", "fromString": "2026-11-25 00:00:00.0",
                 "to": None, "toString": None},
            ],
        },
        issue={"key": "ACME-1", "id": "10001"},
    )
    status, _body = await _post_webhook(client, org.id, payload)
    assert status == 200

    ticket = await db_session.get(Ticket, ticket.id, populate_existing=True)
    assert ticket.due_date is None


@pytest.mark.asyncio
async def test_webhook_duedate_ignored_when_sync_status_off(
    client: AsyncClient, db_session: AsyncSession, fake_jira
):
    from sessionfs.server.llm_keys import encrypt_secret

    org, user, key, project, ticket = await _ready(db_session)
    ticket.due_date = date(2026, 11, 24)
    await db_session.commit()
    # Webhook auth is configured, but status sync is left off.
    conn = (await db_session.execute(
        select(OrgTrackerConnection).where(OrgTrackerConnection.org_id == org.id)
    )).scalar_one()
    conn.sync_status = False
    conn.encrypted_webhook_secret = encrypt_secret(_WEBHOOK_SECRET)
    conn.webhook_secret_hash = hashlib.sha256(_WEBHOOK_SECRET.encode()).hexdigest()
    conn.webhook_secret_last4 = _WEBHOOK_SECRET[-4:]
    await db_session.commit()

    resp = await client.post(
        f"/api/v1/projects/{project.id}/tickets/{ticket.id}/jira/link",
        headers=_hdrs(key), json={"issue_key": "ACME-1"},
    )
    assert resp.status_code == 200, resp.text

    payload = _webhook_payload(
        "jira:issue_updated",
        user={"displayName": "Bob", "accountId": "acc-bob"},
        changelog={
            "id": "20005",
            "items": [
                {"field": "duedate", "fieldId": "duedate",
                 "from": "2026-11-24", "fromString": "2026-11-24 00:00:00.0",
                 "to": "2026-11-25", "toString": "2026-11-25 00:00:00.0"},
            ],
        },
        issue={"key": "ACME-1", "id": "10001"},
    )
    status, _body = await _post_webhook(client, org.id, payload)
    assert status == 200

    ticket = await db_session.get(Ticket, ticket.id, populate_existing=True)
    assert ticket.due_date == date(2026, 11, 24)
    # No timeline event was written for the ignored field.
    due_events = (await db_session.execute(
        select(TicketEvent).where(
            TicketEvent.ticket_id == ticket.id, TicketEvent.field == "due_date"
        )
    )).scalars().all()
    assert due_events == []


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

    monkeypatch.setattr("sessionfs.server.routes.jira._ADOPT_RETRY_DELAY_SECONDS", 0.0)
    engine, factory, app, project_id, ticket_id, key = await _file_backed_app(
        tmp_path, monkeypatch, seed_pending=True
    )
    created = {"count": 0}

    class SlowFake:
        def __init__(self, site_url, account_email, api_token, *, transport=None):
            self.site_url = validate_site_url(site_url)

        async def aclose(self):
            pass

        async def search_issue_by_label(self, project_key, label):
            return None  # nothing survived; fall through to a create

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

    monkeypatch.setattr("sessionfs.server.routes.jira._ADOPT_RETRY_DELAY_SECONDS", 0.0)
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

        async def search_issue_by_label(self, project_key, label):
            return None  # nothing survived; fall through to a create

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

    monkeypatch.setattr("sessionfs.server.routes.jira._ADOPT_RETRY_DELAY_SECONDS", 0.0)
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

        async def search_issue_by_label(self, project_key, label):
            return None  # nothing survived; fall through to a create

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

    # The stale claim is now reclaimed as an *adopt*: A's failed create is an
    # upstream error (kept as unknown, fenced on its own token), not a claim
    # release, and B's newer claim is left untouched.
    assert resp.status_code == 502
    assert resp.json()["error"]["details"]["error"] == "jira_upstream_error"

    async with factory() as db:
        link = (await db.execute(
            select(TicketTrackerLink).where(TicketTrackerLink.ticket_id == ticket_id)
        )).scalar_one()
        # A's fenced mark-unknown did not touch B's claim.
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

        async def create_issue(self, project_key, issue_type, summary, description_adf, *, labels=None, extra_fields=None):
            captured["adf"] = description_adf
            captured["labels"] = labels
            captured["extra_fields"] = extra_fields
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
async def test_adopt_searches_persisted_project_after_mapping_drift(
    client, db_session, monkeypatch
):
    """M2 — a timed-out create's retry searches the PERSISTED project (stored on
    the link), not a mapping that drifted since, so it adopts instead of
    duplicating into a different project."""
    org, user, key, project, ticket = await _ready(db_session)
    calls = {"create": 0, "search": [], "update": 0}

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
            calls["search"].append(project_key)
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

    link = (await db_session.execute(
        select(TicketTrackerLink).where(TicketTrackerLink.ticket_id == ticket.id)
    )).scalar_one()
    assert link.status == "unknown"
    assert link.jira_project_key == "ACME"  # persisted at claim time
    await db_session.commit()  # release the read snapshot

    # Drift the mapping so the retry's *effective* project would differ from the
    # project the original (timed-out) create actually used.
    mapping = (await db_session.execute(
        select(TrackerProjectMapping).where(TrackerProjectMapping.project_id == project.id)
    )).scalar_one()
    mapping.jira_project_key = "OTHER"
    await db_session.commit()

    r2 = await client.post(
        f"/api/v1/projects/{project.id}/tickets/{ticket.id}/jira/publish", headers=_hdrs(key),
        json=_PUBLISH_BODY,
    )
    assert r2.status_code == 200, r2.text
    assert r2.json()["issue_key"] == "ACME-9"
    # The adopt search ran against the original project, not the drifted "OTHER".
    assert calls == {"create": 1, "search": ["ACME"], "update": 1}

    link = (await db_session.execute(
        select(TicketTrackerLink)
        .where(TicketTrackerLink.ticket_id == ticket.id)
        .execution_options(populate_existing=True)
    )).scalar_one()
    assert link.status == "linked"
    assert link.jira_project_key == "ACME"
    assert link.issue_key == "ACME-9"


@pytest.mark.asyncio
async def test_unknown_since_survives_failed_adopts_until_linked(
    client: AsyncClient, db_session: AsyncSession, monkeypatch
):
    """M1 — the durable ``unknown_since`` (not the refreshed claim clock) is the
    adopt cut-off. A timed-out create records it; failed adopt retries (timeout,
    then a 5xx) never overwrite it; the final adopt accepts an issue created at
    the original time even though minutes have passed; linking clears it."""
    org, user, key, project, ticket = await _ready(db_session)

    # Freeze the route's clock so the failed retries advance real minutes past
    # the original publish without waiting, and so the reverted code (which
    # reads the refreshed ``last_pushed_at`` as the cut-off) must reject the
    # issue created at the original time.
    clock = {"now": datetime.now(timezone.utc)}

    class _Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock["now"]

    monkeypatch.setattr("sessionfs.server.routes.jira.datetime", _Frozen)

    calls = {"create": 0, "search": 0, "update": 0}
    phase = {"step": "create_timeout"}
    created_at = {"value": None}

    class FencingFake:
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
            if phase["step"] == "adopt_timeout":
                raise asyncio.TimeoutError
            if phase["step"] == "adopt_5xx":
                raise TrackerError("Jira API 500: boom", status_code=500)
            return TrackerIssue(
                key="ACME-9",
                url=f"{self.site_url}/browse/ACME-9",
                status="",
                id="10009",
                project_key="ACME",
                creator_account_id="a1",
                reporter_account_id="a1",
                created_at=created_at["value"],
            )

        async def update_issue(self, issue_key, *a, **k):
            calls["update"] += 1
            return TrackerIssue(key=issue_key, url=f"{self.site_url}/browse/{issue_key}", status="")

    monkeypatch.setattr("sessionfs.server.routes.jira.JiraClient", FencingFake)

    async def _link() -> TicketTrackerLink:
        return (await db_session.execute(
            select(TicketTrackerLink)
            .where(TicketTrackerLink.ticket_id == ticket.id)
            .execution_options(populate_existing=True)
        )).scalar_one()

    # 1. Fresh create times out: the row becomes unknown and records T1.
    r1 = await client.post(
        f"/api/v1/projects/{project.id}/tickets/{ticket.id}/jira/publish",
        headers=_hdrs(key), json=_PUBLISH_BODY,
    )
    assert r1.status_code == 504
    link = await _link()
    assert link.status == "unknown"
    assert link.unknown_since is not None
    t1 = link.unknown_since
    created_at["value"] = (t1 + timedelta(seconds=1)).isoformat()
    await db_session.commit()  # release the read snapshot for the next publish

    # 2. Adopt retry times out: unknown_since must stay T1 (the first attempt).
    clock["now"] += timedelta(minutes=10)
    phase["step"] = "adopt_timeout"
    r2 = await client.post(
        f"/api/v1/projects/{project.id}/tickets/{ticket.id}/jira/publish",
        headers=_hdrs(key), json=_PUBLISH_BODY,
    )
    assert r2.status_code == 504
    link = await _link()
    assert link.status == "unknown"
    assert link.unknown_since == t1
    await db_session.commit()

    # 3. Adopt retry gets a 5xx: unknown_since must still stay T1.
    clock["now"] += timedelta(minutes=10)
    phase["step"] = "adopt_5xx"
    r3 = await client.post(
        f"/api/v1/projects/{project.id}/tickets/{ticket.id}/jira/publish",
        headers=_hdrs(key), json=_PUBLISH_BODY,
    )
    assert r3.status_code == 502
    link = await _link()
    assert link.status == "unknown"
    assert link.unknown_since == t1
    await db_session.commit()

    # 4. Final adopt accepts the issue created at T1 (the original cut-off, not
    #    the refreshed claim time) and linking clears unknown_since.
    clock["now"] += timedelta(minutes=10)
    phase["step"] = "adopt_ok"
    r4 = await client.post(
        f"/api/v1/projects/{project.id}/tickets/{ticket.id}/jira/publish",
        headers=_hdrs(key), json=_PUBLISH_BODY,
    )
    assert r4.status_code == 200, r4.text
    assert r4.json()["issue_key"] == "ACME-9"
    assert calls == {"create": 1, "search": 3, "update": 1}

    link = await _link()
    assert link.status == "linked"
    assert link.unknown_since is None


@pytest.mark.asyncio
async def test_stale_pending_claim_adopts_survived_issue_not_duplicates(
    client: AsyncClient, db_session: AsyncSession, monkeypatch
):
    """L2 — a dead worker's stale pending claim whose create reached Jira is
    reclaimed as an adopt (search by label first), never a blind create."""
    org, user, key, project, ticket = await _ready(db_session)
    # A stale (>5 min) pending claim, as if a worker died mid-create.
    db_session.add(TicketTrackerLink(
        ticket_id=ticket.id,
        org_id=org.id,
        provider="jira",
        status="pending",
        claim_token="stale-token",
        site_url="https://acme.atlassian.net",
        jira_project_key="ACME",
        last_pushed_at=datetime.now(timezone.utc) - timedelta(minutes=10),
    ))
    await db_session.commit()

    calls = {"create": 0, "search": [], "update": 0}

    class AdoptingFake:
        def __init__(self, site_url, account_email, api_token, *, transport=None):
            self.site_url = validate_site_url(site_url)

        async def aclose(self):
            pass

        async def create_issue(self, *a, **k):
            calls["create"] += 1
            raise AssertionError("stale pending claim must adopt, not create")

        async def myself_account_id(self):
            return "a1"

        async def search_issue_by_label(self, project_key, label):
            calls["search"].append(project_key)
            return TrackerIssue(
                key="ACME-9",
                url=f"{self.site_url}/browse/ACME-9",
                status="",
                id="10009",
                project_key="ACME",
                creator_account_id="a1",
                reporter_account_id="a1",
                # Created after the original claim began (10 min ago), so it is
                # the survived issue and not a pre-existing planted one.
                created_at=(datetime.now(timezone.utc) - timedelta(minutes=9)).isoformat(),
            )

        async def update_issue(self, issue_key, *a, **k):
            calls["update"] += 1
            return TrackerIssue(key=issue_key, url=f"{self.site_url}/browse/{issue_key}", status="")

    monkeypatch.setattr("sessionfs.server.routes.jira.JiraClient", AdoptingFake)

    resp = await client.post(
        f"/api/v1/projects/{project.id}/tickets/{ticket.id}/jira/publish",
        headers=_hdrs(key), json=_PUBLISH_BODY,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["issue_key"] == "ACME-9"
    assert calls == {"create": 0, "search": ["ACME"], "update": 1}

    link = (await db_session.execute(
        select(TicketTrackerLink)
        .where(TicketTrackerLink.ticket_id == ticket.id)
        .execution_options(populate_existing=True)
    )).scalar_one()
    assert link.status == "linked"
    assert link.issue_key == "ACME-9"
    assert link.unknown_since is None


def _adopting_fake(calls: dict, *, project_key: str, issue_key: str, created_minutes_ago: float):
    """A Jira fake whose label search finds one issue; any create fails the test."""

    class AdoptingFake:
        def __init__(self, site_url, account_email, api_token, *, transport=None):
            self.site_url = validate_site_url(site_url)

        async def aclose(self):
            pass

        async def create_issue(self, *a, **k):
            calls["create"] += 1
            raise AssertionError("must adopt, not create")

        async def myself_account_id(self):
            return "a1"

        async def search_issue_by_label(self, project_key_arg, label):
            calls["search"].append(project_key_arg)
            if project_key_arg != project_key:
                return None
            return TrackerIssue(
                key=issue_key,
                url=f"{self.site_url}/browse/{issue_key}",
                status="",
                id="10011",
                project_key=project_key,
                creator_account_id="a1",
                reporter_account_id="a1",
                created_at=(
                    datetime.now(timezone.utc) - timedelta(minutes=created_minutes_ago)
                ).isoformat(),
            )

        async def update_issue(self, issue_key_arg, *a, **k):
            calls["update"] += 1
            return TrackerIssue(
                key=issue_key_arg, url=f"{self.site_url}/browse/{issue_key_arg}", status=""
            )

    return AdoptingFake


@pytest.mark.asyncio
async def test_stale_pending_reclaim_keeps_dead_workers_destination_project(
    client: AsyncClient, db_session: AsyncSession, monkeypatch
):
    """A dead worker created into OTHER (helper override); a later publish whose
    mapping is ACME must adopt the OTHER issue, not create a duplicate in ACME."""
    org, user, key, project, ticket = await _ready(db_session)
    db_session.add(TicketTrackerLink(
        ticket_id=ticket.id,
        org_id=org.id,
        provider="jira",
        status="pending",
        claim_token="stale-token",
        site_url="https://acme.atlassian.net",
        jira_project_key="OTHER",
        last_pushed_at=datetime.now(timezone.utc) - timedelta(minutes=10),
    ))
    await db_session.commit()

    calls: dict = {"create": 0, "search": [], "update": 0}
    monkeypatch.setattr(
        "sessionfs.server.routes.jira.JiraClient",
        _adopting_fake(calls, project_key="OTHER", issue_key="OTHER-5", created_minutes_ago=9),
    )

    resp = await client.post(
        f"/api/v1/projects/{project.id}/tickets/{ticket.id}/jira/publish",
        headers=_hdrs(key), json=_PUBLISH_BODY,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["issue_key"] == "OTHER-5"
    assert calls["create"] == 0
    assert set(calls["search"]) == {"OTHER"}

    link = (await db_session.execute(
        select(TicketTrackerLink)
        .where(TicketTrackerLink.ticket_id == ticket.id)
        .execution_options(populate_existing=True)
    )).scalar_one()
    assert link.jira_project_key == "OTHER"
    assert link.issue_key == "OTHER-5"


@pytest.mark.asyncio
async def test_pre_migration_unknown_row_uses_last_pushed_at_as_cut_off(
    client: AsyncClient, db_session: AsyncSession, monkeypatch
):
    """A row left unknown before unknown_since existed still refuses issues that
    predate the original attempt (cut-off falls back to last_pushed_at)."""
    org, user, key, project, ticket = await _ready(db_session)
    db_session.add(TicketTrackerLink(
        ticket_id=ticket.id,
        org_id=org.id,
        provider="jira",
        status="unknown",
        site_url="https://acme.atlassian.net",
        jira_project_key="ACME",
        unknown_since=None,
        last_pushed_at=datetime.now(timezone.utc) - timedelta(minutes=10),
    ))
    await db_session.commit()

    calls: dict = {"create": 0, "search": [], "update": 0}
    # An issue created long before the original attempt is not ours.
    monkeypatch.setattr(
        "sessionfs.server.routes.jira.JiraClient",
        _adopting_fake(calls, project_key="ACME", issue_key="ACME-1", created_minutes_ago=60),
    )
    resp = await client.post(
        f"/api/v1/projects/{project.id}/tickets/{ticket.id}/jira/publish",
        headers=_hdrs(key), json=_PUBLISH_BODY,
    )
    assert resp.status_code == 409, resp.text
    assert calls["create"] == 0

    link = (await db_session.execute(
        select(TicketTrackerLink)
        .where(TicketTrackerLink.ticket_id == ticket.id)
        .execution_options(populate_existing=True)
    )).scalar_one()
    # The fallback cut-off was persisted, so later retries keep it.
    assert link.unknown_since is not None


@pytest.mark.parametrize("status_code", [502, None])
@pytest.mark.asyncio
async def test_create_5xx_or_transport_error_keeps_row_unknown_then_adopts(
    client: AsyncClient, db_session: AsyncSession, monkeypatch, status_code
):
    """A create that fails with a 5xx (or a transport error, no status code)
    may still have written the issue, so the row stays unknown and the next
    publish adopts it by label instead of creating a duplicate."""
    org, user, key, project, ticket = await _ready(db_session)
    calls: dict = {"create": 0, "search": [], "update": 0}
    created_at = datetime.now(timezone.utc)

    class FlakyCreateFake:
        def __init__(self, site_url, account_email, api_token, *, transport=None):
            self.site_url = validate_site_url(site_url)

        async def aclose(self):
            pass

        async def create_issue(self, *a, **k):
            calls["create"] += 1
            # Jira wrote the issue, but the response was lost.
            raise TrackerError("Jira API error", status_code=status_code)

        async def myself_account_id(self):
            return "a1"

        async def search_issue_by_label(self, project_key, label):
            calls["search"].append(project_key)
            return TrackerIssue(
                key="ACME-7",
                url=f"{self.site_url}/browse/ACME-7",
                status="",
                id="10007",
                project_key="ACME",
                creator_account_id="a1",
                reporter_account_id="a1",
                created_at=created_at.isoformat(),
            )

        async def update_issue(self, issue_key, *a, **k):
            calls["update"] += 1
            return TrackerIssue(key=issue_key, url=f"{self.site_url}/browse/{issue_key}", status="")

    monkeypatch.setattr("sessionfs.server.routes.jira.JiraClient", FlakyCreateFake)

    resp = await client.post(
        f"/api/v1/projects/{project.id}/tickets/{ticket.id}/jira/publish",
        headers=_hdrs(key), json=_PUBLISH_BODY,
    )
    assert resp.status_code == 502, resp.text
    link = (await db_session.execute(
        select(TicketTrackerLink)
        .where(TicketTrackerLink.ticket_id == ticket.id)
        .execution_options(populate_existing=True)
    )).scalar_one()
    assert link.status == "unknown"
    assert link.unknown_since is not None

    resp = await client.post(
        f"/api/v1/projects/{project.id}/tickets/{ticket.id}/jira/publish",
        headers=_hdrs(key), json=_PUBLISH_BODY,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["issue_key"] == "ACME-7"
    assert calls["create"] == 1  # only the first, failed attempt
    assert calls["search"] == ["ACME"]


@pytest.mark.asyncio
async def test_create_4xx_releases_claim_for_a_fresh_retry(
    client: AsyncClient, db_session: AsyncSession, monkeypatch
):
    """A 4xx proves nothing was created, so the claim is released."""
    org, user, key, project, ticket = await _ready(db_session)

    class RejectingFake:
        def __init__(self, site_url, account_email, api_token, *, transport=None):
            self.site_url = validate_site_url(site_url)

        async def aclose(self):
            pass

        async def create_issue(self, *a, **k):
            raise TrackerError("Jira API 403: forbidden", status_code=403)

    monkeypatch.setattr("sessionfs.server.routes.jira.JiraClient", RejectingFake)
    resp = await client.post(
        f"/api/v1/projects/{project.id}/tickets/{ticket.id}/jira/publish",
        headers=_hdrs(key), json=_PUBLISH_BODY,
    )
    assert resp.status_code == 502, resp.text
    link = (await db_session.execute(
        select(TicketTrackerLink)
        .where(TicketTrackerLink.ticket_id == ticket.id)
        .execution_options(populate_existing=True)
    )).scalar_one_or_none()
    assert link is None


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

        async def create_issue(self, project_key, issue_type, summary, description_adf, *, labels=None, extra_fields=None):
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


@pytest.mark.asyncio
async def test_health_unmapped_count_skips_merge_tombstones(
    client: AsyncClient, db_session: AsyncSession
):
    """GitHub #111: a merged source can't be mapped, so it isn't "unmapped"."""
    org = await _org(db_session)
    user, key = await _member(db_session, org, "admin")
    await _insert_connection(db_session, org, user)
    live = await _project(db_session, user, org)
    merged = await _project(db_session, user, org)
    merged.merged_into_project_id = live.id
    merged.merged_at = datetime.now(timezone.utc)
    await db_session.commit()

    health = (await client.get(
        f"/api/v1/orgs/{org.id}/integrations/jira/health", headers=_hdrs(key)
    )).json()
    assert health["unmapped_project_count"] == 1


# ── GitHub #112 phase 2: link an existing Jira issue ──


@pytest.mark.asyncio
async def test_link_existing_issue_links_without_pushing(
    client: AsyncClient, db_session: AsyncSession, fake_jira
):
    org, user, key, project, ticket = await _ready(db_session)
    resp = await client.post(
        f"/api/v1/projects/{project.id}/tickets/{ticket.id}/jira/link",
        headers=_hdrs(key), json={"issue_key": "  acme-123 "},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ticket_id"] == ticket.id
    assert body["issue_key"] == "ACME-123"  # trimmed + uppercased
    assert body["issue_url"] == "https://acme.atlassian.net/browse/ACME-123"
    assert body["last_known_status"] == "Done"
    assert body["last_pushed_at"] is None
    assert body["payload_hash"] is None

    # Linking never pushes anything to Jira.
    assert fake_jira[0].calls["create_issue"] == 0
    assert fake_jira[0].calls["update_issue"] == 0
    assert fake_jira[0].calls["get_issue"] == 1

    link = (await db_session.execute(
        select(TicketTrackerLink).where(TicketTrackerLink.ticket_id == ticket.id)
    )).scalar_one()
    assert link.status == "linked"
    assert link.issue_key == "ACME-123"
    assert link.issue_id == "10001"
    assert link.site_url == "https://acme.atlassian.net"
    assert link.last_pushed_at is None
    assert link.last_status_checked_at is not None

    audits = (await db_session.execute(
        select(OrgAuditEvent).where(OrgAuditEvent.org_id == org.id)
    )).scalars().all()
    assert [e.event_type for e in audits] == ["jira_linked"]
    assert json.loads(audits[0].after) == {
        "issue_key": "ACME-123",
        "issue_url": "https://acme.atlassian.net/browse/ACME-123",
    }
    assert _TOKEN not in (audits[0].after or "")


@pytest.mark.asyncio
async def test_link_records_exactly_one_ticket_event(
    client: AsyncClient, db_session: AsyncSession, fake_jira
):
    org, user, key, project, ticket = await _ready(db_session)
    resp = await client.post(
        f"/api/v1/projects/{project.id}/tickets/{ticket.id}/jira/link",
        headers=_hdrs(key), json={"issue_key": "ACME-1"},
    )
    assert resp.status_code == 200, resp.text

    events = (await db_session.execute(
        select(TicketEvent).where(TicketEvent.ticket_id == ticket.id)
    )).scalars().all()
    assert len(events) == 1
    assert events[0].action == "jira_linked"
    assert json.loads(events[0].after_value) == {"issue_key": "ACME-1"}
    assert events[0].actor_type == "user"
    assert events[0].actor_user_id == user.id


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["linked", "pending", "unknown"])
async def test_link_refuses_each_existing_link_status_with_409(
    client: AsyncClient, db_session: AsyncSession, fake_jira, status: str
):
    org, user, key, project, ticket = await _ready(db_session)
    db_session.add(TicketTrackerLink(
        ticket_id=ticket.id,
        org_id=org.id,
        provider="jira",
        status=status,
        site_url="https://acme.atlassian.net",
        claim_token="tok" if status == "pending" else None,
        last_pushed_at=datetime.now(timezone.utc) if status == "pending" else None,
    ))
    await db_session.commit()

    resp = await client.post(
        f"/api/v1/projects/{project.id}/tickets/{ticket.id}/jira/link",
        headers=_hdrs(key), json={"issue_key": "ACME-1"},
    )
    assert resp.status_code == 409, resp.text
    assert resp.json()["error"]["details"]["error"] == "jira_already_linked"
    # Refused before any outbound read.
    assert fake_jira == []


@pytest.mark.asyncio
async def test_link_rejects_bad_key_with_422(
    client: AsyncClient, db_session: AsyncSession, fake_jira
):
    org, user, key, project, ticket = await _ready(db_session)
    resp = await client.post(
        f"/api/v1/projects/{project.id}/tickets/{ticket.id}/jira/link",
        headers=_hdrs(key), json={"issue_key": "not a key"},
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["details"]["error"] == "invalid_issue_key"
    assert fake_jira == []


@pytest.mark.asyncio
async def test_link_missing_jira_issue_returns_404(
    client: AsyncClient, db_session: AsyncSession, fake_jira
):
    org, user, key, project, ticket = await _ready(db_session)
    fake_jira.state["get_issue_error"] = (404, "issue does not exist or is not accessible")
    resp = await client.post(
        f"/api/v1/projects/{project.id}/tickets/{ticket.id}/jira/link",
        headers=_hdrs(key), json={"issue_key": "ACME-999"},
    )
    assert resp.status_code == 404
    assert resp.json()["error"]["details"]["error"] == "jira_issue_not_found"


@pytest.mark.asyncio
async def test_link_timeout_returns_504(
    client: AsyncClient, db_session: AsyncSession, fake_jira
):
    org, user, key, project, ticket = await _ready(db_session)
    fake_jira.state["get_issue_timeout"] = True
    resp = await client.post(
        f"/api/v1/projects/{project.id}/tickets/{ticket.id}/jira/link",
        headers=_hdrs(key), json={"issue_key": "ACME-1"},
    )
    assert resp.status_code == 504
    assert resp.json()["error"]["details"]["error"] == "jira_timeout"


@pytest.mark.asyncio
async def test_link_other_jira_project_returns_422(
    client: AsyncClient, db_session: AsyncSession, fake_jira
):
    org, user, key, project, ticket = await _ready(db_session)
    fake_jira.state["issue_project_key"] = "OTHER"
    resp = await client.post(
        f"/api/v1/projects/{project.id}/tickets/{ticket.id}/jira/link",
        headers=_hdrs(key), json={"issue_key": "ACME-1"},
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["details"]["error"] == "jira_project_not_mapped"
    # Message names both keys.
    assert "OTHER" in resp.json()["error"]["message"]
    assert "ACME" in resp.json()["error"]["message"]


@pytest.mark.asyncio
async def test_link_issue_linked_elsewhere_409_without_leaking_other_ticket(
    client: AsyncClient, db_session: AsyncSession, fake_jira
):
    org, user, key, project, ticket = await _ready(db_session)
    other = await _ticket(db_session, project, user)
    db_session.add(TicketTrackerLink(
        ticket_id=other.id,
        org_id=org.id,
        provider="jira",
        status="linked",
        site_url="https://acme.atlassian.net",
        issue_id="10001",
        issue_key="ACME-1",
        issue_url="https://acme.atlassian.net/browse/ACME-1",
    ))
    await db_session.commit()

    resp = await client.post(
        f"/api/v1/projects/{project.id}/tickets/{ticket.id}/jira/link",
        headers=_hdrs(key), json={"issue_key": "ACME-1"},
    )
    assert resp.status_code == 409
    assert resp.json()["error"]["details"]["error"] == "jira_issue_linked_elsewhere"
    # Never reveal which other ticket owns the issue.
    assert other.id not in resp.text
    assert other.title not in resp.text


@pytest.mark.asyncio
async def test_link_requires_editor_standing_in_restricted_org(
    client: AsyncClient, db_session: AsyncSession, fake_jira
):
    from sessionfs.server.db.models import (
        ProjectGroup,
        ProjectGroupAccess,
        ProjectGroupMember,
    )

    org, admin, _key, project, ticket = await _ready(db_session)
    reader, reader_key = await _member(db_session, org, "member")
    editor, editor_key = await _member(db_session, org, "member")
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

    org.settings = json.dumps({"general": {"restrict_project_visibility": True}})
    await db_session.commit()

    url = f"/api/v1/projects/{project.id}/tickets/{ticket.id}/jira/link"
    resp = await client.post(url, headers=_hdrs(reader_key), json={"issue_key": "ACME-1"})
    assert resp.status_code == 403, resp.text
    assert resp.json()["error"]["code"] == "project_edit_forbidden"
    assert (await db_session.execute(
        select(TicketTrackerLink).where(TicketTrackerLink.ticket_id == ticket.id)
    )).scalar_one_or_none() is None

    resp = await client.post(url, headers=_hdrs(editor_key), json={"issue_key": "ACME-1"})
    assert resp.status_code == 200, resp.text


@pytest.mark.asyncio
async def test_link_gated_on_project_org_tier(
    client: AsyncClient, db_session: AsyncSession, fake_jira
):
    org, user, key, project, ticket = await _ready(db_session, tier="free")
    resp = await client.post(
        f"/api/v1/projects/{project.id}/tickets/{ticket.id}/jira/link",
        headers=_hdrs(key), json={"issue_key": "ACME-1"},
    )
    assert resp.status_code == 403
    assert resp.json()["error"]["details"]["error"] == "upgrade_required"


@pytest.mark.asyncio
async def test_link_then_unlink_works(
    client: AsyncClient, db_session: AsyncSession, fake_jira
):
    org, user, key, project, ticket = await _ready(db_session)
    resp = await client.post(
        f"/api/v1/projects/{project.id}/tickets/{ticket.id}/jira/link",
        headers=_hdrs(key), json={"issue_key": "ACME-1"},
    )
    assert resp.status_code == 200, resp.text

    resp = await client.delete(
        f"/api/v1/projects/{project.id}/tickets/{ticket.id}/jira", headers=_hdrs(key)
    )
    assert resp.status_code == 204
    assert (await db_session.execute(
        select(TicketTrackerLink).where(TicketTrackerLink.ticket_id == ticket.id)
    )).scalar_one_or_none() is None

    types = [e.event_type for e in (await db_session.execute(
        select(OrgAuditEvent).where(OrgAuditEvent.org_id == org.id)
    )).scalars().all()]
    assert types == ["jira_linked", "jira_unlinked"]

    events = (await db_session.execute(
        select(TicketEvent).where(TicketEvent.ticket_id == ticket.id)
    )).scalars().all()
    assert [e.action for e in events] == ["jira_linked", "jira_unlinked"]
    assert [e.actor_type for e in events] == ["user", "user"]
    assert json.loads(events[1].before_value) == {"issue_key": "ACME-1"}


@pytest.mark.asyncio
async def test_linked_not_published_ticket_receives_inbound_webhook(
    client: AsyncClient, db_session: AsyncSession, fake_jira
):
    from sessionfs.server.llm_keys import encrypt_secret

    org, user, key, project, ticket = await _ready(db_session)
    # The link must be a live sync target: status sync on, webhook secret set.
    conn = (await db_session.execute(
        select(OrgTrackerConnection).where(OrgTrackerConnection.org_id == org.id)
    )).scalar_one()
    conn.sync_status = True
    conn.encrypted_webhook_secret = encrypt_secret(_WEBHOOK_SECRET)
    conn.webhook_secret_hash = hashlib.sha256(_WEBHOOK_SECRET.encode()).hexdigest()
    conn.webhook_secret_last4 = _WEBHOOK_SECRET[-4:]
    await db_session.commit()

    ticket.kind = "issue"
    ticket.status = "in_progress"
    await db_session.commit()

    resp = await client.post(
        f"/api/v1/projects/{project.id}/tickets/{ticket.id}/jira/link",
        headers=_hdrs(key), json={"issue_key": "ACME-1"},
    )
    assert resp.status_code == 200, resp.text

    payload = _webhook_payload(
        "jira:issue_updated",
        user={"displayName": "Bob", "accountId": "acc-bob"},
        changelog={
            "id": "20002",
            "items": [
                {"field": "status", "fieldId": "status",
                 "fromString": "In Progress", "toString": "Done"},
            ],
        },
        issue={
            "key": "ACME-1",
            "id": "10001",
            "fields": {"status": {"name": "Done", "statusCategory": {"key": "done"}}},
        },
    )
    status, _body = await _post_webhook(client, org.id, payload)
    assert status == 200

    ticket = await db_session.get(Ticket, ticket.id, populate_existing=True)
    assert ticket.status == "closed"

    # The inbound transition is attributed to Jira, not any local user.
    status_events = (await db_session.execute(
        select(TicketEvent).where(
            TicketEvent.ticket_id == ticket.id, TicketEvent.field == "status"
        )
    )).scalars().all()
    assert len(status_events) == 1
    assert status_events[0].action == "status_changed"
    assert status_events[0].actor_type == "jira"
    assert status_events[0].actor_user_id is None
    assert json.loads(status_events[0].before_value) == "in_progress"
    assert json.loads(status_events[0].after_value) == "closed"


# ── attached-only overwrite guard + pull ──


@pytest.mark.asyncio
async def test_attached_only_link_flags_on_link_get_and_preview(
    client: AsyncClient, db_session: AsyncSession, fake_jira
):
    org, user, key, project, ticket = await _ready(db_session)
    resp = await client.post(
        f"/api/v1/projects/{project.id}/tickets/{ticket.id}/jira/link",
        headers=_hdrs(key), json={"issue_key": "ACME-1"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["attached_not_published"] is True

    resp = await client.get(
        f"/api/v1/projects/{project.id}/tickets/{ticket.id}/jira", headers=_hdrs(key)
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["attached_not_published"] is True

    preview = (await client.post(
        f"/api/v1/projects/{project.id}/tickets/{ticket.id}/jira/preview", headers=_hdrs(key)
    )).json()
    assert preview["existing"]["attached"] is True


@pytest.mark.asyncio
async def test_publish_attached_only_requires_overwrite_then_succeeds(
    client: AsyncClient, db_session: AsyncSession, fake_jira
):
    org, user, key, project, ticket = await _ready(db_session)
    resp = await client.post(
        f"/api/v1/projects/{project.id}/tickets/{ticket.id}/jira/link",
        headers=_hdrs(key), json={"issue_key": "ACME-1"},
    )
    assert resp.status_code == 200, resp.text

    # Only the link request has constructed a client so far.
    assert len(fake_jira) == 1

    # Without the opt-in, publish refuses before any outbound call.
    resp = await client.post(
        f"/api/v1/projects/{project.id}/tickets/{ticket.id}/jira/publish",
        headers=_hdrs(key), json=_PUBLISH_BODY,
    )
    assert resp.status_code == 409, resp.text
    assert resp.json()["error"]["details"]["error"] == "jira_overwrite_required"
    assert "ACME-1" in resp.json()["error"]["message"]
    assert len(fake_jira) == 1  # the guard raised before a Jira client existed

    # With the opt-in, publish proceeds as an update (no second create).
    resp = await client.post(
        f"/api/v1/projects/{project.id}/tickets/{ticket.id}/jira/publish",
        headers=_hdrs(key), json={**_PUBLISH_BODY, "overwrite_existing": True},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["issue_key"] == "ACME-1"
    assert len(fake_jira) == 2
    assert fake_jira[1].calls["update_issue"] == 1
    assert fake_jira[1].calls["create_issue"] == 0

    link = (await db_session.execute(
        select(TicketTrackerLink).where(TicketTrackerLink.ticket_id == ticket.id)
    )).scalar_one()
    assert link.payload_hash is not None
    assert link.template_slug is not None

    # Now that it's published (payload + template pinned), the flag flips off.
    resp = await client.get(
        f"/api/v1/projects/{project.id}/tickets/{ticket.id}/jira", headers=_hdrs(key)
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["attached_not_published"] is False


@pytest.mark.asyncio
async def test_pull_requires_confirm(client: AsyncClient, db_session: AsyncSession, fake_jira):
    org, user, key, project, ticket = await _ready(db_session)
    resp = await client.post(
        f"/api/v1/projects/{project.id}/tickets/{ticket.id}/jira/pull",
        headers=_hdrs(key), json={},
    )
    assert resp.status_code == 400
    assert fake_jira == []


@pytest.mark.asyncio
async def test_pull_not_linked_returns_409(client: AsyncClient, db_session: AsyncSession, fake_jira):
    org, user, key, project, ticket = await _ready(db_session)
    resp = await client.post(
        f"/api/v1/projects/{project.id}/tickets/{ticket.id}/jira/pull",
        headers=_hdrs(key), json={"confirm": True},
    )
    assert resp.status_code == 409, resp.text
    assert resp.json()["error"]["details"]["error"] == "jira_not_linked"
    assert fake_jira == []


@pytest.mark.asyncio
async def test_pull_updates_ticket_without_outbound_sync(
    client: AsyncClient, db_session: AsyncSession, fake_jira
):
    org, user, key, project, ticket = await _ready(db_session)
    resp = await client.post(
        f"/api/v1/projects/{project.id}/tickets/{ticket.id}/jira/link",
        headers=_hdrs(key), json={"issue_key": "ACME-1"},
    )
    assert resp.status_code == 200, resp.text

    fake_jira.state["pull_summary"] = "Shipped the auth migration"
    fake_jira.state["pull_description"] = {
        "type": "doc", "version": 1,
        "content": [{"type": "paragraph", "content": [{"type": "text", "text": "all done"}]}],
    }

    resp = await client.post(
        f"/api/v1/projects/{project.id}/tickets/{ticket.id}/jira/pull",
        headers=_hdrs(key), json={"confirm": True},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["updated_fields"] == ["title", "description"]
    assert body["jira"]["issue_key"] == "ACME-1"
    assert body["jira"]["attached_not_published"] is True

    # The pulled ticket uses the same detail builder as the GET ticket route,
    # so it carries the full enrichment (Jira link state, child rollup).
    assert body["ticket"]["jira"]["issue_key"] == "ACME-1"
    assert body["ticket"]["child_ticket_ids"] == []

    ticket = await db_session.get(Ticket, ticket.id, populate_existing=True)
    assert ticket.title == "Shipped the auth migration"
    assert ticket.description == "all done"

    # Field edits never enqueue an outbound Jira sync: the pull client only
    # read the issue and made no write (create/update/comment/transition).
    assert len(fake_jira) == 2
    pull_client = fake_jira[1]
    assert pull_client.calls["get_issue_summary_and_description"] == 1
    assert pull_client.calls["create_issue"] == 0
    assert pull_client.calls["update_issue"] == 0
    assert pull_client.calls["add_comment"] == 0
    assert pull_client.calls["transition_issue"] == 0
    assert (await db_session.execute(
        select(TrackerSyncEvent).where(TrackerSyncEvent.link_id == ticket.id)
    )).scalars().all() == []

    comments = (await db_session.execute(
        select(TicketComment).where(
            TicketComment.ticket_id == ticket.id, TicketComment.source == "jira"
        )
    )).scalars().all()
    assert [c.content for c in comments] == ["Updated from Jira ACME-1"]

    audits = (await db_session.execute(
        select(OrgAuditEvent).where(OrgAuditEvent.org_id == org.id)
    )).scalars().all()
    pulled = [e for e in audits if e.event_type == "jira_pulled"]
    assert len(pulled) == 1
    assert json.loads(pulled[0].after)["updated_fields"] == ["title", "description"]


@pytest.mark.asyncio
async def test_pull_noop_when_remote_matches_ticket(
    client: AsyncClient, db_session: AsyncSession, fake_jira
):
    org, user, key, project, ticket = await _ready(db_session)
    resp = await client.post(
        f"/api/v1/projects/{project.id}/tickets/{ticket.id}/jira/link",
        headers=_hdrs(key), json={"issue_key": "ACME-1"},
    )
    assert resp.status_code == 200, resp.text

    # Make the remote text identical to the ticket (fake defaults).
    ticket.title = "Pulled summary"
    ticket.description = "Pulled body"
    await db_session.commit()

    resp = await client.post(
        f"/api/v1/projects/{project.id}/tickets/{ticket.id}/jira/pull",
        headers=_hdrs(key), json={"confirm": True},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["updated_fields"] == []

    audits = (await db_session.execute(
        select(OrgAuditEvent).where(OrgAuditEvent.org_id == org.id)
    )).scalars().all()
    assert [e.event_type for e in audits] == ["jira_linked"]


@pytest.mark.asyncio
async def test_pull_empty_summary_leaves_title_unchanged(
    client: AsyncClient, db_session: AsyncSession, fake_jira
):
    """An empty Jira summary never blanks the ticket title; the description
    still pulls, and ``updated_fields`` only lists what actually changed."""
    org, user, key, project, ticket = await _ready(db_session)
    await client.post(
        f"/api/v1/projects/{project.id}/tickets/{ticket.id}/jira/link",
        headers=_hdrs(key), json={"issue_key": "ACME-1"},
    )
    fake_jira.state["pull_summary"] = ""
    fake_jira.state["pull_description"] = {
        "type": "doc", "version": 1,
        "content": [{"type": "paragraph", "content": [{"type": "text", "text": "new body"}]}],
    }

    resp = await client.post(
        f"/api/v1/projects/{project.id}/tickets/{ticket.id}/jira/pull",
        headers=_hdrs(key), json={"confirm": True},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["updated_fields"] == ["description"]

    ticket = await db_session.get(Ticket, ticket.id, populate_existing=True)
    assert ticket.title == "Ship the auth migration"  # unchanged
    assert ticket.description == "new body"


@pytest.mark.asyncio
async def test_pull_empty_description_leaves_description_unchanged(
    client: AsyncClient, db_session: AsyncSession, fake_jira
):
    """An empty Jira description never blanks the ticket body; the title still
    pulls, and ``updated_fields`` only lists what actually changed."""
    org, user, key, project, ticket = await _ready(db_session)
    await client.post(
        f"/api/v1/projects/{project.id}/tickets/{ticket.id}/jira/link",
        headers=_hdrs(key), json={"issue_key": "ACME-1"},
    )
    fake_jira.state["pull_summary"] = "New summary"
    fake_jira.state["pull_description"] = {"type": "doc", "version": 1, "content": []}

    resp = await client.post(
        f"/api/v1/projects/{project.id}/tickets/{ticket.id}/jira/pull",
        headers=_hdrs(key), json={"confirm": True},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["updated_fields"] == ["title"]

    ticket = await db_session.get(Ticket, ticket.id, populate_existing=True)
    assert ticket.title == "New summary"
    assert ticket.description == "line one\nline two"  # unchanged


@pytest.mark.asyncio
async def test_pull_redacts_known_secrets_before_storing(
    client: AsyncClient, db_session: AsyncSession, fake_jira
):
    """Pulled text is DLP-redacted before it lands on the ticket row: a known
    secret in the remote summary never survives into the stored title."""
    org, user, key, project, ticket = await _ready(db_session)
    await client.post(
        f"/api/v1/projects/{project.id}/tickets/{ticket.id}/jira/link",
        headers=_hdrs(key), json={"issue_key": "ACME-1"},
    )
    fake_jira.state["pull_summary"] = (
        "the key is sk-ant-abc123def456ghi789jkl012mno345 and it is secret"
    )
    fake_jira.state["pull_description"] = {
        "type": "doc", "version": 1, "content": [],
    }

    resp = await client.post(
        f"/api/v1/projects/{project.id}/tickets/{ticket.id}/jira/pull",
        headers=_hdrs(key), json={"confirm": True},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["updated_fields"] == ["title"]

    ticket = await db_session.get(Ticket, ticket.id, populate_existing=True)
    assert "sk-ant-" not in ticket.title
    assert "secret" in ticket.title


@pytest.mark.asyncio
async def test_pull_missing_remote_issue_returns_404(
    client: AsyncClient, db_session: AsyncSession, fake_jira
):
    org, user, key, project, ticket = await _ready(db_session)
    await client.post(
        f"/api/v1/projects/{project.id}/tickets/{ticket.id}/jira/link",
        headers=_hdrs(key), json={"issue_key": "ACME-1"},
    )
    fake_jira.state["pull_error"] = (404, "issue does not exist")
    resp = await client.post(
        f"/api/v1/projects/{project.id}/tickets/{ticket.id}/jira/pull",
        headers=_hdrs(key), json={"confirm": True},
    )
    assert resp.status_code == 404, resp.text
    assert resp.json()["error"]["details"]["error"] == "jira_issue_not_found"


@pytest.mark.asyncio
async def test_pull_timeout_returns_504(
    client: AsyncClient, db_session: AsyncSession, fake_jira
):
    org, user, key, project, ticket = await _ready(db_session)
    await client.post(
        f"/api/v1/projects/{project.id}/tickets/{ticket.id}/jira/link",
        headers=_hdrs(key), json={"issue_key": "ACME-1"},
    )
    fake_jira.state["pull_timeout"] = True
    resp = await client.post(
        f"/api/v1/projects/{project.id}/tickets/{ticket.id}/jira/pull",
        headers=_hdrs(key), json={"confirm": True},
    )
    assert resp.status_code == 504, resp.text
    assert resp.json()["error"]["details"]["error"] == "jira_timeout"


@pytest.mark.asyncio
async def test_pull_403_for_non_creator_non_admin(
    client: AsyncClient, db_session: AsyncSession, fake_jira
):
    """A member who is neither the ticket creator nor a project admin cannot
    pull the issue back into the ticket.

    The pull runs the same creator/project-admin edit sequence as the ticket
    edit route (not just the broader editor standing the Jira gates use), so
    a plain member cannot overwrite someone else's ticket text from Jira.
    """
    org = await _org(db_session)
    admin, admin_key = await _member(db_session, org, "admin")
    member, member_key = await _member(db_session, org, "member")
    await _insert_connection(db_session, org, admin)
    project = await _project(db_session, admin, org)
    await _insert_mapping(db_session, org, project, admin)
    ticket = await _ticket(db_session, project, admin)
    await client.post(
        f"/api/v1/projects/{project.id}/tickets/{ticket.id}/jira/link",
        headers=_hdrs(admin_key), json={"issue_key": "ACME-1"},
    )

    resp = await client.post(
        f"/api/v1/projects/{project.id}/tickets/{ticket.id}/jira/pull",
        headers=_hdrs(member_key), json={"confirm": True},
    )
    assert resp.status_code == 403, resp.text

    # Only the admin's link call created a Jira client; the refused pull never
    # instantiated one, so the remote issue was never read.
    assert len(fake_jira) == 1


@pytest.mark.asyncio
async def test_adopt_refuses_issue_linked_elsewhere_keeping_unknown(
    client: AsyncClient, db_session: AsyncSession, monkeypatch
):
    """A timed-out create whose label search lands on an issue another ticket
    already owns is refused and the link stays ``unknown``."""
    monkeypatch.setattr("sessionfs.server.routes.jira._ADOPT_RETRY_DELAY_SECONDS", 0.0)
    org, user, key, project, ticket = await _ready(db_session)
    calls = {"create": 0, "search": 0, "update": 0, "myself": 0}

    class TimeoutThenLinkedElsewhereFake:
        def __init__(self, site_url, account_email, api_token, *, transport=None):
            self.site_url = validate_site_url(site_url)

        async def aclose(self):
            pass

        async def create_issue(self, *a, **k):
            calls["create"] += 1
            raise asyncio.TimeoutError

        async def myself_account_id(self):
            calls["myself"] += 1
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

    monkeypatch.setattr("sessionfs.server.routes.jira.JiraClient", TimeoutThenLinkedElsewhereFake)

    r1 = await client.post(
        f"/api/v1/projects/{project.id}/tickets/{ticket.id}/jira/publish",
        headers=_hdrs(key), json=_PUBLISH_BODY,
    )
    assert r1.status_code == 504

    link = (await db_session.execute(
        select(TicketTrackerLink).where(TicketTrackerLink.ticket_id == ticket.id)
    )).scalar_one()
    assert link.status == "unknown"
    await db_session.commit()  # release the read snapshot for the retry

    # Another ticket already owns ACME-9 on this org/site.
    other = await _ticket(db_session, project, user)
    db_session.add(TicketTrackerLink(
        ticket_id=other.id,
        org_id=org.id,
        provider="jira",
        status="linked",
        site_url="https://acme.atlassian.net",
        issue_id="10009",
        issue_key="ACME-9",
        issue_url="https://acme.atlassian.net/browse/ACME-9",
    ))
    await db_session.commit()

    r2 = await client.post(
        f"/api/v1/projects/{project.id}/tickets/{ticket.id}/jira/publish",
        headers=_hdrs(key), json=_PUBLISH_BODY,
    )
    assert r2.status_code == 409, r2.text
    assert r2.json()["error"]["details"]["error"] == "jira_issue_linked_elsewhere"
    # Never reveal the other ticket.
    assert other.id not in r2.text
    assert other.title not in r2.text

    # The label search found a candidate but adoption was refused before update.
    assert calls["update"] == 0

    link = (await db_session.execute(
        select(TicketTrackerLink).where(TicketTrackerLink.ticket_id == ticket.id)
    )).scalar_one()
    assert link.status == "unknown"


@pytest.mark.asyncio
async def test_service_key_cannot_pull(
    client: AsyncClient, db_session: AsyncSession, fake_jira
):
    org, user, key, project, ticket = await _ready(db_session)
    resp = await client.post(
        f"/api/v1/projects/{project.id}/tickets/{ticket.id}/jira/link",
        headers=_hdrs(key), json={"issue_key": "ACME-1"},
    )
    assert resp.status_code == 200, resp.text
    title_before = ticket.title

    svc_key = await _service_key(
        db_session, org, user, ["tickets:read", "tickets:write"], project_ids=[project.id]
    )
    fake_jira.state["pull_summary"] = "Rewritten by a service key"
    resp = await client.post(
        f"/api/v1/projects/{project.id}/tickets/{ticket.id}/jira/pull",
        headers=_hdrs(svc_key), json={"confirm": True},
    )
    assert resp.status_code == 403
    assert resp.json()["error"]["details"]["error"] == "service_key_not_allowed"
    await db_session.refresh(ticket)
    assert ticket.title == title_before


# ── review round 2: sprint + parent mapping (M2 / L-a..L-f) ──


@pytest.mark.asyncio
async def test_adopt_no_match_creates_into_persisted_project(
    client: AsyncClient, db_session: AsyncSession, monkeypatch
):
    """M2 — when the adopt search finds nothing, the re-create lands in the
    PERSISTED project (where the original timed-out create targeted), never a
    mapping/override that drifted since."""
    monkeypatch.setattr("sessionfs.server.routes.jira._ADOPT_RETRY_DELAY_SECONDS", 0.0)
    org, user, key, project, ticket = await _ready(db_session)
    calls = {"create_project": [], "search": [], "create": 0}

    class TimeoutThenNoneFake:
        def __init__(self, site_url, account_email, api_token, *, transport=None):
            self.site_url = validate_site_url(site_url)

        async def aclose(self):
            pass

        async def create_issue(self, project_key, *a, **k):
            calls["create"] += 1
            calls["create_project"].append(project_key)
            if calls["create"] == 1:
                raise asyncio.TimeoutError
            return TrackerIssue(
                key="ACME-2", url=f"{self.site_url}/browse/ACME-2", status="", id="10002"
            )

        async def search_issue_by_label(self, project_key, label):
            calls["search"].append(project_key)
            return None

        async def update_issue(self, issue_key, *a, **k):
            raise AssertionError("no update expected when the search finds nothing")

    monkeypatch.setattr("sessionfs.server.routes.jira.JiraClient", TimeoutThenNoneFake)

    r1 = await client.post(
        f"/api/v1/projects/{project.id}/tickets/{ticket.id}/jira/publish", headers=_hdrs(key),
        json=_PUBLISH_BODY,
    )
    assert r1.status_code == 504

    link = (await db_session.execute(
        select(TicketTrackerLink).where(TicketTrackerLink.ticket_id == ticket.id)
    )).scalar_one()
    assert link.status == "unknown"
    assert link.jira_project_key == "ACME"  # persisted at claim time
    await db_session.commit()

    # Drift the mapping so the retry's *effective* project differs from the
    # original create's destination.
    mapping = (await db_session.execute(
        select(TrackerProjectMapping).where(TrackerProjectMapping.project_id == project.id)
    )).scalar_one()
    mapping.jira_project_key = "OTHER"
    await db_session.commit()

    r2 = await client.post(
        f"/api/v1/projects/{project.id}/tickets/{ticket.id}/jira/publish", headers=_hdrs(key),
        json=_PUBLISH_BODY,
    )
    assert r2.status_code == 200, r2.text
    assert r2.json()["issue_key"] == "ACME-2"
    # Search and the no-match re-create both used the persisted project, never
    # the drifted "OTHER".
    assert calls == {"create": 2, "search": ["ACME", "ACME"], "create_project": ["ACME", "ACME"]}

    link = (await db_session.execute(
        select(TicketTrackerLink)
        .where(TicketTrackerLink.ticket_id == ticket.id)
        .execution_options(populate_existing=True)
    )).scalar_one()
    assert link.status == "linked"
    assert link.jira_project_key == "ACME"
    assert link.issue_key == "ACME-2"


@pytest.mark.asyncio
async def test_update_keeps_stored_project_key_after_mapping_drift(
    client: AsyncClient, db_session: AsyncSession, fake_jira
):
    """L-a — an update never rewrites the stored destination: the issue still
    lives where it was created even after the mapping drifted."""
    org, user, key, project, ticket = await _ready(db_session)

    r1 = await client.post(
        f"/api/v1/projects/{project.id}/tickets/{ticket.id}/jira/publish", headers=_hdrs(key),
        json=_PUBLISH_BODY,
    )
    assert r1.status_code == 200, r1.text
    assert r1.json()["issue_key"] == "ACME-1"

    mapping = (await db_session.execute(
        select(TrackerProjectMapping).where(TrackerProjectMapping.project_id == project.id)
    )).scalar_one()
    mapping.jira_project_key = "OTHER"
    await db_session.commit()

    r2 = await client.post(
        f"/api/v1/projects/{project.id}/tickets/{ticket.id}/jira/publish", headers=_hdrs(key),
        json={**_PUBLISH_BODY, "summary": "Ship the auth migration v2"},
    )
    assert r2.status_code == 200, r2.text

    link = (await db_session.execute(
        select(TicketTrackerLink)
        .where(TicketTrackerLink.ticket_id == ticket.id)
        .execution_options(populate_existing=True)
    )).scalar_one()
    assert link.status == "linked"
    assert link.issue_key == "ACME-1"
    # The update left the recorded destination alone.
    assert link.jira_project_key == "ACME"


@pytest.mark.asyncio
async def test_inbound_webhook_sprint_changelog_absent_field_keeps_cache(
    client: AsyncClient, db_session: AsyncSession, fake_jira
):
    """L-b — a sprint changelog whose issue payload omits the sprint field
    (Jira can drop unchanged fields) must not clear the cached sprint."""
    from sessionfs.server.llm_keys import encrypt_secret

    org, user, key, project, ticket = await _ready(db_session)
    conn = (await db_session.execute(
        select(OrgTrackerConnection).where(OrgTrackerConnection.org_id == org.id)
    )).scalar_one()
    conn.sprint_field_id = "customfield_10010"
    conn.sync_status = True
    conn.encrypted_webhook_secret = encrypt_secret(_WEBHOOK_SECRET)
    conn.webhook_secret_hash = hashlib.sha256(_WEBHOOK_SECRET.encode()).hexdigest()
    conn.webhook_secret_last4 = _WEBHOOK_SECRET[-4:]
    await db_session.commit()

    resp = await client.post(
        f"/api/v1/projects/{project.id}/tickets/{ticket.id}/jira/link",
        headers=_hdrs(key), json={"issue_key": "ACME-1"},
    )
    assert resp.status_code == 200, resp.text

    link = (await db_session.execute(
        select(TicketTrackerLink).where(TicketTrackerLink.ticket_id == ticket.id)
    )).scalar_one()
    link.jira_sprint_id = "10"
    link.jira_sprint_name = "Sprint 1"
    link.jira_sprint_state = "active"
    await db_session.commit()

    # A sprint changelog item with a non-empty destination, but the issue
    # payload omits the sprint field entirely (an unrelated edit): the field is
    # merely absent, not emptied, so the cache must survive.
    payload = _webhook_payload(
        "jira:issue_updated",
        user={"displayName": "Bob", "accountId": "acc-bob"},
        changelog={
            "id": "20005",
            "items": [{"field": "Sprint", "fieldId": "customfield_10010",
                       "fromString": "Sprint 1", "toString": "Sprint 1"}],
        },
        issue={"key": "ACME-1", "id": "10001", "fields": {"summary": "unchanged"}},
    )
    status, _body = await _post_webhook(client, org.id, payload)
    assert status == 200

    link = (await db_session.execute(
        select(TicketTrackerLink)
        .where(TicketTrackerLink.ticket_id == ticket.id)
        .execution_options(populate_existing=True)
    )).scalar_one()
    assert link.jira_sprint_id == "10"
    assert link.jira_sprint_name == "Sprint 1"
    assert link.jira_sprint_state == "active"


@pytest.mark.asyncio
async def test_timeout_message_reports_real_budget(
    client: AsyncClient, db_session: AsyncSession, monkeypatch
):
    """L-d — the 504 message states the real 240 s deadline, not a stale figure."""
    org, user, key, project, ticket = await _ready(db_session)

    class TimeoutFake:
        def __init__(self, site_url, account_email, api_token, *, transport=None):
            self.site_url = validate_site_url(site_url)

        async def aclose(self):
            pass

        async def create_issue(self, *a, **k):
            raise asyncio.TimeoutError

    monkeypatch.setattr("sessionfs.server.routes.jira.JiraClient", TimeoutFake)

    resp = await client.post(
        f"/api/v1/projects/{project.id}/tickets/{ticket.id}/jira/publish", headers=_hdrs(key),
        json=_PUBLISH_BODY,
    )
    assert resp.status_code == 504
    assert resp.json()["error"]["details"]["error"] == "jira_timeout"
    assert resp.json()["error"]["message"] == (
        "Jira did not respond within 240 s; the issue may or may not have been created."
    )


@pytest.mark.asyncio
async def test_adopt_search_error_keeps_unknown_row(
    client: AsyncClient, db_session: AsyncSession, monkeypatch
):
    """L-f — an adopt search that fails upstream leaves the link ``unknown``
    (never releases the claim), so the next publish can still adopt-or-create."""
    org, user, key, project, ticket = await _ready(db_session)
    calls = {"create": 0, "search": 0}

    class TimeoutThenSearchErrorFake:
        def __init__(self, site_url, account_email, api_token, *, transport=None):
            self.site_url = validate_site_url(site_url)

        async def aclose(self):
            pass

        async def create_issue(self, *a, **k):
            calls["create"] += 1
            raise asyncio.TimeoutError

        async def search_issue_by_label(self, project_key, label):
            calls["search"] += 1
            raise TrackerError("Jira API 500: boom")

        async def update_issue(self, issue_key, *a, **k):
            raise AssertionError("no update expected on the adopt search path")

    monkeypatch.setattr("sessionfs.server.routes.jira.JiraClient", TimeoutThenSearchErrorFake)

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
    assert r2.status_code == 502, r2.text
    assert r2.json()["error"]["details"]["error"] == "jira_upstream_error"
    assert calls == {"create": 1, "search": 1}

    # The claim row survives as 'unknown' so a corrected retry can adopt/create.
    link = (await db_session.execute(
        select(TicketTrackerLink)
        .where(TicketTrackerLink.ticket_id == ticket.id)
        .execution_options(populate_existing=True)
    )).scalar_one()
    assert link.status == "unknown"
    assert link.claim_token is None


@pytest.mark.asyncio
async def test_unknown_reclaim_refreshes_staleness_but_preserves_unknown_since(db_session):
    """L-f — reclaiming an ``unknown`` row for an adopt refreshes its claim
    timestamp (so a concurrent publish can't treat it as stale) while still
    returning the durable ``unknown_since`` for adoption validation."""
    from sessionfs.server.routes.jira import _acquire_publish_claim

    org, _user, _key, project, ticket = await _ready(db_session)
    old = datetime.now(timezone.utc) - timedelta(minutes=10)
    db_session.add(TicketTrackerLink(
        ticket_id=ticket.id,
        org_id=org.id,
        provider="jira",
        status="unknown",
        site_url="https://acme.atlassian.net",
        jira_project_key="ACME",
        last_pushed_at=old,
        unknown_since=old,
    ))
    await db_session.commit()

    link, action, token, unknown_since = await _acquire_publish_claim(
        db_session,
        ticket_id=ticket.id,
        org_id=org.id,
        site_url="https://acme.atlassian.net",
        jira_project_key="ACME",
    )

    assert action == "adopt"
    assert token is not None
    # The durable (pre-reclaim) cut-off is preserved, not the refreshed clock.
    assert unknown_since == old
    # The reclaimed claim's staleness clock is refreshed.
    assert link.status == "pending"
    assert link.claim_token == token
    assert link.last_pushed_at is not None
    assert (datetime.utcnow() - link.last_pushed_at).total_seconds() < 60


@pytest.mark.asyncio
async def test_publish_create_400_error_map_names_sprint_and_parent_drops_both(
    client: AsyncClient, db_session: AsyncSession, fake_jira
):
    """L-e — a 400 whose body carries both ``errorMessages`` and an ``errors``
    map naming the sprint field and parent is parsed so the outbound ladder
    drops both fields and the publish still succeeds."""
    org, user, key, project, child = await _ready(db_session)
    await _set_conn_fields(db_session, org, sprint="customfield_10010", epic=None)
    child, _parent = await _link_parent(db_session, org, project, user, parent_key="PARENT-1")

    fake_jira.state["create_script"] = [
        {"error": (400, (
            "Jira API 400: One or more fields are invalid; "
            "customfield_10010: Field 'customfield_10010' cannot be set; "
            "parent: Field 'parent' cannot be set"
        ))},
    ]

    body = await _preview_publish_body(client, project, child, key, sprint_id=42)
    resp = await client.post(
        f"/api/v1/projects/{project.id}/tickets/{child.id}/jira/publish",
        headers=_hdrs(key), json=body,
    )
    assert resp.status_code == 200, resp.text
    assert set(resp.json()["warnings"]) == {
        "jira_parent_not_set",
        "jira_sprint_not_set:field_rejected",
    }
    # Both droppable fields were sent on the first attempt, then dropped in one
    # retry (no epic-link fallback configured).
    assert fake_jira.state["create_extra_fields"] == [
        {"customfield_10010": 42, "parent": {"key": "PARENT-1"}},
        None,
    ]
