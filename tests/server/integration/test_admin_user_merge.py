"""Audited admin user merge (duplicate signups).

POST /api/v1/admin/users/{source_id}/merge-into/{target_id}: dry run,
apply, every refusal, org-member dedupe, entitlement conflict, key
revocation and the audit row. PostgreSQL coverage lives in
test_admin_user_merge_pg.py.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server.auth.keys import generate_api_key, hash_api_key
from sessionfs.server.db.models import (
    AdminAction,
    AgentPersona,
    ApiKey,
    BookmarkFolder,
    Entitlement,
    GitLabSettings,
    KnowledgeEntry,
    OrgAuditEvent,
    OrgMember,
    Organization,
    Project,
    Session,
    Team,
    TeamMember,
    Ticket,
    TicketNotification,
    TicketWatcher,
    TicketWatchCursor,
    TrustedReviewer,
    User,
    UserNotificationSettings,
)


async def _user(db: AsyncSession, name: str, tier: str = "free", **kw) -> tuple[User, str]:
    user = User(
        id=str(uuid.uuid4()),
        email=f"{name}-{uuid.uuid4().hex[:6]}@example.com",
        display_name=name,
        tier=tier,
        email_verified=True,
        created_at=datetime.now(timezone.utc),
        **kw,
    )
    db.add(user)
    raw = generate_api_key()
    db.add(ApiKey(id=str(uuid.uuid4()), user_id=user.id, key_hash=hash_api_key(raw),
                  name=f"{name}-key", created_at=datetime.now(timezone.utc)))
    await db.commit()
    return user, raw


def _h(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


async def _org(db: AsyncSession) -> Organization:
    org = Organization(id=f"org_{uuid.uuid4().hex[:12]}", name="Org",
                       slug=f"o-{uuid.uuid4().hex[:8]}", tier="enterprise")
    db.add(org)
    await db.commit()
    return org


async def _member(db: AsyncSession, org: Organization, user: User, role: str) -> None:
    db.add(OrgMember(org_id=org.id, user_id=user.id, role=role))
    await db.commit()


async def _role(db: AsyncSession, org_id: str, user_id: str) -> str | None:
    return (
        await db.execute(
            select(OrgMember.role).where(OrgMember.org_id == org_id, OrgMember.user_id == user_id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()


async def _count(db: AsyncSession, model, *where) -> int:
    return (await db.execute(select(func.count()).select_from(model).where(*where))).scalar() or 0


async def _populate(db: AsyncSession, src: User) -> dict:
    """Give the source rows in several tables."""
    project = Project(id=f"proj_{uuid.uuid4().hex[:12]}", name="p",
                      git_remote_normalized=f"github.com/x/{uuid.uuid4().hex[:8]}", owner_id=src.id)
    db.add(project)
    await db.flush()
    sessions = []
    for _ in range(2):
        s = Session(id=f"ses_{uuid.uuid4().hex[:16]}", user_id=src.id, source_tool="claude-code",
                    blob_key="b", etag="e", created_at=datetime.now(timezone.utc),
                    updated_at=datetime.now(timezone.utc))
        db.add(s)
        sessions.append(s)
    await db.flush()
    ticket = Ticket(id=f"tk_{uuid.uuid4().hex[:16]}", project_id=project.id, title="t",
                    created_by_user_id=src.id, assigned_user_id=src.id)
    db.add(ticket)
    db.add(KnowledgeEntry(project_id=project.id, session_id=sessions[0].id, user_id=src.id,
                          entry_type="decision", content="x"))
    folder = BookmarkFolder(id=str(uuid.uuid4()), user_id=src.id, name="f")
    db.add(folder)
    await db.commit()
    return {"project": project, "sessions": sessions, "ticket": ticket}


async def _merge(client: AsyncClient, key: str, src: str, tgt: str, **body):
    body.setdefault("reason", "duplicate signup with a typo")
    return await client.post(f"/api/v1/admin/users/{src}/merge-into/{tgt}", json=body,
                             headers=_h(key))


@pytest.mark.asyncio
async def test_dry_run_writes_nothing(client: AsyncClient, db_session: AsyncSession) -> None:
    _, admin_key = await _user(db_session, "ops", "admin")
    src, _ = await _user(db_session, "typo")
    tgt, _ = await _user(db_session, "real")
    data = await _populate(db_session, src)

    resp = await _merge(client, admin_key, src.id, tgt.id)  # dry_run defaults to true
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "dry_run" and body["dry_run"] is True
    rows = body["plan"]["reassigned"]
    assert rows["sessions.user_id"]["count"] == 2
    assert sorted(rows["sessions.user_id"]["ids"]) == sorted(s.id for s in data["sessions"])
    assert rows["projects.owner_id"]["ids"] == [data["project"].id]
    assert "tickets.created_by_user_id" in rows
    assert rows["tickets.assigned_user_id"]["ids"] == [data["ticket"].id]
    assert body["plan"]["revoked"]["api_keys"]["count"] == 1

    assert await _count(db_session, Session, Session.user_id == src.id) == 2
    assert await _count(db_session, AdminAction, AdminAction.action == "user_merge") == 0
    assert (await db_session.execute(
        select(User.is_active).where(User.id == src.id).execution_options(populate_existing=True)
    )).scalar_one() is True


@pytest.mark.asyncio
async def test_apply_reassigns_revokes_and_audits(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    admin, admin_key = await _user(db_session, "ops", "admin")
    src, src_key = await _user(db_session, "typo", storage_used_bytes=100)
    tgt, _ = await _user(db_session, "real", storage_used_bytes=5)
    data = await _populate(db_session, src)
    db_session.add(TrustedReviewer(id=str(uuid.uuid4()), org_id=None, project_id=data["project"].id,
                                   user_id=src.id, reviewer_persona="codex-reviewer",
                                   is_active=True, created_by_user_id=admin.id))
    await db_session.commit()

    resp = await _merge(client, admin_key, src.id, tgt.id, dry_run=False, reason="  fix typo  ")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "merged"

    for model, col in ((Session, Session.user_id), (Project, Project.owner_id),
                       (Ticket, Ticket.created_by_user_id),
                       (Ticket, Ticket.assigned_user_id),
                       (KnowledgeEntry, KnowledgeEntry.user_id),
                       (BookmarkFolder, BookmarkFolder.user_id)):
        assert await _count(db_session, model, col == src.id) == 0
        assert await _count(db_session, model, col == tgt.id) >= 1
    assert await _count(db_session, Session, Session.user_id == tgt.id) == 2

    users = {u.id: u for u in (await db_session.execute(
        select(User).where(User.id.in_([src.id, tgt.id])).execution_options(populate_existing=True)
    )).scalars()}
    assert users[src.id].is_active is False
    assert users[tgt.id].is_active is True
    assert users[tgt.id].storage_used_bytes == 105 and users[src.id].storage_used_bytes == 0

    key = (await db_session.execute(
        select(ApiKey).where(ApiKey.user_id == src.id).execution_options(populate_existing=True)
    )).scalar_one()
    assert key.is_active is False and key.revoked_at is not None and key.revoke_reason == "user_merged"
    reviewer = (await db_session.execute(
        select(TrustedReviewer).where(TrustedReviewer.user_id == src.id)
        .execution_options(populate_existing=True)
    )).scalar_one()
    assert reviewer.is_active is False

    # the revoked key no longer authenticates
    me = await client.get("/api/v1/auth/me", headers=_h(src_key))
    assert me.status_code in (401, 403)

    [entry] = (await db_session.execute(
        select(AdminAction).where(AdminAction.action == "user_merge")
    )).scalars().all()
    assert entry.id == body["admin_action_id"]
    assert entry.admin_id == admin.id and entry.source == "api"
    assert entry.target_type == "user" and entry.target_id == src.id
    assert entry.reason == "fix typo"
    before, after = json.loads(entry.before_state), json.loads(entry.after_state)
    assert before["users"]["source"]["is_active"] is True
    assert after["users"]["source"]["is_active"] is False
    assert after["merged_into"] == tgt.id
    assert after["rows"]["reassigned"]["sessions.user_id"]["count"] == 2
    assert json.loads(entry.details)["merged_into"] == tgt.id


@pytest.mark.asyncio
async def test_refusals(client: AsyncClient, db_session: AsyncSession) -> None:
    _, admin_key = await _user(db_session, "ops", "admin")
    other_admin, _ = await _user(db_session, "ops2", "admin")
    src, _ = await _user(db_session, "typo")
    tgt, _ = await _user(db_session, "real")
    gone, _ = await _user(db_session, "gone")
    gone.is_active = False
    await db_session.commit()

    same = await _merge(client, admin_key, src.id, src.id, dry_run=False)
    assert same.status_code == 400
    assert (await _merge(client, admin_key, "nope", tgt.id)).status_code == 404
    assert (await _merge(client, admin_key, src.id, "nope")).status_code == 404

    inactive = await _merge(client, admin_key, src.id, gone.id)
    assert inactive.status_code == 409
    assert inactive.json()["error"]["code"] == "target_inactive"

    adm = await _merge(client, admin_key, other_admin.id, tgt.id, dry_run=False)
    assert adm.status_code == 409
    assert adm.json()["error"]["code"] == "source_is_platform_admin"

    # non-admins cannot call it
    _, plain_key = await _user(db_session, "plain")
    assert (await _merge(client, plain_key, src.id, tgt.id)).status_code == 403

    # strict body validation
    for bad in (1, 0, "false", None):
        r = await client.post(f"/api/v1/admin/users/{src.id}/merge-into/{tgt.id}",
                              json={"reason": "r", "dry_run": bad}, headers=_h(admin_key))
        assert r.status_code == 422, bad
    for bad_reason in ("", "   ", 5):
        r = await client.post(f"/api/v1/admin/users/{src.id}/merge-into/{tgt.id}",
                              json={"reason": bad_reason, "dry_run": True}, headers=_h(admin_key))
        assert r.status_code == 422
    r = await client.post(f"/api/v1/admin/users/{src.id}/merge-into/{tgt.id}", json={"dry_run": True},
                          headers=_h(admin_key))
    assert r.status_code == 422

    # nothing was merged by any refusal
    assert await _count(db_session, AdminAction, AdminAction.action == "user_merge") == 0


@pytest.mark.asyncio
async def test_billing_marketplace_and_github_refusals(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    _, admin_key = await _user(db_session, "ops", "admin")
    tgt, _ = await _user(db_session, "real")

    paid, _ = await _user(db_session, "paid", stripe_subscription_id="sub_1")
    r = await _merge(client, admin_key, paid.id, tgt.id, dry_run=False)
    assert r.status_code == 409 and r.json()["error"]["code"] == "source_has_billing"

    from sessionfs.server.db.models import GitHubInstallation, SkillPublisher

    gh_src, _ = await _user(db_session, "gh1")
    gh_tgt, _ = await _user(db_session, "gh2")
    for u, n in ((gh_src, 1), (gh_tgt, 2)):
        db_session.add(GitHubInstallation(id=n * 1000 + uuid.uuid4().int % 1000, user_id=u.id,
                                          account_login=f"a{n}", account_type="User"))
    await db_session.commit()
    r = await _merge(client, admin_key, gh_src.id, gh_tgt.id)
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "github_installation_conflict"

    pub, _ = await _user(db_session, "pub")
    db_session.add(SkillPublisher(id=str(uuid.uuid4()), owner_type="user", user_id=pub.id,
                                  namespace="pubns", namespace_normalized="pubns",
                                  display_name="p"))
    await db_session.commit()
    r = await _merge(client, admin_key, pub.id, tgt.id)
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "marketplace_assets_owned"
    assert "skill_publishers" in r.json()["error"]["details"]["tables"]


@pytest.mark.asyncio
async def test_org_member_dedupe_role_rule_and_single_owner(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    _, admin_key = await _user(db_session, "ops", "admin")
    src, _ = await _user(db_session, "typo")
    tgt, _ = await _user(db_session, "real")
    only_src = await _org(db_session)            # moved
    src_higher = await _org(db_session)          # source owner, target member -> target owner
    tgt_higher = await _org(db_session)          # target admin, source member -> stays admin
    tie = await _org(db_session)                 # both member
    await _member(db_session, only_src, src, "admin")
    await _member(db_session, src_higher, src, "owner")
    await _member(db_session, src_higher, tgt, "member")
    await _member(db_session, tgt_higher, tgt, "admin")
    await _member(db_session, tgt_higher, src, "member")
    await _member(db_session, tie, src, "member")
    await _member(db_session, tie, tgt, "member")

    dry = (await _merge(client, admin_key, src.id, tgt.id)).json()["plan"]["org_members"]
    by_org = {m["org_id"]: m for m in dry}
    assert by_org[only_src.id]["action"] == "moved"
    assert by_org[src_higher.id]["resulting_role"] == "owner"
    assert by_org[tgt_higher.id]["resulting_role"] == "admin"
    assert await _role(db_session, src_higher.id, src.id) == "owner"  # untouched by dry run

    r = await _merge(client, admin_key, src.id, tgt.id, dry_run=False)
    assert r.status_code == 200, r.text
    assert await _role(db_session, only_src.id, tgt.id) == "admin"
    assert await _role(db_session, src_higher.id, tgt.id) == "owner"
    assert await _role(db_session, tgt_higher.id, tgt.id) == "admin"
    assert await _role(db_session, tie.id, tgt.id) == "member"
    assert await _count(db_session, OrgMember, OrgMember.user_id == src.id) == 0
    for org in (src_higher, tgt_higher, tie, only_src):
        owners = await _count(db_session, OrgMember, OrgMember.org_id == org.id,
                              OrgMember.role == "owner")
        assert owners <= 1
    events = (await db_session.execute(
        select(OrgAuditEvent).where(OrgAuditEvent.event_type == "user_merge_membership")
    )).scalars().all()
    assert {e.org_id for e in events} == {only_src.id, src_higher.id, tgt_higher.id, tie.id}


@pytest.mark.asyncio
async def test_org_project_owner_stays_member(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    _, admin_key = await _user(db_session, "ops", "admin")
    src, _ = await _user(db_session, "typo")
    tgt, _ = await _user(db_session, "real")
    org = await _org(db_session)
    await _member(db_session, org, src, "member")
    proj = Project(id=f"proj_{uuid.uuid4().hex[:12]}", name="p",
                   git_remote_normalized=f"github.com/x/{uuid.uuid4().hex[:8]}",
                   owner_id=src.id, org_id=org.id)
    db_session.add(proj)
    await db_session.commit()
    r = await _merge(client, admin_key, src.id, tgt.id, dry_run=False)
    assert r.status_code == 200, r.text
    assert await _role(db_session, org.id, tgt.id) == "member"
    owner = (await db_session.execute(
        select(Project.owner_id).where(Project.id == proj.id).execution_options(populate_existing=True)
    )).scalar_one()
    assert owner == tgt.id

    # a stale owner who is not an org member, with a target outside the org, is refused
    src2, _ = await _user(db_session, "typo2")
    tgt2, _ = await _user(db_session, "real2")
    db_session.add(Project(id=f"proj_{uuid.uuid4().hex[:12]}", name="p2",
                           git_remote_normalized=f"github.com/x/{uuid.uuid4().hex[:8]}",
                           owner_id=src2.id, org_id=org.id))
    await db_session.commit()
    r = await _merge(client, admin_key, src2.id, tgt2.id)
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "project_owner_not_member"


@pytest.mark.asyncio
async def test_entitlement_conflict_and_move(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    _, admin_key = await _user(db_session, "ops", "admin")
    a, _ = await _user(db_session, "a")
    b, _ = await _user(db_session, "b")
    for u in (a, b):
        db_session.add(Entitlement(owner_type="user", owner_id=u.id,
                                   source="manual", tier="pro", status="active"))
    await db_session.commit()
    r = await _merge(client, admin_key, a.id, b.id, dry_run=False)
    assert r.status_code == 409 and r.json()["error"]["code"] == "entitlement_conflict"
    assert await _count(db_session, Entitlement, Entitlement.owner_id == a.id) == 1

    c, _ = await _user(db_session, "c")
    d, _ = await _user(db_session, "d")
    db_session.add(Entitlement(owner_type="user", owner_id=c.id,
                               source="manual", tier="pro", status="active"))
    await db_session.commit()
    r = await _merge(client, admin_key, c.id, d.id, dry_run=False)
    assert r.status_code == 200, r.text
    assert await _count(db_session, Entitlement, Entitlement.owner_id == d.id,
                        Entitlement.status == "active") == 1


@pytest.mark.asyncio
async def test_unique_key_dedupe_and_persona(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    _, admin_key = await _user(db_session, "ops", "admin")
    src, _ = await _user(db_session, "typo")
    tgt, _ = await _user(db_session, "real")
    org = await _org(db_session)
    await _member(db_session, org, src, "member")
    team = Team(id=str(uuid.uuid4()), org_id=org.id, name="t", slug="t")
    db_session.add(team)
    await db_session.flush()
    db_session.add(TeamMember(team_id=team.id, user_id=src.id))
    db_session.add(TeamMember(team_id=team.id, user_id=tgt.id))
    for u in (src, tgt):
        db_session.add(GitLabSettings(user_id=u.id, encrypted_access_token=f"tok-{u.id}",
                                      webhook_secret="s"))
    persona = AgentPersona(id=str(uuid.uuid4()), name="rep", role="r", org_id=org.id,
                           created_by=src.id, represented_user_id=src.id)
    db_session.add(persona)
    await db_session.commit()

    r = await _merge(client, admin_key, src.id, tgt.id, dry_run=False)
    assert r.status_code == 200, r.text
    dedup = r.json()["plan"]["deduped"]
    assert dedup["team_members"]["dropped_target_wins"]["count"] == 1
    assert dedup["gitlab_settings"]["dropped_target_wins"]["count"] == 1
    assert await _count(db_session, TeamMember, TeamMember.team_id == team.id) == 1
    gl = (await db_session.execute(
        select(GitLabSettings.encrypted_access_token).execution_options(populate_existing=True)
    )).scalars().all()
    assert gl == [f"tok-{tgt.id}"]
    p = (await db_session.execute(
        select(AgentPersona).where(AgentPersona.id == persona.id).execution_options(populate_existing=True)
    )).scalar_one()
    assert p.represented_user_id == tgt.id and p.created_by == tgt.id


@pytest.mark.asyncio
async def test_merge_reassigns_and_dedupes_ticket_watcher_tables(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """The four watcher-era user-FK tables are classified: ticket_notifications
    reassigns (no per-user unique key), while ticket_watchers /
    ticket_watch_cursors / user_notification_settings dedupe with the target's
    row winning."""
    _, admin_key = await _user(db_session, "ops", "admin")
    src, _ = await _user(db_session, "typo")
    tgt, _ = await _user(db_session, "real")

    project = Project(id=f"proj_{uuid.uuid4().hex[:12]}", name="p",
                      git_remote_normalized=f"github.com/x/{uuid.uuid4().hex[:8]}",
                      owner_id=tgt.id)
    db_session.add(project)
    await db_session.flush()
    t1 = Ticket(id=f"tk_{uuid.uuid4().hex[:16]}", project_id=project.id, title="t1",
                created_by_user_id=tgt.id)
    t2 = Ticket(id=f"tk_{uuid.uuid4().hex[:16]}", project_id=project.id, title="t2",
                created_by_user_id=tgt.id)
    db_session.add_all([t1, t2])
    await db_session.flush()

    # Source rows: a notification to reassign, a conflicting watcher + cursor
    # + settings (target wins), and one non-conflicting watcher to move.
    db_session.add(TicketNotification(user_id=src.id, ticket_id=t1.id, project_id=project.id,
                                      kind="comment", summary="x"))
    db_session.add(TicketWatcher(ticket_id=t1.id, user_id=src.id, source="explicit"))
    db_session.add(TicketWatcher(ticket_id=t2.id, user_id=src.id, source="explicit"))
    db_session.add(TicketWatchCursor(user_id=src.id, persona_name="atlas",
                                     last_seen_notification_id=5))
    db_session.add(UserNotificationSettings(user_id=src.id, ticket_email="all"))
    # Target rows that conflict on the dedupe keys.
    db_session.add(TicketWatcher(ticket_id=t1.id, user_id=tgt.id, source="explicit", muted=True))
    db_session.add(TicketWatchCursor(user_id=tgt.id, persona_name="atlas",
                                     last_seen_notification_id=9))
    db_session.add(UserNotificationSettings(user_id=tgt.id, ticket_email="off"))
    await db_session.commit()

    r = await _merge(client, admin_key, src.id, tgt.id, dry_run=False)
    assert r.status_code == 200, r.text
    plan = r.json()["plan"]

    assert plan["reassigned"]["ticket_notifications.user_id"]["count"] == 1
    dedup = plan["deduped"]
    assert dedup["ticket_watchers"]["dropped_target_wins"]["count"] == 1
    assert dedup["ticket_watchers"]["moved"]["count"] == 1
    assert dedup["ticket_watch_cursors"]["dropped_target_wins"]["count"] == 1
    assert dedup["user_notification_settings"]["dropped_target_wins"]["count"] == 1

    # Reassignment: the notification now belongs to the target.
    assert await _count(db_session, TicketNotification,
                        TicketNotification.user_id == src.id) == 0
    assert await _count(db_session, TicketNotification,
                        TicketNotification.user_id == tgt.id) == 1

    # Dedupe: the conflicting watcher/cursor/settings keep the target's row.
    assert await _count(db_session, TicketWatcher, TicketWatcher.user_id == src.id) == 0
    assert await _count(db_session, TicketWatcher, TicketWatcher.ticket_id == t1.id) == 1
    moved = (await db_session.execute(
        select(TicketWatcher).where(TicketWatcher.ticket_id == t2.id)
    )).scalar_one()
    assert moved.user_id == tgt.id
    cursor = (await db_session.execute(
        select(TicketWatchCursor).where(TicketWatchCursor.user_id == tgt.id)
    )).scalars().all()
    assert [(c.persona_name, c.last_seen_notification_id) for c in cursor] == [("atlas", 9)]
    settings = (await db_session.execute(
        select(UserNotificationSettings).where(UserNotificationSettings.user_id == tgt.id)
    )).scalar_one()
    assert settings.ticket_email == "off"


@pytest.mark.asyncio
async def test_merge_moves_project_group_grants_keeping_higher_role(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """GitHub #101 — project_group_members dedupes on (group, user) with the
    higher group role winning; grants the target lacks move over; the
    attribution columns reassign; one org audit row per grant."""
    from sessionfs.server.db.models import ProjectGroup, ProjectGroupAccess, ProjectGroupMember

    _, admin_key = await _user(db_session, "ops", "admin")
    src, _ = await _user(db_session, "typo")
    tgt, _ = await _user(db_session, "real")
    org = await _org(db_session)
    await _member(db_session, org, src, "member")
    await _member(db_session, org, tgt, "member")
    project = Project(id=f"proj_{uuid.uuid4().hex[:12]}", name="p",
                      git_remote_normalized=f"github.com/x/{uuid.uuid4().hex[:8]}",
                      owner_id=tgt.id, org_id=org.id)
    db_session.add(project)
    groups = []
    for i in range(3):
        g = ProjectGroup(id=f"pg_{uuid.uuid4().hex[:12]}", org_id=org.id, slug=f"g{i}",
                         name=f"G{i}", created_by_user_id=src.id)
        db_session.add(g)
        groups.append(g)
    await db_session.flush()
    up, keep, move = groups
    db_session.add(ProjectGroupAccess(group_id=move.id, project_id=project.id,
                                      added_by_user_id=src.id))
    db_session.add_all([
        ProjectGroupMember(group_id=up.id, user_id=src.id, role="admin", granted_by_user_id=src.id),
        ProjectGroupMember(group_id=up.id, user_id=tgt.id, role="reader"),
        ProjectGroupMember(group_id=keep.id, user_id=src.id, role="reader"),
        ProjectGroupMember(group_id=keep.id, user_id=tgt.id, role="editor"),
        ProjectGroupMember(group_id=move.id, user_id=src.id, role="editor"),
    ])
    await db_session.commit()

    r = await _merge(client, admin_key, src.id, tgt.id, dry_run=False)
    assert r.status_code == 200, r.text
    dedup = r.json()["plan"]["deduped"]["project_group_members"]
    assert dedup["moved"]["count"] == 1
    assert dedup["merged_higher_role_wins"]["count"] == 2

    rows = (await db_session.execute(
        select(ProjectGroupMember.group_id, ProjectGroupMember.user_id, ProjectGroupMember.role)
        .execution_options(populate_existing=True)
        .where(ProjectGroupMember.group_id.in_([g.id for g in groups]))
    )).all()
    assert {(g, u, role) for g, u, role in rows} == {
        (up.id, tgt.id, "admin"),
        (keep.id, tgt.id, "editor"),
        (move.id, tgt.id, "editor"),
    }
    assert await _count(db_session, ProjectGroup, ProjectGroup.created_by_user_id == src.id) == 0
    assert await _count(db_session, ProjectGroupAccess, ProjectGroupAccess.added_by_user_id == tgt.id) == 1
    assert await _count(
        db_session, OrgAuditEvent,
        OrgAuditEvent.org_id == org.id, OrgAuditEvent.event_type == "project_group_member_merged",
    ) == 3


def test_project_group_user_columns_are_classified() -> None:
    """Every users.id FK on the project-group tables is reassigned or deduped."""
    from sessionfs.server.db.models import Base
    from sessionfs.server.services import user_merge

    classified = set(user_merge.REASSIGN_COLUMNS) | {("project_group_members", "user_id")}
    for name in ("project_groups", "project_group_members", "project_group_access"):
        for col in Base.metadata.tables[name].columns:
            if any(fk.column.table.name == "users" for fk in col.foreign_keys):
                assert (name, col.name) in classified, (name, col.name)


@pytest.mark.asyncio
async def test_merge_drops_grants_outside_source_orgs(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """A source grant in an org the source no longer belongs to is dropped
    (audited), never handed to the target."""
    from sessionfs.server.db.models import ProjectGroup, ProjectGroupMember

    _, admin_key = await _user(db_session, "ops", "admin")
    src, _ = await _user(db_session, "typo")
    tgt, _ = await _user(db_session, "real")
    member_org = await _org(db_session)
    foreign_org = await _org(db_session)
    await _member(db_session, member_org, src, "member")
    kept = ProjectGroup(id=f"pg_{uuid.uuid4().hex[:12]}", org_id=member_org.id, slug="k", name="K")
    orphan = ProjectGroup(id=f"pg_{uuid.uuid4().hex[:12]}", org_id=foreign_org.id, slug="o", name="O")
    db_session.add_all([kept, orphan])
    await db_session.flush()
    db_session.add_all([
        ProjectGroupMember(group_id=kept.id, user_id=src.id, role="reader"),
        ProjectGroupMember(group_id=orphan.id, user_id=src.id, role="admin"),
    ])
    await db_session.commit()

    r = await _merge(client, admin_key, src.id, tgt.id, dry_run=False)
    assert r.status_code == 200, r.text
    dedup = r.json()["plan"]["deduped"]["project_group_members"]
    assert dedup["moved"]["count"] == 1
    assert dedup["dropped_not_org_member"]["count"] == 1

    rows = (await db_session.execute(
        select(ProjectGroupMember.group_id, ProjectGroupMember.user_id)
        .execution_options(populate_existing=True)
        .where(ProjectGroupMember.group_id.in_([kept.id, orphan.id]))
    )).all()
    assert set(rows) == {(kept.id, tgt.id)}
    assert await _count(
        db_session, OrgAuditEvent,
        OrgAuditEvent.org_id == foreign_org.id,
        OrgAuditEvent.event_type == "project_group_member_revoked",
    ) == 1
