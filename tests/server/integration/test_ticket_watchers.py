"""Ticket watchers integration tests.

Covers the notification layer behind the follow/unfollow feature: who gets
auto-subscribed, self-notification suppression, ``your_turn`` derivation,
burst coalescing (with kind ranking), tenant-isolation on read and email,
service-key actor attribution, the agent delta cursor + ack, and the email
digest sweep's preference gating and idempotency.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from httpx import AsyncClient
from sqlalchemy import delete, select, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server.auth.keys import generate_api_key, hash_api_key
from sessionfs.server.db.models import (
    AgentPersona,
    ApiKey,
    OrgMember,
    Organization,
    Project,
    TicketComment,
    TicketNotification,
    TicketWatcher,
    TicketWatchCursor,
    TrustedReviewer,
    User,
    UserNotificationSettings,
)


async def _make_user(
    db: AsyncSession, name: str = "alice", tier: str = "team"
) -> tuple[User, str]:
    """Create a user with a wildcard-scoped user key. Returns (user, raw)."""
    user = User(
        id=str(uuid.uuid4()),
        email=f"{name}-{uuid.uuid4().hex[:6]}@example.com",
        display_name=name.capitalize(),
        tier=tier,
        email_verified=True,
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
            scopes='["*"]',
            created_at=datetime.now(timezone.utc),
        )
    )
    await db.commit()
    return user, raw


def _hdrs(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


async def _make_project(db: AsyncSession, owner: User, org: Organization | None = None) -> Project:
    project = Project(
        id=f"proj_{uuid.uuid4().hex[:16]}",
        name=f"watch-{uuid.uuid4().hex[:6]}",
        git_remote_normalized=f"acme/w-{uuid.uuid4().hex[:6]}",
        context_document="",
        owner_id=owner.id,
        org_id=org.id if org else None,
    )
    db.add(project)
    await db.commit()
    await db.refresh(project)
    return project


async def _make_persona(
    db: AsyncSession, project: Project, owner: User, name: str = "atlas"
) -> AgentPersona:
    persona = AgentPersona(
        id=f"per_{uuid.uuid4().hex[:16]}",
        project_id=project.id,
        name=name,
        role="Backend",
        created_by=owner.id,
    )
    db.add(persona)
    await db.commit()
    await db.refresh(persona)
    return persona


async def _make_org(db: AsyncSession, slug: str | None = None) -> Organization:
    org = Organization(
        id=f"org_{uuid.uuid4().hex[:12]}",
        name=f"Org {uuid.uuid4().hex[:6]}",
        slug=slug or f"o-{uuid.uuid4().hex[:8]}",
        tier="team",
        created_at=datetime.now(timezone.utc),
    )
    db.add(org)
    await db.commit()
    await db.refresh(org)
    return org


async def _make_member(db: AsyncSession, org: Organization, user: User, role: str = "member") -> None:
    db.add(
        OrgMember(
            org_id=org.id,
            user_id=user.id,
            role=role,
            joined_at=datetime.now(timezone.utc),
        )
    )
    await db.commit()


async def _create_ticket(client: AsyncClient, project: Project, key: str, **overrides) -> str:
    body = {"title": "Fix the flaky test", "description": "integration coverage"}
    body.update(overrides)
    resp = await client.post(
        f"/api/v1/projects/{project.id}/tickets", headers=_hdrs(key), json=body
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


async def _comment(
    client: AsyncClient, project: Project, ticket_id: str, key: str, content: str,
    author_persona: str | None = None,
) -> None:
    resp = await client.post(
        f"/api/v1/projects/{project.id}/tickets/{ticket_id}/comments",
        headers=_hdrs(key),
        json={"content": content, "author_persona": author_persona},
    )
    assert resp.status_code == 201, resp.text


async def _notification_rows(
    db: AsyncSession, user_id: str
) -> list[TicketNotification]:
    return (
        await db.execute(
            select(TicketNotification)
            .where(TicketNotification.user_id == user_id)
            .order_by(TicketNotification.id.asc())
        )
    ).scalars().all()


async def _backdate_updated_at(
    db: AsyncSession, user_id: str, age: timedelta = timedelta(minutes=30)
) -> None:
    """Age a user's notification rows past the delta commit horizon and the
    email sweep's minimum age, so the endpoints under test treat them as
    settled."""
    await db.execute(
        update(TicketNotification)
        .where(TicketNotification.user_id == user_id)
        .values(updated_at=datetime.now(timezone.utc) - age)
    )
    await db.commit()


async def _watcher_rows(db: AsyncSession, ticket_id: str) -> list[TicketWatcher]:
    return (
        await db.execute(
            select(TicketWatcher)
            .where(TicketWatcher.ticket_id == ticket_id)
            .order_by(TicketWatcher.user_id)
        )
    ).scalars().all()


async def _platform_admin_headers(db: AsyncSession) -> tuple[User, dict]:
    admin = User(
        id=str(uuid.uuid4()),
        email=f"platform-admin-{uuid.uuid4().hex[:6]}@example.com",
        display_name="Platform Admin",
        tier="admin",
        created_at=datetime.now(timezone.utc),
        email_verified=True,
    )
    raw = generate_api_key()
    db.add(admin)
    db.add(
        ApiKey(
            id=str(uuid.uuid4()),
            user_id=admin.id,
            key_hash=hash_api_key(raw),
            name="admin-key",
            scopes='["*"]',
            created_at=datetime.now(timezone.utc),
        )
    )
    await db.commit()
    return admin, {"Authorization": f"Bearer {raw}"}


class _RecordingProvider:
    def __init__(self) -> None:
        self.sends: list[dict] = []

    async def send_ticket_notification(
        self, to_email: str, notifications: list[dict], dashboard_url: str
    ) -> dict:
        self.sends.append({"to": to_email, "notifications": notifications})
        return {"status": "sent"}


# ── Auto-subscribe + self-notification ─────────────────────────


@pytest.mark.asyncio
async def test_creator_auto_subscribes(client: AsyncClient, db_session: AsyncSession):
    owner, key = await _make_user(db_session, "owner")
    project = await _make_project(db_session, owner)
    ticket_id = await _create_ticket(client, project, key)

    watchers = await _watcher_rows(db_session, ticket_id)
    assert [(w.user_id, w.source) for w in watchers] == [(owner.id, "creator")]


@pytest.mark.asyncio
async def test_commenter_auto_subscribes_and_actor_is_not_notified(
    client: AsyncClient, db_session: AsyncSession
):
    owner, owner_key = await _make_user(db_session, "owner")
    commenter, commenter_key = await _make_user(db_session, "commenter")
    org = await _make_org(db_session)
    project = await _make_project(db_session, owner, org)
    await _make_member(db_session, org, owner, role="admin")
    await _make_member(db_session, org, commenter)
    ticket_id = await _create_ticket(client, project, owner_key)

    await _comment(client, project, ticket_id, commenter_key, "nice catch")

    watchers = await _watcher_rows(db_session, ticket_id)
    sources = {w.user_id: w.source for w in watchers}
    assert sources == {owner.id: "creator", commenter.id: "commenter"}

    # The commenter learns nothing about their own comment; the owner does.
    assert await _notification_rows(db_session, commenter.id) == []
    owner_rows = await _notification_rows(db_session, owner.id)
    assert len(owner_rows) == 1
    assert owner_rows[0].kind == "comment"
    assert owner_rows[0].actor_user_id == commenter.id


@pytest.mark.asyncio
async def test_reviewer_auto_subscribes_and_emits_review_verdict(
    client: AsyncClient, db_session: AsyncSession
):
    owner, owner_key = await _make_user(db_session, "owner")
    reviewer, reviewer_key = await _make_user(db_session, "reviewer")
    follower, follower_key = await _make_user(db_session, "follower")
    org = await _make_org(db_session)
    project = await _make_project(db_session, owner, org)
    await _make_member(db_session, org, owner, role="admin")
    await _make_member(db_session, org, reviewer)
    await _make_member(db_session, org, follower)
    ticket_id = await _create_ticket(client, project, owner_key)

    # A neutral third watcher so the review_verdict kind itself is observable
    # (the reporter's own row is upgraded to your_turn for a clean verdict).
    await client.put(
        f"/api/v1/projects/{project.id}/tickets/{ticket_id}/watch",
        headers=_hdrs(follower_key),
        json={"muted": False},
    )

    db_session.add(
        TrustedReviewer(
            id=f"tr_{uuid.uuid4().hex[:12]}",
            org_id=org.id,
            user_id=reviewer.id,
            reviewer_persona="codex-reviewer",
            created_by_user_id=owner.id,
            is_active=True,
        )
    )
    await db_session.commit()

    await _comment(
        client, project, ticket_id, reviewer_key,
        "Codex R1 review on tk_abc: VERIFIED-CLEAN",
        author_persona="codex-reviewer",
    )

    watchers = await _watcher_rows(db_session, ticket_id)
    sources = {w.user_id: w.source for w in watchers}
    assert sources == {
        owner.id: "creator",
        reviewer.id: "reviewer",
        follower.id: "explicit",
    }

    # The reviewer (actor) learns nothing; the reporter gets your_turn; the
    # neutral follower gets the plain review_verdict.
    assert await _notification_rows(db_session, reviewer.id) == []
    owner_rows = await _notification_rows(db_session, owner.id)
    assert [r.kind for r in owner_rows] == ["your_turn"]
    assert owner_rows[0].actor_user_id == reviewer.id
    follower_rows = await _notification_rows(db_session, follower.id)
    assert [r.kind for r in follower_rows] == ["review_verdict"]


@pytest.mark.asyncio
async def test_assignee_human_auto_subscribes_on_create(
    client: AsyncClient, db_session: AsyncSession
):
    owner, owner_key = await _make_user(db_session, "owner")
    assignee, _assignee_key = await _make_user(db_session, "assignee")
    org = await _make_org(db_session)
    project = await _make_project(db_session, owner, org)
    await _make_member(db_session, org, owner, role="admin")
    await _make_member(db_session, org, assignee)
    db_session.add(
        AgentPersona(
            id=f"per_{uuid.uuid4().hex[:12]}",
            org_id=org.id,
            name="atlas",
            role="Backend",
            created_by=owner.id,
            represented_user_id=assignee.id,
        )
    )
    await db_session.commit()

    ticket_id = await _create_ticket(
        client, project, owner_key, assigned_to="atlas"
    )

    watchers = await _watcher_rows(db_session, ticket_id)
    by_user = {w.user_id: w for w in watchers}
    assert set(by_user) == {owner.id, assignee.id}
    assert by_user[assignee.id].source == "assignee"
    assert by_user[assignee.id].persona_name == "atlas"


# ── Coalescing ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_burst_coalesces_into_one_row_with_kind_ranking(
    client: AsyncClient, db_session: AsyncSession
):
    owner, owner_key = await _make_user(db_session, "owner")
    commenter, commenter_key = await _make_user(db_session, "commenter")
    follower, follower_key = await _make_user(db_session, "follower")
    org = await _make_org(db_session)
    project = await _make_project(db_session, owner, org)
    await _make_member(db_session, org, owner, role="admin")
    await _make_member(db_session, org, commenter)
    await _make_member(db_session, org, follower)
    ticket_id = await _create_ticket(client, project, owner_key)
    await client.put(
        f"/api/v1/projects/{project.id}/tickets/{ticket_id}/watch",
        headers=_hdrs(follower_key),
        json={"muted": False},
    )

    # A comment (kind rank 1) followed by a reassignment (kind rank 3) on the
    # same ticket within the coalescing window must stay a single unread row
    # for the neutral follower, whose kind upgrades and event_count accumulates.
    await _comment(client, project, ticket_id, commenter_key, "first")
    resp = await client.put(
        f"/api/v1/projects/{project.id}/tickets/{ticket_id}",
        headers=_hdrs(owner_key),
        json={"assigned_to": "atlas"},
    )
    assert resp.status_code == 200, resp.text

    rows = await _notification_rows(db_session, follower.id)
    assert len(rows) == 1
    assert rows[0].kind == "assignment"
    assert rows[0].event_count == 2
    assert rows[0].read_at is None
    assert rows[0].emailed_at is None


@pytest.mark.asyncio
async def test_reassignment_hands_assignee_human_your_turn(
    client: AsyncClient, db_session: AsyncSession
):
    owner, owner_key = await _make_user(db_session, "owner")
    assignee, _assignee_key = await _make_user(db_session, "assignee")
    follower, follower_key = await _make_user(db_session, "follower")
    org = await _make_org(db_session)
    project = await _make_project(db_session, owner, org)
    await _make_member(db_session, org, owner, role="admin")
    await _make_member(db_session, org, assignee)
    await _make_member(db_session, org, follower)
    db_session.add(
        AgentPersona(
            id=f"per_{uuid.uuid4().hex[:12]}",
            org_id=org.id,
            name="atlas",
            role="Backend",
            created_by=owner.id,
            represented_user_id=assignee.id,
        )
    )
    await db_session.commit()
    ticket_id = await _create_ticket(client, project, owner_key)

    # A neutral watcher so the plain assignment kind is observable (the owner,
    # who reassigns, is the actor and therefore never notified).
    watch = await client.put(
        f"/api/v1/projects/{project.id}/tickets/{ticket_id}/watch",
        headers=_hdrs(follower_key),
        json={"muted": False},
    )
    assert watch.status_code == 200, watch.text

    resp = await client.put(
        f"/api/v1/projects/{project.id}/tickets/{ticket_id}",
        headers=_hdrs(owner_key),
        json={"assigned_to": "atlas"},
    )
    assert resp.status_code == 200, resp.text

    # The owner (actor) learns nothing; the assignee's human owner gets the
    # urgent your_turn and is auto-subscribed first so they receive it; the
    # neutral follower gets the plain assignment event.
    assert await _notification_rows(db_session, owner.id) == []

    assignee_rows = await _notification_rows(db_session, assignee.id)
    assert len(assignee_rows) == 1
    assert assignee_rows[0].kind == "your_turn"
    assert assignee_rows[0].actor_user_id == owner.id

    follower_rows = await _notification_rows(db_session, follower.id)
    assert [r.kind for r in follower_rows] == ["assignment"]

    watchers = await _watcher_rows(db_session, ticket_id)
    assert {w.user_id for w in watchers} == {owner.id, assignee.id, follower.id}


# ── Follow / unfollow / mute lifecycle + isolation ────────────


@pytest.mark.asyncio
async def test_follow_unfollow_mute_lifecycle(
    client: AsyncClient, db_session: AsyncSession
):
    owner, owner_key = await _make_user(db_session, "owner")
    follower, follower_key = await _make_user(db_session, "follower")
    org = await _make_org(db_session)
    project = await _make_project(db_session, owner, org)
    await _make_member(db_session, org, owner, role="admin")
    await _make_member(db_session, org, follower)
    ticket_id = await _create_ticket(client, project, owner_key)

    # Follow explicitly, then mute.
    resp = await client.put(
        f"/api/v1/projects/{project.id}/tickets/{ticket_id}/watch",
        headers=_hdrs(follower_key),
        json={"muted": False},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["source"] == "explicit"
    assert resp.json()["muted"] is False

    resp = await client.put(
        f"/api/v1/projects/{project.id}/tickets/{ticket_id}/watch",
        headers=_hdrs(follower_key),
        json={"muted": True},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["muted"] is True

    watchers = await client.get(
        f"/api/v1/projects/{project.id}/tickets/{ticket_id}/watchers",
        headers=_hdrs(owner_key),
    )
    assert watchers.status_code == 200, watchers.text
    by_user = {w["user_id"]: w for w in watchers.json()}
    assert by_user[follower.id]["muted"] is True

    resp = await client.delete(
        f"/api/v1/projects/{project.id}/tickets/{ticket_id}/watch",
        headers=_hdrs(follower_key),
    )
    assert resp.status_code == 204, resp.text
    # The follower's explicit watcher is gone; only the creator's
    # auto-subscription remains.
    remaining = await _watcher_rows(db_session, ticket_id)
    assert {w.user_id for w in remaining} == {owner.id}


@pytest.mark.asyncio
async def test_watch_endpoints_return_404_not_403_across_orgs(
    client: AsyncClient, db_session: AsyncSession
):
    owner, owner_key = await _make_user(db_session, "owner")
    outsider, outsider_key = await _make_user(db_session, "outsider")
    project = await _make_project(db_session, owner)
    ticket_id = await _create_ticket(client, project, owner_key)

    base = f"/api/v1/projects/{project.id}/tickets/{ticket_id}"
    for method, url, kwargs in [
        ("PUT", f"{base}/watch", {"json": {"muted": False}}),
        ("GET", f"{base}/watchers", {}),
        ("DELETE", f"{base}/watch", {}),
    ]:
        resp = await client.request(
            method, url, headers=_hdrs(outsider_key), **kwargs
        )
        assert resp.status_code == 404, (method, resp.text)


# ── Inbox list / unread-count / mark-read ─────────────────────


@pytest.mark.asyncio
async def test_inbox_list_unread_count_and_mark_read(
    client: AsyncClient, db_session: AsyncSession
):
    owner, owner_key = await _make_user(db_session, "owner")
    commenter, commenter_key = await _make_user(db_session, "commenter")
    org = await _make_org(db_session)
    project = await _make_project(db_session, owner, org)
    await _make_member(db_session, org, owner, role="admin")
    await _make_member(db_session, org, commenter)
    ticket_id = await _create_ticket(client, project, owner_key)
    await _comment(client, project, ticket_id, commenter_key, "first")
    await _comment(client, project, ticket_id, commenter_key, "second")

    count = await client.get(
        "/api/v1/me/ticket-notifications/unread-count", headers=_hdrs(owner_key)
    )
    assert count.status_code == 200
    body = count.json()
    assert body["count"] == 1  # coalesced into one unread row
    assert "ETag" in count.headers

    # ETag poll: same tag → 304.
    again = await client.get(
        "/api/v1/me/ticket-notifications/unread-count",
        headers={**_hdrs(owner_key), "If-None-Match": count.headers["ETag"]},
    )
    assert again.status_code == 304

    listing = await client.get(
        "/api/v1/me/ticket-notifications", headers=_hdrs(owner_key)
    )
    assert listing.status_code == 200, listing.text
    items = listing.json()["items"]
    assert len(items) == 1
    assert items[0]["kind"] == "comment"
    assert items[0]["event_count"] == 2
    assert items[0]["ticket_title"] == "Fix the flaky test"

    marked = await client.post(
        "/api/v1/me/ticket-notifications/read",
        headers=_hdrs(owner_key),
        json={"all": True},
    )
    assert marked.status_code == 200, marked.text
    assert marked.json() == {"updated": 1}

    after = await client.get(
        "/api/v1/me/ticket-notifications/unread-count", headers=_hdrs(owner_key)
    )
    assert after.json()["count"] == 0


# ── Access loss hides rows and blocks email ───────────────────


@pytest.mark.asyncio
async def test_access_loss_hides_rows_and_blocks_email(
    client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
):
    owner, owner_key = await _make_user(db_session, "owner")
    member, member_key = await _make_user(db_session, "member")
    org = await _make_org(db_session)
    project = await _make_project(db_session, owner, org)
    await _make_member(db_session, org, owner, role="admin")
    await _make_member(db_session, org, member)
    ticket_id = await _create_ticket(client, project, owner_key)

    # The member comments, which auto-subscribes them and leaves the owner a
    # notification — but to give the member a notification of their own, the
    # owner comments back.
    await _comment(client, project, ticket_id, member_key, "member speaks")
    await _comment(client, project, ticket_id, owner_key, "owner replies")

    member_rows = await _notification_rows(db_session, member.id)
    assert len(member_rows) == 1

    # Backdate the member's notification so the sweep considers it eligible.
    await db_session.execute(
        update(TicketNotification)
        .where(TicketNotification.id == member_rows[0].id)
        .values(updated_at=datetime.now(timezone.utc) - timedelta(minutes=30))
    )
    await db_session.commit()

    # The member is removed from the org: their inbox and email eligibility
    # must evaporate immediately.
    await db_session.execute(delete(OrgMember).where(OrgMember.user_id == member.id))
    await db_session.commit()

    listing = await client.get(
        "/api/v1/me/ticket-notifications", headers=_hdrs(member_key)
    )
    assert listing.status_code == 200
    assert listing.json()["items"] == []

    count = await client.get(
        "/api/v1/me/ticket-notifications/unread-count", headers=_hdrs(member_key)
    )
    assert count.json()["count"] == 0

    admin, admin_headers = await _platform_admin_headers(db_session)
    recorder = _RecordingProvider()
    import sessionfs.server.email as email_mod

    monkeypatch.setattr(email_mod, "create_email_provider", lambda config: recorder)
    sweep = await client.post(
        "/api/v1/admin/sweeps/ticket-notifications", headers=admin_headers, json={}
    )
    assert sweep.status_code == 200, sweep.text
    assert recorder.sends == []
    # The member's now-inaccessible row is claimed (marked emailed) so it never
    # resurfaces, and counted as skipped-access.
    assert sweep.json()["notifications_skipped_access"] >= 1


# ── Agent delta cursor + ack idempotency ──────────────────────


@pytest.mark.asyncio
async def test_agent_delta_and_ack_are_cursor_based_and_idempotent(
    client: AsyncClient, db_session: AsyncSession
):
    owner, owner_key = await _make_user(db_session, "owner")
    commenter, commenter_key = await _make_user(db_session, "commenter")
    org = await _make_org(db_session)
    project = await _make_project(db_session, owner, org)
    await _make_member(db_session, org, owner, role="admin")
    await _make_member(db_session, org, commenter)
    ticket_id = await _create_ticket(client, project, owner_key)
    await _comment(client, project, ticket_id, commenter_key, "one")
    await _comment(client, project, ticket_id, commenter_key, "two")
    await _backdate_updated_at(db_session, owner.id)

    first = await client.get(
        "/api/v1/me/ticket-updates", headers=_hdrs(owner_key), params={"persona": "atlas"}
    )
    assert first.status_code == 200, first.text
    items = first.json()["items"]
    assert len(items) == 1  # coalesced
    cursor = first.json()["next_cursor"]
    assert cursor == items[-1]["id"]

    ack = await client.post(
        "/api/v1/me/ticket-updates/ack",
        headers=_hdrs(owner_key),
        json={"cursor": cursor, "persona": "atlas"},
    )
    assert ack.status_code == 200, ack.text
    assert ack.json() == {"cursor": cursor}

    second = await client.get(
        "/api/v1/me/ticket-updates", headers=_hdrs(owner_key), params={"persona": "atlas"}
    )
    assert second.json()["items"] == []
    assert second.json()["next_cursor"] == cursor

    # Acking an older cursor is a no-op — the stored cursor never moves back.
    stale = await client.post(
        "/api/v1/me/ticket-updates/ack",
        headers=_hdrs(owner_key),
        json={"cursor": 1, "persona": "atlas"},
    )
    assert stale.status_code == 200, stale.text
    assert stale.json() == {"cursor": cursor}


# ── Service-key actor attribution ─────────────────────────────


@pytest.mark.asyncio
async def test_service_key_comment_attributes_service_key_and_notifies_minter(
    client: AsyncClient, db_session: AsyncSession
):
    owner, owner_key = await _make_user(db_session, "owner")
    org = await _make_org(db_session)
    project = await _make_project(db_session, owner, org)
    await _make_member(db_session, org, owner, role="admin")
    ticket_id = await _create_ticket(client, project, owner_key)

    raw = generate_api_key()
    db_session.add(
        ApiKey(
            id=str(uuid.uuid4()),
            user_id=owner.id,
            key_hash=hash_api_key(raw),
            name=f"svc-{uuid.uuid4().hex[:6]}",
            is_active=True,
            key_kind="service",
            org_id=org.id,
            scopes=json.dumps(["tickets:write"]),
            created_by_user_id=owner.id,
            service_key_name="triage-bot",
            project_ids=json.dumps([project.id]),
            created_at=datetime.now(timezone.utc),
        )
    )
    await db_session.commit()

    await _comment(
        client, project, ticket_id, raw, "automated triage", author_persona="triage"
    )

    # The service key's minter is NOT the actor (actor_user_id=None) and is
    # therefore notified like any other watcher, instead of being silently
    # suppressed. The event is attributed to the service key itself.
    rows = await _notification_rows(db_session, owner.id)
    assert len(rows) == 1
    assert rows[0].kind == "comment"
    assert rows[0].actor_type == "service_key"
    assert rows[0].actor_user_id is None
    assert rows[0].service_key_name == "triage-bot"


# ── Email sweep: preference gating + idempotency ──────────────


@pytest.mark.asyncio
async def test_sweep_respects_off_and_all_preferences(
    client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
):
    # User A prefers "off": never emailed. User B prefers "all": emailed even
    # though their only pending notification is a plain comment.
    admin, admin_headers = await _platform_admin_headers(db_session)

    users = {}
    for name in ("off", "all"):
        owner, owner_key = await _make_user(db_session, f"owner-{name}")
        recipient, recipient_key = await _make_user(db_session, f"recv-{name}")
        org = await _make_org(db_session)
        project = await _make_project(db_session, owner, org)
        await _make_member(db_session, org, owner, role="admin")
        await _make_member(db_session, org, recipient)
        ticket_id = await _create_ticket(client, project, owner_key)
        await _comment(client, project, ticket_id, recipient_key, f"{name} speaks")
        await _comment(client, project, ticket_id, owner_key, "owner replies")
        rows = await _notification_rows(db_session, recipient.id)
        assert len(rows) == 1
        await db_session.execute(
            update(TicketNotification)
            .where(TicketNotification.id == rows[0].id)
            .values(updated_at=datetime.now(timezone.utc) - timedelta(minutes=30))
        )
        users[name] = {"user": recipient, "key": recipient_key, "row": rows[0]}
        await db_session.commit()

    # Persist the two preferences.
    db_session.add(UserNotificationSettings(user_id=users["off"]["user"].id, ticket_email="off"))
    db_session.add(UserNotificationSettings(user_id=users["all"]["user"].id, ticket_email="all"))
    await db_session.commit()

    recorder = _RecordingProvider()
    import sessionfs.server.email as email_mod

    monkeypatch.setattr(email_mod, "create_email_provider", lambda config: recorder)
    sweep = await client.post(
        "/api/v1/admin/sweeps/ticket-notifications", headers=admin_headers, json={}
    )
    assert sweep.status_code == 200, sweep.text
    body = sweep.json()
    assert body["users_emailed"] == 1
    assert body["notifications_skipped_preference"] >= 1
    assert [s["to"] for s in recorder.sends] == [users["all"]["user"].email]

    # Both rows are now claimed, so a second sweep sends nothing.
    recorder.sends.clear()
    second = await client.post(
        "/api/v1/admin/sweeps/ticket-notifications", headers=admin_headers, json={}
    )
    assert second.status_code == 200, second.text
    assert recorder.sends == []
    assert second.json()["users_emailed"] == 0


@pytest.mark.asyncio
async def test_sweep_your_turn_gate_emails_only_your_turn(
    client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
):
    # Default preference is your_turn: a plain-comment recipient is skipped,
    # while a recipient with a your_turn notification is emailed.
    admin, admin_headers = await _platform_admin_headers(db_session)

    # Comment-only recipient (default your_turn → skipped).
    owner, owner_key = await _make_user(db_session, "owner")
    commenter, commenter_key = await _make_user(db_session, "commenter")
    comment_org = await _make_org(db_session)
    project = await _make_project(db_session, owner, comment_org)
    await _make_member(db_session, comment_org, owner, role="admin")
    await _make_member(db_session, comment_org, commenter)
    ticket_id = await _create_ticket(client, project, owner_key)
    await _comment(client, project, ticket_id, commenter_key, "speaks")
    await _comment(client, project, ticket_id, owner_key, "replies")
    comment_rows = await _notification_rows(db_session, commenter.id)
    assert len(comment_rows) == 1
    await db_session.execute(
        update(TicketNotification)
        .where(TicketNotification.id == comment_rows[0].id)
        .values(updated_at=datetime.now(timezone.utc) - timedelta(minutes=30))
    )

    # A separate your_turn notification for a fresh user (default your_turn).
    assignee, _assignee_key = await _make_user(db_session, "assignee")
    org = await _make_org(db_session)
    org_project = await _make_project(db_session, owner, org)
    await _make_member(db_session, org, owner, role="admin")
    await _make_member(db_session, org, assignee)
    db_session.add(
        AgentPersona(
            id=f"per_{uuid.uuid4().hex[:12]}",
            org_id=org.id,
            name="atlas",
            role="Backend",
            created_by=owner.id,
            represented_user_id=assignee.id,
        )
    )
    await db_session.commit()
    org_ticket = await _create_ticket(client, org_project, owner_key)
    reassign = await client.put(
        f"/api/v1/projects/{org_project.id}/tickets/{org_ticket}",
        headers=_hdrs(owner_key),
        json={"assigned_to": "atlas"},
    )
    assert reassign.status_code == 200, reassign.text
    your_turn_rows = await _notification_rows(db_session, assignee.id)
    assert [r.kind for r in your_turn_rows] == ["your_turn"]
    await db_session.execute(
        update(TicketNotification)
        .where(TicketNotification.id == your_turn_rows[0].id)
        .values(updated_at=datetime.now(timezone.utc) - timedelta(minutes=30))
    )
    await db_session.commit()

    recorder = _RecordingProvider()
    import sessionfs.server.email as email_mod

    monkeypatch.setattr(email_mod, "create_email_provider", lambda config: recorder)
    sweep = await client.post(
        "/api/v1/admin/sweeps/ticket-notifications", headers=admin_headers, json={}
    )
    assert sweep.status_code == 200, sweep.text
    body = sweep.json()
    assert body["users_emailed"] == 1
    assert [s["to"] for s in recorder.sends] == [assignee.email]
    assert body["notifications_skipped_preference"] >= 1


# ── Service-key tenant isolation on the agent delta ───────────


@pytest.mark.asyncio
async def test_service_key_delta_is_scoped_to_key_org_and_allowlist(
    client: AsyncClient, db_session: AsyncSession
):
    """A service key shares its minter's ``user_id``; without scoping the
    delta to the key's org + project allowlist it would leak every
    notification the minter can see (other orgs included)."""
    owner, owner_key = await _make_user(db_session, "owner")
    commenter_a, commenter_a_key = await _make_user(db_session, "ca")
    commenter_b, commenter_b_key = await _make_user(db_session, "cb")

    org_a = await _make_org(db_session)
    org_b = await _make_org(db_session)
    proj_a = await _make_project(db_session, owner, org_a)
    proj_b = await _make_project(db_session, owner, org_b)
    await _make_member(db_session, org_a, owner, role="admin")
    await _make_member(db_session, org_a, commenter_a)
    await _make_member(db_session, org_b, owner, role="admin")
    await _make_member(db_session, org_b, commenter_b)

    tk_a = await _create_ticket(client, proj_a, owner_key)
    tk_b = await _create_ticket(client, proj_b, owner_key)
    await _comment(client, proj_a, tk_a, commenter_a_key, "a speaks")
    await _comment(client, proj_b, tk_b, commenter_b_key, "b speaks")
    await _backdate_updated_at(db_session, owner.id)

    raw = generate_api_key()
    db_session.add(
        ApiKey(
            id=str(uuid.uuid4()),
            user_id=owner.id,
            key_hash=hash_api_key(raw),
            name=f"svc-{uuid.uuid4().hex[:6]}",
            is_active=True,
            key_kind="service",
            org_id=org_a.id,
            scopes=json.dumps(["tickets:read"]),
            created_by_user_id=owner.id,
            service_key_name="scoped-bot",
            project_ids=json.dumps([proj_a.id]),
            created_at=datetime.now(timezone.utc),
        )
    )
    await db_session.commit()

    delta = await client.get("/api/v1/me/ticket-updates", headers=_hdrs(raw))
    assert delta.status_code == 200, delta.text
    items = delta.json()["items"]
    # Only the org-A (allowlisted) project's notification surfaces; the
    # minter's org-B notification must not leak through the service key.
    assert [i["project_id"] for i in items] == [proj_a.id]


# ── Delta cursor must advance past inaccessible rows ──────────


@pytest.mark.asyncio
async def test_delta_cursor_advances_past_inaccessible_rows(
    client: AsyncClient, db_session: AsyncSession
):
    """A long run of rows the caller lost access to must not wedge the
    cursor: ``next_cursor`` advances to the last row *scanned*, so the single
    visible row after a 100+ invisible block is still returned."""
    recipient, recipient_key = await _make_user(db_session, "recipient")
    owner_a, owner_a_key = await _make_user(db_session, "owner-a")
    owner_b, owner_b_key = await _make_user(db_session, "owner-b")

    org_a = await _make_org(db_session)
    org_b = await _make_org(db_session)
    proj_a = await _make_project(db_session, owner_a, org_a)
    proj_b = await _make_project(db_session, owner_b, org_b)
    await _make_member(db_session, org_a, owner_a, role="admin")
    await _make_member(db_session, org_a, recipient)
    await _make_member(db_session, org_b, owner_b, role="admin")
    await _make_member(db_session, org_b, recipient)

    tk_a = await _create_ticket(client, proj_a, owner_a_key)
    tk_b = await _create_ticket(client, proj_b, owner_b_key)

    # 105 inaccessible rows (more than one 100-row page) followed by one
    # visible row. Inserted directly to keep the test fast.
    for _ in range(105):
        db_session.add(
            TicketNotification(
                user_id=recipient.id,
                ticket_id=tk_a,
                project_id=proj_a.id,
                kind="comment",
                summary="hidden",
            )
        )
    db_session.add(
        TicketNotification(
            user_id=recipient.id,
            ticket_id=tk_b,
            project_id=proj_b.id,
            kind="comment",
            summary="visible",
        )
    )
    await db_session.commit()
    await _backdate_updated_at(db_session, recipient.id)

    # The recipient loses access to org A, making all 105 proj_a rows hidden.
    await db_session.execute(
        delete(OrgMember).where(
            OrgMember.user_id == recipient.id, OrgMember.org_id == org_a.id
        )
    )
    await db_session.commit()

    delta = await client.get("/api/v1/me/ticket-updates", headers=_hdrs(recipient_key))
    assert delta.status_code == 200, delta.text
    body = delta.json()
    # The single visible row is found after scanning the invisible block.
    assert [i["project_id"] for i in body["items"]] == [proj_b.id]
    # The cursor advanced to the last scanned row (the visible row), not zero.
    assert body["next_cursor"] == body["items"][-1]["id"]


# ── Sweep retention deletes stale delivered rows ──────────────


@pytest.mark.asyncio
async def test_sweep_deletes_stale_read_notifications(
    client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
):
    """A read notification older than the retention window is purged by the
    sweep, never emailed again."""
    admin, admin_headers = await _platform_admin_headers(db_session)

    owner, owner_key = await _make_user(db_session, "owner")
    commenter, commenter_key = await _make_user(db_session, "commenter")
    org = await _make_org(db_session)
    project = await _make_project(db_session, owner, org)
    await _make_member(db_session, org, owner, role="admin")
    await _make_member(db_session, org, commenter)
    ticket_id = await _create_ticket(client, project, owner_key)
    await _comment(client, project, ticket_id, commenter_key, "hello")

    rows = await _notification_rows(db_session, owner.id)
    assert len(rows) == 1
    # Mark read and backdate past the 90-day retention horizon.
    await db_session.execute(
        update(TicketNotification)
        .where(TicketNotification.id == rows[0].id)
        .values(
            read_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc) - timedelta(days=120),
        )
    )
    await db_session.commit()

    recorder = _RecordingProvider()
    import sessionfs.server.email as email_mod

    monkeypatch.setattr(email_mod, "create_email_provider", lambda config: recorder)
    sweep = await client.post(
        "/api/v1/admin/sweeps/ticket-notifications", headers=admin_headers, json={}
    )
    assert sweep.status_code == 200, sweep.text
    # The stale read row was deleted, not emailed.
    assert await _notification_rows(db_session, owner.id) == []
    assert recorder.sends == []
    assert sweep.json()["notifications_deleted_retention"] == 1


# ── Unread count aggregates in SQL + hides inaccessible projects ─


@pytest.mark.asyncio
async def test_unread_count_aggregates_and_hides_inaccessible_projects(
    client: AsyncClient, db_session: AsyncSession
):
    owner, owner_key = await _make_user(db_session, "owner")
    member, member_key = await _make_user(db_session, "member")

    org_a = await _make_org(db_session)
    org_b = await _make_org(db_session)
    proj_a = await _make_project(db_session, owner, org_a)
    proj_b = await _make_project(db_session, owner, org_b)
    await _make_member(db_session, org_a, owner, role="admin")
    await _make_member(db_session, org_a, member)
    await _make_member(db_session, org_b, owner, role="admin")
    await _make_member(db_session, org_b, member)

    tk_a = await _create_ticket(client, proj_a, owner_key)
    tk_b = await _create_ticket(client, proj_b, owner_key)
    await client.put(
        f"/api/v1/projects/{proj_a.id}/tickets/{tk_a}/watch",
        headers=_hdrs(member_key),
        json={"muted": False},
    )
    await client.put(
        f"/api/v1/projects/{proj_b.id}/tickets/{tk_b}/watch",
        headers=_hdrs(member_key),
        json={"muted": False},
    )
    await _comment(client, proj_a, tk_a, owner_key, "a")
    await _comment(client, proj_b, tk_b, owner_key, "b")

    count = await client.get(
        "/api/v1/me/ticket-notifications/unread-count", headers=_hdrs(member_key)
    )
    assert count.status_code == 200, count.text
    assert count.json()["count"] == 2

    # Removing the member from org A hides that project's unread rows.
    await db_session.execute(
        delete(OrgMember).where(
            OrgMember.user_id == member.id, OrgMember.org_id == org_a.id
        )
    )
    await db_session.commit()

    count = await client.get(
        "/api/v1/me/ticket-notifications/unread-count", headers=_hdrs(member_key)
    )
    assert count.status_code == 200, count.text
    assert count.json()["count"] == 1


# ── Watcher cleanup on member removal + user deactivation ─────


@pytest.mark.asyncio
async def test_member_removal_deletes_watcher_rows(
    client: AsyncClient, db_session: AsyncSession
):
    owner, owner_key = await _make_user(db_session, "owner")
    member, member_key = await _make_user(db_session, "member")
    org = await _make_org(db_session)
    project = await _make_project(db_session, owner, org)
    await _make_member(db_session, org, owner, role="admin")
    await _make_member(db_session, org, member)
    ticket_id = await _create_ticket(client, project, owner_key)

    # The member also follows a ticket in a SECOND org and one in their own
    # personal project — removal from the first org must leave both intact.
    owner_b, owner_b_key = await _make_user(db_session, "owner-b")
    org_b = await _make_org(db_session)
    project_b = await _make_project(db_session, owner_b, org_b)
    await _make_member(db_session, org_b, owner_b, role="admin")
    await _make_member(db_session, org_b, member)
    ticket_b = await _create_ticket(client, project_b, owner_b_key)

    personal_project = await _make_project(db_session, member)
    personal_ticket = await _create_ticket(client, personal_project, member_key)

    for proj, tk in (
        (project, ticket_id),
        (project_b, ticket_b),
        (personal_project, personal_ticket),
    ):
        watch = await client.put(
            f"/api/v1/projects/{proj.id}/tickets/{tk}/watch",
            headers=_hdrs(member_key),
            json={"muted": False},
        )
        assert watch.status_code == 200, watch.text

    assert {w.user_id for w in await _watcher_rows(db_session, ticket_id)} == {
        owner.id,
        member.id,
    }

    resp = await client.delete(
        f"/api/v1/orgs/{org.id}/members/{member.id}", headers=_hdrs(owner_key)
    )
    assert resp.status_code == 200, resp.text

    # Only the first org's watcher rows are gone; the second org and the
    # personal project keep the member subscribed.
    remaining = await _watcher_rows(db_session, ticket_id)
    assert [w.user_id for w in remaining] == [owner.id]
    surviving_b = await _watcher_rows(db_session, ticket_b)
    assert {w.user_id for w in surviving_b} == {owner_b.id, member.id}
    surviving_personal = await _watcher_rows(db_session, personal_ticket)
    assert [w.user_id for w in surviving_personal] == [member.id]


@pytest.mark.asyncio
async def test_user_deactivation_deletes_watcher_rows(
    client: AsyncClient, db_session: AsyncSession
):
    owner, owner_key = await _make_user(db_session, "owner")
    target, target_key = await _make_user(db_session, "target")
    org = await _make_org(db_session)
    project = await _make_project(db_session, owner, org)
    await _make_member(db_session, org, owner, role="admin")
    await _make_member(db_session, org, target)
    ticket_id = await _create_ticket(client, project, owner_key)

    watch = await client.put(
        f"/api/v1/projects/{project.id}/tickets/{ticket_id}/watch",
        headers=_hdrs(target_key),
        json={"muted": False},
    )
    assert watch.status_code == 200, watch.text

    admin, admin_headers = await _platform_admin_headers(db_session)
    resp = await client.delete(
        f"/api/v1/admin/users/{target.id}", headers=admin_headers
    )
    assert resp.status_code == 204, resp.text

    remaining = await _watcher_rows(db_session, ticket_id)
    assert [w.user_id for w in remaining] == [owner.id]


# ── Coalescing must never resurrect an emailed row ────────────


@pytest.mark.asyncio
async def test_coalesce_never_resurrects_emailed_row(
    client: AsyncClient, db_session: AsyncSession
):
    """A row the sweep already claimed (emailed_at set) must not be
    coalesced back into an unread row by a later event."""
    owner, owner_key = await _make_user(db_session, "owner")
    commenter, commenter_key = await _make_user(db_session, "commenter")
    org = await _make_org(db_session)
    project = await _make_project(db_session, owner, org)
    await _make_member(db_session, org, owner, role="admin")
    await _make_member(db_session, org, commenter)
    ticket_id = await _create_ticket(client, project, owner_key)

    await _comment(client, project, ticket_id, commenter_key, "first")
    rows = await _notification_rows(db_session, owner.id)
    assert len(rows) == 1

    # Simulate the sweep having already claimed the row for email.
    await db_session.execute(
        update(TicketNotification)
        .where(TicketNotification.id == rows[0].id)
        .values(emailed_at=datetime.now(timezone.utc))
    )
    await db_session.commit()

    # A second comment within the coalesce window must NOT fold into the
    # emailed row — it creates a fresh row instead.
    await _comment(client, project, ticket_id, commenter_key, "second")

    rows = await _notification_rows(db_session, owner.id)
    assert len(rows) == 2
    assert [r.event_count for r in rows] == [1, 1]
    assert rows[0].emailed_at is not None
    assert rows[1].emailed_at is None


# ── Delta commit horizon ──


@pytest.mark.asyncio
async def test_delta_holds_back_rows_inside_commit_horizon(
    client: AsyncClient, db_session: AsyncSession
):
    """A brand-new row is inside the commit horizon, so the delta must not
    return it or advance the cursor past it; once it ages out it surfaces."""
    owner, owner_key = await _make_user(db_session, "owner")
    commenter, commenter_key = await _make_user(db_session, "commenter")
    org = await _make_org(db_session)
    project = await _make_project(db_session, owner, org)
    await _make_member(db_session, org, owner, role="admin")
    await _make_member(db_session, org, commenter)
    ticket_id = await _create_ticket(client, project, owner_key)
    await _comment(client, project, ticket_id, commenter_key, "fresh")

    delta = await client.get(
        "/api/v1/me/ticket-updates", headers=_hdrs(owner_key), params={"persona": "atlas"}
    )
    assert delta.status_code == 200, delta.text
    assert delta.json()["items"] == []
    assert delta.json()["next_cursor"] == 0

    await _backdate_updated_at(db_session, owner.id)
    delta = await client.get(
        "/api/v1/me/ticket-updates", headers=_hdrs(owner_key), params={"persona": "atlas"}
    )
    assert delta.status_code == 200, delta.text
    items = delta.json()["items"]
    assert len(items) == 1
    assert delta.json()["next_cursor"] == items[0]["id"]


# ── Service-key cursor namespace ───────────────────────────────


@pytest.mark.asyncio
async def test_user_key_rejects_reserved_svc_persona(
    client: AsyncClient, db_session: AsyncSession
):
    owner, owner_key = await _make_user(db_session, "owner")
    resp = await client.get(
        "/api/v1/me/ticket-updates",
        headers=_hdrs(owner_key),
        params={"persona": "svc:someone-else"},
    )
    assert resp.status_code == 422, resp.text


@pytest.mark.asyncio
async def test_rotate_service_key_copies_watch_cursor(
    client: AsyncClient, db_session: AsyncSession
):
    owner, owner_key = await _make_user(db_session, "owner")
    org = await _make_org(db_session)
    await _make_member(db_session, org, owner, role="admin")

    create = await client.post(
        f"/api/v1/orgs/{org.id}/service-keys",
        headers=_hdrs(owner_key),
        json={"name": "resident-bot", "scopes": ["tickets:read"], "project_ids": []},
    )
    assert create.status_code == 201, create.text
    key_id = create.json()["id"]

    db_session.add(
        TicketWatchCursor(
            user_id=owner.id,
            persona_name=f"svc:{key_id}",
            last_seen_notification_id=42,
            updated_at=datetime.now(timezone.utc),
        )
    )
    await db_session.commit()

    rotate = await client.post(
        f"/api/v1/orgs/{org.id}/service-keys/{key_id}/rotate",
        headers=_hdrs(owner_key),
    )
    assert rotate.status_code == 200, rotate.text
    new_id = rotate.json()["id"]

    copied = (
        await db_session.execute(
            select(TicketWatchCursor).where(
                TicketWatchCursor.user_id == owner.id,
                TicketWatchCursor.persona_name == f"svc:{new_id}",
            )
        )
    ).scalar_one_or_none()
    assert copied is not None
    assert copied.last_seen_notification_id == 42


# ── Coalesce fallback when the row is claimed between load and merge ──


@pytest.mark.asyncio
async def test_coalesce_falls_back_to_fresh_row_when_row_claimed(
    client: AsyncClient, db_session: AsyncSession
):
    """If the row is claimed (read/emailed) after emission loads it but
    before ``_coalesce`` runs, the guarded delete matches nothing and a fresh
    row must still be inserted so the event is never dropped."""
    import sessionfs.server.services.ticket_watchers as tw

    owner, owner_key = await _make_user(db_session, "owner")
    commenter, commenter_key = await _make_user(db_session, "commenter")
    org = await _make_org(db_session)
    project = await _make_project(db_session, owner, org)
    await _make_member(db_session, org, owner, role="admin")
    await _make_member(db_session, org, commenter)
    ticket_id = await _create_ticket(client, project, owner_key)
    await _comment(client, project, ticket_id, commenter_key, "first")
    rows = await _notification_rows(db_session, owner.id)
    existing = rows[0]

    # Claim the row after it was loaded but before coalesce.
    await db_session.execute(
        update(TicketNotification)
        .where(TicketNotification.id == existing.id)
        .values(read_at=datetime.now(timezone.utc))
        .execution_options(synchronize_session=False)
    )
    await db_session.commit()

    await tw._coalesce(
        db_session,
        existing,
        "your_turn",
        "upgraded",
        datetime.now(timezone.utc),
        actor_type="user",
        actor_user_id=commenter.id,
        service_key_name=None,
    )
    await db_session.commit()

    rows = await _notification_rows(db_session, owner.id)
    assert len(rows) == 2  # the claimed row plus the fresh fallback row
    fresh = [r for r in rows if r.id != existing.id][0]
    assert fresh.kind == "your_turn"
    assert fresh.event_count == 1


# ── Sweep candidate-user selection prevents head-of-queue starvation ──


@pytest.mark.asyncio
async def test_sweep_does_not_starve_users_behind_a_failing_head(
    client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
):
    import sessionfs.server.email as email_mod
    import sessionfs.server.routes.admin as admin_mod

    admin, admin_headers = await _platform_admin_headers(db_session)
    owner, owner_key = await _make_user(db_session, "owner")
    user_a, _ = await _make_user(db_session, "user-a")
    user_b, _ = await _make_user(db_session, "user-b")
    org = await _make_org(db_session)
    project = await _make_project(db_session, owner, org)
    await _make_member(db_session, org, owner, role="admin")
    await _make_member(db_session, org, user_a)
    await _make_member(db_session, org, user_b)
    ticket_id = await _create_ticket(client, project, owner_key)

    now = datetime.now(timezone.utc)
    settled = now - timedelta(minutes=30)
    for i in range(5):
        db_session.add(
            TicketNotification(
                user_id=user_a.id,
                ticket_id=ticket_id,
                project_id=project.id,
                kind="your_turn",
                summary=f"a-{i}",
                created_at=settled,
                updated_at=settled,
            )
        )
    db_session.add(
        TicketNotification(
            user_id=user_b.id,
            ticket_id=ticket_id,
            project_id=project.id,
            kind="your_turn",
            summary="b",
            created_at=settled,
            updated_at=settled,
        )
    )
    await db_session.commit()

    # A small row limit means A's backlog alone would have filled the old
    # row-based selection and hidden B entirely.
    monkeypatch.setattr(admin_mod, "_TICKET_SWEEP_LIMIT", 5)

    class _FailingHeadProvider:
        def __init__(self, fail_email: str) -> None:
            self.fail_email = fail_email
            self.sends: list[dict] = []

        async def send_ticket_notification(
            self, to_email: str, notifications: list[dict], dashboard_url: str
        ) -> dict:
            self.sends.append({"to": to_email, "notifications": notifications})
            return {"status": "failed"} if to_email == self.fail_email else {"status": "sent"}

    recorder = _FailingHeadProvider(user_a.email)
    monkeypatch.setattr(email_mod, "create_email_provider", lambda config: recorder)

    sweep = await client.post(
        "/api/v1/admin/sweeps/ticket-notifications", headers=admin_headers, json={}
    )
    assert sweep.status_code == 200, sweep.text
    body = sweep.json()
    # A failed, but B was still selected and emailed in the same run. The
    # recorder sees A's failed attempt too, so assert B was delivered rather
    # than that the send log contains only B.
    assert body["users_emailed"] == 1
    assert user_b.email in {s["to"] for s in recorder.sends}
    assert [e["user_id"] for e in body["errors"]] == [user_a.id]


# ── Trusted-reviewer prose without the Codex header is a plain comment ──


@pytest.mark.asyncio
async def test_trusted_reviewer_prose_without_header_is_plain_comment(
    client: AsyncClient, db_session: AsyncSession
):
    owner, owner_key = await _make_user(db_session, "owner")
    reviewer, reviewer_key = await _make_user(db_session, "reviewer")
    org = await _make_org(db_session)
    project = await _make_project(db_session, owner, org)
    await _make_member(db_session, org, owner, role="admin")
    await _make_member(db_session, org, reviewer)
    db_session.add(
        TrustedReviewer(
            id=f"tr_{uuid.uuid4().hex[:12]}",
            org_id=org.id,
            user_id=reviewer.id,
            reviewer_persona="codex-reviewer",
            created_by_user_id=owner.id,
            is_active=True,
        )
    )
    await db_session.commit()

    tk_a = await _create_ticket(client, project, owner_key)
    await _comment(
        client, project, tk_a, reviewer_key,
        "Looks good to me",
        author_persona="codex-reviewer",
    )

    tk_b = await _create_ticket(client, project, owner_key)
    await _comment(
        client, project, tk_b, reviewer_key,
        "not APPROVED yet — CHANGES REQUESTED",
        author_persona="codex-reviewer",
    )

    rows = await _notification_rows(db_session, owner.id)
    assert [r.kind for r in rows] == ["comment", "comment"]
    assert all(r.summary == "New comment" for r in rows)


# ── Overlapping sweep claim is idempotent ──────────────────────


@pytest.mark.asyncio
async def test_overlapping_sweep_claim_emails_nothing_twice(
    client: AsyncClient, db_session: AsyncSession
):
    import sessionfs.server.routes.admin as admin_mod

    owner, owner_key = await _make_user(db_session, "owner")
    commenter, commenter_key = await _make_user(db_session, "commenter")
    org = await _make_org(db_session)
    project = await _make_project(db_session, owner, org)
    await _make_member(db_session, org, owner, role="admin")
    await _make_member(db_session, org, commenter)
    ticket_id = await _create_ticket(client, project, owner_key)
    await _comment(client, project, ticket_id, commenter_key, "hello")
    rows = await _notification_rows(db_session, owner.id)
    ids = [r.id for r in rows]
    now = datetime.now(timezone.utc)

    # The first overlapping run claims the row; the second, claiming the same
    # ids concurrently, wins nothing, so its digest is empty and the row is
    # never emailed twice.
    assert await admin_mod._claim_notifications(db_session, owner.id, ids, now) == ids
    await db_session.commit()
    assert await admin_mod._claim_notifications(db_session, owner.id, ids, now) == []


# ── Notification emission failures never roll back the ticket write ──


@pytest.mark.asyncio
async def test_comment_survives_notification_emission_failure(
    client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
):
    import sessionfs.server.services.ticket_watchers as tw

    owner, owner_key = await _make_user(db_session, "owner")
    commenter, commenter_key = await _make_user(db_session, "commenter")
    org = await _make_org(db_session)
    project = await _make_project(db_session, owner, org)
    await _make_member(db_session, org, owner, role="admin")
    await _make_member(db_session, org, commenter)
    ticket_id = await _create_ticket(client, project, owner_key)

    # A first comment so the second one coalesces (and reaches the patched
    # helper inside the emission savepoint).
    await _comment(client, project, ticket_id, commenter_key, "first")

    async def _boom(db, existing, kind, summary, now):
        raise SQLAlchemyError("injected failure")

    monkeypatch.setattr(tw, "_coalesce", _boom)

    resp = await client.post(
        f"/api/v1/projects/{project.id}/tickets/{ticket_id}/comments",
        headers=_hdrs(commenter_key),
        json={"content": "second", "author_persona": None},
    )
    assert resp.status_code == 201, resp.text

    comments = (
        await db_session.execute(
            select(TicketComment).where(TicketComment.ticket_id == ticket_id)
        )
    ).scalars().all()
    assert "second" in [c.content for c in comments]



async def test_notification_ids_never_repeat_on_sqlite(db_session: AsyncSession):
    """Coalescing deletes and re-inserts the newest row; on SQLite the
    replacement must still get a higher id or the agent delta misses it."""
    from sessionfs.server.db.models import TicketNotification

    assert TicketNotification.__table__.dialect_options["sqlite"]["autoincrement"] is True
    if db_session.bind.dialect.name != "sqlite":
        return
    conn = await db_session.connection()
    ddl = (
        await conn.exec_driver_sql(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='ticket_notifications'"
        )
    ).scalar_one()
    assert "AUTOINCREMENT" in ddl.upper()


async def test_coalesce_attributes_the_upgrading_actor():
    """When an event upgrades a coalesced row (or falls back to a fresh row),
    the row is attributed to the actor of that event, not the earlier one."""
    from datetime import datetime, timezone
    from unittest.mock import AsyncMock, MagicMock

    from sessionfs.server.db.models import TicketNotification
    from sessionfs.server.services import ticket_watchers as tw

    existing = TicketNotification(
        id=7, user_id="u1", ticket_id="t1", project_id="p1", kind="comment",
        summary="New comment", actor_type="user", actor_user_id="human-1",
        service_key_name=None, event_count=1, created_at=datetime.now(timezone.utc),
    )
    for deleted in (1, 0):  # 1 = merged replacement, 0 = sweep-claimed fallback
        db = MagicMock()
        db.execute = AsyncMock(return_value=MagicMock(rowcount=deleted))
        added: list = []
        db.add = added.append
        await tw._coalesce(
            db, existing, "your_turn", "Your turn", datetime.now(timezone.utc),
            actor_type="service", actor_user_id=None, service_key_name="resident-a",
        )
        row = added[-1]
        assert row.kind == "your_turn"
        assert (row.actor_type, row.actor_user_id, row.service_key_name) == (
            "service", None, "resident-a",
        )
