"""Session-helper Jira integration (GitHub #112 phase 1).

Three deliverables, exercised against the helper's own tool handlers and the
confirm route:

- ``propose_comment`` accepts ``send_to_jira`` and only proposes when the
  ticket has a live Jira link, the caller can edit, the org has the
  ``jira_integration`` plan gate, and outbound comment forwarding is on.  At
  confirm the same preconditions are re-checked before the comment is written,
  and a success queues an outbound sync event that is drained after commit.
- ``get_ticket`` surfaces a live Jira link in the header and attributes
  mirrored ``source='jira'`` comments as "Jira comment by <name>".
- ``get_jira_issue`` reads the issue linked to a ticket (never a free-form
  key), re-checks access + the plan gate + org membership, and returns a
  capped, redactable summary.

No real Jira call is ever made: the client is monkeypatched where it would
touch the network, and the request-time drain is captured.
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
    HelperAction,
    HelperConversation,
    JiraTemplate,
    JiraTemplateVersion,
    OrgAuditEvent,
    OrgMember,
    OrgTrackerConnection,
    Organization,
    Project,
    RetrievalAuditEvent,
    Session,
    Ticket,
    TicketComment,
    TicketEvent,
    TicketTrackerLink,
    TrackerProjectMapping,
    TrackerSyncEvent,
    User,
)
from sessionfs.server.helper.context import HelperContext, HelperScope, RetrievalBudget
from sessionfs.server.helper.tools import TOOLS, ToolArgumentError
from sessionfs.server.integrations.trackers.base import TrackerError, TrackerIssue, TrackerSprint
from sessionfs.server.integrations.trackers.jira import JiraIssueComment, JiraIssueDetail
from sessionfs.server.llm_keys import encrypt_secret
from sessionfs.server.services import jira_sync

_SITE = "https://acme.atlassian.net"
_TOKEN = "atlassian-secret-token-1234"


# ── helpers ──


async def _make_user(
    db: AsyncSession, name: str = "alice", *, tier: str = "enterprise"
) -> tuple[User, str]:
    user = User(
        id=str(uuid.uuid4()),
        email=f"{name}-{uuid.uuid4().hex[:6]}@example.com",
        display_name=name.capitalize(),
        tier=tier,
        email_verified=True,
        is_active=True,
        created_at=datetime.now(timezone.utc),
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    raw = generate_api_key()
    db.add(
        ApiKey(
            id=str(uuid.uuid4()),
            user_id=user.id,
            key_hash=hash_api_key(raw),
            name=f"{name}-key",
            key_kind="user",
            created_at=datetime.now(timezone.utc),
        )
    )
    await db.commit()
    return user, raw


def _hdrs(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


async def _make_org(
    db: AsyncSession,
    admin: User,
    *,
    member: User | None = None,
    tier: str = "enterprise",
) -> Organization:
    org = Organization(
        id=f"org_{uuid.uuid4().hex[:12]}",
        name="Test Org",
        slug=f"test-{uuid.uuid4().hex[:6]}",
        tier=tier,
        seats_limit=10,
        settings="{}",
    )
    db.add(org)
    await db.commit()
    db.add(OrgMember(org_id=org.id, user_id=admin.id, role="admin"))
    if member is not None:
        db.add(OrgMember(org_id=org.id, user_id=member.id, role="member"))
    await db.commit()
    await db.refresh(org)
    return org


async def _make_project(
    db: AsyncSession, user: User, org: Organization, name: str = "Repo"
) -> Project:
    project = Project(
        id=f"prj_{uuid.uuid4().hex[:12]}",
        name=name,
        owner_id=user.id,
        org_id=org.id,
    )
    db.add(project)
    await db.commit()
    await db.refresh(project)
    return project


async def _make_ticket(
    db: AsyncSession,
    user: User,
    project: Project,
    *,
    title: str = "Flaky CI",
    status: str = "open",
) -> Ticket:
    ticket = Ticket(
        id=f"tk_{uuid.uuid4().hex[:16]}",
        project_id=project.id,
        title=title,
        description="old description",
        priority="medium",
        kind="task",
        status=status,
        created_by_user_id=user.id,
        context_refs="[]",
        file_refs="[]",
        related_sessions="[]",
        acceptance_criteria="[]",
        actor_type="user",
        lease_epoch=0,
    )
    db.add(ticket)
    await db.commit()
    await db.refresh(ticket)
    return ticket


async def _make_connection(
    db: AsyncSession,
    org: Organization,
    user: User,
    *,
    sync_comments: str = "off",
) -> OrgTrackerConnection:
    conn = OrgTrackerConnection(
        org_id=org.id,
        site_url=_SITE,
        account_email="jira@acme.com",
        encrypted_api_token=encrypt_secret(_TOKEN),
        default_template="agile",
        enabled=True,
        connected_by_user_id=user.id,
        sync_comments=sync_comments,
    )
    db.add(conn)
    await db.commit()
    return conn


async def _make_link(
    db: AsyncSession,
    org: Organization,
    ticket: Ticket,
    *,
    issue_key: str = "KEY-1",
) -> TicketTrackerLink:
    link = TicketTrackerLink(
        ticket_id=ticket.id,
        org_id=org.id,
        provider="jira",
        status="linked",
        site_url=_SITE,
        issue_id="10001",
        issue_key=issue_key,
        issue_url=f"{_SITE}/browse/{issue_key}",
    )
    db.add(link)
    await db.commit()
    return link


async def _make_mapping(
    db: AsyncSession,
    org: Organization,
    project: Project,
    user: User,
    *,
    jira_project_key: str = "ACME",
) -> TrackerProjectMapping:
    mapping = TrackerProjectMapping(
        id=f"tpm_{uuid.uuid4().hex[:16]}",
        org_id=org.id,
        project_id=project.id,
        jira_project_key=jira_project_key,
        issue_type="Story",
        created_by_user_id=user.id,
    )
    db.add(mapping)
    await db.commit()
    return mapping


async def _make_template(
    db: AsyncSession,
    org: Organization,
    user: User,
    *,
    slug: str = "custom-release",
    version: int = 1,
) -> JiraTemplate:
    """An active org-custom Jira template (Deliverable C override pinning)."""
    template = JiraTemplate(
        id=f"jtm_{uuid.uuid4().hex[:16]}",
        org_id=org.id,
        slug=slug,
        name=slug.replace("-", " ").title(),
        description=None,
        sections=json.dumps(
            [
                {
                    "key": "summary",
                    "heading": "Summary",
                    "mode": "rewrite",
                    "source_fields": ["title", "description"],
                    "guidance": "Summarize the ticket.",
                }
            ]
        ),
        version=version,
        created_by_user_id=user.id,
        updated_by_user_id=user.id,
    )
    db.add(template)
    # Mirror the create/update route: every version has an immutable snapshot so
    # the helper can publish a pinned (older) version after a later edit (L1).
    db.add(
        JiraTemplateVersion(
            id=f"jtv_{uuid.uuid4().hex[:16]}",
            template_id=template.id,
            version=version,
            sections=template.sections,
            created_by_user_id=user.id,
        )
    )
    await db.commit()
    await db.refresh(template)
    return template


async def _make_session(
    db: AsyncSession, user: User, *, project_id: str | None = None, title: str = "Test Session"
) -> Session:
    """A captured session that grants the caller read (not edit) access."""
    now = datetime.now(timezone.utc)
    session = Session(
        id=f"ses_{uuid.uuid4().hex[:16]}",
        user_id=user.id,
        title=title,
        source_tool="claude-code",
        blob_key=f"sessions/{user.id}/x.tar.gz",
        etag=uuid.uuid4().hex,
        created_at=now,
        updated_at=now,
        uploaded_at=now,
        project_id=project_id,
    )
    db.add(session)
    await db.commit()
    await db.refresh(session)
    return session


async def _make_conversation(db: AsyncSession, user: User) -> HelperConversation:
    conv = HelperConversation(
        id=f"hc_{uuid.uuid4().hex[:24]}",
        user_id=user.id,
        title="Actions thread",
    )
    db.add(conv)
    await db.commit()
    await db.refresh(conv)
    return conv


async def _make_action(
    db: AsyncSession,
    user: User,
    *,
    kind: str,
    params: dict,
    conversation: HelperConversation,
) -> HelperAction:
    action = HelperAction(
        id=f"ha_{uuid.uuid4().hex[:24]}",
        user_id=user.id,
        conversation_id=conversation.id,
        kind=kind,
        params=json.dumps(params, sort_keys=True),
        preview=json.dumps({"summary": "test", "targets": []}, sort_keys=True),
        status="pending",
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=15),
        created_at=datetime.now(timezone.utc),
    )
    db.add(action)
    await db.commit()
    await db.refresh(action)
    return action


def _ctx(db: AsyncSession, user: User) -> HelperContext:
    return HelperContext(
        user=user,
        db=db,
        org_id=None,
        scope=HelperScope(),
        budget=RetrievalBudget(),
        audit=None,
    )


def _comment_args(ticket: Ticket, body: str = "Looks good") -> dict:
    return {
        "ticket_id": ticket.id,
        "body": body,
        "sources": {"body": ["user"]},
    }


# ── propose_comment: send_to_jira ──


@pytest.mark.asyncio
async def test_propose_comment_send_to_jira_without_link_is_refused(
    db_session: AsyncSession,
):
    user, _ = await _make_user(db_session)
    project = await _make_project(db_session, user, await _make_org(db_session, user))
    ticket = await _make_ticket(db_session, user, project)
    ctx = _ctx(db_session, user)

    output = await TOOLS["propose_comment"].handler(
        ctx, {**_comment_args(ticket), "send_to_jira": True}
    )

    assert ctx.pending_actions == []
    assert "not linked to a Jira issue" in output.text


@pytest.mark.asyncio
async def test_propose_comment_send_to_jira_forwarding_off_is_refused(
    db_session: AsyncSession,
):
    user, _ = await _make_user(db_session)
    org = await _make_org(db_session, user)
    project = await _make_project(db_session, user, org)
    ticket = await _make_ticket(db_session, user, project)
    await _make_connection(db_session, org, user, sync_comments="off")
    await _make_link(db_session, org, ticket)
    ctx = _ctx(db_session, user)

    output = await TOOLS["propose_comment"].handler(
        ctx, {**_comment_args(ticket), "send_to_jira": True}
    )

    assert ctx.pending_actions == []
    assert "not set to outbound/both" in output.text


@pytest.mark.asyncio
async def test_propose_comment_send_to_jira_proposes_with_key(db_session: AsyncSession):
    user, _ = await _make_user(db_session)
    org = await _make_org(db_session, user)
    project = await _make_project(db_session, user, org)
    ticket = await _make_ticket(db_session, user, project)
    await _make_connection(db_session, org, user, sync_comments="both")
    await _make_link(db_session, org, ticket, issue_key="KEY-7")
    ctx = _ctx(db_session, user)

    output = await TOOLS["propose_comment"].handler(
        ctx, {**_comment_args(ticket), "send_to_jira": True}
    )

    assert len(ctx.pending_actions) == 1
    proposal = ctx.pending_actions[0]
    assert proposal["kind"] == "comment_ticket"
    assert proposal["params"]["send_to_jira"] is True
    assert proposal["params"]["issue_key"] == "KEY-7"
    assert "will also be posted to Jira issue KEY-7" in proposal["preview"]["summary"]
    assert "Proposed action" in output.text


# ── confirm: forwarded comment re-checks + enqueues + drains ──


@pytest.mark.asyncio
async def test_confirm_comment_forwards_to_jira_and_schedules_drain(
    client: AsyncClient, db_session: AsyncSession, monkeypatch
):
    user, key = await _make_user(db_session)
    org = await _make_org(db_session, user)
    project = await _make_project(db_session, user, org)
    ticket = await _make_ticket(db_session, user, project)
    await _make_connection(db_session, org, user, sync_comments="both")
    await _make_link(db_session, org, ticket, issue_key="KEY-3")
    conv = await _make_conversation(db_session, user)
    action = await _make_action(
        db_session,
        user,
        kind="comment_ticket",
        params={
            "ticket_id": ticket.id,
            "project_id": project.id,
            "body": "Looks good",
            "sources": {"body": ["user"]},
            "send_to_jira": True,
            "issue_key": "KEY-3",
        },
        conversation=conv,
    )

    drained: list[str] = []
    monkeypatch.setattr(jira_sync, "schedule_drain", lambda ids: drained.extend(ids))

    resp = await client.post(
        f"/api/v1/helper/actions/{action.id}/confirm", headers=_hdrs(key)
    )
    assert resp.status_code == 200, resp.text
    comment_id = resp.json()["result"]["targets"][0]["comment_id"]

    comment = (
        await db_session.execute(
            select(TicketComment).where(TicketComment.id == comment_id)
        )
    ).scalar_one()
    assert comment.content.startswith("Looks good")

    events = (
        (
            await db_session.execute(
                select(TrackerSyncEvent).where(TrackerSyncEvent.ticket_id == ticket.id)
            )
        )
        .scalars()
        .all()
    )
    assert len(events) == 1
    assert events[0].kind == "comment"
    assert events[0].direction == "outbound"
    assert drained == [events[0].id]


@pytest.mark.asyncio
async def test_confirm_comment_forwarding_off_after_proposal_writes_nothing(
    client: AsyncClient, db_session: AsyncSession, monkeypatch
):
    user, key = await _make_user(db_session)
    org = await _make_org(db_session, user)
    project = await _make_project(db_session, user, org)
    ticket = await _make_ticket(db_session, user, project)
    conn = await _make_connection(db_session, org, user, sync_comments="both")
    await _make_link(db_session, org, ticket)
    conv = await _make_conversation(db_session, user)
    action = await _make_action(
        db_session,
        user,
        kind="comment_ticket",
        params={
            "ticket_id": ticket.id,
            "project_id": project.id,
            "body": "Looks good",
            "sources": {"body": ["user"]},
            "send_to_jira": True,
            "issue_key": "KEY-1",
        },
        conversation=conv,
    )

    # The org flips forwarding off between the proposal and the confirm.
    conn.sync_comments = "off"
    await db_session.commit()

    drained: list[str] = []
    monkeypatch.setattr(jira_sync, "schedule_drain", lambda ids: drained.extend(ids))

    resp = await client.post(
        f"/api/v1/helper/actions/{action.id}/confirm", headers=_hdrs(key)
    )
    assert resp.status_code == 200, resp.text
    target = resp.json()["result"]["targets"][0]
    assert target["ok"] is False
    assert target["error"] == "jira_comment_sync_off"

    # The failed target wrote no comment and queued no outbox event.
    assert (await db_session.execute(select(TicketComment))).scalars().all() == []
    assert (
        await db_session.execute(
            select(TrackerSyncEvent).where(TrackerSyncEvent.ticket_id == ticket.id)
        )
    ).scalars().all() == []
    assert drained == []


@pytest.mark.asyncio
async def test_confirm_comment_link_removed_after_proposal_writes_nothing(
    client: AsyncClient, db_session: AsyncSession, monkeypatch
):
    user, key = await _make_user(db_session)
    org = await _make_org(db_session, user)
    project = await _make_project(db_session, user, org)
    ticket = await _make_ticket(db_session, user, project)
    await _make_connection(db_session, org, user, sync_comments="both")
    link = await _make_link(db_session, org, ticket)
    conv = await _make_conversation(db_session, user)
    action = await _make_action(
        db_session,
        user,
        kind="comment_ticket",
        params={
            "ticket_id": ticket.id,
            "project_id": project.id,
            "body": "Looks good",
            "sources": {"body": ["user"]},
            "send_to_jira": True,
            "issue_key": "KEY-1",
        },
        conversation=conv,
    )

    # The ticket is unlinked between the proposal and the confirm.
    await db_session.delete(link)
    await db_session.commit()

    drained: list[str] = []
    monkeypatch.setattr(jira_sync, "schedule_drain", lambda ids: drained.extend(ids))

    resp = await client.post(
        f"/api/v1/helper/actions/{action.id}/confirm", headers=_hdrs(key)
    )
    assert resp.status_code == 200, resp.text
    target = resp.json()["result"]["targets"][0]
    assert target["ok"] is False
    assert target["error"] == "jira_comment_sync_off"
    assert (await db_session.execute(select(TicketComment))).scalars().all() == []
    assert drained == []


# ── get_ticket: Jira provenance ──


@pytest.mark.asyncio
async def test_get_ticket_shows_jira_link_and_comment_provenance(
    db_session: AsyncSession,
):
    user, _ = await _make_user(db_session)
    org = await _make_org(db_session, user)
    project = await _make_project(db_session, user, org)
    ticket = await _make_ticket(db_session, user, project)
    await _make_connection(db_session, org, user, sync_comments="both")
    await _make_link(db_session, org, ticket, issue_key="KEY-9")
    now = datetime.now(timezone.utc)
    db_session.add(
        TicketComment(
            id=f"tc_{uuid.uuid4().hex[:16]}",
            ticket_id=ticket.id,
            author_user_id="jira-sync",
            content="A comment from Jira",
            source="jira",
            author_persona="Alice (Jira)",
            created_at=now,
        )
    )
    await db_session.commit()
    ctx = _ctx(db_session, user)

    output = await TOOLS["get_ticket"].handler(ctx, {"ticket_id": ticket.id})

    assert "jira: KEY-9 (https://acme.atlassian.net/browse/KEY-9) — linked" in output.text
    assert "Jira comment by Alice: A comment from Jira" in output.text
    assert "Alice (Jira)" not in output.text


# ── get_jira_issue: read-only linked issue ──


def _fake_jira_client(
    monkeypatch,
    *,
    detail: JiraIssueDetail | None = None,
    error: str | None = None,
    timeout: bool = False,
):
    """Replace ``jira_sync.JiraClient`` with a canned ``get_issue_detail``."""
    calls: list[str] = []

    class Fake:
        def __init__(self, site_url, account_email, api_token, *, transport=None):
            pass

        async def aclose(self) -> None:
            pass

        async def get_issue_detail(self, issue_key, *, max_comments=5, max_comment_chars=1500):
            calls.append(issue_key)
            if timeout:
                raise asyncio.TimeoutError
            if error is not None:
                raise TrackerError(error)
            return detail

    monkeypatch.setattr(jira_sync, "JiraClient", Fake)
    return calls


def _fake_link_client(
    monkeypatch,
    *,
    issue: TrackerIssue | None = None,
    error: str | None = None,
    status_code: int | None = None,
    timeout: bool = False,
):
    """Replace ``jira_links.JiraClient`` with a canned ``get_issue``."""
    calls: list[str] = []

    class Fake:
        def __init__(self, site_url, account_email, api_token):
            pass

        async def aclose(self) -> None:
            pass

        async def get_issue(self, issue_key, sprint_field_id=None, epic_link_field_id=None):
            calls.append(issue_key)
            if timeout:
                raise asyncio.TimeoutError
            if error is not None:
                raise TrackerError(error, status_code=status_code)
            return issue

    monkeypatch.setattr("sessionfs.server.services.jira_links.JiraClient", Fake)
    return calls


@pytest.mark.asyncio
async def test_get_jira_issue_reads_linked_issue(db_session: AsyncSession, monkeypatch):
    user, _ = await _make_user(db_session)
    org = await _make_org(db_session, user)
    project = await _make_project(db_session, user, org)
    ticket = await _make_ticket(db_session, user, project)
    await _make_connection(db_session, org, user, sync_comments="both")
    await _make_link(db_session, org, ticket, issue_key="KEY-5")
    calls = _fake_jira_client(
        monkeypatch,
        detail=JiraIssueDetail(
            key="KEY-5",
            url=f"{_SITE}/browse/KEY-5",
            summary="Ship the auth migration",
            issue_type="Story",
            status="In Progress",
            assignee="Alice",
            labels=["auth", "backend"],
            components=["core"],
            priority="High",
            updated_at="2026-10-20T10:00:00Z",
            comments=[
                JiraIssueComment(id="1", author_name="Bob", created="2026-10-19T09:00:00Z", body_text="Looking into it")
            ],
        ),
    )
    ctx = _ctx(db_session, user)

    output = await TOOLS["get_jira_issue"].handler(ctx, {"ticket_id": ticket.id})

    assert "Jira issue KEY-5" in output.text
    assert "summary: Ship the auth migration" in output.text
    assert "status: In Progress" in output.text
    assert "assignee: Alice" in output.text
    assert "priority: High" in output.text
    assert "labels: auth, backend" in output.text
    assert "components: core" in output.text
    assert "Jira comment by Bob: Looking into it" in output.text
    assert calls == ["KEY-5"]


@pytest.mark.asyncio
async def test_get_jira_issue_sanitizes_remote_display_names(
    db_session: AsyncSession, monkeypatch
):
    # Remote assignee/author names are cleaned like captured metadata so a Jira
    # display name can't forge a ``[session:…]`` marker or inject brackets.
    user, _ = await _make_user(db_session)
    org = await _make_org(db_session, user)
    project = await _make_project(db_session, user, org)
    ticket = await _make_ticket(db_session, user, project)
    await _make_connection(db_session, org, user, sync_comments="both")
    await _make_link(db_session, org, ticket)
    _fake_jira_client(
        monkeypatch,
        detail=JiraIssueDetail(
            key="KEY-9",
            url=f"{_SITE}/browse/KEY-9",
            summary="Summary",
            status="Open",
            assignee="[session: bob]",
            comments=[
                JiraIssueComment(
                    id="1",
                    author_name="Al[ice]",
                    created="2026-10-19T09:00:00Z",
                    body_text="hi",
                )
            ],
        ),
    )
    ctx = _ctx(db_session, user)

    output = await TOOLS["get_jira_issue"].handler(ctx, {"ticket_id": ticket.id})

    assert "assignee: session: bob" in output.text
    assert "Jira comment by Al ice: hi" in output.text
    assert "assignee: [session:" not in output.text


@pytest.mark.asyncio
async def test_get_jira_issue_no_link_is_refused(db_session: AsyncSession):
    user, _ = await _make_user(db_session)
    org = await _make_org(db_session, user)
    project = await _make_project(db_session, user, org)
    ticket = await _make_ticket(db_session, user, project)
    await _make_connection(db_session, org, user, sync_comments="both")
    ctx = _ctx(db_session, user)

    output = await TOOLS["get_jira_issue"].handler(ctx, {"ticket_id": ticket.id})

    assert "has no linked Jira issue" in output.text


@pytest.mark.asyncio
async def test_get_jira_issue_non_org_member_is_refused(db_session: AsyncSession):
    # The project owner can read the project, but is not an OrgMember, so they
    # must not read the issue through the org's Jira credential.
    owner, _ = await _make_user(db_session)
    admin, _ = await _make_user(db_session, name="admin")
    org = await _make_org(db_session, admin)  # owner is NOT a member
    project = await _make_project(db_session, owner, org)
    ticket = await _make_ticket(db_session, owner, project)
    await _make_connection(db_session, org, admin, sync_comments="both")
    await _make_link(db_session, org, ticket)
    ctx = _ctx(db_session, owner)

    output = await TOOLS["get_jira_issue"].handler(ctx, {"ticket_id": ticket.id})

    assert "Only members of this project's organization" in output.text


@pytest.mark.asyncio
async def test_get_jira_issue_plan_gate_is_refused(db_session: AsyncSession):
    user, _ = await _make_user(db_session, tier="team")
    org = await _make_org(db_session, user, tier="team")
    project = await _make_project(db_session, user, org)
    ticket = await _make_ticket(db_session, user, project)
    await _make_connection(db_session, org, user, sync_comments="both")
    await _make_link(db_session, org, ticket)
    ctx = _ctx(db_session, user)

    output = await TOOLS["get_jira_issue"].handler(ctx, {"ticket_id": ticket.id})

    assert "Jira integration is not available" in output.text


@pytest.mark.asyncio
async def test_get_jira_issue_tracker_error_is_reported(db_session: AsyncSession, monkeypatch):
    user, _ = await _make_user(db_session)
    org = await _make_org(db_session, user)
    project = await _make_project(db_session, user, org)
    ticket = await _make_ticket(db_session, user, project)
    await _make_connection(db_session, org, user, sync_comments="both")
    await _make_link(db_session, org, ticket)
    _fake_jira_client(monkeypatch, error="upstream exploded")
    ctx = _ctx(db_session, user)

    output = await TOOLS["get_jira_issue"].handler(ctx, {"ticket_id": ticket.id})

    assert "Could not read the Jira issue: upstream exploded" in output.text


@pytest.mark.asyncio
async def test_get_jira_issue_timeout_is_reported(db_session: AsyncSession, monkeypatch):
    user, _ = await _make_user(db_session)
    org = await _make_org(db_session, user)
    project = await _make_project(db_session, user, org)
    ticket = await _make_ticket(db_session, user, project)
    await _make_connection(db_session, org, user, sync_comments="both")
    await _make_link(db_session, org, ticket)
    _fake_jira_client(monkeypatch, timeout=True)
    ctx = _ctx(db_session, user)

    output = await TOOLS["get_jira_issue"].handler(ctx, {"ticket_id": ticket.id})

    assert "Timed out reading the Jira issue." in output.text


@pytest.mark.asyncio
async def test_get_jira_issue_outside_conversation_scope_is_not_found(db_session: AsyncSession):
    user, _ = await _make_user(db_session)
    org = await _make_org(db_session, user)
    project = await _make_project(db_session, user, org)
    ticket = await _make_ticket(db_session, user, project)
    await _make_connection(db_session, org, user, sync_comments="both")
    await _make_link(db_session, org, ticket)
    ctx = _ctx(db_session, user)
    ctx.scope = HelperScope(project_id="prj_somewhere_else")

    output = await TOOLS["get_jira_issue"].handler(ctx, {"ticket_id": ticket.id})

    assert "Ticket not found." in output.text


@pytest.mark.asyncio
async def test_propose_comment_send_to_jira_non_editor_is_refused(db_session: AsyncSession):
    admin, _ = await _make_user(db_session, name="admin")
    reader, _ = await _make_user(db_session, name="reader")
    org = await _make_org(db_session, admin)
    project = await _make_project(db_session, admin, org)
    await _make_session(db_session, reader, project_id=project.id)
    ticket = await _make_ticket(db_session, admin, project)
    await _make_connection(db_session, org, admin, sync_comments="both")
    await _make_link(db_session, org, ticket)
    ctx = _ctx(db_session, reader)

    output = await TOOLS["propose_comment"].handler(
        ctx, {**_comment_args(ticket), "send_to_jira": True}
    )

    assert ctx.pending_actions == []
    assert "requires editor access" in output.text


@pytest.mark.asyncio
async def test_propose_comment_send_to_jira_plan_gate_is_refused(db_session: AsyncSession):
    user, _ = await _make_user(db_session)
    org = await _make_org(db_session, user, tier="team")
    project = await _make_project(db_session, user, org)
    ticket = await _make_ticket(db_session, user, project)
    await _make_connection(db_session, org, user, sync_comments="both")
    await _make_link(db_session, org, ticket)
    ctx = _ctx(db_session, user)

    output = await TOOLS["propose_comment"].handler(
        ctx, {**_comment_args(ticket), "send_to_jira": True}
    )

    assert ctx.pending_actions == []
    assert "plan does not include Jira integration" in output.text


@pytest.mark.asyncio
async def test_confirm_lost_edit_rights_is_jira_edit_required(
    client: AsyncClient, db_session: AsyncSession, monkeypatch
):
    admin, _ = await _make_user(db_session, name="admin")
    reader, reader_key = await _make_user(db_session, name="reader")
    org = await _make_org(db_session, admin)
    project = await _make_project(db_session, admin, org)
    await _make_session(db_session, reader, project_id=project.id)
    ticket = await _make_ticket(db_session, admin, project)
    await _make_connection(db_session, org, admin, sync_comments="both")
    await _make_link(db_session, org, ticket)
    conv = await _make_conversation(db_session, reader)
    action = await _make_action(
        db_session,
        reader,
        kind="comment_ticket",
        params={
            "ticket_id": ticket.id,
            "project_id": project.id,
            "body": "Looks good",
            "sources": {"body": ["user"]},
            "send_to_jira": True,
            "issue_key": "KEY-1",
        },
        conversation=conv,
    )

    drained: list[str] = []
    monkeypatch.setattr(jira_sync, "schedule_drain", lambda ids: drained.extend(ids))

    resp = await client.post(
        f"/api/v1/helper/actions/{action.id}/confirm", headers=_hdrs(reader_key)
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "failed"
    target = body["result"]["targets"][0]
    assert target["ok"] is False
    assert target["error"] == "jira_edit_required"
    assert (await db_session.execute(select(TicketComment))).scalars().all() == []
    assert (await db_session.execute(select(TrackerSyncEvent))).scalars().all() == []
    assert drained == []


@pytest.mark.asyncio
async def test_confirm_reader_with_relinked_issue_learns_nothing(
    client: AsyncClient, db_session: AsyncSession, monkeypatch
):
    # A reader whose captured session still grants read access must hit the
    # editor gate before any relink check: the failure is jira_edit_required,
    # never jira_link_changed (which would leak the current issue key).
    admin, _ = await _make_user(db_session, name="admin")
    reader, reader_key = await _make_user(db_session, name="reader")
    org = await _make_org(db_session, admin)
    project = await _make_project(db_session, admin, org)
    await _make_session(db_session, reader, project_id=project.id)
    ticket = await _make_ticket(db_session, admin, project)
    await _make_connection(db_session, org, admin, sync_comments="both")
    link = await _make_link(db_session, org, ticket, issue_key="KEY-1")
    conv = await _make_conversation(db_session, reader)
    action = await _make_action(
        db_session,
        reader,
        kind="comment_ticket",
        params={
            "ticket_id": ticket.id,
            "project_id": project.id,
            "body": "Looks good",
            "sources": {"body": ["user"]},
            "send_to_jira": True,
            "issue_key": "KEY-1",
        },
        conversation=conv,
    )

    # The link is re-pointed between proposal and confirm.
    link.issue_key = "KEY-2"
    link.issue_url = f"{_SITE}/browse/KEY-2"
    await db_session.commit()

    drained: list[str] = []
    monkeypatch.setattr(jira_sync, "schedule_drain", lambda ids: drained.extend(ids))

    resp = await client.post(
        f"/api/v1/helper/actions/{action.id}/confirm", headers=_hdrs(reader_key)
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "failed"
    target = body["result"]["targets"][0]
    assert target["ok"] is False
    assert target["error"] == "jira_edit_required"
    assert (await db_session.execute(select(TicketComment))).scalars().all() == []
    assert drained == []


@pytest.mark.asyncio
async def test_confirm_org_lost_jira_integration_is_refused(
    client: AsyncClient, db_session: AsyncSession, monkeypatch
):
    user, key = await _make_user(db_session)
    org = await _make_org(db_session, user)
    project = await _make_project(db_session, user, org)
    ticket = await _make_ticket(db_session, user, project)
    await _make_connection(db_session, org, user, sync_comments="both")
    await _make_link(db_session, org, ticket)
    conv = await _make_conversation(db_session, user)
    action = await _make_action(
        db_session,
        user,
        kind="comment_ticket",
        params={
            "ticket_id": ticket.id,
            "project_id": project.id,
            "body": "Looks good",
            "sources": {"body": ["user"]},
            "send_to_jira": True,
            "issue_key": "KEY-1",
        },
        conversation=conv,
    )

    # The org drops to a plan without jira_integration after the proposal.
    org.tier = "team"
    await db_session.commit()

    drained: list[str] = []
    monkeypatch.setattr(jira_sync, "schedule_drain", lambda ids: drained.extend(ids))

    resp = await client.post(
        f"/api/v1/helper/actions/{action.id}/confirm", headers=_hdrs(key)
    )
    assert resp.status_code == 200, resp.text
    target = resp.json()["result"]["targets"][0]
    assert target["ok"] is False
    assert target["error"] == "upgrade_required"
    assert (await db_session.execute(select(TicketComment))).scalars().all() == []
    assert drained == []


@pytest.mark.asyncio
async def test_confirm_relink_between_propose_and_confirm_is_refused(
    client: AsyncClient, db_session: AsyncSession, monkeypatch
):
    user, key = await _make_user(db_session)
    org = await _make_org(db_session, user)
    project = await _make_project(db_session, user, org)
    ticket = await _make_ticket(db_session, user, project)
    await _make_connection(db_session, org, user, sync_comments="both")
    link = await _make_link(db_session, org, ticket, issue_key="KEY-1")
    conv = await _make_conversation(db_session, user)
    action = await _make_action(
        db_session,
        user,
        kind="comment_ticket",
        params={
            "ticket_id": ticket.id,
            "project_id": project.id,
            "body": "Looks good",
            "sources": {"body": ["user"]},
            "send_to_jira": True,
            "issue_key": "KEY-1",
        },
        conversation=conv,
    )

    # The link is re-pointed at a different issue between proposal and confirm.
    link.issue_key = "KEY-2"
    link.issue_url = f"{_SITE}/browse/KEY-2"
    await db_session.commit()

    drained: list[str] = []
    monkeypatch.setattr(jira_sync, "schedule_drain", lambda ids: drained.extend(ids))

    resp = await client.post(
        f"/api/v1/helper/actions/{action.id}/confirm", headers=_hdrs(key)
    )
    assert resp.status_code == 200, resp.text
    target = resp.json()["result"]["targets"][0]
    assert target["ok"] is False
    assert target["error"] == "jira_link_changed"
    assert (await db_session.execute(select(TicketComment))).scalars().all() == []
    assert (
        await db_session.execute(
            select(TrackerSyncEvent).where(TrackerSyncEvent.ticket_id == ticket.id)
        )
    ).scalars().all() == []
    assert drained == []


@pytest.mark.asyncio
async def test_confirm_user_edited_body_forwards_to_jira(
    client: AsyncClient, db_session: AsyncSession, monkeypatch
):
    user, key = await _make_user(db_session)
    org = await _make_org(db_session, user)
    project = await _make_project(db_session, user, org)
    ticket = await _make_ticket(db_session, user, project)
    await _make_connection(db_session, org, user, sync_comments="both")
    await _make_link(db_session, org, ticket, issue_key="KEY-3")
    conv = await _make_conversation(db_session, user)
    action = await _make_action(
        db_session,
        user,
        kind="comment_ticket",
        params={
            "ticket_id": ticket.id,
            "project_id": project.id,
            "body": "Looks good",
            "sources": {"body": ["user"]},
            "send_to_jira": True,
            "issue_key": "KEY-3",
        },
        conversation=conv,
    )

    drained: list[str] = []
    monkeypatch.setattr(jira_sync, "schedule_drain", lambda ids: drained.extend(ids))

    resp = await client.post(
        f"/api/v1/helper/actions/{action.id}/confirm",
        headers=_hdrs(key),
        json={"body": "User edited this"},
    )
    assert resp.status_code == 200, resp.text
    target = resp.json()["result"]["targets"][0]
    assert target["ok"] is True
    assert target["jira_queued"] is True
    comment_id = target["comment_id"]

    comment = (
        await db_session.execute(
            select(TicketComment).where(TicketComment.id == comment_id)
        )
    ).scalar_one()
    assert comment.content.startswith("User edited this")

    events = (
        (
            await db_session.execute(
                select(TrackerSyncEvent).where(TrackerSyncEvent.ticket_id == ticket.id)
            )
        )
        .scalars()
        .all()
    )
    assert len(events) == 1
    assert drained == [events[0].id]


@pytest.mark.asyncio
async def test_get_ticket_native_comment_with_jira_suffix_is_not_jira(
    db_session: AsyncSession,
):
    user, _ = await _make_user(db_session)
    org = await _make_org(db_session, user)
    project = await _make_project(db_session, user, org)
    ticket = await _make_ticket(db_session, user, project)
    await _make_connection(db_session, org, user, sync_comments="both")
    await _make_link(db_session, org, ticket, issue_key="KEY-9")
    db_session.add(
        TicketComment(
            id=f"tc_{uuid.uuid4().hex[:16]}",
            ticket_id=ticket.id,
            author_user_id=user.id,
            content="A native comment",
            source="sessionfs",
            author_persona="Mallory (Jira)",
            created_at=datetime.now(timezone.utc),
        )
    )
    await db_session.commit()
    ctx = _ctx(db_session, user)

    output = await TOOLS["get_ticket"].handler(ctx, {"ticket_id": ticket.id})

    assert "Mallory (Jira): A native comment" in output.text
    assert "Jira comment by Mallory" not in output.text


# ── propose_jira_link: link an existing Jira issue (phase 2) ──


def _link_args(ticket: Ticket, issue_key: str = "ACME-7") -> dict:
    return {"ticket_id": ticket.id, "issue_key": issue_key}


@pytest.mark.asyncio
async def test_propose_jira_link_invalid_key_is_refused(db_session: AsyncSession):
    user, _ = await _make_user(db_session)
    org = await _make_org(db_session, user)
    project = await _make_project(db_session, user, org)
    ticket = await _make_ticket(db_session, user, project)
    await _make_connection(db_session, org, user)
    await _make_mapping(db_session, org, project, user)
    ctx = _ctx(db_session, user)

    output = await TOOLS["propose_jira_link"].handler(
        ctx, {"ticket_id": ticket.id, "issue_key": "not a key"}
    )

    assert ctx.pending_actions == []
    assert "Invalid Jira issue key" in output.text


@pytest.mark.asyncio
async def test_propose_jira_link_non_editor_is_refused(db_session: AsyncSession):
    admin, _ = await _make_user(db_session, name="admin")
    reader, _ = await _make_user(db_session, name="reader")
    org = await _make_org(db_session, admin)
    project = await _make_project(db_session, admin, org)
    await _make_session(db_session, reader, project_id=project.id)
    ticket = await _make_ticket(db_session, admin, project)
    await _make_connection(db_session, org, admin)
    await _make_mapping(db_session, org, project, admin)
    ctx = _ctx(db_session, reader)

    output = await TOOLS["propose_jira_link"].handler(ctx, _link_args(ticket))

    assert ctx.pending_actions == []
    assert "requires editor access" in output.text


@pytest.mark.asyncio
async def test_propose_jira_link_without_connection_is_refused(db_session: AsyncSession):
    user, _ = await _make_user(db_session)
    org = await _make_org(db_session, user)
    project = await _make_project(db_session, user, org)
    ticket = await _make_ticket(db_session, user, project)
    await _make_mapping(db_session, org, project, user)
    ctx = _ctx(db_session, user)

    output = await TOOLS["propose_jira_link"].handler(ctx, _link_args(ticket))

    assert ctx.pending_actions == []
    assert "has not connected Jira" in output.text


@pytest.mark.asyncio
async def test_propose_jira_link_without_mapping_is_refused(db_session: AsyncSession):
    user, _ = await _make_user(db_session)
    org = await _make_org(db_session, user)
    project = await _make_project(db_session, user, org)
    ticket = await _make_ticket(db_session, user, project)
    await _make_connection(db_session, org, user)
    ctx = _ctx(db_session, user)

    output = await TOOLS["propose_jira_link"].handler(ctx, _link_args(ticket))

    assert ctx.pending_actions == []
    assert "no Jira mapping" in output.text


@pytest.mark.asyncio
async def test_propose_jira_link_already_linked_is_refused(db_session: AsyncSession):
    user, _ = await _make_user(db_session)
    org = await _make_org(db_session, user)
    project = await _make_project(db_session, user, org)
    ticket = await _make_ticket(db_session, user, project)
    await _make_connection(db_session, org, user)
    await _make_mapping(db_session, org, project, user)
    await _make_link(db_session, org, ticket)
    ctx = _ctx(db_session, user)

    output = await TOOLS["propose_jira_link"].handler(ctx, _link_args(ticket))

    assert ctx.pending_actions == []
    assert "already has a Jira link" in output.text


@pytest.mark.asyncio
async def test_propose_jira_link_plan_gate_is_refused(db_session: AsyncSession):
    user, _ = await _make_user(db_session)
    org = await _make_org(db_session, user, tier="team")
    project = await _make_project(db_session, user, org)
    ticket = await _make_ticket(db_session, user, project)
    await _make_connection(db_session, org, user)
    await _make_mapping(db_session, org, project, user)
    ctx = _ctx(db_session, user)

    output = await TOOLS["propose_jira_link"].handler(ctx, _link_args(ticket))

    assert ctx.pending_actions == []
    assert "plan does not include Jira integration" in output.text


@pytest.mark.asyncio
async def test_propose_jira_link_proposes_with_normalized_key(db_session: AsyncSession):
    user, _ = await _make_user(db_session)
    org = await _make_org(db_session, user)
    project = await _make_project(db_session, user, org)
    ticket = await _make_ticket(db_session, user, project)
    await _make_connection(db_session, org, user)
    await _make_mapping(db_session, org, project, user, jira_project_key="ACME")
    ctx = _ctx(db_session, user)

    output = await TOOLS["propose_jira_link"].handler(
        ctx, {"ticket_id": ticket.id, "issue_key": "  acme-7  "}
    )

    assert len(ctx.pending_actions) == 1
    prop = ctx.pending_actions[0]
    assert prop["kind"] == "link_jira"
    assert prop["params"]["ticket_id"] == ticket.id
    assert prop["params"]["project_id"] == project.id
    assert prop["params"]["issue_key"] == "ACME-7"
    assert prop["preview"]["summary"] == f"Link {ticket.title} to Jira ACME-7"
    assert "Proposed action" in output.text


def _acme_issue(key: str = "ACME-7") -> TrackerIssue:
    return TrackerIssue(
        key=key,
        url=f"{_SITE}/browse/{key}",
        id="10007",
        project_key="ACME",
        status="In Progress",
    )


@pytest.mark.asyncio
async def test_confirm_jira_link_creates_link_without_push(
    client: AsyncClient, db_session: AsyncSession, monkeypatch
):
    user, key = await _make_user(db_session)
    org = await _make_org(db_session, user)
    project = await _make_project(db_session, user, org)
    ticket = await _make_ticket(db_session, user, project)
    await _make_connection(db_session, org, user)
    await _make_mapping(db_session, org, project, user, jira_project_key="ACME")
    conv = await _make_conversation(db_session, user)
    action = await _make_action(
        db_session,
        user,
        kind="link_jira",
        params={"ticket_id": ticket.id, "project_id": project.id, "issue_key": "ACME-7"},
        conversation=conv,
    )
    calls = _fake_link_client(monkeypatch, issue=_acme_issue())

    resp = await client.post(
        f"/api/v1/helper/actions/{action.id}/confirm", headers=_hdrs(key)
    )
    assert resp.status_code == 200, resp.text
    target = resp.json()["result"]["targets"][0]
    assert target["ok"] is True
    assert target["error"] is None
    assert target["issue_key"] == "ACME-7"
    assert target["issue_url"] == f"{_SITE}/browse/ACME-7"
    assert calls == ["ACME-7"]

    link = (
        await db_session.execute(
            select(TicketTrackerLink).where(TicketTrackerLink.ticket_id == ticket.id)
        )
    ).scalar_one()
    assert link.status == "linked"
    assert link.issue_key == "ACME-7"
    assert link.issue_id == "10007"

    # Linking reads one issue but never pushes: no outbox event is queued.
    assert (
        await db_session.execute(
            select(TrackerSyncEvent).where(TrackerSyncEvent.ticket_id == ticket.id)
        )
    ).scalars().all() == []

    audit = (
        await db_session.execute(
            select(OrgAuditEvent).where(OrgAuditEvent.target_id == ticket.id)
        )
    ).scalars().one()
    assert audit.event_type == "jira_linked"
    assert audit.target_type == "ticket"

    # The event is recorded inside the service, not at the route, so the helper
    # confirm path emits exactly one ticket event too.
    events = (await db_session.execute(
        select(TicketEvent).where(TicketEvent.ticket_id == ticket.id)
    )).scalars().all()
    assert [e.action for e in events] == ["jira_linked"]
    assert events[0].actor_type == "user"
    assert events[0].actor_user_id == user.id
    assert json.loads(events[0].after_value) == {"issue_key": "ACME-7"}


@pytest.mark.asyncio
async def test_confirm_jira_link_issue_linked_elsewhere_fails_without_link(
    client: AsyncClient, db_session: AsyncSession, monkeypatch
):
    user, key = await _make_user(db_session)
    org = await _make_org(db_session, user)
    project = await _make_project(db_session, user, org)
    ticket = await _make_ticket(db_session, user, project)
    other = await _make_ticket(db_session, user, project, title="Other ticket")
    await _make_connection(db_session, org, user)
    await _make_mapping(db_session, org, project, user, jira_project_key="ACME")
    # The same Jira issue is already linked to a different ticket.
    await _make_link(db_session, org, other, issue_key="ACME-7")
    conv = await _make_conversation(db_session, user)
    action = await _make_action(
        db_session,
        user,
        kind="link_jira",
        params={"ticket_id": ticket.id, "project_id": project.id, "issue_key": "ACME-7"},
        conversation=conv,
    )
    _fake_link_client(monkeypatch, issue=_acme_issue())

    resp = await client.post(
        f"/api/v1/helper/actions/{action.id}/confirm", headers=_hdrs(key)
    )
    assert resp.status_code == 200, resp.text
    target = resp.json()["result"]["targets"][0]
    assert target["ok"] is False
    assert target["error"] == "jira_issue_linked_elsewhere"

    # Our ticket got no link row (the other ticket keeps its link).
    assert (
        await db_session.execute(
            select(TicketTrackerLink).where(TicketTrackerLink.ticket_id == ticket.id)
        )
    ).scalars().all() == []


@pytest.mark.asyncio
async def test_confirm_jira_link_missing_issue_is_jira_issue_not_found(
    client: AsyncClient, db_session: AsyncSession, monkeypatch
):
    user, key = await _make_user(db_session)
    org = await _make_org(db_session, user)
    project = await _make_project(db_session, user, org)
    ticket = await _make_ticket(db_session, user, project)
    await _make_connection(db_session, org, user)
    await _make_mapping(db_session, org, project, user, jira_project_key="ACME")
    conv = await _make_conversation(db_session, user)
    action = await _make_action(
        db_session,
        user,
        kind="link_jira",
        params={"ticket_id": ticket.id, "project_id": project.id, "issue_key": "ACME-7"},
        conversation=conv,
    )
    _fake_link_client(monkeypatch, error="not found", status_code=404)

    resp = await client.post(
        f"/api/v1/helper/actions/{action.id}/confirm", headers=_hdrs(key)
    )
    assert resp.status_code == 200, resp.text
    target = resp.json()["result"]["targets"][0]
    assert target["ok"] is False
    assert target["error"] == "jira_issue_not_found"
    assert (
        await db_session.execute(
            select(TicketTrackerLink).where(TicketTrackerLink.ticket_id == ticket.id)
        )
    ).scalars().all() == []


@pytest.mark.asyncio
async def test_confirm_jira_link_wrong_project_is_jira_project_not_mapped(
    client: AsyncClient, db_session: AsyncSession, monkeypatch
):
    user, key = await _make_user(db_session)
    org = await _make_org(db_session, user)
    project = await _make_project(db_session, user, org)
    ticket = await _make_ticket(db_session, user, project)
    await _make_connection(db_session, org, user)
    await _make_mapping(db_session, org, project, user, jira_project_key="OTHER")
    conv = await _make_conversation(db_session, user)
    action = await _make_action(
        db_session,
        user,
        kind="link_jira",
        params={"ticket_id": ticket.id, "project_id": project.id, "issue_key": "ACME-7"},
        conversation=conv,
    )
    _fake_link_client(monkeypatch, issue=_acme_issue())

    resp = await client.post(
        f"/api/v1/helper/actions/{action.id}/confirm", headers=_hdrs(key)
    )
    assert resp.status_code == 200, resp.text
    target = resp.json()["result"]["targets"][0]
    assert target["ok"] is False
    assert target["error"] == "jira_project_not_mapped"
    assert (
        await db_session.execute(
            select(TicketTrackerLink).where(TicketTrackerLink.ticket_id == ticket.id)
        )
    ).scalars().all() == []


@pytest.mark.asyncio
async def test_confirm_jira_link_already_linked_is_jira_already_linked(
    client: AsyncClient, db_session: AsyncSession, monkeypatch
):
    user, key = await _make_user(db_session)
    org = await _make_org(db_session, user)
    project = await _make_project(db_session, user, org)
    ticket = await _make_ticket(db_session, user, project)
    await _make_connection(db_session, org, user)
    await _make_mapping(db_session, org, project, user, jira_project_key="ACME")
    # The ticket was linked between the proposal and the confirm.
    await _make_link(db_session, org, ticket, issue_key="ACME-9")
    conv = await _make_conversation(db_session, user)
    action = await _make_action(
        db_session,
        user,
        kind="link_jira",
        params={"ticket_id": ticket.id, "project_id": project.id, "issue_key": "ACME-7"},
        conversation=conv,
    )
    _fake_link_client(monkeypatch, issue=_acme_issue())

    resp = await client.post(
        f"/api/v1/helper/actions/{action.id}/confirm", headers=_hdrs(key)
    )
    assert resp.status_code == 200, resp.text
    target = resp.json()["result"]["targets"][0]
    assert target["ok"] is False
    assert target["error"] == "jira_already_linked"
    # The pre-existing link is untouched.
    link = (
        await db_session.execute(
            select(TicketTrackerLink).where(TicketTrackerLink.ticket_id == ticket.id)
        )
    ).scalar_one()
    assert link.issue_key == "ACME-9"


# ── propose_ticket: publish_to_jira (phase 2, Deliverable C) ──


def _publish_ticket_args(project: Project, *, publish_to_jira: bool = True) -> dict:
    return {
        "project_id": project.id,
        "title": "Fix the flaky test",
        "description": "The session showed the CI job failing intermittently.",
        "acceptance_criteria": ["CI is green on 3 consecutive runs"],
        "priority": "high",
        "kind": "task",
        "sources": {
            "title": ["user"],
            "description": ["user"],
            "acceptance_criteria": ["user"],
        },
        "publish_to_jira": publish_to_jira,
    }


@pytest.mark.asyncio
async def test_propose_ticket_publish_non_editor_is_refused(db_session: AsyncSession):
    admin, _ = await _make_user(db_session, name="admin")
    reader, _ = await _make_user(db_session, name="reader")
    org = await _make_org(db_session, admin)
    project = await _make_project(db_session, admin, org)
    await _make_session(db_session, reader, project_id=project.id)
    await _make_connection(db_session, org, admin)
    await _make_mapping(db_session, org, project, admin)
    ctx = _ctx(db_session, reader)

    output = await TOOLS["propose_ticket"].handler(
        ctx, _publish_ticket_args(project)
    )

    assert ctx.pending_actions == []
    assert "requires editor access" in output.text


@pytest.mark.asyncio
async def test_propose_ticket_publish_without_connection_is_refused(db_session: AsyncSession):
    user, _ = await _make_user(db_session)
    org = await _make_org(db_session, user)
    project = await _make_project(db_session, user, org)
    await _make_mapping(db_session, org, project, user)
    ctx = _ctx(db_session, user)

    output = await TOOLS["propose_ticket"].handler(
        ctx, _publish_ticket_args(project)
    )

    assert ctx.pending_actions == []
    assert "has not connected Jira" in output.text


@pytest.mark.asyncio
async def test_propose_ticket_publish_without_mapping_is_refused(db_session: AsyncSession):
    user, _ = await _make_user(db_session)
    org = await _make_org(db_session, user)
    project = await _make_project(db_session, user, org)
    await _make_connection(db_session, org, user)
    ctx = _ctx(db_session, user)

    output = await TOOLS["propose_ticket"].handler(
        ctx, _publish_ticket_args(project)
    )

    assert ctx.pending_actions == []
    assert "no Jira mapping" in output.text


@pytest.mark.asyncio
async def test_propose_ticket_publish_plan_gate_is_refused(db_session: AsyncSession):
    user, _ = await _make_user(db_session, tier="team")
    org = await _make_org(db_session, user, tier="team")
    project = await _make_project(db_session, user, org)
    await _make_connection(db_session, org, user)
    await _make_mapping(db_session, org, project, user)
    ctx = _ctx(db_session, user)

    output = await TOOLS["propose_ticket"].handler(
        ctx, _publish_ticket_args(project)
    )

    assert ctx.pending_actions == []
    assert "plan does not include Jira integration" in output.text


@pytest.mark.asyncio
async def test_propose_ticket_publish_false_does_not_gate_on_jira(db_session: AsyncSession):
    # Without publish_to_jira the create stays exactly as before: no editor/
    # connection/mapping checks and no Jira params or summary.
    user, _ = await _make_user(db_session)
    org = await _make_org(db_session, user)
    project = await _make_project(db_session, user, org)
    ctx = _ctx(db_session, user)

    output = await TOOLS["propose_ticket"].handler(
        ctx, _publish_ticket_args(project, publish_to_jira=False)
    )

    assert len(ctx.pending_actions) == 1
    prop = ctx.pending_actions[0]
    assert "publish_to_jira" not in prop["params"]
    assert "jira_project_key" not in prop["params"]
    assert "publish it to Jira" not in prop["preview"]["summary"]
    assert "Proposed action" in output.text


@pytest.mark.asyncio
async def test_propose_ticket_publish_proposes_with_key(db_session: AsyncSession):
    user, _ = await _make_user(db_session)
    org = await _make_org(db_session, user)
    project = await _make_project(db_session, user, org)
    await _make_connection(db_session, org, user)
    await _make_mapping(db_session, org, project, user, jira_project_key="ACME")
    ctx = _ctx(db_session, user)

    output = await TOOLS["propose_ticket"].handler(
        ctx, _publish_ticket_args(project)
    )

    assert len(ctx.pending_actions) == 1
    prop = ctx.pending_actions[0]
    assert prop["kind"] == "create_ticket"
    assert prop["params"]["publish_to_jira"] is True
    assert prop["params"]["jira_project_key"] == "ACME"
    assert "publish it to Jira ACME" in prop["preview"]["summary"]
    assert "Proposed action" in output.text


@pytest.mark.asyncio
async def test_propose_ticket_publish_overrides_pin_key_template_and_sprint(
    db_session: AsyncSession,
):
    user, _ = await _make_user(db_session)
    org = await _make_org(db_session, user)
    project = await _make_project(db_session, user, org)
    other = await _make_project(db_session, user, org, name="Backend")
    conn = await _make_connection(db_session, org, user)
    conn.sprint_field_id = "customfield_10010"
    await db_session.commit()
    await _make_mapping(db_session, org, project, user, jira_project_key="ACME")
    # The override key must name a Jira project some org project maps to.
    await _make_mapping(db_session, org, other, user, jira_project_key="BETA")
    await _make_template(db_session, org, user, slug="custom-release")
    ctx = _ctx(db_session, user)

    output = await TOOLS["propose_ticket"].handler(
        ctx,
        {
            **_publish_ticket_args(project),
            "jira_project_key": "BETA",
            "jira_template": "custom-release",
            "jira_sprint_id": 5,
        },
    )

    assert len(ctx.pending_actions) == 1
    prop = ctx.pending_actions[0]
    assert prop["params"]["jira_project_key"] == "BETA"
    assert prop["params"]["jira_project_key_override"] == "BETA"
    assert prop["params"]["jira_template"] == "custom-release"
    assert prop["params"]["jira_template_version"] == 1
    assert prop["params"]["jira_sprint_id"] == 5
    assert "publish it to Jira BETA with template custom-release into sprint 5" in prop[
        "preview"
    ]["summary"]
    assert "Proposed action" in output.text


@pytest.mark.asyncio
async def test_propose_ticket_publish_unmapped_key_override_is_refused(
    db_session: AsyncSession,
):
    user, _ = await _make_user(db_session)
    org = await _make_org(db_session, user)
    project = await _make_project(db_session, user, org)
    await _make_connection(db_session, org, user)
    await _make_mapping(db_session, org, project, user, jira_project_key="ACME")
    ctx = _ctx(db_session, user)

    output = await TOOLS["propose_ticket"].handler(
        ctx, {**_publish_ticket_args(project), "jira_project_key": "UNMAPPED"}
    )

    assert ctx.pending_actions == []
    assert "is not a Jira project you can publish to" in output.text


@pytest.mark.asyncio
async def test_propose_ticket_publish_key_override_requires_edit_standing(
    db_session: AsyncSession,
):
    """M1 — an override key that is mapped in the org but to a project the
    caller can't edit is refused."""
    admin, _ = await _make_user(db_session, name="admin")
    member, _ = await _make_user(db_session, name="member")
    # Restricted visibility makes a plain member an editor only of projects they
    # own (or have a group grant for), so ``other`` (owned by admin) is off-limits.
    org = await _make_org(db_session, admin, member=member)
    org.settings = json.dumps({"general": {"restrict_project_visibility": True}})
    await db_session.commit()
    project = await _make_project(db_session, member, org)
    other = await _make_project(db_session, admin, org, name="Backend")
    await _make_connection(db_session, org, admin)
    await _make_mapping(db_session, org, project, member, jira_project_key="ACME")
    await _make_mapping(db_session, org, other, admin, jira_project_key="BETA")
    ctx = _ctx(db_session, member)

    output = await TOOLS["propose_ticket"].handler(
        ctx, {**_publish_ticket_args(project), "jira_project_key": "BETA"}
    )

    assert ctx.pending_actions == []
    # The same generic refusal as an unmapped key, so it never confirms which
    # keys restricted projects use.
    assert "is not a Jira project you can publish to" in output.text


@pytest.mark.asyncio
async def test_propose_ticket_publish_unknown_template_is_refused(db_session: AsyncSession):
    user, _ = await _make_user(db_session)
    org = await _make_org(db_session, user)
    project = await _make_project(db_session, user, org)
    await _make_connection(db_session, org, user)
    await _make_mapping(db_session, org, project, user, jira_project_key="ACME")
    ctx = _ctx(db_session, user)

    output = await TOOLS["propose_ticket"].handler(
        ctx, {**_publish_ticket_args(project), "jira_template": "no-such-template"}
    )

    assert ctx.pending_actions == []
    assert "unknown Jira template" in output.text


@pytest.mark.asyncio
async def test_propose_ticket_publish_sprint_without_field_is_refused(
    db_session: AsyncSession,
):
    user, _ = await _make_user(db_session)
    org = await _make_org(db_session, user)
    project = await _make_project(db_session, user, org)
    await _make_connection(db_session, org, user)  # sprint_field_id stays None
    await _make_mapping(db_session, org, project, user, jira_project_key="ACME")
    ctx = _ctx(db_session, user)

    output = await TOOLS["propose_ticket"].handler(
        ctx, {**_publish_ticket_args(project), "jira_sprint_id": 5}
    )

    assert ctx.pending_actions == []
    assert "no sprint field" in output.text


@pytest.mark.asyncio
async def test_propose_ticket_publish_custom_shadowing_builtin_is_pinned(
    db_session: AsyncSession,
):
    user, _ = await _make_user(db_session)
    org = await _make_org(db_session, user)
    project = await _make_project(db_session, user, org)
    await _make_connection(db_session, org, user)
    await _make_mapping(db_session, org, project, user, jira_project_key="ACME")
    # "agile" is a built-in slug, but this org shadows it with a custom template,
    # which must be pinned like any custom template (not treated as a builtin).
    await _make_template(db_session, org, user, slug="agile")
    ctx = _ctx(db_session, user)

    output = await TOOLS["propose_ticket"].handler(
        ctx, {**_publish_ticket_args(project), "jira_template": "agile"}
    )

    assert len(ctx.pending_actions) == 1
    prop = ctx.pending_actions[0]
    assert prop["params"]["jira_template"] == "agile"
    assert prop["params"]["jira_template_version"] == 1
    # Recorded as a custom pin (not a built-in), so confirm refuses instead of
    # falling back to the same-slug built-in once the custom template is gone.
    assert prop["params"]["jira_template_builtin"] is False
    assert "Proposed action" in output.text


# ── confirm: create-ticket-and-publish ──


def _fake_publish_client(
    monkeypatch,
    *,
    issue_key: str = "ACME-1",
    timeout: bool = False,
    error: str | None = None,
    slow_create_seconds: float | None = None,
) -> dict:
    """Replace ``routes.jira.JiraClient`` with a canned publish client."""
    state: dict = {
        "create_issue": 0,
        "update_issue": 0,
        "search_issue_by_label": 0,
    }

    class Fake:
        def __init__(self, site_url, account_email, api_token, *, transport=None):
            pass

        async def aclose(self) -> None:
            pass

        async def create_issue(self, project_key, issue_type, summary, adf, *, labels=None, extra_fields=None):
            state["create_issue"] += 1
            state.setdefault("create_project_keys", []).append(project_key)
            state.setdefault("create_extra_fields", []).append(extra_fields)
            state.setdefault("create_adf", []).append(adf)
            if slow_create_seconds is not None:
                await asyncio.sleep(slow_create_seconds)
            if timeout:
                raise asyncio.TimeoutError
            if error is not None:
                raise TrackerError(error)
            return TrackerIssue(
                key=issue_key,
                url=f"{_SITE}/browse/{issue_key}",
                id="10001",
                status="Open",
            )

        async def update_issue(self, issue_key_, summary, adf, extra_fields=None):
            state["update_issue"] += 1
            state.setdefault("update_extra_fields", []).append(extra_fields)
            if timeout:
                raise asyncio.TimeoutError
            if error is not None:
                raise TrackerError(error)
            return TrackerIssue(
                key=issue_key_,
                url=f"{_SITE}/browse/{issue_key_}",
                id="10001",
                status="Open",
            )

        async def search_issue_by_label(self, project_key, label):
            state["search_issue_by_label"] += 1
            return None

        async def move_issue_to_sprint(self, issue_key, sprint_id) -> None:
            state.setdefault("sprint_moves", []).append((issue_key, sprint_id))

        async def get_sprint(self, sprint_id) -> TrackerSprint:
            return TrackerSprint(id=str(sprint_id), name="Sprint 1", state="active", board_name="")

    monkeypatch.setattr("sessionfs.server.routes.jira.JiraClient", Fake)
    return state


def _create_publish_params(project: Project) -> dict:
    return {
        "project_id": project.id,
        "title": "Fix the flaky test",
        "description": "The session showed the CI job failing intermittently.",
        "acceptance_criteria": ["CI is green on 3 consecutive runs"],
        "priority": "high",
        "kind": "task",
        "assigned_to": None,
        "sources": {
            "title": ["user"],
            "description": ["user"],
            "acceptance_criteria": ["user"],
        },
        "publish_to_jira": True,
        "jira_project_key": "ACME",
    }


@pytest.mark.asyncio
async def test_confirm_create_ticket_publishes_to_jira_without_llm(
    client: AsyncClient, db_session: AsyncSession, monkeypatch
):
    user, key = await _make_user(db_session)
    org = await _make_org(db_session, user)
    project = await _make_project(db_session, user, org)
    await _make_connection(db_session, org, user)
    await _make_mapping(db_session, org, project, user, jira_project_key="ACME")
    conv = await _make_conversation(db_session, user)
    action = await _make_action(
        db_session,
        user,
        kind="create_ticket",
        params=_create_publish_params(project),
        conversation=conv,
    )

    def _no_llm(*a, **k):
        raise AssertionError("LLM must not be called for a deterministic publish")

    monkeypatch.setattr(
        "sessionfs.server.integrations.trackers.promote.resolve_helper_llm", _no_llm
    )
    monkeypatch.setattr(
        "sessionfs.server.integrations.trackers.promote._rewrite", _no_llm
    )
    state = _fake_publish_client(monkeypatch, issue_key="ACME-42")

    resp = await client.post(
        f"/api/v1/helper/actions/{action.id}/confirm", headers=_hdrs(key)
    )
    assert resp.status_code == 200, resp.text
    target = resp.json()["result"]["targets"][0]
    assert target["ok"] is True
    assert target["jira_published"] is True
    assert target["jira"] == {
        "issue_key": "ACME-42",
        "issue_url": f"{_SITE}/browse/ACME-42",
    }

    ticket = await db_session.get(Ticket, target["ticket_id"])
    assert ticket is not None
    assert ticket.project_id == project.id

    link = (
        await db_session.execute(
            select(TicketTrackerLink).where(TicketTrackerLink.ticket_id == ticket.id)
        )
    ).scalar_one()
    assert link.status == "linked"
    assert link.issue_key == "ACME-42"

    # A fresh create only ever issues one outbound create.
    assert state["create_issue"] == 1
    assert state["update_issue"] == 0
    assert state["search_issue_by_label"] == 0

    audit = (
        await db_session.execute(
            select(OrgAuditEvent).where(OrgAuditEvent.target_id == ticket.id)
        )
    ).scalars().one()
    assert audit.event_type == "jira_issue_created"


@pytest.mark.asyncio
async def test_confirm_create_ticket_publish_timeout_leaves_ticket_and_unknown_link(
    client: AsyncClient, db_session: AsyncSession, monkeypatch
):
    user, key = await _make_user(db_session)
    org = await _make_org(db_session, user)
    project = await _make_project(db_session, user, org)
    await _make_connection(db_session, org, user)
    await _make_mapping(db_session, org, project, user, jira_project_key="ACME")
    conv = await _make_conversation(db_session, user)
    action = await _make_action(
        db_session,
        user,
        kind="create_ticket",
        params=_create_publish_params(project),
        conversation=conv,
    )
    state = _fake_publish_client(monkeypatch, timeout=True)

    resp = await client.post(
        f"/api/v1/helper/actions/{action.id}/confirm", headers=_hdrs(key)
    )
    assert resp.status_code == 200, resp.text
    target = resp.json()["result"]["targets"][0]
    assert target["ok"] is True  # the ticket itself was created
    assert target["jira_published"] is False
    assert target["jira_error"] == "jira_timeout"

    ticket = await db_session.get(Ticket, target["ticket_id"])
    assert ticket is not None

    # A timed-out create keeps the claim row as ``unknown`` for a later adopt.
    link = (
        await db_session.execute(
            select(TicketTrackerLink).where(TicketTrackerLink.ticket_id == ticket.id)
        )
    ).scalar_one()
    assert link.status == "unknown"
    assert state["create_issue"] == 1


@pytest.mark.asyncio
async def test_confirm_create_ticket_publish_deadline_bounds_slow_create(
    client: AsyncClient, db_session: AsyncSession, monkeypatch
):
    # L5 — one shared deadline bounds the whole outbound phase. With a tiny
    # deadline and a create call that would otherwise hang for seconds, the
    # publish must give up quickly (jira_timeout) instead of waiting it out.
    user, key = await _make_user(db_session)
    org = await _make_org(db_session, user)
    project = await _make_project(db_session, user, org)
    await _make_connection(db_session, org, user)
    await _make_mapping(db_session, org, project, user, jira_project_key="ACME")
    conv = await _make_conversation(db_session, user)
    action = await _make_action(
        db_session,
        user,
        kind="create_ticket",
        params=_create_publish_params(project),
        conversation=conv,
    )
    state = _fake_publish_client(monkeypatch, slow_create_seconds=5.0)
    monkeypatch.setattr("sessionfs.server.routes.jira._JIRA_DEADLINE_SECONDS", 0.1)

    started = asyncio.get_running_loop().time()
    resp = await client.post(
        f"/api/v1/helper/actions/{action.id}/confirm", headers=_hdrs(key)
    )
    elapsed = asyncio.get_running_loop().time() - started

    assert resp.status_code == 200, resp.text
    target = resp.json()["result"]["targets"][0]
    assert target["jira_published"] is False
    assert target["jira_error"] == "jira_timeout"
    assert state["create_issue"] == 1
    # The 5s hang is cut short by the 0.1s deadline (generous upper bound).
    assert elapsed < 2.0


@pytest.mark.asyncio
async def test_confirm_create_ticket_publish_upstream_error_leaves_ticket_only(
    client: AsyncClient, db_session: AsyncSession, monkeypatch
):
    user, key = await _make_user(db_session)
    org = await _make_org(db_session, user)
    project = await _make_project(db_session, user, org)
    await _make_connection(db_session, org, user)
    await _make_mapping(db_session, org, project, user, jira_project_key="ACME")
    conv = await _make_conversation(db_session, user)
    action = await _make_action(
        db_session,
        user,
        kind="create_ticket",
        params=_create_publish_params(project),
        conversation=conv,
    )
    state = _fake_publish_client(monkeypatch, error="Jira API 500: boom")

    resp = await client.post(
        f"/api/v1/helper/actions/{action.id}/confirm", headers=_hdrs(key)
    )
    assert resp.status_code == 200, resp.text
    target = resp.json()["result"]["targets"][0]
    assert target["ok"] is True
    assert target["jira_published"] is False
    assert target["jira_error"] == "jira_upstream_error"

    ticket = await db_session.get(Ticket, target["ticket_id"])
    assert ticket is not None

    # A 5xx (or an error with no status) may have written the issue, so the
    # claim stays ``unknown`` and the next publish adopts by label.
    links = (
        await db_session.execute(
            select(TicketTrackerLink).where(TicketTrackerLink.ticket_id == ticket.id)
        )
    ).scalars().all()
    assert [link.status for link in links] == ["unknown"]
    assert state["create_issue"] == 1


@pytest.mark.asyncio
async def test_confirm_create_ticket_publish_mapping_removed_creates_nothing(
    client: AsyncClient, db_session: AsyncSession, monkeypatch
):
    user, key = await _make_user(db_session)
    org = await _make_org(db_session, user)
    project = await _make_project(db_session, user, org)
    await _make_connection(db_session, org, user)
    mapping = await _make_mapping(db_session, org, project, user, jira_project_key="ACME")
    conv = await _make_conversation(db_session, user)
    action = await _make_action(
        db_session,
        user,
        kind="create_ticket",
        params=_create_publish_params(project),
        conversation=conv,
    )

    # The mapping is removed between the proposal and the confirm.
    await db_session.delete(mapping)
    await db_session.commit()

    state = _fake_publish_client(monkeypatch)

    resp = await client.post(
        f"/api/v1/helper/actions/{action.id}/confirm", headers=_hdrs(key)
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "failed"
    target = body["result"]["targets"][0]
    assert target["ok"] is False
    assert target["error"] == "jira_project_unmapped"

    # Nothing was created: no ticket, no link, no outbound call.
    assert (await db_session.execute(select(Ticket))).scalars().all() == []
    assert (
        await db_session.execute(select(TicketTrackerLink))
    ).scalars().all() == []
    assert state["create_issue"] == 0


@pytest.mark.asyncio
async def test_confirm_create_ticket_publish_with_key_template_and_sprint_overrides(
    client: AsyncClient, db_session: AsyncSession, monkeypatch
):
    user, key = await _make_user(db_session)
    org = await _make_org(db_session, user)
    project = await _make_project(db_session, user, org)
    other = await _make_project(db_session, user, org, name="Backend")
    conn = await _make_connection(db_session, org, user)
    conn.sprint_field_id = "customfield_10010"
    await db_session.commit()
    await _make_mapping(db_session, org, project, user, jira_project_key="ACME")
    await _make_mapping(db_session, org, other, user, jira_project_key="BETA")
    await _make_template(db_session, org, user, slug="custom-release")
    conv = await _make_conversation(db_session, user)
    action = await _make_action(
        db_session,
        user,
        kind="create_ticket",
        params={
            **_create_publish_params(project),
            "jira_project_key_override": "BETA",
            "jira_template": "custom-release",
            "jira_template_version": 1,
            "jira_sprint_id": 5,
        },
        conversation=conv,
    )

    state = _fake_publish_client(monkeypatch, issue_key="BETA-9")

    resp = await client.post(
        f"/api/v1/helper/actions/{action.id}/confirm", headers=_hdrs(key)
    )
    assert resp.status_code == 200, resp.text
    target = resp.json()["result"]["targets"][0]
    assert target["ok"] is True
    assert target["jira_published"] is True
    assert target["jira"] == {
        "issue_key": "BETA-9",
        "issue_url": f"{_SITE}/browse/BETA-9",
    }

    # The destination key and the inline sprint field both reach create_issue.
    assert state["create_project_keys"] == ["BETA"]
    assert state["create_extra_fields"] == [{"customfield_10010": 5}]


@pytest.mark.asyncio
async def test_confirm_create_ticket_publish_key_override_rechecked_at_confirm(
    client: AsyncClient, db_session: AsyncSession, monkeypatch
):
    """M1 — a restricted project's key override is re-checked at confirm.

    Between propose and confirm the caller's standing can't improve, so an
    override to a project they can't edit is refused with the same generic
    message, before the ticket is created and without any outbound call.
    """
    admin, _ = await _make_user(db_session, name="admin")
    member, member_key = await _make_user(db_session, name="member")
    org = await _make_org(db_session, admin, member=member)
    org.settings = json.dumps({"general": {"restrict_project_visibility": True}})
    await db_session.commit()
    project = await _make_project(db_session, member, org)
    other = await _make_project(db_session, admin, org, name="Backend")
    await _make_connection(db_session, org, admin)
    await _make_mapping(db_session, org, project, member, jira_project_key="ACME")
    await _make_mapping(db_session, org, other, admin, jira_project_key="BETA")
    conv = await _make_conversation(db_session, member)
    action = await _make_action(
        db_session,
        member,
        kind="create_ticket",
        params={
            **_create_publish_params(project),
            "jira_project_key_override": "BETA",
        },
        conversation=conv,
    )

    state = _fake_publish_client(monkeypatch)

    resp = await client.post(
        f"/api/v1/helper/actions/{action.id}/confirm", headers=_hdrs(member_key)
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "failed"
    target = body["result"]["targets"][0]
    assert target["ok"] is False
    assert target["error"] == "jira_project_key_invalid"

    # The refusal happens before the ticket is created and with no outbound call.
    assert (await db_session.execute(select(Ticket))).scalars().all() == []
    assert state["create_issue"] == 0


@pytest.mark.asyncio
async def test_confirm_create_ticket_publish_sprint_without_field_is_refused(
    client: AsyncClient, db_session: AsyncSession, monkeypatch
):
    user, key = await _make_user(db_session)
    org = await _make_org(db_session, user)
    project = await _make_project(db_session, user, org)
    await _make_connection(db_session, org, user)  # sprint_field_id stays None
    await _make_mapping(db_session, org, project, user, jira_project_key="ACME")
    conv = await _make_conversation(db_session, user)
    action = await _make_action(
        db_session,
        user,
        kind="create_ticket",
        params={
            **_create_publish_params(project),
            "jira_sprint_id": 5,
        },
        conversation=conv,
    )

    state = _fake_publish_client(monkeypatch)

    resp = await client.post(
        f"/api/v1/helper/actions/{action.id}/confirm", headers=_hdrs(key)
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "failed"
    target = body["result"]["targets"][0]
    assert target["ok"] is False
    assert target["error"] == "jira_sprint_field_unknown"

    # The refusal happens before the ticket is created and with no outbound call.
    assert (await db_session.execute(select(Ticket))).scalars().all() == []
    assert state["create_issue"] == 0


@pytest.mark.asyncio
async def test_confirm_create_ticket_publish_uses_pinned_template_snapshot(
    client: AsyncClient, db_session: AsyncSession, monkeypatch
):
    user, key = await _make_user(db_session)
    org = await _make_org(db_session, user)
    project = await _make_project(db_session, user, org)
    await _make_connection(db_session, org, user)
    await _make_mapping(db_session, org, project, user, jira_project_key="ACME")
    template = await _make_template(db_session, org, user, slug="custom-release")
    conv = await _make_conversation(db_session, user)
    action = await _make_action(
        db_session,
        user,
        kind="create_ticket",
        params={
            **_create_publish_params(project),
            "jira_template": "custom-release",
            "jira_template_version": 1,
        },
        conversation=conv,
    )

    # The custom template moves to v2 (new heading) after the proposal pinned
    # v1.  L1: the pinned v1 snapshot still exists, so confirm publishes v1.
    v2_sections = json.dumps(
        [
            {
                "key": "summary",
                "heading": "Summary v2",
                "mode": "rewrite",
                "source_fields": ["title", "description"],
                "guidance": "Summarize the ticket.",
            }
        ]
    )
    template.version = 2
    template.sections = v2_sections
    db_session.add(
        JiraTemplateVersion(
            id=f"jtv_{uuid.uuid4().hex[:16]}",
            template_id=template.id,
            version=2,
            sections=v2_sections,
            created_by_user_id=user.id,
        )
    )
    await db_session.commit()

    state = _fake_publish_client(monkeypatch, issue_key="ACME-7")

    resp = await client.post(
        f"/api/v1/helper/actions/{action.id}/confirm", headers=_hdrs(key)
    )
    assert resp.status_code == 200, resp.text
    target = resp.json()["result"]["targets"][0]
    assert target["ok"] is True
    assert target["jira_published"] is True
    assert state["create_issue"] == 1

    # The published description renders the pinned v1 heading, not v2, and the
    # link records the pinned version.
    adf = state["create_adf"][0]
    headings = [
        block.get("content", [{}])[0].get("text")
        for block in adf["content"]
        if block.get("type") == "heading"
    ]
    assert "Summary" in headings
    assert "Summary v2" not in headings
    link = (
        await db_session.execute(select(TicketTrackerLink))
    ).scalar_one()
    assert link.template_slug == "custom-release"
    assert link.template_version == 1


@pytest.mark.asyncio
async def test_confirm_create_ticket_publish_template_version_gone_is_refused(
    client: AsyncClient, db_session: AsyncSession, monkeypatch
):
    user, key = await _make_user(db_session)
    org = await _make_org(db_session, user)
    project = await _make_project(db_session, user, org)
    await _make_connection(db_session, org, user)
    await _make_mapping(db_session, org, project, user, jira_project_key="ACME")
    template = await _make_template(db_session, org, user, slug="custom-release")
    conv = await _make_conversation(db_session, user)
    action = await _make_action(
        db_session,
        user,
        kind="create_ticket",
        params={
            **_create_publish_params(project),
            "jira_template": "custom-release",
            "jira_template_version": 1,
        },
        conversation=conv,
    )

    # The pinned v1 snapshot disappears (here: the whole template is replaced),
    # so the confirm refuses jira_template_changed before creating anything.
    snapshot = (
        await db_session.execute(
            select(JiraTemplateVersion).where(
                JiraTemplateVersion.template_id == template.id,
                JiraTemplateVersion.version == 1,
            )
        )
    ).scalar_one()
    await db_session.delete(snapshot)
    await db_session.commit()

    state = _fake_publish_client(monkeypatch)

    resp = await client.post(
        f"/api/v1/helper/actions/{action.id}/confirm", headers=_hdrs(key)
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "failed"
    target = body["result"]["targets"][0]
    assert target["ok"] is False
    assert target["error"] == "jira_template_changed"

    assert (await db_session.execute(select(Ticket))).scalars().all() == []
    assert state["create_issue"] == 0


@pytest.mark.asyncio
async def test_confirm_create_ticket_publish_archived_template_is_refused(
    client: AsyncClient, db_session: AsyncSession, monkeypatch
):
    user, key = await _make_user(db_session)
    org = await _make_org(db_session, user)
    project = await _make_project(db_session, user, org)
    await _make_connection(db_session, org, user)
    await _make_mapping(db_session, org, project, user, jira_project_key="ACME")
    template = await _make_template(db_session, org, user, slug="custom-release")
    conv = await _make_conversation(db_session, user)
    action = await _make_action(
        db_session,
        user,
        kind="create_ticket",
        params={
            **_create_publish_params(project),
            "jira_template": "custom-release",
            "jira_template_version": 1,
        },
        conversation=conv,
    )

    # The template is archived after the proposal (its v1 snapshot still exists).
    template.archived_at = datetime.now(timezone.utc)
    await db_session.commit()

    state = _fake_publish_client(monkeypatch)

    resp = await client.post(
        f"/api/v1/helper/actions/{action.id}/confirm", headers=_hdrs(key)
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "failed"
    target = body["result"]["targets"][0]
    assert target["ok"] is False
    assert target["error"] == "jira_template_changed"

    assert (await db_session.execute(select(Ticket))).scalars().all() == []
    assert state["create_issue"] == 0


@pytest.mark.asyncio
async def test_confirm_create_ticket_publish_custom_shadowing_builtin_is_pinned(
    client: AsyncClient, db_session: AsyncSession, monkeypatch
):
    user, key = await _make_user(db_session)
    org = await _make_org(db_session, user)
    project = await _make_project(db_session, user, org)
    await _make_connection(db_session, org, user)
    await _make_mapping(db_session, org, project, user, jira_project_key="ACME")
    # A custom "agile" shadows the built-in of the same slug. It is pinned like
    # any custom template, so a later v2 edit still publishes the pinned v1.
    template = await _make_template(db_session, org, user, slug="agile")
    conv = await _make_conversation(db_session, user)
    action = await _make_action(
        db_session,
        user,
        kind="create_ticket",
        params={
            **_create_publish_params(project),
            "jira_template": "agile",
            "jira_template_version": 1,
        },
        conversation=conv,
    )

    v2_sections = json.dumps(
        [
            {
                "key": "summary",
                "heading": "Summary v2",
                "mode": "rewrite",
                "source_fields": ["title", "description"],
                "guidance": "Summarize the ticket.",
            }
        ]
    )
    template.version = 2
    template.sections = v2_sections
    db_session.add(
        JiraTemplateVersion(
            id=f"jtv_{uuid.uuid4().hex[:16]}",
            template_id=template.id,
            version=2,
            sections=v2_sections,
            created_by_user_id=user.id,
        )
    )
    await db_session.commit()

    state = _fake_publish_client(monkeypatch, issue_key="ACME-8")

    resp = await client.post(
        f"/api/v1/helper/actions/{action.id}/confirm", headers=_hdrs(key)
    )
    assert resp.status_code == 200, resp.text
    target = resp.json()["result"]["targets"][0]
    assert target["ok"] is True
    assert target["jira_published"] is True
    assert state["create_issue"] == 1

    # The pinned custom v1 renders only its own "Summary" section, not the
    # built-in "agile" sections nor the v2 heading.
    adf = state["create_adf"][0]
    headings = [
        block.get("content", [{}])[0].get("text")
        for block in adf["content"]
        if block.get("type") == "heading"
    ]
    assert headings == ["Summary"]
    link = (
        await db_session.execute(select(TicketTrackerLink))
    ).scalar_one()
    assert link.template_slug == "agile"
    assert link.template_version == 1


@pytest.mark.asyncio
async def test_confirm_create_ticket_publish_pinned_custom_gone_refuses_not_fallback(
    client: AsyncClient, db_session: AsyncSession, monkeypatch
):
    user, key = await _make_user(db_session)
    org = await _make_org(db_session, user)
    project = await _make_project(db_session, user, org)
    await _make_connection(db_session, org, user)
    await _make_mapping(db_session, org, project, user, jira_project_key="ACME")
    # A custom "agile" shadows the built-in of the same slug. The proposal pins
    # it as a custom version, so once the custom template is gone the confirm
    # refuses jira_template_changed — it must not fall back to the built-in
    # that happens to share the slug.
    template = await _make_template(db_session, org, user, slug="agile")
    conv = await _make_conversation(db_session, user)
    action = await _make_action(
        db_session,
        user,
        kind="create_ticket",
        params={
            **_create_publish_params(project),
            "jira_template": "agile",
            "jira_template_version": 1,
            "jira_template_builtin": False,
        },
        conversation=conv,
    )

    await db_session.delete(template)
    await db_session.commit()

    state = _fake_publish_client(monkeypatch)

    resp = await client.post(
        f"/api/v1/helper/actions/{action.id}/confirm", headers=_hdrs(key)
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "failed"
    target = body["result"]["targets"][0]
    assert target["ok"] is False
    assert target["error"] == "jira_template_changed"

    assert (await db_session.execute(select(Ticket))).scalars().all() == []
    assert state["create_issue"] == 0


@pytest.mark.asyncio
async def test_confirm_create_ticket_publish_builtin_shadowed_later_stays_builtin(
    client: AsyncClient, db_session: AsyncSession, monkeypatch
):
    user, key = await _make_user(db_session)
    org = await _make_org(db_session, user)
    project = await _make_project(db_session, user, org)
    await _make_connection(db_session, org, user)
    await _make_mapping(db_session, org, project, user, jira_project_key="ACME")
    conv = await _make_conversation(db_session, user)
    # The proposal pinned the built-in "agile" (no custom row, hence no version).
    action = await _make_action(
        db_session,
        user,
        kind="create_ticket",
        params={
            **_create_publish_params(project),
            "jira_template": "agile",
            "jira_template_version": None,
            "jira_template_builtin": True,
        },
        conversation=conv,
    )

    # A custom template later takes the slug, but the pin says "built-in", so
    # confirm still publishes the built-in instead of the shadowing custom.
    await _make_template(db_session, org, user, slug="agile")

    state = _fake_publish_client(monkeypatch, issue_key="ACME-9")

    resp = await client.post(
        f"/api/v1/helper/actions/{action.id}/confirm", headers=_hdrs(key)
    )
    assert resp.status_code == 200, resp.text
    target = resp.json()["result"]["targets"][0]
    assert target["ok"] is True
    assert target["jira_published"] is True
    assert state["create_issue"] == 1

    # The built-in renders all three "agile" headings; the custom would have
    # rendered only its single "Summary" section.
    adf = state["create_adf"][0]
    headings = [
        block.get("content", [{}])[0].get("text")
        for block in adf["content"]
        if block.get("type") == "heading"
    ]
    assert headings == ["Summary", "Acceptance Criteria", "Context & Prior Work"]

    link = (
        await db_session.execute(select(TicketTrackerLink))
    ).scalar_one()
    assert link.template_slug == "agile"
    assert link.template_version is None


# ── propose_jira_overwrite: overwrite one side of a linked pair ──


@pytest.mark.asyncio
async def test_propose_jira_overwrite_bad_direction_is_refused(db_session: AsyncSession):
    user, _ = await _make_user(db_session)
    ctx = _ctx(db_session, user)

    with pytest.raises(ToolArgumentError, match="to_jira.*from_jira"):
        await TOOLS["propose_jira_overwrite"].handler(
            ctx, {"ticket_id": "tk_nope", "direction": "sideways"}
        )


@pytest.mark.asyncio
async def test_propose_jira_overwrite_without_link_is_refused(db_session: AsyncSession):
    user, _ = await _make_user(db_session)
    org = await _make_org(db_session, user)
    project = await _make_project(db_session, user, org)
    ticket = await _make_ticket(db_session, user, project)
    await _make_connection(db_session, org, user)
    await _make_mapping(db_session, org, project, user)
    ctx = _ctx(db_session, user)

    output = await TOOLS["propose_jira_overwrite"].handler(
        ctx, {"ticket_id": ticket.id, "direction": "to_jira"}
    )

    assert ctx.pending_actions == []
    assert "no linked Jira issue" in output.text


@pytest.mark.asyncio
async def test_propose_jira_overwrite_to_jira_proposes_summary(db_session: AsyncSession):
    user, _ = await _make_user(db_session)
    org = await _make_org(db_session, user)
    project = await _make_project(db_session, user, org)
    ticket = await _make_ticket(db_session, user, project)
    await _make_connection(db_session, org, user)
    await _make_mapping(db_session, org, project, user, jira_project_key="ACME")
    await _make_link(db_session, org, ticket, issue_key="ACME-7")
    ctx = _ctx(db_session, user)

    output = await TOOLS["propose_jira_overwrite"].handler(
        ctx, {"ticket_id": ticket.id, "direction": "to_jira"}
    )

    assert len(ctx.pending_actions) == 1
    prop = ctx.pending_actions[0]
    assert prop["kind"] == "jira_overwrite"
    assert prop["params"]["ticket_id"] == ticket.id
    assert prop["params"]["project_id"] == project.id
    assert prop["params"]["issue_key"] == "ACME-7"
    assert prop["params"]["direction"] == "to_jira"
    assert prop["preview"]["summary"] == f"Replace Jira ACME-7 with {ticket.title}"
    assert "Proposed action" in output.text


@pytest.mark.asyncio
async def test_propose_jira_overwrite_from_jira_proposes_summary(db_session: AsyncSession):
    user, _ = await _make_user(db_session)
    org = await _make_org(db_session, user)
    project = await _make_project(db_session, user, org)
    ticket = await _make_ticket(db_session, user, project)
    await _make_connection(db_session, org, user)
    await _make_mapping(db_session, org, project, user, jira_project_key="ACME")
    await _make_link(db_session, org, ticket, issue_key="ACME-7")
    ctx = _ctx(db_session, user)

    output = await TOOLS["propose_jira_overwrite"].handler(
        ctx, {"ticket_id": ticket.id, "direction": "from_jira"}
    )

    assert len(ctx.pending_actions) == 1
    prop = ctx.pending_actions[0]
    assert prop["kind"] == "jira_overwrite"
    assert prop["params"]["direction"] == "from_jira"
    assert prop["preview"]["summary"] == f"Replace {ticket.title} with Jira ACME-7"
    assert "Proposed action" in output.text


@pytest.mark.asyncio
async def test_propose_jira_overwrite_non_editor_is_refused(db_session: AsyncSession):
    admin, _ = await _make_user(db_session, name="admin")
    reader, _ = await _make_user(db_session, name="reader")
    org = await _make_org(db_session, admin)
    project = await _make_project(db_session, admin, org)
    await _make_session(db_session, reader, project_id=project.id)
    ticket = await _make_ticket(db_session, admin, project)
    await _make_connection(db_session, org, admin)
    await _make_mapping(db_session, org, project, admin)
    await _make_link(db_session, org, ticket)
    ctx = _ctx(db_session, reader)

    output = await TOOLS["propose_jira_overwrite"].handler(
        ctx, {"ticket_id": ticket.id, "direction": "to_jira"}
    )

    assert ctx.pending_actions == []
    assert "requires editor access" in output.text


@pytest.mark.asyncio
async def test_propose_jira_overwrite_plan_gate_is_refused(db_session: AsyncSession):
    user, _ = await _make_user(db_session)
    org = await _make_org(db_session, user, tier="team")
    project = await _make_project(db_session, user, org)
    ticket = await _make_ticket(db_session, user, project)
    await _make_connection(db_session, org, user)
    await _make_mapping(db_session, org, project, user)
    await _make_link(db_session, org, ticket)
    ctx = _ctx(db_session, user)

    output = await TOOLS["propose_jira_overwrite"].handler(
        ctx, {"ticket_id": ticket.id, "direction": "to_jira"}
    )

    assert ctx.pending_actions == []
    assert "plan does not include Jira integration" in output.text


@pytest.mark.asyncio
async def test_propose_jira_overwrite_without_mapping_is_refused(db_session: AsyncSession):
    user, _ = await _make_user(db_session)
    org = await _make_org(db_session, user)
    project = await _make_project(db_session, user, org)
    ticket = await _make_ticket(db_session, user, project)
    await _make_connection(db_session, org, user)
    await _make_link(db_session, org, ticket)
    ctx = _ctx(db_session, user)

    output = await TOOLS["propose_jira_overwrite"].handler(
        ctx, {"ticket_id": ticket.id, "direction": "to_jira"}
    )

    assert ctx.pending_actions == []
    assert "no Jira mapping" in output.text


# ── confirm: jira_overwrite (to_jira publish / from_jira pull) ──


def _fake_pull_client(
    monkeypatch,
    *,
    summary: str = "Shipped the auth migration",
    description_adf: dict | None = None,
    error: str | None = None,
    timeout: bool = False,
    due_date: str | None = None,
    original_estimate: str | None = None,
) -> dict:
    """Replace ``routes.jira.JiraClient`` with a canned pull read."""
    if description_adf is None:
        description_adf = {
            "type": "doc",
            "version": 1,
            "content": [
                {"type": "paragraph", "content": [{"type": "text", "text": "all done"}]}
            ],
        }
    state: dict = {"get_issue_summary_and_description": 0, "get_issue": 0}

    class Fake:
        def __init__(self, site_url, account_email, api_token, *, transport=None):
            pass

        async def aclose(self) -> None:
            pass

        async def get_issue_summary_and_description(self, issue_key):
            state["get_issue_summary_and_description"] += 1
            if timeout:
                raise asyncio.TimeoutError
            if error is not None:
                raise TrackerError(error, status_code=404)
            return summary, description_adf

        async def get_issue(self, issue_key, sprint_field_id=None, epic_link_field_id=None):
            state["get_issue"] += 1
            if timeout:
                raise asyncio.TimeoutError
            if error is not None:
                raise TrackerError(error, status_code=404)
            return TrackerIssue(
                key=issue_key,
                url=f"{_SITE}/browse/{issue_key}",
                status="Done",
                id="10001",
                due_date=due_date,
                original_estimate=original_estimate,
            )

    monkeypatch.setattr("sessionfs.server.routes.jira.JiraClient", Fake)
    return state


@pytest.mark.asyncio
async def test_confirm_jira_overwrite_to_jira_publishes(
    client: AsyncClient, db_session: AsyncSession, monkeypatch
):
    user, key = await _make_user(db_session)
    org = await _make_org(db_session, user)
    project = await _make_project(db_session, user, org)
    ticket = await _make_ticket(db_session, user, project)
    await _make_connection(db_session, org, user)
    await _make_mapping(db_session, org, project, user, jira_project_key="ACME")
    # Attached-only link: linked from Jira, never published from SessionFS.
    await _make_link(db_session, org, ticket, issue_key="ACME-7")
    conv = await _make_conversation(db_session, user)
    action = await _make_action(
        db_session,
        user,
        kind="jira_overwrite",
        params={
            "ticket_id": ticket.id,
            "project_id": project.id,
            "issue_key": "ACME-7",
            "direction": "to_jira",
        },
        conversation=conv,
    )
    state = _fake_publish_client(monkeypatch, issue_key="ACME-7")

    resp = await client.post(
        f"/api/v1/helper/actions/{action.id}/confirm", headers=_hdrs(key)
    )
    assert resp.status_code == 200, resp.text
    target = resp.json()["result"]["targets"][0]
    assert target["ok"] is True
    assert target["error"] is None
    assert target["jira_published"] is True
    assert target["issue_key"] == "ACME-7"
    assert target["issue_url"] == f"{_SITE}/browse/ACME-7"

    # The attached-only link was re-published in place (update, never create).
    assert state["update_issue"] == 1
    assert state["create_issue"] == 0


@pytest.mark.asyncio
async def test_confirm_jira_overwrite_from_jira_pulls(
    client: AsyncClient, db_session: AsyncSession, monkeypatch
):
    user, key = await _make_user(db_session)
    org = await _make_org(db_session, user)
    project = await _make_project(db_session, user, org)
    ticket = await _make_ticket(db_session, user, project)
    await _make_connection(db_session, org, user)
    await _make_mapping(db_session, org, project, user, jira_project_key="ACME")
    await _make_link(db_session, org, ticket, issue_key="ACME-7")
    conv = await _make_conversation(db_session, user)
    action = await _make_action(
        db_session,
        user,
        kind="jira_overwrite",
        params={
            "ticket_id": ticket.id,
            "project_id": project.id,
            "issue_key": "ACME-7",
            "direction": "from_jira",
        },
        conversation=conv,
    )
    state = _fake_pull_client(monkeypatch)

    resp = await client.post(
        f"/api/v1/helper/actions/{action.id}/confirm", headers=_hdrs(key)
    )
    assert resp.status_code == 200, resp.text
    target = resp.json()["result"]["targets"][0]
    assert target["ok"] is True
    assert target["error"] is None
    assert target["issue_key"] == "ACME-7"
    assert target["updated_fields"] == ["title", "description"]

    ticket = await db_session.get(Ticket, ticket.id, populate_existing=True)
    assert ticket.title == "Shipped the auth migration"
    assert ticket.description == "all done"

    # A pull only reads the issue; it never writes to Jira or queues a sync.
    assert state["get_issue_summary_and_description"] == 1
    assert (
        await db_session.execute(
            select(TrackerSyncEvent).where(TrackerSyncEvent.ticket_id == ticket.id)
        )
    ).scalars().all() == []


@pytest.mark.asyncio
async def test_confirm_create_ticket_publish_unexpected_exception_reports_error(
    client: AsyncClient, db_session: AsyncSession, monkeypatch
):
    user, key = await _make_user(db_session)
    org = await _make_org(db_session, user)
    project = await _make_project(db_session, user, org)
    await _make_connection(db_session, org, user)
    await _make_mapping(db_session, org, project, user, jira_project_key="ACME")
    conv = await _make_conversation(db_session, user)
    action = await _make_action(
        db_session,
        user,
        kind="create_ticket",
        params=_create_publish_params(project),
        conversation=conv,
    )

    async def _boom(*a, **k):
        raise RuntimeError("unexpected publish bug")

    monkeypatch.setattr(
        "sessionfs.server.routes.helper.publish_ticket_to_jira", _boom
    )

    resp = await client.post(
        f"/api/v1/helper/actions/{action.id}/confirm", headers=_hdrs(key)
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "confirmed"
    target = body["result"]["targets"][0]
    assert target["ok"] is True  # the ticket itself was created
    assert target["jira_published"] is False
    assert target["jira_error"] == "error"

    # The ticket is durable so the caller can re-publish from its page.
    ticket = await db_session.get(Ticket, target["ticket_id"])
    assert ticket is not None
    assert ticket.project_id == project.id


@pytest.mark.asyncio
@pytest.mark.parametrize("direction", ["to_jira", "from_jira"])
async def test_confirm_jira_overwrite_relinked_between_propose_and_confirm_is_refused(
    client: AsyncClient, db_session: AsyncSession, monkeypatch, direction: str
):
    """Re-pointing the link after the proposal pinned its key must refuse.

    The overwrite proposal records the linked issue's key; if the ticket is
    re-pointed (or relinked) before confirm, both directions must refuse with
    ``jira_link_changed`` and write nothing to Jira or the ticket.
    """
    user, key = await _make_user(db_session)
    org = await _make_org(db_session, user)
    project = await _make_project(db_session, user, org)
    ticket = await _make_ticket(db_session, user, project)
    await _make_connection(db_session, org, user)
    await _make_mapping(db_session, org, project, user, jira_project_key="ACME")
    link = await _make_link(db_session, org, ticket, issue_key="ACME-7")
    conv = await _make_conversation(db_session, user)
    action = await _make_action(
        db_session,
        user,
        kind="jira_overwrite",
        params={
            "ticket_id": ticket.id,
            "project_id": project.id,
            "issue_key": "ACME-7",
            "direction": direction,
        },
        conversation=conv,
    )

    # Re-point the link after the proposal pinned ACME-7.
    link.issue_key = "ACME-9"
    link.issue_url = f"{_SITE}/browse/ACME-9"
    await db_session.commit()

    if direction == "to_jira":
        state = _fake_publish_client(monkeypatch, issue_key="ACME-9")
    else:
        state = _fake_pull_client(monkeypatch)

    resp = await client.post(
        f"/api/v1/helper/actions/{action.id}/confirm", headers=_hdrs(key)
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "failed"
    target = body["result"]["targets"][0]
    assert target["ok"] is False
    assert target["error"] == "jira_link_changed"

    # Nothing was written to Jira or to the ticket in either direction.
    if direction == "to_jira":
        assert state["create_issue"] == 0
        assert state["update_issue"] == 0
    else:
        assert state["get_issue_summary_and_description"] == 0
    ticket = await db_session.get(Ticket, ticket.id, populate_existing=True)
    assert ticket.title == "Flaky CI"
    assert ticket.description == "old description"


@pytest.mark.asyncio
@pytest.mark.parametrize("direction", ["to_jira", "from_jira"])
async def test_confirm_jira_overwrite_unexpected_exception_reports_error(
    client: AsyncClient, db_session: AsyncSession, monkeypatch, direction: str
):
    """A bug in the publish/pull path reports a stable generic failure.

    The confirm must never 500; the target reports ``error: "error"`` (plus
    ``jira_published``/``jira_error`` for to_jira), the action is marked failed,
    and a retrieval-audit event records the decision.
    """
    user, key = await _make_user(db_session)
    org = await _make_org(db_session, user)
    project = await _make_project(db_session, user, org)
    ticket = await _make_ticket(db_session, user, project)
    await _make_connection(db_session, org, user)
    await _make_mapping(db_session, org, project, user, jira_project_key="ACME")
    await _make_link(db_session, org, ticket, issue_key="ACME-7")
    conv = await _make_conversation(db_session, user)
    action = await _make_action(
        db_session,
        user,
        kind="jira_overwrite",
        params={
            "ticket_id": ticket.id,
            "project_id": project.id,
            "issue_key": "ACME-7",
            "direction": direction,
        },
        conversation=conv,
    )

    async def _boom(*a, **k):
        raise RuntimeError("unexpected overwrite bug")

    if direction == "to_jira":
        monkeypatch.setattr(
            "sessionfs.server.services.helper_ticket_actions.publish_ticket_to_jira",
            _boom,
        )
    else:
        monkeypatch.setattr("sessionfs.server.routes.jira.pull_ticket_from_jira", _boom)

    resp = await client.post(
        f"/api/v1/helper/actions/{action.id}/confirm", headers=_hdrs(key)
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "failed"
    target = body["result"]["targets"][0]
    assert target["ok"] is False
    assert target["error"] == "error"
    if direction == "to_jira":
        assert target["jira_published"] is False
        assert target["jira_error"] == "error"

    # The decision is durable: a source=helper audit event records the failure.
    events = (await db_session.execute(select(RetrievalAuditEvent))).scalars().all()
    event = next(e for e in events if e.tool_name == "propose_jira_overwrite")
    assert event.source == "helper"
    args = json.loads(event.arguments)
    assert args["action_id"] == action.id and args["decision"] == "failed"


@pytest.mark.asyncio
async def test_confirm_jira_overwrite_link_changed_rollback_is_clean(
    client: AsyncClient, db_session: AsyncSession, monkeypatch
):
    """The publish-side ``link_changed`` rollback must not corrupt the confirm.

    When ``publish_ticket_draft`` rolls back after its fenced mark write finds
    the link re-pointed, the confirm handler still re-loads the action and
    returns a clean ``link_changed`` target (no MissingGreenlet/500).
    """
    user, key = await _make_user(db_session)
    org = await _make_org(db_session, user)
    project = await _make_project(db_session, user, org)
    ticket = await _make_ticket(db_session, user, project)
    await _make_connection(db_session, org, user)
    await _make_mapping(db_session, org, project, user, jira_project_key="ACME")
    await _make_link(db_session, org, ticket, issue_key="ACME-7")
    conv = await _make_conversation(db_session, user)
    action = await _make_action(
        db_session,
        user,
        kind="jira_overwrite",
        params={
            "ticket_id": ticket.id,
            "project_id": project.id,
            "issue_key": "ACME-7",
            "direction": "to_jira",
        },
        conversation=conv,
    )
    state = _fake_publish_client(monkeypatch, issue_key="ACME-7")

    async def _fenced_false(*a, **k):
        return False

    # Simulate the link changing between the overwrite's re-check and its
    # publish claim: the fenced UPDATE matches no row, so the publish path
    # rolls back and raises link_changed.
    monkeypatch.setattr("sessionfs.server.routes.jira._fenced_mark_updated", _fenced_false)

    resp = await client.post(
        f"/api/v1/helper/actions/{action.id}/confirm", headers=_hdrs(key)
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "failed"
    target = body["result"]["targets"][0]
    assert target["ok"] is False
    assert target["error"] == "link_changed"
    assert target["jira_published"] is False
    assert target["jira_error"] == "link_changed"

    # The outbound update happened, but the link write was fenced off.
    assert state["update_issue"] == 1
    assert state["create_issue"] == 0

    # The rollback expired this request's ORM objects; the handler re-loaded
    # the action and still wrote its result + audit row.
    events = (await db_session.execute(select(RetrievalAuditEvent))).scalars().all()
    event = next(e for e in events if e.tool_name == "propose_jira_overwrite")
    assert event.source == "helper"
    args = json.loads(event.arguments)
    assert args["action_id"] == action.id and args["decision"] == "failed"


@pytest.mark.asyncio
async def test_confirm_jira_overwrite_from_jira_non_creator_non_admin_refused(
    client: AsyncClient, db_session: AsyncSession, monkeypatch
):
    """A plain org member (editor, but not the ticket creator or a project
    admin) cannot pull the issue back into someone else's ticket.

    The confirm re-runs the creator/project-admin edit sequence inside
    ``pull_ticket_from_jira``, so the target refuses without reading Jira.
    """
    admin, _ = await _make_user(db_session, name="admin")
    member, key = await _make_user(db_session, name="member")
    org = await _make_org(db_session, admin, member=member)
    project = await _make_project(db_session, admin, org)
    ticket = await _make_ticket(db_session, admin, project)  # created by admin
    await _make_connection(db_session, org, admin)
    await _make_mapping(db_session, org, project, admin, jira_project_key="ACME")
    await _make_link(db_session, org, ticket, issue_key="ACME-7")
    conv = await _make_conversation(db_session, member)
    action = await _make_action(
        db_session,
        member,
        kind="jira_overwrite",
        params={
            "ticket_id": ticket.id,
            "project_id": project.id,
            "issue_key": "ACME-7",
            "direction": "from_jira",
        },
        conversation=conv,
    )
    state = _fake_pull_client(monkeypatch)

    resp = await client.post(
        f"/api/v1/helper/actions/{action.id}/confirm", headers=_hdrs(key)
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "failed"
    target = body["result"]["targets"][0]
    assert target["ok"] is False
    assert target["error"] == "forbidden"
    # The remote issue was never read.
    assert state["get_issue_summary_and_description"] == 0


@pytest.mark.asyncio
async def test_propose_jira_overwrite_from_jira_non_creator_non_admin_is_refused(
    db_session: AsyncSession,
):
    admin, _ = await _make_user(db_session, name="admin")
    member, _key = await _make_user(db_session, name="member")
    org = await _make_org(db_session, admin, member=member)
    project = await _make_project(db_session, admin, org)
    ticket = await _make_ticket(db_session, admin, project)
    await _make_connection(db_session, org, admin)
    await _make_mapping(db_session, org, project, admin, jira_project_key="ACME")
    await _make_link(db_session, org, ticket, issue_key="ACME-7")
    ctx = _ctx(db_session, member)

    output = await TOOLS["propose_jira_overwrite"].handler(
        ctx, {"ticket_id": ticket.id, "direction": "from_jira"}
    )
    assert ctx.pending_actions == []
    assert "creator" in output.text

    # Pushing the ticket to Jira only needs editor standing, so it still proposes.
    await TOOLS["propose_jira_overwrite"].handler(
        ctx, {"ticket_id": ticket.id, "direction": "to_jira"}
    )
    assert len(ctx.pending_actions) == 1
