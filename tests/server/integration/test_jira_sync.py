"""Integration tests for two-way Jira sync (GitHub #98 part B).

Covers the inbound webhook (HMAC auth, fail-closed, streamed body cap,
idempotency, echo suppression, the event kinds), the durable outbound outbox
(enqueue in the request, drain, sweep retry/backoff/exhaustion), completion
evidence, editor-only outbound, and cross-org isolation.

The Jira client is monkeypatched with an in-memory fake — no real Jira call is
ever made. The request-time drain is stubbed (``drained`` fixture) so a test
delivers queued rows explicitly with ``jira_sync.process_event`` or the sweep.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server.auth.keys import generate_api_key, hash_api_key
from sessionfs.server.db.models import (
    AgentRun,
    ApiKey,
    KnowledgeEntry,
    OrgAuditEvent,
    OrgMember,
    OrgTrackerConnection,
    Organization,
    Project,
    Session,
    Ticket,
    TicketComment,
    TicketDependency,
    TicketTrackerLink,
    TrackerProjectMapping,
    TrackerSyncEvent,
    TrustedReviewer,
    User,
)
from sessionfs.server.integrations.trackers.base import TrackerError
from sessionfs.server.llm_keys import encrypt_secret
from sessionfs.server.services import jira_sync

_SECRET = "whsec_test_secret_value_1234"
_TOKEN = "atlassian-secret-token-1234"
_SITE = "https://acme.atlassian.net"


# ── helpers ──


def _sign(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


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
    db.add(OrgMember(org_id=org.id, user_id=user.id, role=role, joined_at=datetime.now(timezone.utc)))
    await db.commit()
    return user, raw


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


async def _ticket(
    db: AsyncSession, project: Project, user: User, *, status: str = "open", kind: str = "task"
) -> Ticket:
    ticket = Ticket(
        id=f"tk_{uuid.uuid4().hex[:16]}",
        project_id=project.id,
        title="Ship the auth migration",
        description="line one line two long enough",
        acceptance_criteria=json.dumps(["users can log in"]),
        created_by_user_id=user.id,
        status=status,
        kind=kind,
    )
    db.add(ticket)
    await db.commit()
    await db.refresh(ticket)
    return ticket


async def _connection(
    db: AsyncSession,
    org: Organization,
    user: User,
    *,
    with_secret: bool = False,
    sync_status: bool = False,
    sync_comments: str = "off",
    status_map: str | None = None,
) -> OrgTrackerConnection:
    conn = OrgTrackerConnection(
        org_id=org.id,
        site_url=_SITE,
        account_email="jira@acme.com",
        encrypted_api_token=encrypt_secret(_TOKEN),
        default_template="agile",
        enabled=True,
        connected_by_user_id=user.id,
        sync_status=sync_status,
        sync_comments=sync_comments,
        status_map=status_map,
    )
    if with_secret:
        conn.encrypted_webhook_secret = encrypt_secret(_SECRET)
        conn.webhook_secret_hash = jira_sync.hash_webhook_secret(_SECRET)
        conn.webhook_secret_last4 = jira_sync.webhook_secret_last4(_SECRET)
    db.add(conn)
    await db.commit()
    return conn


async def _link(
    db: AsyncSession,
    org: Organization,
    ticket: Ticket,
    *,
    issue_key: str = "KEY-1",
    issue_id: str = "10001",
) -> TicketTrackerLink:
    link = TicketTrackerLink(
        ticket_id=ticket.id,
        org_id=org.id,
        provider="jira",
        status="linked",
        site_url=_SITE,
        issue_id=issue_id,
        issue_key=issue_key,
        issue_url=f"{_SITE}/browse/{issue_key}",
    )
    db.add(link)
    await db.commit()
    return link


async def _mapping(
    db: AsyncSession, org: Organization, project: Project, user: User
) -> TrackerProjectMapping:
    mapping = TrackerProjectMapping(
        id=f"tpm_{uuid.uuid4().hex[:16]}",
        org_id=org.id,
        project_id=project.id,
        jira_project_key="ACME",
        issue_type="Story",
        created_by_user_id=user.id,
    )
    db.add(mapping)
    await db.commit()
    return mapping


def _webhook_payload(event: str, **overrides: object) -> dict:
    base: dict = {"webhookEvent": event, "timestamp": 1700000000000}
    base.update(overrides)
    return base




_WEBHOOK = "/api/v1/webhooks/jira/{org}"
_AWS_KEY = "AKIAIOSFODNN7EXAMPLE"


async def _post_webhook(
    client: AsyncClient,
    org_id: str,
    payload: dict,
    *,
    secret: str | None = _SECRET,
    headers: dict | None = None,
) -> tuple[int, dict]:
    body = json.dumps(payload).encode()
    hdrs: dict[str, str] = {"content-type": "application/json", **(headers or {})}
    if secret is not None:
        hdrs["x-hub-signature"] = _sign(secret, body)
    resp = await client.post(_WEBHOOK.format(org=org_id), content=body, headers=hdrs)
    try:
        return resp.status_code, resp.json()
    except Exception:
        return resp.status_code, {}


async def _sync_event_rows(db: AsyncSession, ticket_id: str) -> list[TrackerSyncEvent]:
    return (
        (
            await db.execute(
                select(TrackerSyncEvent)
                .where(TrackerSyncEvent.ticket_id == ticket_id)
                .order_by(TrackerSyncEvent.created_at, TrackerSyncEvent.id)
                .execution_options(populate_existing=True)
            )
        )
        .scalars()
        .all()
    )


async def _comments(db: AsyncSession, ticket_id: str) -> list[TicketComment]:
    return (
        (
            await db.execute(
                select(TicketComment)
                .where(TicketComment.ticket_id == ticket_id)
                .execution_options(populate_existing=True)
            )
        )
        .scalars()
        .all()
    )


def _status_change(
    *,
    category: str,
    to_name: str,
    changelog_id: str = "20002",
    extra_items: list | None = None,
    user: dict | None = None,
) -> dict:
    return _webhook_payload(
        "jira:issue_updated",
        user=user or {"displayName": "Bob", "accountId": "acc-bob"},
        changelog={
            "id": changelog_id,
            "items": [
                {"field": "status", "fieldId": "status", "fromString": "In Progress", "toString": to_name},
                *(extra_items or []),
            ],
        },
        issue={
            "key": "KEY-1",
            "id": "10001",
            "fields": {"status": {"name": to_name, "statusCategory": {"key": category}}},
        },
    )


async def _contributor(db: AsyncSession, project: Project) -> tuple[User, str]:
    """A reader whose only access is a captured session on the project."""
    user, raw = await _user(db)
    db.add(
        Session(
            id=f"ses_{uuid.uuid4().hex[:16]}",
            user_id=user.id,
            source_tool="claude-code",
            blob_key="sessions/x/s.tar.gz",
            etag="etag",
            project_id=project.id,
        )
    )
    await db.commit()
    return user, raw


# ── fake Jira client + drain stub ──


@pytest.fixture
def fake_sync_jira(monkeypatch):
    calls: dict[str, list] = {
        "transition_issue": [], "add_comment": [], "validate_connection": [], "myself": [],
        "get_issue": [], "find_comment": [],
    }
    state: dict = {
        "fail_transition": False, "fail_comment": False, "hang": False, "account_id": "acc-self",
        "issue_status": "To Do", "hang_comment_once": False, "fail_lookup": False,
        # The workflow: whether the transition is offered right now, and the
        # status each transition name leads to (default: the same name).
        "offered": True, "workflow": {}, "pending": None,
        # Comments already on the Jira issue: (id, text-or-adf).
        "issue_comments": [],
    }

    class Fake:
        def __init__(self, site_url, account_email, api_token, *, transport=None):
            self.site_url = site_url
            self.account_email = account_email
            self.api_token = api_token

        async def aclose(self) -> None:
            pass

        async def validate_connection(self) -> dict:
            calls["validate_connection"].append(True)
            return {"account_id": state["account_id"], "display_name": "N", "email": self.account_email}

        async def myself_account_id(self) -> str:
            calls["myself"].append(True)
            return state["account_id"]

        async def find_transition(self, issue_key, transition_name):
            if state["fail_transition"]:
                raise TrackerError("Jira API 500: transition boom")
            if not state["offered"]:
                return None
            to_name = state["workflow"].get(transition_name, transition_name)
            state["pending"] = (transition_name, to_name)
            return {"id": "31", "name": transition_name, "to_name": to_name, "to_category": None}

        async def apply_transition(self, issue_key, transition_id):
            name, to_name = state["pending"]
            calls["transition_issue"].append((issue_key, name))
            if state["hang"]:
                await asyncio.sleep(5)
            state["issue_status"] = to_name

        async def get_issue(self, issue_key):
            from sessionfs.server.integrations.trackers.base import TrackerIssue

            calls["get_issue"].append(issue_key)
            return TrackerIssue(key=issue_key, url="u", status=state["issue_status"], updated_at=None)

        async def find_comment_with_marker(self, issue_key, marker):
            calls["find_comment"].append(marker)
            if state["fail_lookup"]:
                raise TrackerError("Jira response exceeded the size limit")
            for cid, body in state["issue_comments"]:
                if marker in json.dumps(body):
                    return cid
            return None

        async def add_comment(self, issue_key, adf):
            calls["add_comment"].append((issue_key, adf))
            if state["fail_comment"]:
                raise TrackerError("Jira API 500: comment boom")
            cid = f"jc{len(state['issue_comments']) + 1}"
            state["issue_comments"].append((cid, adf))
            if state["hang_comment_once"]:
                # The POST lands, but the client times out waiting for it.
                state["hang_comment_once"] = False
                await asyncio.sleep(5)
            return cid

    monkeypatch.setattr("sessionfs.server.services.jira_sync.JiraClient", Fake)
    monkeypatch.setattr("sessionfs.server.routes.jira.JiraClient", Fake)
    return {"calls": calls, "state": state}


@pytest.fixture(autouse=True)
def drained(monkeypatch):
    """Capture request-time drains instead of spawning background tasks."""
    queued: list[str] = []
    monkeypatch.setattr(jira_sync, "schedule_drain", lambda ids: queued.extend(ids))
    return queued


async def _deliver_all(db: AsyncSession, ids: list[str]) -> list[str]:
    return [await jira_sync.process_event(db, event_id) for event_id in ids]


# ── inbound webhook: auth, fail-closed, body cap ──


async def test_webhook_unknown_org_or_no_connection_returns_404(client: AsyncClient, db_session: AsyncSession):
    org = await _org(db_session)
    status, _ = await _post_webhook(client, org.id, _webhook_payload("comment_created"))
    assert status == 404


async def test_webhook_missing_secret_returns_503(client: AsyncClient, db_session: AsyncSession):
    owner, _ = await _user(db_session)
    org = await _org(db_session)
    await _connection(db_session, org, owner, with_secret=False)
    status, _ = await _post_webhook(client, org.id, _webhook_payload("comment_created"))
    assert status == 503


async def test_bad_signatures_are_counted_and_audited_at_most_hourly(
    client: AsyncClient, db_session: AsyncSession
):
    org = await _org(db_session)
    admin, raw = await _member(db_session, org, "admin")
    await _connection(db_session, org, admin, with_secret=True)
    body = json.dumps(_webhook_payload("comment_created")).encode()
    for _ in range(3):
        resp = await client.post(
            _WEBHOOK.format(org=org.id),
            content=body,
            headers={"x-hub-signature": "sha256=" + "0" * 64},
        )
        assert resp.status_code == 401
    audits = (
        await db_session.execute(
            select(OrgAuditEvent).where(
                OrgAuditEvent.org_id == org.id,
                OrgAuditEvent.event_type == "jira_webhook_auth_failure",
            )
        )
    ).scalars().all()
    assert len(audits) == 1  # coalesced: one row per org per hour
    conn = (
        await db_session.execute(select(OrgTrackerConnection).where(OrgTrackerConnection.org_id == org.id))
    ).scalar_one()
    assert conn.webhook_auth_failure_count == 3
    health = (
        await client.get(
            f"/api/v1/orgs/{org.id}/integrations/jira/health",
            headers={"Authorization": f"Bearer {raw}"},
        )
    ).json()
    assert health["webhook_auth_failures_24h"] == 3


async def test_auth_failure_window_resets_after_24h(client: AsyncClient, db_session: AsyncSession):
    owner, _ = await _user(db_session)
    org = await _org(db_session)
    conn = await _connection(db_session, org, owner, with_secret=True)
    conn.webhook_auth_failure_count = 40
    conn.webhook_auth_failure_window_start = datetime.now(timezone.utc) - timedelta(hours=30)
    await db_session.commit()
    resp = await client.post(
        _WEBHOOK.format(org=org.id), content=b"{}", headers={"x-hub-signature": "sha256=00"}
    )
    assert resp.status_code == 401
    conn = await db_session.get(OrgTrackerConnection, org.id, populate_existing=True)
    assert conn.webhook_auth_failure_count == 1


async def test_webhook_valid_signature_unknown_issue_is_ignored(client: AsyncClient, db_session: AsyncSession):
    owner, _ = await _user(db_session)
    org = await _org(db_session)
    await _connection(db_session, org, owner, with_secret=True)
    payload = _webhook_payload("comment_created", issue={"key": "NOPE-9", "id": "999"})
    status, body = await _post_webhook(client, org.id, payload)
    assert status == 200
    assert body == {"status": "ignored", "reason": "unknown_issue"}


async def test_webhook_oversized_body_returns_413(client: AsyncClient, db_session: AsyncSession):
    owner, _ = await _user(db_session)
    org = await _org(db_session)
    await _connection(db_session, org, owner, with_secret=True)
    huge = "x" * (jira_sync.MAX_INBOUND_BODY_BYTES + 1)
    body = json.dumps(_webhook_payload("comment_created", pad=huge)).encode()
    resp = await client.post(
        _WEBHOOK.format(org=org.id), content=body, headers={"x-hub-signature": _sign(_SECRET, body)}
    )
    assert resp.status_code == 413


async def test_webhook_chunked_body_over_cap_returns_413(client: AsyncClient, db_session: AsyncSession):
    """A chunked upload has no Content-Length; the streamed read still stops."""
    owner, _ = await _user(db_session)
    org = await _org(db_session)
    await _connection(db_session, org, owner, with_secret=True)
    chunk = b"x" * 65536
    sent = {"bytes": 0}

    async def gen():
        for _ in range(40):  # 2.5 MiB total
            sent["bytes"] += len(chunk)
            yield chunk

    resp = await client.post(
        _WEBHOOK.format(org=org.id),
        content=gen(),
        headers={"x-hub-signature": "sha256=" + "0" * 64},
    )
    assert resp.status_code == 413


async def test_webhook_unsupported_event_is_ignored(client: AsyncClient, db_session: AsyncSession):
    owner, _ = await _user(db_session)
    org = await _org(db_session)
    await _connection(db_session, org, owner, with_secret=True)
    status, body = await _post_webhook(client, org.id, _webhook_payload("jira:issue_deleted"))
    assert status == 200
    assert body["status"] == "ignored"


async def test_webhook_refuses_when_org_lost_jira_entitlement(client: AsyncClient, db_session: AsyncSession):
    owner, _ = await _user(db_session)
    org = await _org(db_session, tier="team")
    await _connection(db_session, org, owner, with_secret=True, sync_comments="inbound")
    project = await _project(db_session, owner, org)
    ticket = await _ticket(db_session, project, owner)
    await _link(db_session, org, ticket)
    payload = _webhook_payload(
        "comment_created",
        comment={"id": "30100", "author": {"displayName": "Alice"}, "body": "hi"},
        issue={"key": "KEY-1", "id": "10001"},
    )
    status, body = await _post_webhook(client, org.id, payload)
    assert status == 200
    assert body == {"status": "ignored", "reason": "not_entitled"}
    assert await _comments(db_session, ticket.id) == []
    events = await _sync_event_rows(db_session, ticket.id)
    assert [e.kind for e in events] == ["error"]
    assert "plan" in events[0].error


# ── inbound: field edits ──


async def test_allow_listed_field_edit_becomes_system_comment(client: AsyncClient, db_session: AsyncSession):
    owner, _ = await _user(db_session)
    org = await _org(db_session)
    await _connection(db_session, org, owner, with_secret=True)
    project = await _project(db_session, owner, org)
    ticket = await _ticket(db_session, project, owner)
    await _link(db_session, org, ticket)

    payload = _webhook_payload(
        "jira:issue_updated",
        user={"displayName": "Bob", "accountId": "acc-bob"},
        changelog={
            "id": "20001",
            "items": [
                {"field": "summary", "fromString": "Old", "toString": "New summary"},
                {"field": "labels", "fromString": "", "toString": "backend"},
                {"field": "Rank", "toString": "Ranked higher"},
            ],
        },
        issue={"key": "KEY-1", "id": "10001"},
    )
    status, _ = await _post_webhook(client, org.id, payload)
    assert status == 200

    events = await _sync_event_rows(db_session, ticket.id)
    assert [e.kind for e in events] == ["field_edit"]  # labels / Rank are not allow-listed
    assert events[0].jira_event_id == "jira:issue_updated:changelog:20001"
    comments = await _comments(db_session, ticket.id)
    assert len(comments) == 1
    assert comments[0].source == "jira" and comments[0].verdict_trusted is False
    assert "Edited in Jira by Bob: summary changed" in comments[0].content


async def test_field_edit_excerpt_is_redacted_before_cap_and_stored_redacted(
    client: AsyncClient, db_session: AsyncSession
):
    owner, _ = await _user(db_session)
    org = await _org(db_session)
    await _connection(db_session, org, owner, with_secret=True)
    project = await _project(db_session, owner, org)
    ticket = await _ticket(db_session, project, owner)
    await _link(db_session, org, ticket)

    # The secret straddles the 500-char cap: capping first would leave a
    # truncated prefix the scanner no longer recognizes.
    long_desc = "y" * 490 + " " + _AWS_KEY + " tail"
    payload = _webhook_payload(
        "jira:issue_updated",
        user={"displayName": "Bob"},
        changelog={"id": "20011", "items": [{"field": "description", "toString": long_desc}]},
        issue={"key": "KEY-1", "id": "10001"},
    )
    status, _ = await _post_webhook(client, org.id, payload)
    assert status == 200
    (event,) = await _sync_event_rows(db_session, ticket.id)
    stored = json.loads(event.payload)["excerpt"]
    assert "AKIA" not in stored
    assert len(stored) <= jira_sync.FIELD_EXCERPT_MAX_CHARS
    (comment,) = await _comments(db_session, ticket.id)
    assert "AKIA" not in comment.content


async def test_field_edit_from_connection_account_is_mirrored(client: AsyncClient, db_session: AsyncSession):
    """Admins often connect with their personal token: their own edits are
    real changes, not echoes (SessionFS never pushes field edits)."""
    owner, _ = await _user(db_session)
    org = await _org(db_session)
    conn = await _connection(db_session, org, owner, with_secret=True)
    conn.jira_account_id = "acc-self"
    await db_session.commit()
    project = await _project(db_session, owner, org)
    ticket = await _ticket(db_session, project, owner)
    await _link(db_session, org, ticket)
    payload = _webhook_payload(
        "jira:issue_updated",
        user={"displayName": "SessionFS bot", "accountId": "acc-self"},
        changelog={"id": "20012", "items": [{"field": "summary", "toString": "Ours"}]},
        issue={"key": "KEY-1", "id": "10001"},
    )
    status, _ = await _post_webhook(client, org.id, payload)
    assert status == 200
    (comment,) = await _comments(db_session, ticket.id)
    assert "summary changed" in comment.content
    assert [e.kind for e in await _sync_event_rows(db_session, ticket.id)] == ["field_edit"]


# ── inbound: status ──


async def test_real_done_transition_resolves_task_and_unblocks_dependents(
    client: AsyncClient, db_session: AsyncSession, drained
):
    owner, _ = await _user(db_session)
    org = await _org(db_session)
    await _connection(db_session, org, owner, with_secret=True, sync_status=True)
    project = await _project(db_session, owner, org)
    ticket = await _ticket(db_session, project, owner, status="review")
    dependent = await _ticket(db_session, project, owner, status="blocked")
    db_session.add(TicketDependency(ticket_id=dependent.id, depends_on_id=ticket.id))
    await db_session.commit()
    await _link(db_session, org, ticket)

    status, _ = await _post_webhook(client, org.id, _status_change(category="done", to_name="Done"))
    assert status == 200

    ticket = await db_session.get(Ticket, ticket.id, populate_existing=True)
    dependent = await db_session.get(Ticket, dependent.id, populate_existing=True)
    assert ticket.status == "done"
    assert ticket.resolved_at is not None
    assert dependent.status == "in_progress"  # auto-unblocked
    dep_comments = await _comments(db_session, dependent.id)
    assert any("completed" in c.content for c in dep_comments)

    events = await _sync_event_rows(db_session, ticket.id)
    inbound = [e for e in events if e.direction == "inbound"]
    assert len(inbound) == 1 and inbound[0].applied is True
    # Completion evidence is queued (and only evidence — no status echo back).
    outbound = [e for e in events if e.direction == "outbound"]
    assert [e.kind for e in outbound] == ["evidence"]
    assert outbound[0].state == "pending"
    assert drained == [outbound[0].id]


async def test_inbound_done_closes_issue_with_resolved_at(client: AsyncClient, db_session: AsyncSession):
    owner, _ = await _user(db_session)
    org = await _org(db_session)
    await _connection(db_session, org, owner, with_secret=True, sync_status=True)
    project = await _project(db_session, owner, org)
    issue = await _ticket(db_session, project, owner, status="in_progress", kind="issue")
    await _link(db_session, org, issue)
    status, _ = await _post_webhook(client, org.id, _status_change(category="done", to_name="Done"))
    assert status == 200
    issue = await db_session.get(Ticket, issue.id, populate_existing=True)
    assert issue.status == "closed"
    assert issue.resolved_at is not None


async def test_unrelated_edit_does_not_touch_status(client: AsyncClient, db_session: AsyncSession):
    """Every delivery carries the issue's current status; without a status
    changelog item it must never be (re)applied."""
    owner, _ = await _user(db_session)
    org = await _org(db_session)
    await _connection(db_session, org, owner, with_secret=True, sync_status=True)
    project = await _project(db_session, owner, org)
    ticket = await _ticket(db_session, project, owner, status="review")
    await _link(db_session, org, ticket)
    payload = _webhook_payload(
        "jira:issue_updated",
        user={"displayName": "Bob"},
        changelog={"id": "20020", "items": [{"field": "summary", "toString": "Renamed"}]},
        issue={
            "key": "KEY-1",
            "id": "10001",
            "fields": {"status": {"statusCategory": {"key": "done"}}},
        },
    )
    status, _ = await _post_webhook(client, org.id, payload)
    assert status == 200
    assert (await db_session.get(Ticket, ticket.id, populate_existing=True)).status == "review"
    assert [e.kind for e in await _sync_event_rows(db_session, ticket.id)] == ["field_edit"]


async def test_default_map_ignores_moves_back_to_todo_or_in_progress(
    client: AsyncClient, db_session: AsyncSession
):
    owner, _ = await _user(db_session)
    org = await _org(db_session)
    await _connection(db_session, org, owner, with_secret=True, sync_status=True)
    project = await _project(db_session, owner, org)
    ticket = await _ticket(db_session, project, owner, status="review")
    await _link(db_session, org, ticket)
    for i, (cat, name) in enumerate((("indeterminate", "In Progress"), ("new", "To Do"))):
        status, _ = await _post_webhook(
            client, org.id, _status_change(category=cat, to_name=name, changelog_id=f"2003{i}")
        )
        assert status == 200
    assert (await db_session.get(Ticket, ticket.id, populate_existing=True)).status == "review"
    assert await _sync_event_rows(db_session, ticket.id) == []


async def test_category_mapped_to_none_does_nothing(client: AsyncClient, db_session: AsyncSession):
    owner, _ = await _user(db_session)
    org = await _org(db_session)
    await _connection(
        db_session,
        org,
        owner,
        with_secret=True,
        sync_status=True,
        status_map=json.dumps({"categories": {"done": "none"}}),
    )
    project = await _project(db_session, owner, org)
    ticket = await _ticket(db_session, project, owner, status="review")
    await _link(db_session, org, ticket)
    status, _ = await _post_webhook(client, org.id, _status_change(category="done", to_name="Done"))
    assert status == 200
    assert (await db_session.get(Ticket, ticket.id, populate_existing=True)).status == "review"


async def test_human_done_from_connection_account_resolves(client: AsyncClient, db_session: AsyncSession):
    """The connection's accountId alone never makes a delivery an echo."""
    owner, _ = await _user(db_session)
    org = await _org(db_session)
    conn = await _connection(db_session, org, owner, with_secret=True, sync_status=True)
    conn.jira_account_id = "acc-self"
    await db_session.commit()
    project = await _project(db_session, owner, org)
    ticket = await _ticket(db_session, project, owner, status="review")
    await _link(db_session, org, ticket)
    payload = _status_change(category="done", to_name="Done", user={"displayName": "Ada", "accountId": "acc-self"})
    status, _ = await _post_webhook(client, org.id, payload)
    assert status == 200
    assert (await db_session.get(Ticket, ticket.id, populate_existing=True)).status == "done"
    inbound = [e for e in await _sync_event_rows(db_session, ticket.id) if e.direction == "inbound"]
    assert inbound[0].applied is True


async def _recent_push(db: AsyncSession, org, link, ticket, *, to_status: str, transition: str) -> None:
    jira_sync.record_sync_event(
        db,
        org_id=org.id,
        link_id=link.ticket_id,
        ticket_id=ticket.id,
        direction="outbound",
        kind="status",
        payload={"to_status": to_status, "transition": transition},
        applied=True,
    )
    await db.commit()


async def test_echo_of_our_pushed_transition_is_ignored(client: AsyncClient, db_session: AsyncSession):
    """We pushed Done for a ticket that was then reopened locally; the late
    webhook echo of that push must not re-resolve it."""
    owner, _ = await _user(db_session)
    org = await _org(db_session)
    conn = await _connection(db_session, org, owner, with_secret=True, sync_status=True)
    conn.jira_account_id = "acc-self"
    await db_session.commit()
    project = await _project(db_session, owner, org)
    ticket = await _ticket(db_session, project, owner, status="review")
    link = await _link(db_session, org, ticket)
    await _recent_push(db_session, org, link, ticket, to_status="done", transition="Done")
    payload = _status_change(category="done", to_name="Done", user={"displayName": "bot", "accountId": "acc-self"})
    status, _ = await _post_webhook(client, org.id, payload)
    assert status == 200
    assert (await db_session.get(Ticket, ticket.id, populate_existing=True)).status == "review"
    inbound = [e for e in await _sync_event_rows(db_session, ticket.id) if e.direction == "inbound"]
    assert inbound[0].state == "skipped" and json.loads(inbound[0].payload)["outcome"] == "echo"


async def test_recent_push_for_another_status_or_actor_is_not_an_echo(
    client: AsyncClient, db_session: AsyncSession
):
    owner, _ = await _user(db_session)
    org = await _org(db_session)
    conn = await _connection(db_session, org, owner, with_secret=True, sync_status=True)
    conn.jira_account_id = "acc-self"
    await db_session.commit()
    project = await _project(db_session, owner, org)
    # Same transition name, but pushed for a different SessionFS status.
    t1 = await _ticket(db_session, project, owner, status="review")
    l1 = await _link(db_session, org, t1, issue_key="KEY-1")
    await _recent_push(db_session, org, l1, t1, to_status="closed", transition="Done")
    status, _ = await _post_webhook(client, org.id, _status_change(category="done", to_name="Done"))
    assert status == 200
    assert (await db_session.get(Ticket, t1.id, populate_existing=True)).status == "done"
    # Matching push, but a different Jira user made this change.
    t2 = await _ticket(db_session, project, owner, status="review")
    l2 = await _link(db_session, org, t2, issue_key="KEY-2", issue_id="10002")
    await _recent_push(db_session, org, l2, t2, to_status="done", transition="Done")
    payload = _status_change(category="done", to_name="Done", changelog_id="20099", user={"accountId": "acc-human"})
    payload["issue"]["key"], payload["issue"]["id"] = "KEY-2", "10002"
    status, _ = await _post_webhook(client, org.id, payload)
    assert status == 200
    assert (await db_session.get(Ticket, t2.id, populate_existing=True)).status == "done"


async def test_illegal_inbound_status_recorded_not_forced_and_counted(
    client: AsyncClient, db_session: AsyncSession
):
    org = await _org(db_session)
    admin, raw = await _member(db_session, org, "admin")
    await _connection(db_session, org, admin, with_secret=True, sync_status=True)
    project = await _project(db_session, admin, org)
    ticket = await _ticket(db_session, project, admin, status="open")  # open → done illegal
    await _link(db_session, org, ticket)

    status, _ = await _post_webhook(client, org.id, _status_change(category="done", to_name="Done"))
    assert status == 200
    assert (await db_session.get(Ticket, ticket.id, populate_existing=True)).status == "open"  # never forced
    (event,) = await _sync_event_rows(db_session, ticket.id)
    assert event.applied is False and event.error
    assert json.loads(event.payload)["outcome"] == "refused"
    health = (
        await client.get(
            f"/api/v1/orgs/{org.id}/integrations/jira/health",
            headers={"Authorization": f"Bearer {raw}"},
        )
    ).json()
    assert health["failed_sync_events_24h"] == 1


# ── inbound: comments ──


async def test_comment_created_mirrors_inbound_not_trusted_and_redacted(
    client: AsyncClient, db_session: AsyncSession
):
    owner, _ = await _user(db_session)
    org = await _org(db_session)
    await _connection(db_session, org, owner, with_secret=True, sync_comments="inbound")
    project = await _project(db_session, owner, org)
    ticket = await _ticket(db_session, project, owner)
    await _link(db_session, org, ticket)

    payload = _webhook_payload(
        "comment_created",
        comment={
            "id": "30001",
            "author": {"displayName": "Alice", "accountId": "acc-alice"},
            "body": "Use key sk-ant-abc123def456ghi789jkl012mno345 please",
        },
        issue={"key": "KEY-1", "id": "10001"},
    )
    status, _ = await _post_webhook(client, org.id, payload)
    assert status == 200
    (comment,) = await _comments(db_session, ticket.id)
    assert comment.source == "jira"
    assert comment.verdict_trusted is False
    assert comment.author_persona == "Alice (Jira)"
    assert "sk-ant-" not in comment.content


async def test_inbound_comment_and_author_are_capped_and_redacted(client: AsyncClient, db_session: AsyncSession):
    owner, _ = await _user(db_session)
    org = await _org(db_session)
    await _connection(db_session, org, owner, with_secret=True, sync_comments="both")
    project = await _project(db_session, owner, org)
    ticket = await _ticket(db_session, project, owner)
    await _link(db_session, org, ticket)
    payload = _webhook_payload(
        "comment_created",
        comment={
            "id": "30002",
            "author": {"displayName": _AWS_KEY + " " + "N" * 80},
            "body": "z" * 25_000,
        },
        issue={"key": "KEY-1", "id": "10001"},
    )
    status, _ = await _post_webhook(client, org.id, payload)
    assert status == 200
    (comment,) = await _comments(db_session, ticket.id)
    assert len(comment.content) <= jira_sync.INBOUND_COMMENT_MAX_CHARS
    assert comment.author_persona.endswith(" (Jira)")
    assert len(comment.author_persona) <= 50
    assert "AKIA" not in comment.author_persona


async def test_connection_account_human_comment_is_mirrored(client: AsyncClient, db_session: AsyncSession):
    owner, _ = await _user(db_session)
    org = await _org(db_session)
    conn = await _connection(db_session, org, owner, with_secret=True, sync_comments="both")
    conn.jira_account_id = "acc-self"
    await db_session.commit()
    project = await _project(db_session, owner, org)
    ticket = await _ticket(db_session, project, owner)
    await _link(db_session, org, ticket)
    payload = _webhook_payload(
        "comment_created",
        comment={"id": "30150", "author": {"accountId": "acc-self", "displayName": "Ada"}, "body": "Looks good"},
        issue={"key": "KEY-1", "id": "10001"},
    )
    status, _ = await _post_webhook(client, org.id, payload)
    assert status == 200
    (comment,) = await _comments(db_session, ticket.id)
    assert comment.content == "Looks good" and comment.author_persona == "Ada (Jira)"


async def test_echo_of_our_pushed_comment_is_dropped(
    client: AsyncClient, db_session: AsyncSession, fake_sync_jira
):
    owner, _ = await _user(db_session)
    org = await _org(db_session)
    await _connection(db_session, org, owner, with_secret=True, sync_comments="both")
    project = await _project(db_session, owner, org)
    ticket = await _ticket(db_session, project, owner)
    await _link(db_session, org, ticket)
    comment = TicketComment(
        id=f"tc_{uuid.uuid4().hex[:16]}", ticket_id=ticket.id, author_user_id=owner.id, content="ship it"
    )
    db_session.add(comment)
    await db_session.commit()
    first = (await jira_sync.enqueue_comment(db_session, ticket, comment.id, reason="send_to_jira"))[0]
    second = (await jira_sync.enqueue_comment(db_session, ticket, comment.id, reason="send_to_jira"))[0]
    await db_session.commit()
    assert await jira_sync.process_event(db_session, first) == "done"
    row = await db_session.get(TrackerSyncEvent, first, populate_existing=True)
    assert row.jira_event_id == "pushed-comment:jc1"
    (_cid, posted_adf) = fake_sync_jira["state"]["issue_comments"][0]

    # (a) The echo of the post, recognised by its recorded Jira comment id.
    echo = _webhook_payload(
        "comment_created",
        comment={"id": "jc1", "author": {"displayName": "bot"}, "body": posted_adf},
        issue={"key": "KEY-1", "id": "10001"},
    )
    assert (await _post_webhook(client, org.id, echo))[0] == 200
    # (b) A post whose id was never recorded (e.g. lost commit), recognised by
    # the outbox reference it carries.
    echo2 = _webhook_payload(
        "comment_created",
        comment={"id": "jc99", "author": {"displayName": "bot"},
                 "body": f"via SessionFS — ship it\n[{jira_sync.outbox_marker(second)}]"},
        issue={"key": "KEY-1", "id": "10001"},
    )
    assert (await _post_webhook(client, org.id, echo2))[0] == 200
    # (c) A reference to a row of another link is not ours here → mirrored.
    forged = _webhook_payload(
        "comment_created",
        comment={"id": "jc100", "author": {"displayName": "Eve"},
                 "body": f"[{jira_sync.outbox_marker('tse_' + '0' * 32)}]"},
        issue={"key": "KEY-1", "id": "10001"},
    )
    assert (await _post_webhook(client, org.id, forged))[0] == 200
    mirrored = [c for c in await _comments(db_session, ticket.id) if c.source == "jira"]
    assert [c.author_persona for c in mirrored] == ["Eve (Jira)"]


async def test_comment_created_ignored_when_sync_comments_off(client: AsyncClient, db_session: AsyncSession):
    owner, _ = await _user(db_session)
    org = await _org(db_session)
    await _connection(db_session, org, owner, with_secret=True, sync_comments="outbound")
    project = await _project(db_session, owner, org)
    ticket = await _ticket(db_session, project, owner)
    await _link(db_session, org, ticket)
    payload = _webhook_payload(
        "comment_created",
        comment={"id": "30003", "author": {"displayName": "Alice"}, "body": "hi"},
        issue={"key": "KEY-1", "id": "10001"},
    )
    status, _ = await _post_webhook(client, org.id, payload)
    assert status == 200
    assert await _comments(db_session, ticket.id) == []


# ── idempotency ──


async def test_replay_with_delivery_identifier_is_duplicate(client: AsyncClient, db_session: AsyncSession):
    owner, _ = await _user(db_session)
    org = await _org(db_session)
    await _connection(db_session, org, owner, with_secret=True)
    project = await _project(db_session, owner, org)
    ticket = await _ticket(db_session, project, owner)
    await _link(db_session, org, ticket)
    payload = _webhook_payload(
        "jira:issue_updated",
        changelog={"id": "20004", "items": [{"field": "summary", "toString": "X"}]},
        user={"displayName": "Bob"},
        issue={"key": "KEY-1", "id": "10001"},
    )
    hdr = {"X-Atlassian-Webhook-Identifier": "991"}
    status, _ = await _post_webhook(client, org.id, payload, headers=hdr)
    assert status == 200
    # A retry: same identifier, payload re-serialized with a new timestamp.
    status2, body2 = await _post_webhook(client, org.id, {**payload, "timestamp": 1}, headers=hdr)
    assert status2 == 200 and body2 == {"status": "duplicate"}
    events = await _sync_event_rows(db_session, ticket.id)
    assert [e.jira_event_id for e in events] == ["wh:991"]
    assert len(await _comments(db_session, ticket.id)) == 1


async def test_replay_without_identifier_dedupes_on_jira_ids(client: AsyncClient, db_session: AsyncSession):
    owner, _ = await _user(db_session)
    org = await _org(db_session)
    await _connection(db_session, org, owner, with_secret=True)
    project = await _project(db_session, owner, org)
    ticket = await _ticket(db_session, project, owner)
    await _link(db_session, org, ticket)
    payload = _webhook_payload(
        "jira:issue_updated",
        changelog={"id": "20005", "items": [{"field": "summary", "toString": "X"}]},
        issue={"key": "KEY-1", "id": "10001"},
    )
    assert (await _post_webhook(client, org.id, payload))[0] == 200
    assert (await _post_webhook(client, org.id, payload))[1] == {"status": "duplicate"}
    assert len(await _comments(db_session, ticket.id)) == 1


@pytest.mark.parametrize("comment_first", [True, False])
async def test_comment_created_never_deduped_against_issue_updated(
    client: AsyncClient, db_session: AsyncSession, comment_first: bool
):
    """Jira sends ``comment_created`` and an ``issue_updated`` carrying the same
    comment; whichever arrives first, the comment is still mirrored once."""
    owner, _ = await _user(db_session)
    org = await _org(db_session)
    await _connection(db_session, org, owner, with_secret=True, sync_status=True, sync_comments="inbound")
    project = await _project(db_session, owner, org)
    ticket = await _ticket(db_session, project, owner)
    await _link(db_session, org, ticket)
    jira_comment = {"id": "30009", "author": {"displayName": "Alice"}, "body": "looks good"}
    created = _webhook_payload("comment_created", comment=jira_comment, issue={"key": "KEY-1", "id": "10001"})
    updated = _webhook_payload(
        "jira:issue_updated",
        issue_event_type_name="issue_commented",
        comment=jira_comment,
        user={"displayName": "Alice"},
        changelog={"items": [{"field": "summary", "toString": "Also renamed"}]},
        issue={"key": "KEY-1", "id": "10001"},
    )
    order = [created, updated] if comment_first else [updated, created]
    for payload in order:
        status, body = await _post_webhook(client, org.id, payload)
        assert status == 200 and body == {"status": "ok"}
    comments = await _comments(db_session, ticket.id)
    assert sum(1 for c in comments if c.content == "looks good") == 1
    assert sum(1 for c in comments if c.content.startswith("Edited in Jira")) == 1


# ── link resolution / isolation / data exposure ──


async def test_inbound_link_lookup_handles_duplicates_and_other_sites(
    client: AsyncClient, db_session: AsyncSession
):
    owner, _ = await _user(db_session)
    org = await _org(db_session)
    await _connection(db_session, org, owner, with_secret=True, sync_comments="inbound")
    project = await _project(db_session, owner, org)
    older = await _ticket(db_session, project, owner)
    newer = await _ticket(db_session, project, owner)
    other_site = await _ticket(db_session, project, owner)
    now = datetime.now(timezone.utc)
    l1 = await _link(db_session, org, older)
    l2 = await _link(db_session, org, newer)
    l3 = await _link(db_session, org, other_site)
    l1.last_pushed_at = now - timedelta(days=2)
    l2.last_pushed_at = now
    l3.site_url = "https://other.atlassian.net"
    l3.last_pushed_at = now + timedelta(days=1)
    await db_session.commit()
    payload = _webhook_payload(
        "comment_created",
        comment={"id": "30200", "author": {"displayName": "Alice"}, "body": "dup"},
        issue={"key": "KEY-1", "id": "10001"},
    )
    status, _ = await _post_webhook(client, org.id, payload)
    assert status == 200
    assert len(await _comments(db_session, newer.id)) == 1
    assert await _comments(db_session, older.id) == []
    assert await _comments(db_session, other_site.id) == []


async def test_webhook_cross_org_isolation(client: AsyncClient, db_session: AsyncSession):
    owner_a, _ = await _user(db_session)
    owner_b, _ = await _user(db_session)
    org_a = await _org(db_session)
    org_b = await _org(db_session)
    await _connection(db_session, org_a, owner_a, with_secret=True, sync_comments="inbound")
    await _connection(db_session, org_b, owner_b, with_secret=True, sync_comments="inbound")
    ticket_a = await _ticket(db_session, await _project(db_session, owner_a, org_a), owner_a)
    ticket_b = await _ticket(db_session, await _project(db_session, owner_b, org_b), owner_b)
    await _link(db_session, org_a, ticket_a, issue_key="KEY-1")
    await _link(db_session, org_b, ticket_b, issue_key="KEY-1")
    payload = _webhook_payload(
        "comment_created",
        comment={"id": "40001", "author": {"displayName": "Alice"}, "body": "cross-org"},
        issue={"key": "KEY-1", "id": "10001"},
    )
    status, _ = await _post_webhook(client, org_a.id, payload)
    assert status == 200
    assert len(await _comments(db_session, ticket_a.id)) == 1
    assert await _comments(db_session, ticket_b.id) == []
    # org B's secret does not authenticate a delivery addressed to org A.
    status, _ = await _post_webhook(client, org_b.id, payload, secret="not-the-secret")
    assert status == 401


async def test_webhook_response_reveals_no_ticket_data(client: AsyncClient, db_session: AsyncSession):
    owner, _ = await _user(db_session)
    org = await _org(db_session)
    await _connection(db_session, org, owner, with_secret=True)
    project = await _project(db_session, owner, org)
    ticket = await _ticket(db_session, project, owner)
    await _link(db_session, org, ticket)
    payload = _webhook_payload(
        "jira:issue_updated",
        changelog={"id": "20006", "items": [{"field": "summary", "toString": "X"}]},
        issue={"key": "KEY-1", "id": "10001"},
    )
    status, body = await _post_webhook(client, org.id, payload)
    assert status == 200
    assert body == {"status": "ok"}
    assert ticket.title not in json.dumps(body)


# ── admin surface: secret, connection merge, sync PATCH, events ──


async def test_webhook_secret_generate_returns_once_and_stores_encrypted(
    client: AsyncClient, db_session: AsyncSession
):
    org = await _org(db_session)
    admin, raw = await _member(db_session, org, "admin")
    resp = await client.post(
        f"/api/v1/orgs/{org.id}/integrations/jira/webhook-secret",
        headers={"Authorization": f"Bearer {raw}"},
    )
    assert resp.status_code == 404  # a connection must exist first

    await _connection(db_session, org, admin, with_secret=False)
    resp = await client.post(
        f"/api/v1/orgs/{org.id}/integrations/jira/webhook-secret",
        headers={"Authorization": f"Bearer {raw}"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["webhook_secret_last4"] == data["webhook_secret"][-4:]
    conn = await db_session.get(OrgTrackerConnection, org.id, populate_existing=True)
    assert conn.encrypted_webhook_secret and data["webhook_secret"] not in conn.encrypted_webhook_secret
    read = (
        await client.get(f"/api/v1/orgs/{org.id}/integrations/jira", headers={"Authorization": f"Bearer {raw}"})
    ).json()
    assert data["webhook_secret"] not in json.dumps(read)
    assert read["webhook_url"].endswith(f"/api/v1/webhooks/jira/{org.id}")


async def test_connection_put_merges_sync_settings(
    client: AsyncClient, db_session: AsyncSession, fake_sync_jira
):
    org = await _org(db_session)
    admin, raw = await _member(db_session, org, "admin")
    await _connection(
        db_session,
        org,
        admin,
        sync_status=True,
        sync_comments="both",
        status_map=json.dumps({"transitions": {"done": "Ship it"}}),
    )
    headers = {"Authorization": f"Bearer {raw}"}
    body = {"site_url": _SITE, "account_email": "jira@acme.com", "api_token": "new-token", "default_template": "agile"}
    resp = await client.put(f"/api/v1/orgs/{org.id}/integrations/jira", json=body, headers=headers)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["sync_status"] is True
    assert data["sync_comments"] == "both"
    assert data["status_map"]["transitions"]["done"] == "Ship it"
    assert (await db_session.get(OrgTrackerConnection, org.id, populate_existing=True)).jira_account_id == "acc-self"

    # Explicit fields are applied.
    resp = await client.put(
        f"/api/v1/orgs/{org.id}/integrations/jira", json={**body, "sync_comments": "off"}, headers=headers
    )
    assert resp.json()["sync_comments"] == "off" and resp.json()["sync_status"] is True


async def test_patch_sync_settings_merges_without_token(client: AsyncClient, db_session: AsyncSession):
    org = await _org(db_session)
    admin, raw = await _member(db_session, org, "admin")
    member, raw_member = await _member(db_session, org, "member")
    headers = {"Authorization": f"Bearer {raw}"}
    url = f"/api/v1/orgs/{org.id}/integrations/jira/sync"
    assert (await client.patch(url, json={"sync_status": True}, headers=headers)).status_code == 404

    await _connection(db_session, org, admin, sync_comments="inbound")
    resp = await client.patch(url, json={"sync_status": True}, headers=headers)
    assert resp.status_code == 200
    assert resp.json()["sync_status"] is True and resp.json()["sync_comments"] == "inbound"

    resp = await client.patch(
        url,
        json={"status_map": {"transitions": {"review": "In Review", "done": ""}, "categories": {"new": "reopen", "done": None}}},
        headers=headers,
    )
    assert resp.status_code == 200
    smap = resp.json()["status_map"]
    assert smap["transitions"]["review"] == "In Review"
    assert smap["transitions"]["done"] == "none"
    assert smap["categories"] == {"new": "reopen", "indeterminate": "none", "done": "none"}

    bad = await client.patch(url, json={"status_map": {"categories": {"done": "explode"}}}, headers=headers)
    assert bad.status_code == 422
    bad = await client.patch(url, json={"sync_comments": "sideways"}, headers=headers)
    assert bad.status_code == 422
    forbidden = await client.patch(url, json={"sync_status": False}, headers={"Authorization": f"Bearer {raw_member}"})
    assert forbidden.status_code == 403


async def test_list_jira_sync_events_returns_shaped_ledger_and_filters(
    client: AsyncClient, db_session: AsyncSession
):
    org = await _org(db_session)
    admin, raw = await _member(db_session, org, "admin")
    await _connection(db_session, org, admin, sync_status=True)
    project = await _project(db_session, admin, org)
    ticket = await _ticket(db_session, project, admin)
    link = await _link(db_session, org, ticket)
    jira_sync.record_sync_event(
        db_session,
        org_id=org.id,
        link_id=link.ticket_id,
        ticket_id=ticket.id,
        direction="outbound",
        kind="status",
        payload={"to_status": "in_progress", "transition": "In Progress"},
        state="pending",
    )
    await db_session.commit()
    jira_sync.record_sync_event(
        db_session,
        org_id=org.id,
        link_id=link.ticket_id,
        ticket_id=ticket.id,
        direction="inbound",
        kind="comment",
        payload={"comment_id": "c1"},
        jira_event_id="comment_created:comment:c1",
        applied=True,
    )
    await db_session.commit()

    headers = {"Authorization": f"Bearer {raw}"}
    events = (await client.get(f"/api/v1/orgs/{org.id}/integrations/jira/sync-events", headers=headers)).json()["events"]
    assert [e["direction"] for e in events] == ["inbound", "outbound"]
    assert events[0]["issue_key"] == "KEY-1" and events[0]["state"] == "done"
    assert events[1]["state"] == "pending" and events[1]["retry_count"] == 0
    resp = await client.get(f"/api/v1/orgs/{org.id}/integrations/jira/sync-events?direction=outbound", headers=headers)
    assert [e["direction"] for e in resp.json()["events"]] == ["outbound"]


async def test_list_jira_sync_events_requires_admin(client: AsyncClient, db_session: AsyncSession):
    org = await _org(db_session)
    admin, _ = await _member(db_session, org, "admin")
    _member_user, raw = await _member(db_session, org, "member")
    await _connection(db_session, org, admin)
    resp = await client.get(
        f"/api/v1/orgs/{org.id}/integrations/jira/sync-events",
        headers={"Authorization": f"Bearer {raw}"},
    )
    assert resp.status_code == 403


async def test_ticket_jira_sync_events_for_member_and_empty_without_link(
    client: AsyncClient, db_session: AsyncSession
):
    org = await _org(db_session)
    admin, raw = await _member(db_session, org, "admin")
    await _connection(db_session, org, admin, sync_status=True)
    project = await _project(db_session, admin, org)
    await _mapping(db_session, org, project, admin)
    linked = await _ticket(db_session, project, admin)
    unlinked = await _ticket(db_session, project, admin)
    link = await _link(db_session, org, linked)
    jira_sync.record_sync_event(
        db_session,
        org_id=org.id,
        link_id=link.ticket_id,
        ticket_id=linked.id,
        direction="outbound",
        kind="status",
        payload={"to_status": "done", "transition": "Done"},
        applied=True,
    )
    await db_session.commit()
    headers = {"Authorization": f"Bearer {raw}"}
    events = (
        await client.get(f"/api/v1/projects/{project.id}/tickets/{linked.id}/jira-sync-events", headers=headers)
    ).json()["events"]
    assert len(events) == 1 and events[0]["payload"]["to_status"] == "done"
    resp = await client.get(f"/api/v1/projects/{project.id}/tickets/{unlinked.id}/jira-sync-events", headers=headers)
    assert resp.status_code == 200 and resp.json()["events"] == []


async def test_jira_link_reports_comment_forwarding_for_editors_only(
    client: AsyncClient, db_session: AsyncSession, fake_sync_jira, monkeypatch
):
    org = await _org(db_session)
    admin, raw = await _member(db_session, org, "admin")
    await _connection(db_session, org, admin, sync_comments="both")
    project = await _project(db_session, admin, org)
    await _mapping(db_session, org, project, admin)
    ticket = await _ticket(db_session, project, admin)
    link = await _link(db_session, org, ticket)
    link.last_status_checked_at = datetime.now(timezone.utc)  # skip the live GET
    await db_session.commit()
    data = (
        await client.get(
            f"/api/v1/projects/{project.id}/tickets/{ticket.id}/jira",
            headers={"Authorization": f"Bearer {raw}"},
        )
    ).json()
    assert data["comment_sync_outbound"] is True and data["can_send_to_jira"] is True

    async def no_edit(*_a, **_k):
        return False

    monkeypatch.setattr("sessionfs.server.auth.project_access.user_can_edit_project", no_edit)
    data = (
        await client.get(
            f"/api/v1/projects/{project.id}/tickets/{ticket.id}/jira",
            headers={"Authorization": f"Bearer {raw}"},
        )
    ).json()
    assert data["can_send_to_jira"] is False


# ── outbound: request-time enqueue ──


async def test_editor_status_change_enqueues_status_and_evidence_then_delivers(
    client: AsyncClient, db_session: AsyncSession, fake_sync_jira, drained
):
    owner, raw = await _user(db_session)
    org = await _org(db_session)
    await _connection(db_session, org, owner, sync_status=True)
    project = await _project(db_session, owner, org)
    ticket = await _ticket(db_session, project, owner, status="review")
    await _link(db_session, org, ticket)

    resp = await client.post(
        f"/api/v1/projects/{project.id}/tickets/{ticket.id}/accept",
        headers={"Authorization": f"Bearer {raw}"},
    )
    assert resp.status_code == 200
    rows = await _sync_event_rows(db_session, ticket.id)
    assert sorted(e.kind for e in rows) == ["evidence", "status"]
    assert all(e.state == "pending" for e in rows)
    assert sorted(drained) == sorted(e.id for e in rows)

    outcomes = await _deliver_all(db_session, drained)
    assert outcomes == ["done", "done"]
    assert fake_sync_jira["calls"]["transition_issue"] == [("KEY-1", "Done")]
    assert len(fake_sync_jira["calls"]["add_comment"]) == 1  # the evidence
    conn = await db_session.get(OrgTrackerConnection, org.id, populate_existing=True)
    assert conn.jira_account_id == "acc-self"  # learned for echo suppression
    # A second drain of the same rows is a no-op (already delivered).
    assert await _deliver_all(db_session, drained) == ["not_due", "not_due"]


async def test_reader_status_change_sends_nothing_outbound(
    client: AsyncClient, db_session: AsyncSession, drained
):
    owner, raw_owner = await _user(db_session)
    org = await _org(db_session)
    await _connection(
        db_session,
        org,
        owner,
        sync_status=True,
        status_map=json.dumps({"transitions": {"blocked": "Block"}}),
    )
    project = await _project(db_session, owner, org)
    reader, raw_reader = await _contributor(db_session, project)
    reader_ticket = await _ticket(db_session, project, reader, status="in_progress")
    owner_ticket = await _ticket(db_session, project, owner, status="in_progress")
    await _link(db_session, org, reader_ticket, issue_key="KEY-1")
    await _link(db_session, org, owner_ticket, issue_key="KEY-2")

    resp = await client.post(
        f"/api/v1/projects/{project.id}/tickets/{reader_ticket.id}/block",
        headers={"Authorization": f"Bearer {raw_reader}"},
    )
    assert resp.status_code == 200, resp.text
    assert await _sync_event_rows(db_session, reader_ticket.id) == []
    assert drained == []

    resp = await client.post(
        f"/api/v1/projects/{project.id}/tickets/{owner_ticket.id}/block",
        headers={"Authorization": f"Bearer {raw_owner}"},
    )
    assert resp.status_code == 200
    (row,) = await _sync_event_rows(db_session, owner_ticket.id)
    assert json.loads(row.payload)["transition"] == "Block"


async def test_unmapped_status_is_not_queued(client: AsyncClient, db_session: AsyncSession, drained):
    owner, raw = await _user(db_session)
    org = await _org(db_session)
    await _connection(db_session, org, owner, sync_status=True)  # blocked → none by default
    project = await _project(db_session, owner, org)
    ticket = await _ticket(db_session, project, owner, status="in_progress")
    await _link(db_session, org, ticket)
    resp = await client.post(
        f"/api/v1/projects/{project.id}/tickets/{ticket.id}/block",
        headers={"Authorization": f"Bearer {raw}"},
    )
    assert resp.status_code == 200
    assert await _sync_event_rows(db_session, ticket.id) == []


async def test_enqueue_failure_does_not_fail_ticket_request(
    client: AsyncClient, db_session: AsyncSession, monkeypatch
):
    owner, raw = await _user(db_session)
    org = await _org(db_session)
    await _connection(db_session, org, owner, sync_status=True)
    project = await _project(db_session, owner, org)
    ticket = await _ticket(db_session, project, owner, status="review")
    await _link(db_session, org, ticket)

    async def boom(*_a, **_k):
        raise RuntimeError("outbox down")

    monkeypatch.setattr(jira_sync, "enqueue_status_change", boom)
    resp = await client.post(
        f"/api/v1/projects/{project.id}/tickets/{ticket.id}/accept",
        headers={"Authorization": f"Bearer {raw}"},
    )
    assert resp.status_code == 200
    assert (await db_session.get(Ticket, ticket.id, populate_existing=True)).status == "done"


async def test_lost_drain_is_delivered_by_the_sweep(
    client: AsyncClient, db_session: AsyncSession, fake_sync_jira, drained
):
    owner, raw = await _user(db_session)
    org = await _org(db_session)
    await _connection(db_session, org, owner, sync_status=True)
    project = await _project(db_session, owner, org)
    ticket = await _ticket(db_session, project, owner, status="open")
    await _link(db_session, org, ticket)
    resp = await client.post(
        f"/api/v1/projects/{project.id}/tickets/{ticket.id}/start",
        headers={"Authorization": f"Bearer {raw}"},
    )
    assert resp.status_code == 200
    assert len(drained) == 1  # the background task "died" (stubbed out)

    result = await jira_sync.run_jira_sync_sweep(db_session)
    assert result["succeeded"] == 1
    assert fake_sync_jira["calls"]["transition_issue"] == [("KEY-1", "In Progress")]


# ── outbound comments ──


async def _trusted_reviewer(db: AsyncSession, project: Project, user: User) -> None:
    db.add(
        TrustedReviewer(
            id=f"tr_{uuid.uuid4().hex[:16]}",
            org_id=None,
            project_id=project.id,
            user_id=user.id,
            reviewer_persona="codex-reviewer",
            is_active=True,
            created_by_user_id=user.id,
            created_at=datetime.now(timezone.utc),
        )
    )
    await db.commit()


async def test_send_to_jira_is_editor_only(client: AsyncClient, db_session: AsyncSession, drained):
    owner, raw_owner = await _user(db_session)
    org = await _org(db_session)
    await _connection(db_session, org, owner, sync_comments="outbound")
    project = await _project(db_session, owner, org)
    ticket = await _ticket(db_session, project, owner)
    await _link(db_session, org, ticket)
    _reader, raw_reader = await _contributor(db_session, project)

    async def comment(raw, content):
        return await client.post(
            f"/api/v1/projects/{project.id}/tickets/{ticket.id}/comments",
            json={"content": content, "send_to_jira": True},
            headers={"Authorization": f"Bearer {raw}"},
        )

    assert (await comment(raw_owner, "ship it")).status_code == 201
    assert (await comment(raw_reader, "me too")).status_code == 201  # flag ignored
    rows = await _sync_event_rows(db_session, ticket.id)
    assert [e.kind for e in rows] == ["comment"]
    assert json.loads(rows[0].payload)["reason"] == "send_to_jira"


async def test_send_to_jira_refused_when_comment_sync_not_outbound(client: AsyncClient, db_session: AsyncSession):
    owner, raw = await _user(db_session)
    org = await _org(db_session)
    await _connection(db_session, org, owner, sync_comments="inbound")
    project = await _project(db_session, owner, org)
    ticket = await _ticket(db_session, project, owner)
    unlinked = await _ticket(db_session, project, owner)
    await _link(db_session, org, ticket)
    for t in (ticket, unlinked):
        resp = await client.post(
            f"/api/v1/projects/{project.id}/tickets/{t.id}/comments",
            json={"content": "push me", "send_to_jira": True},
            headers={"Authorization": f"Bearer {raw}"},
        )
        assert resp.status_code == 409
        assert resp.json()["error"]["details"]["error"] == "jira_comment_sync_off"
        assert await _comments(db_session, t.id) == []  # nothing written


@pytest.mark.parametrize(
    ("verdict", "forwarded"),
    [("VERIFIED-CLEAN", True), ("CHANGES-REQUESTED", False)],
)
async def test_trusted_verdict_forwarded_only_when_verified_clean(
    client: AsyncClient, db_session: AsyncSession, verdict: str, forwarded: bool
):
    owner, raw = await _user(db_session)
    org = await _org(db_session)
    await _connection(db_session, org, owner, sync_comments="both")
    project = await _project(db_session, owner, org)
    ticket = await _ticket(db_session, project, owner)
    await _link(db_session, org, ticket)
    await _trusted_reviewer(db_session, project, owner)
    resp = await client.post(
        f"/api/v1/projects/{project.id}/tickets/{ticket.id}/comments",
        json={"content": f"Codex R1 review on {ticket.id}: {verdict}\n\nnotes", "author_persona": "codex-reviewer"},
        headers={"Authorization": f"Bearer {raw}"},
    )
    assert resp.status_code == 201
    rows = await _sync_event_rows(db_session, ticket.id)
    assert bool(rows) is forwarded
    if forwarded:
        assert json.loads(rows[0].payload)["reason"] == "verified_clean"


async def test_untrusted_verified_clean_is_not_forwarded(client: AsyncClient, db_session: AsyncSession):
    owner, raw = await _user(db_session)
    org = await _org(db_session)
    await _connection(db_session, org, owner, sync_comments="both")
    project = await _project(db_session, owner, org)
    ticket = await _ticket(db_session, project, owner)
    await _link(db_session, org, ticket)
    resp = await client.post(
        f"/api/v1/projects/{project.id}/tickets/{ticket.id}/comments",
        json={"content": f"Codex R1 review on {ticket.id}: VERIFIED-CLEAN", "author_persona": "codex-reviewer"},
        headers={"Authorization": f"Bearer {raw}"},
    )
    assert resp.status_code == 201
    assert await _sync_event_rows(db_session, ticket.id) == []


async def test_outbound_comment_delivery_redacts_and_fits_budget(
    db_session: AsyncSession, fake_sync_jira
):
    from sessionfs.server.integrations.trackers.promote import adf_text_length

    owner, _ = await _user(db_session)
    org = await _org(db_session)
    await _connection(db_session, org, owner, sync_comments="outbound")
    project = await _project(db_session, owner, org)
    ticket = await _ticket(db_session, project, owner)
    await _link(db_session, org, ticket)
    comment = TicketComment(
        id=f"tc_{uuid.uuid4().hex[:16]}",
        ticket_id=ticket.id,
        author_user_id=owner.id,
        content=f"key {_AWS_KEY} " + "w" * 40_000,
    )
    db_session.add(comment)
    await db_session.commit()
    (event_id,) = await jira_sync.enqueue_comment(db_session, ticket, comment.id, reason="send_to_jira")
    await db_session.commit()
    assert await jira_sync.process_event(db_session, event_id) == "done"
    (_key, adf) = fake_sync_jira["calls"]["add_comment"][0]
    assert adf_text_length(adf) <= jira_sync.OUTBOUND_COMMENT_ADF_BUDGET
    assert "AKIA" not in json.dumps(adf)
    assert "via SessionFS" in json.dumps(adf)


# ── outbox delivery: failures, backoff, exhaustion, sweep ──


async def _pending_status_row(db: AsyncSession, org, link, ticket, **overrides) -> TrackerSyncEvent:
    event = jira_sync.record_sync_event(
        db,
        org_id=org.id,
        link_id=link.ticket_id,
        ticket_id=ticket.id,
        direction="outbound",
        kind="status",
        payload={"to_status": ticket.status, "transition": "In Progress"},
        state="pending",
        next_attempt_at=datetime.now(timezone.utc) - timedelta(seconds=1),
    )
    for key, value in overrides.items():
        setattr(event, key, value)
    await db.commit()
    return event


async def _status_fixture(db: AsyncSession, **conn_kw):
    owner, _ = await _user(db)
    org = await _org(db)
    await _connection(db, org, owner, sync_status=True, **conn_kw)
    project = await _project(db, owner, org)
    ticket = await _ticket(db, project, owner, status="in_progress")
    link = await _link(db, org, ticket)
    return org, project, ticket, link


async def test_failed_delivery_backs_off_and_sweep_respects_the_gate(
    db_session: AsyncSession, fake_sync_jira
):
    org, _project_, ticket, link = await _status_fixture(db_session)
    event = await _pending_status_row(db_session, org, link, ticket)
    fake_sync_jira["state"]["fail_transition"] = True
    assert await jira_sync.process_event(db_session, event.id) == "failed"
    row = await db_session.get(TrackerSyncEvent, event.id, populate_existing=True)
    assert row.state == "pending" and row.retry_count == 1 and "boom" in row.error
    assert jira_sync._aware(row.next_attempt_at) > datetime.now(timezone.utc) + timedelta(seconds=30)

    # Not due yet: the sweep leaves it alone.
    result = await jira_sync.run_jira_sync_sweep(db_session)
    assert result["attempted"] == 0
    assert fake_sync_jira["calls"]["transition_issue"] == []  # nothing reached Jira

    # Once due, the sweep retries and a success clears the error.
    row.next_attempt_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    await db_session.commit()
    fake_sync_jira["state"]["fail_transition"] = False
    result = await jira_sync.run_jira_sync_sweep(db_session)
    assert result["succeeded"] == 1
    assert fake_sync_jira["calls"]["transition_issue"] == [("KEY-1", "In Progress")]
    row = await db_session.get(TrackerSyncEvent, event.id, populate_existing=True)
    assert row.state == "done" and row.error is None and row.applied is True


def test_backoff_is_exponential():
    assert [jira_sync.backoff_seconds(n) for n in (1, 2, 3, 4)] == [60, 240, 960, 3840]


async def test_event_exhausts_after_five_attempts(db_session: AsyncSession, fake_sync_jira):
    org, _p, ticket, link = await _status_fixture(db_session)
    event = await _pending_status_row(db_session, org, link, ticket, retry_count=4)
    fake_sync_jira["state"]["fail_transition"] = True
    assert await jira_sync.process_event(db_session, event.id) == "exhausted"
    row = await db_session.get(TrackerSyncEvent, event.id, populate_existing=True)
    assert row.state == "exhausted" and row.retry_count == 5 and row.next_attempt_at is None
    # Exhausted rows are never selected again.
    result = await jira_sync.run_jira_sync_sweep(db_session)
    assert result["due"] == 0


async def test_terminal_rows_do_not_starve_a_new_failure(db_session: AsyncSession, fake_sync_jira):
    org, _p, ticket, link = await _status_fixture(db_session)
    past = datetime.now(timezone.utc) - timedelta(hours=1)
    for i in range(100):
        state = ("exhausted", "skipped", "done")[i % 3]
        db_session.add(
            TrackerSyncEvent(
                id=f"tse_old{i:04d}{uuid.uuid4().hex[:8]}",
                org_id=org.id,
                link_id=link.ticket_id,
                ticket_id=ticket.id,
                direction="outbound",
                kind="status",
                payload="{}",
                error="old failure",
                state=state,
                retry_count=5 if state == "exhausted" else 0,
                created_at=past,
            )
        )
    await db_session.commit()
    fresh = await _pending_status_row(db_session, org, link, ticket)
    result = await jira_sync.run_jira_sync_sweep(db_session, limit=10)
    assert result["due"] == 1 and result["succeeded"] == 1
    assert (await db_session.get(TrackerSyncEvent, fresh.id, populate_existing=True)).state == "done"


async def test_per_event_exception_is_recorded_and_sweep_continues(
    db_session: AsyncSession, fake_sync_jira, monkeypatch
):
    org, _p, ticket, link = await _status_fixture(db_session)
    first = await _pending_status_row(db_session, org, link, ticket)
    second = await _pending_status_row(db_session, org, link, ticket)
    real_deliver = jira_sync._deliver

    async def flaky(db, event):
        if event.id == first.id:
            raise RuntimeError("secret internals")
        return await real_deliver(db, event)

    monkeypatch.setattr(jira_sync, "_deliver", flaky)
    result = await jira_sync.run_jira_sync_sweep(db_session)
    assert result["failed"] == 1 and result["succeeded"] == 1
    row = await db_session.get(TrackerSyncEvent, first.id, populate_existing=True)
    assert row.state == "pending" and row.retry_count == 1
    assert row.error == "internal error (RuntimeError)"
    assert (await db_session.get(TrackerSyncEvent, second.id, populate_existing=True)).state == "done"


async def test_timeout_on_second_attempt_records_failure(
    db_session: AsyncSession, fake_sync_jira, monkeypatch
):
    org, _p, ticket, link = await _status_fixture(db_session)
    event = await _pending_status_row(db_session, org, link, ticket)
    monkeypatch.setattr(jira_sync, "OUTBOUND_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(jira_sync, "OUTBOUND_RETRY_DELAY_SECONDS", 0)
    fake_sync_jira["state"]["hang"] = True
    assert await jira_sync.process_event(db_session, event.id) == "failed"
    assert len(fake_sync_jira["calls"]["transition_issue"]) == 2  # first try + one retry
    row = await db_session.get(TrackerSyncEvent, event.id, populate_existing=True)
    assert row.error == "Jira request timed out" and row.retry_count == 1


async def test_jira_missing_transition_is_an_error(db_session: AsyncSession, fake_sync_jira):
    org, _p, ticket, link = await _status_fixture(db_session)
    event = await _pending_status_row(db_session, org, link, ticket)
    fake_sync_jira["state"]["fail_transition"] = True
    await jira_sync.process_event(db_session, event.id)
    assert "transition boom" in (await db_session.get(TrackerSyncEvent, event.id, populate_existing=True)).error


async def test_superseded_and_unmapped_and_unentitled_rows_are_skipped(
    db_session: AsyncSession, fake_sync_jira
):
    org, _p, ticket, link = await _status_fixture(db_session)
    stale = await _pending_status_row(db_session, org, link, ticket)
    stale.payload = json.dumps({"to_status": "open", "transition": "To Do"})
    await db_session.commit()
    assert await jira_sync.process_event(db_session, stale.id) == "skipped"

    org.tier = "team"
    await db_session.commit()
    lost = await _pending_status_row(db_session, org, link, ticket)
    assert await jira_sync.process_event(db_session, lost.id) == "skipped"
    row = await db_session.get(TrackerSyncEvent, lost.id, populate_existing=True)
    assert "plan" in row.error and json.loads(row.payload)["skip_reason"] == "not_entitled"
    assert fake_sync_jira["calls"]["transition_issue"] == []


async def test_expired_terminal_rows_are_deleted_pending_rows_kept(db_session: AsyncSession, fake_sync_jira):
    org, _p, ticket, link = await _status_fixture(db_session)
    old = datetime.now(timezone.utc) - timedelta(days=120)
    for state in ("done", "pending"):
        db_session.add(
            TrackerSyncEvent(
                id=f"tse_{state}{uuid.uuid4().hex[:8]}",
                org_id=org.id,
                link_id=link.ticket_id,
                ticket_id=ticket.id,
                direction="outbound",
                kind="status",
                payload=json.dumps({"to_status": "in_progress"}),
                state=state,
                created_at=old,
                next_attempt_at=old,
            )
        )
    await db_session.commit()
    result = await jira_sync.run_jira_sync_sweep(db_session)
    assert result["expired_events"] == 1
    assert result["succeeded"] == 1


async def test_sweep_route_requires_platform_sweeper(client: AsyncClient, auth_headers: dict):
    resp = await client.post("/api/v1/admin/sweeps/jira-sync", json={}, headers=auth_headers)
    assert resp.status_code == 403


async def test_sweep_route_runs_for_platform_admin(client: AsyncClient, db_session: AsyncSession):
    from tests.server.integration.test_platform_sweeps_scope import _sweep_key

    _admin, _org_id, _key, headers = await _sweep_key(db_session)
    resp = await client.post(
        "/api/v1/admin/sweeps/jira-sync", json={"limit": 5, "budget_seconds": 2}, headers=headers
    )
    assert resp.status_code == 200
    assert resp.json()["budget_exhausted"] is False


async def test_sweep_stops_at_the_wall_clock_budget(db_session: AsyncSession, fake_sync_jira):
    org, _p, ticket, link = await _status_fixture(db_session)
    for _ in range(3):
        await _pending_status_row(db_session, org, link, ticket)
    result = await jira_sync.run_jira_sync_sweep(db_session, budget_seconds=0)
    assert result["budget_exhausted"] is True and result["attempted"] == 0


# ── completion evidence ──


async def test_evidence_is_claimed_once_per_completion(db_session: AsyncSession, fake_sync_jira):
    owner, _ = await _user(db_session)
    org = await _org(db_session)
    await _connection(db_session, org, owner, sync_status=True)
    project = await _project(db_session, owner, org)
    ticket = await _ticket(db_session, project, owner, status="done")
    ticket.resolved_at = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)
    await db_session.commit()
    link = await _link(db_session, org, ticket)

    first = await jira_sync.enqueue_evidence(db_session, ticket, link)
    await db_session.commit()
    assert first is not None
    assert await jira_sync.enqueue_evidence(db_session, ticket, link) is None
    await db_session.commit()
    # Two concurrent drains of the same row: only one delivers.
    assert await jira_sync.process_event(db_session, first) == "done"
    assert await jira_sync.process_event(db_session, first) == "not_due"
    assert len(fake_sync_jira["calls"]["add_comment"]) == 1

    # Reopen + re-resolve → a new completion posts fresh evidence.
    ticket.resolved_at = datetime(2026, 10, 2, 12, 0, tzinfo=timezone.utc)
    await db_session.commit()
    second = await jira_sync.enqueue_evidence(db_session, ticket, link)
    await db_session.commit()
    assert second is not None and second != first


async def test_evidence_failure_does_not_roll_back_status_push(
    db_session: AsyncSession, fake_sync_jira, monkeypatch
):
    owner, _ = await _user(db_session)
    org = await _org(db_session)
    await _connection(db_session, org, owner, sync_status=True)
    project = await _project(db_session, owner, org)
    ticket = await _ticket(db_session, project, owner, status="done")
    ticket.resolved_at = datetime.now(timezone.utc)
    await db_session.commit()
    await _link(db_session, org, ticket)
    ids = await jira_sync.enqueue_status_change(db_session, ticket)
    await db_session.commit()

    async def broken(*_a, **_k):
        raise ValueError("cannot build")

    ticket_id = ticket.id
    monkeypatch.setattr(jira_sync, "build_evidence_adf", broken)
    outcomes = await _deliver_all(db_session, ids)
    assert outcomes == ["done", "failed"]
    rows = {e.kind: e for e in await _sync_event_rows(db_session, ticket_id)}
    assert rows["status"].state == "done" and rows["status"].applied is True
    assert rows["evidence"].state == "pending" and rows["evidence"].error == "internal error (ValueError)"


async def test_evidence_adf_includes_verdict_persona_and_badge(db_session: AsyncSession):
    owner, _ = await _user(db_session)
    org = await _org(db_session)
    project = await _project(db_session, owner, org)
    ticket = await _ticket(db_session, project, owner, status="done")
    ticket.assigned_to = "atlas"
    db_session.add(
        AgentRun(
            id=f"run_{uuid.uuid4().hex[:16]}",
            project_id=project.id,
            persona_name="atlas",
            tool="claude-code",
            trigger_source="manual",
            ticket_id=ticket.id,
            status="passed",
            triggered_by_user_id=owner.id,
        )
    )
    await db_session.commit()
    text = json.dumps(await jira_sync.build_evidence_adf(db_session, ticket, org))
    assert "SessionFS completion evidence" in text
    assert "Verdict: passed" in text
    assert "atlas" in text
    assert "Badge" in text


async def test_evidence_caps_files_and_claims(db_session: AsyncSession, monkeypatch):
    owner, _ = await _user(db_session)
    org = await _org(db_session)
    project = await _project(db_session, owner, org)
    ticket = await _ticket(db_session, project, owner, status="done")
    ids = []
    for i in range(7):
        entry = KnowledgeEntry(
            project_id=project.id,
            session_id="s",
            user_id=owner.id,
            entry_type="decision",
            content=f"Claim number {i}\nbody",
        )
        db_session.add(entry)
        await db_session.flush()
        ids.append(entry.id)
    ticket.knowledge_entry_ids = json.dumps(ids)
    await db_session.commit()

    async def many_files(_sessions):
        return [{"path": f"src/f{i}.py", "operation": "modified", "added": 1, "removed": 0} for i in range(30)]

    monkeypatch.setattr(jira_sync, "_ticket_file_changes", many_files)
    text = json.dumps(await jira_sync.build_evidence_adf(db_session, ticket, org))
    assert text.count("src/f") == jira_sync.EVIDENCE_FILE_CAP
    assert "and 10 more" in text
    assert "Knowledge base claims" in text and "(7)" in text
    assert sum(text.count(f"Claim number {i}") for i in range(7)) == jira_sync.EVIDENCE_KB_CAP


async def test_evidence_reads_only_the_tickets_own_project(db_session: AsyncSession):
    owner, _ = await _user(db_session)
    org = await _org(db_session)
    project = await _project(db_session, owner, org)
    other = await _project(db_session, owner, org)

    def session(project_id: str, *, deleted: bool = False) -> Session:
        s = Session(
            id=f"ses_{uuid.uuid4().hex[:16]}",
            user_id=owner.id,
            source_tool="claude-code",
            blob_key="sessions/x/s.tar.gz",
            etag="etag",
            project_id=project_id,
            is_deleted=deleted,
        )
        db_session.add(s)
        return s

    own = session(project.id)
    foreign = session(other.id)
    deleted = session(project.id, deleted=True)

    def claim(project_id: str, content: str, **kw) -> KnowledgeEntry:
        e = KnowledgeEntry(
            project_id=project_id, session_id="s", user_id=owner.id, entry_type="decision", content=content, **kw
        )
        db_session.add(e)
        return e

    mine = claim(project.id, "Mine")
    theirs = claim(other.id, "Theirs")
    dismissed = claim(project.id, "Dismissed", dismissed=True)
    await db_session.flush()
    superseded = claim(project.id, "Superseded", superseded_by=mine.id)
    await db_session.flush()
    ticket = await _ticket(db_session, project, owner, status="done")
    ticket.resolver_session_id = foreign.id
    ticket.related_sessions = json.dumps([own.id, deleted.id])
    ticket.knowledge_entry_ids = json.dumps(
        [str(mine.id), theirs.id, dismissed.id, superseded.id, "abc", True, None]
    )
    await db_session.commit()

    sessions = await jira_sync.linked_sessions(db_session, ticket)
    assert [s.id for s in sessions] == [own.id]
    assert await jira_sync.ticket_claims(db_session, ticket) == ["Mine"]


# ── outbound idempotency: timeouts and redelivery ──


async def _comment_row(db: AsyncSession, **conn_kw):
    owner, _ = await _user(db)
    org = await _org(db)
    await _connection(db, org, owner, sync_comments="outbound", **conn_kw)
    project = await _project(db, owner, org)
    ticket = await _ticket(db, project, owner)
    await _link(db, org, ticket)
    comment = TicketComment(
        id=f"tc_{uuid.uuid4().hex[:16]}", ticket_id=ticket.id, author_user_id=owner.id, content="ship it"
    )
    db.add(comment)
    await db.commit()
    (event_id,) = await jira_sync.enqueue_comment(db, ticket, comment.id, reason="send_to_jira")
    await db.commit()
    return event_id


async def test_comment_post_that_landed_before_a_timeout_is_not_duplicated(
    db_session: AsyncSession, fake_sync_jira, monkeypatch
):
    monkeypatch.setattr(jira_sync, "OUTBOUND_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(jira_sync, "OUTBOUND_RETRY_DELAY_SECONDS", 0)
    event_id = await _comment_row(db_session)
    fake_sync_jira["state"]["hang_comment_once"] = True
    assert await jira_sync.process_event(db_session, event_id) == "done"
    assert len(fake_sync_jira["calls"]["add_comment"]) == 1  # retry found the marker
    assert len(fake_sync_jira["state"]["issue_comments"]) == 1
    row = await db_session.get(TrackerSyncEvent, event_id, populate_existing=True)
    assert row.jira_event_id == "pushed-comment:jc1" and row.error is None


async def test_comment_redelivery_after_lost_commit_does_not_repost(
    db_session: AsyncSession, fake_sync_jira
):
    event_id = await _comment_row(db_session)
    # The comment already reached Jira on a previous delivery whose commit
    # was lost: the issue carries our reference.
    marker = jira_sync.outbox_marker(event_id)
    fake_sync_jira["state"]["issue_comments"].append(("jc7", {"text": f"via SessionFS — ship it [{marker}]"}))
    assert await jira_sync.process_event(db_session, event_id) == "done"
    assert fake_sync_jira["calls"]["add_comment"] == []
    row = await db_session.get(TrackerSyncEvent, event_id, populate_existing=True)
    assert row.jira_event_id == "pushed-comment:jc7"


async def test_posted_comments_and_evidence_carry_the_outbox_reference(
    db_session: AsyncSession, fake_sync_jira
):
    event_id = await _comment_row(db_session)
    await jira_sync.process_event(db_session, event_id)
    (_key, adf) = fake_sync_jira["calls"]["add_comment"][0]
    assert jira_sync.outbox_marker(event_id) in json.dumps(adf)


async def test_transition_already_applied_counts_as_done(db_session: AsyncSession, fake_sync_jira):
    """Redelivery of a transition that landed (lost commit): Jira no longer
    offers it, but the issue is already at the target — not an error."""
    org, _p, ticket, link = await _status_fixture(db_session)
    event = await _pending_status_row(db_session, org, link, ticket)
    fake_sync_jira["state"]["offered"] = False
    fake_sync_jira["state"]["issue_status"] = "In Progress"
    assert await jira_sync.process_event(db_session, event.id) == "done"
    row = await db_session.get(TrackerSyncEvent, event.id, populate_existing=True)
    assert row.error is None and json.loads(row.payload)["result"] == "already_at_target"


async def test_transition_timeout_that_landed_is_not_retried(
    db_session: AsyncSession, fake_sync_jira, monkeypatch
):
    org, _p, ticket, link = await _status_fixture(db_session)
    event = await _pending_status_row(db_session, org, link, ticket)
    monkeypatch.setattr(jira_sync, "OUTBOUND_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(jira_sync, "OUTBOUND_RETRY_DELAY_SECONDS", 0)
    fake_sync_jira["state"]["hang"] = True
    fake_sync_jira["state"]["issue_status"] = "In Progress"  # it landed
    assert await jira_sync.process_event(db_session, event.id) == "done"
    assert len(fake_sync_jira["calls"]["transition_issue"]) == 1


def test_can_send_to_jira_requires_a_current_link():
    from sessionfs.server.routes.jira import _can_send_to_jira

    project = Project(id="p", name="p", owner_id="u", org_id="org_a")
    conn = OrgTrackerConnection(org_id="org_a", site_url=_SITE, enabled=True, sync_comments="both")
    link = TicketTrackerLink(ticket_id="t", org_id="org_a", status="linked", site_url=_SITE, issue_key="K-1")
    assert _can_send_to_jira(link, conn, project, can_edit=True) is True
    assert _can_send_to_jira(link, conn, project, can_edit=False) is False
    stale = TicketTrackerLink(
        ticket_id="t", org_id="org_a", status="linked", site_url="https://old.atlassian.net", issue_key="K-1"
    )
    assert _can_send_to_jira(stale, conn, project, can_edit=True) is False
    moved = TicketTrackerLink(ticket_id="t", org_id="org_b", status="linked", site_url=_SITE, issue_key="K-1")
    assert _can_send_to_jira(moved, conn, project, can_edit=True) is False


# ── follow-ups: quoted refs, marker lookup page, custom transitions ──


async def test_reply_quoting_a_sessionfs_ref_is_still_mirrored(
    client: AsyncClient, db_session: AsyncSession, fake_sync_jira
):
    owner, _ = await _user(db_session)
    org = await _org(db_session)
    conn = await _connection(db_session, org, owner, with_secret=True, sync_comments="both")
    conn.jira_account_id = "acc-self"
    await db_session.commit()
    project = await _project(db_session, owner, org)
    ticket = await _ticket(db_session, project, owner)
    await _link(db_session, org, ticket)
    comment = TicketComment(
        id=f"tc_{uuid.uuid4().hex[:16]}", ticket_id=ticket.id, author_user_id=owner.id, content="ship it"
    )
    db_session.add(comment)
    await db_session.commit()
    (ours,) = await jira_sync.enqueue_comment(db_session, ticket, comment.id, reason="send_to_jira")
    await db_session.commit()
    ref = f"[{jira_sync.outbox_marker(ours)}]"

    # A different person pastes our reference as the last line of their reply.
    pasted = _webhook_payload(
        "comment_created",
        comment={"id": "jc500", "author": {"accountId": "acc-human", "displayName": "Ann"},
                 "body": f"Agreed with this:\nvia SessionFS — ship it\n{ref}"},
        issue={"key": "KEY-1", "id": "10001"},
    )
    # Our own account quotes it mid-text (not as the final line).
    quoted = _webhook_payload(
        "comment_created",
        comment={"id": "jc501", "author": {"accountId": "acc-self", "displayName": "Ada"},
                 "body": f"See {ref} above — reopening."},
        issue={"key": "KEY-1", "id": "10001"},
    )
    # Genuinely ours: our account, reference as the final line.
    echo = _webhook_payload(
        "comment_created",
        comment={"id": "jc502", "author": {"accountId": "acc-self", "displayName": "bot"},
                 "body": f"via SessionFS — ship it\n{ref}"},
        issue={"key": "KEY-1", "id": "10001"},
    )
    for payload in (pasted, quoted, echo):
        assert (await _post_webhook(client, org.id, payload))[0] == 200
    mirrored = sorted(c.author_persona for c in await _comments(db_session, ticket.id) if c.source == "jira")
    assert mirrored == ["Ada (Jira)", "Ann (Jira)"]


async def test_marker_lookup_failure_falls_back_to_posting(db_session: AsyncSession, fake_sync_jira):
    event_id = await _comment_row(db_session)
    fake_sync_jira["state"]["fail_lookup"] = True
    assert await jira_sync.process_event(db_session, event_id) == "done"
    assert len(fake_sync_jira["calls"]["add_comment"]) == 1
    row = await db_session.get(TrackerSyncEvent, event_id, populate_existing=True)
    assert row.error is None and row.jira_event_id == "pushed-comment:jc1"


async def test_marker_lookup_reads_a_small_newest_first_page():
    import httpx

    from sessionfs.server.integrations.trackers import JiraClient

    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"comments": [{"id": "9", "body": "x [SessionFS ref tse_a]"}]})

    client = JiraClient(_SITE, "jira@acme.com", "tok", transport=httpx.MockTransport(handler))
    try:
        assert await client.find_comment_with_marker("KEY-1", "SessionFS ref tse_a") == "9"
    finally:
        await client.aclose()
    params = dict(seen[0].url.params)
    assert params["maxResults"] == "20" and params["orderBy"] == "-created"


async def test_custom_transition_with_different_target_status_is_idempotent(
    db_session: AsyncSession, fake_sync_jira
):
    """A custom map: transition "Resolve" leads to status "Resolved". The
    target comes from the transition's ``to`` status and is persisted before
    the POST, so a redelivery after a lost commit (Jira no longer offers
    "Resolve") still recognises the issue is already there."""
    org, _p, ticket, link = await _status_fixture(
        db_session, status_map=json.dumps({"transitions": {"in_progress": "Resolve"}})
    )
    fake_sync_jira["state"]["workflow"] = {"Resolve": "Resolved"}
    event = await _pending_status_row(db_session, org, link, ticket)
    event_id = event.id
    assert await jira_sync.process_event(db_session, event_id) == "done"
    row = await db_session.get(TrackerSyncEvent, event_id, populate_existing=True)
    assert json.loads(row.payload)["target_status"] == "Resolved"
    assert fake_sync_jira["state"]["issue_status"] == "Resolved"

    # Simulate the lost commit: the row is pending again, the target survived.
    row.state, row.applied, row.next_attempt_at = "pending", False, None
    await db_session.commit()
    fake_sync_jira["state"]["offered"] = False
    assert await jira_sync.process_event(db_session, event_id) == "done"
    row = await db_session.get(TrackerSyncEvent, event_id, populate_existing=True)
    assert row.error is None and json.loads(row.payload)["result"] == "already_at_target"
    assert len(fake_sync_jira["calls"]["transition_issue"]) == 1


async def test_custom_transition_missing_and_not_at_target_is_an_error(
    db_session: AsyncSession, fake_sync_jira
):
    org, _p, ticket, link = await _status_fixture(
        db_session, status_map=json.dumps({"transitions": {"in_progress": "Resolve"}})
    )
    fake_sync_jira["state"]["offered"] = False
    fake_sync_jira["state"]["issue_status"] = "Open"
    event = await _pending_status_row(db_session, org, link, ticket)
    assert await jira_sync.process_event(db_session, event.id) == "failed"
    row = await db_session.get(TrackerSyncEvent, event.id, populate_existing=True)
    assert "no transition named 'Resolve'" in row.error


# ── project left the link's org ──


async def test_inbound_ignored_after_project_leaves_the_org(
    client: AsyncClient, db_session: AsyncSession, drained
):
    """A project moved out of the org keeps its stale link; the former org's
    Jira must neither transition nor comment on its tickets."""
    owner, _ = await _user(db_session)
    org = await _org(db_session)
    await _connection(db_session, org, owner, with_secret=True, sync_status=True)
    project = await _project(db_session, owner, org)
    ticket = await _ticket(db_session, project, owner, status="review")
    await _link(db_session, org, ticket)
    project.org_id = None
    await db_session.commit()

    status, body = await _post_webhook(client, org.id, _status_change(category="done", to_name="Done"))
    assert status == 200
    assert body["status"] == "ignored" and body["reason"] == "stale_link"
    ticket = await db_session.get(Ticket, ticket.id, populate_existing=True)
    assert ticket.status == "review"
    assert await _sync_event_rows(db_session, ticket.id) == []


async def test_outbound_skipped_after_project_leaves_the_org(
    db_session: AsyncSession, fake_sync_jira
):
    """Queued and new outbound rows never reach the former org's Jira."""
    org, project, ticket, link = await _status_fixture(db_session)
    event = await _pending_status_row(db_session, org, link, ticket)
    project.org_id = None
    await db_session.commit()

    assert await jira_sync._sync_target(db_session, ticket.id) is None
    assert await jira_sync.enqueue_status_change(db_session, ticket) == []
    assert await _deliver_all(db_session, [event.id]) == ["skipped"]
    assert fake_sync_jira["calls"]["transition_issue"] == []
