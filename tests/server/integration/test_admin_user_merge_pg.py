"""Admin user merge on real PostgreSQL.

Runs when SFS_TEST_PG_URL points at a scratch PostgreSQL database. The
schema comes from the real migration chain, not ``create_all``: some
unique indexes (session aliases per user, sync_watchlist user+session)
exist only in migrations. Covers the merge against the real constraints (one owner per org, one active
entitlement per owner, composite uniques), the advisory scope locks, and
concurrent merges (same direction and opposite directions) finishing
without deadlock or half-applied state. Behaviour coverage lives in
test_admin_user_merge.py (SQLite).
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from datetime import datetime, timezone

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from sessionfs.server.auth.keys import generate_api_key, hash_api_key
from sessionfs.server.db.models import (
    AdminAction,
    ApiKey,
    Entitlement,
    GitLabSettings,
    OrgMember,
    Organization,
    Project,
    Session,
    SyncWatchlist,
    Team,
    TeamMember,
    User,
)
from tests.pg_helpers import upgrade_pg_to_head

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        not os.environ.get("SFS_TEST_PG_URL"),
        reason="set SFS_TEST_PG_URL to a scratch PostgreSQL database",
    ),
]


async def _user(factory, tier: str = "free", **kw) -> tuple[User, dict]:
    raw = generate_api_key()
    user = User(id=str(uuid.uuid4()), email=f"{uuid.uuid4().hex}@example.com", tier=tier,
                created_at=datetime.now(timezone.utc), email_verified=True, **kw)
    async with factory() as db:
        db.add(user)
        await db.flush()
        db.add(ApiKey(id=str(uuid.uuid4()), user_id=user.id, key_hash=hash_api_key(raw),
                      name="k", created_at=datetime.now(timezone.utc)))
        await db.commit()
    return user, {"Authorization": f"Bearer {raw}"}


async def _setup():
    from sessionfs.server.app import create_app
    from sessionfs.server.config import ServerConfig
    from sessionfs.server.db.engine import get_db

    engine = create_async_engine(os.environ["SFS_TEST_PG_URL"], pool_size=10)
    await asyncio.to_thread(upgrade_pg_to_head)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    app = create_app(ServerConfig(database_url="sqlite+aiosqlite://"))

    async def override_get_db():
        async with factory() as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db
    return engine, factory, app


def _session(user_id: str) -> Session:
    return Session(id=f"ses_{uuid.uuid4().hex[:16]}", user_id=user_id, source_tool="claude-code",
                   blob_key="b", etag="e", created_at=datetime.now(timezone.utc),
                   updated_at=datetime.now(timezone.utc))


async def test_user_merge_on_postgres() -> None:
    engine, factory, app = await _setup()
    admin, admin_h = await _user(factory, "admin")
    src, src_h = await _user(factory, storage_used_bytes=10)
    tgt, _ = await _user(factory, storage_used_bytes=1)
    owner_org, member_org, org_proj = (f"org_{uuid.uuid4().hex[:12]}" for _ in range(3))
    proj_id = f"proj_{uuid.uuid4().hex[:12]}"
    async with factory() as db:
        for oid in (owner_org, member_org, org_proj):
            db.add(Organization(id=oid, name="Org", slug=f"s-{uuid.uuid4().hex[:8]}", tier="enterprise"))
        await db.flush()
        db.add(OrgMember(org_id=owner_org, user_id=src.id, role="owner"))
        db.add(OrgMember(org_id=owner_org, user_id=tgt.id, role="member"))
        db.add(OrgMember(org_id=member_org, user_id=src.id, role="member"))
        db.add(OrgMember(org_id=member_org, user_id=tgt.id, role="admin"))
        db.add(OrgMember(org_id=org_proj, user_id=src.id, role="member"))
        db.add(Project(id=proj_id, name="p", git_remote_normalized=f"github.com/x/{proj_id}",
                       owner_id=src.id, org_id=org_proj))
        for _ in range(3):
            db.add(_session(src.id))
        # Both users have a session aliased "work" (aliases are unique per
        # user) and both watch the same session (one row per user+session).
        clash = _session(src.id)
        clash.alias = "work"
        mine = _session(tgt.id)
        mine.alias = "work"
        db.add_all([clash, mine])
        db.add(SyncWatchlist(user_id=src.id, session_id=mine.id))
        db.add(SyncWatchlist(user_id=tgt.id, session_id=mine.id))
        team = Team(id=str(uuid.uuid4()), org_id=owner_org, name="t", slug="t")
        db.add(team)
        await db.flush()
        db.add(TeamMember(team_id=team.id, user_id=src.id))
        db.add(TeamMember(team_id=team.id, user_id=tgt.id))
        db.add(GitLabSettings(user_id=src.id, encrypted_access_token="a", webhook_secret="s"))
        db.add(Entitlement(owner_type="user", owner_id=src.id, source="manual", tier="pro",
                           status="active"))
        await db.commit()

    url = f"/api/v1/admin/users/{src.id}/merge-into/{tgt.id}"
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        dry = await client.post(url, json={"reason": "dup"}, headers=admin_h)
        assert dry.status_code == 200, dry.text
        assert dry.json()["plan"]["reassigned"]["sessions.user_id"]["count"] == 4
        assert dry.json()["plan"]["deduped"]["sessions.alias"]["cleared"]["ids"] == [clash.id]
        done = await client.post(url, json={"reason": "dup", "dry_run": False}, headers=admin_h)
        assert done.status_code == 200, done.text
        assert done.json()["status"] == "merged"

        # the revoked key is dead
        me = await client.get("/api/v1/auth/me", headers=src_h)
        assert me.status_code in (401, 403)

    async with factory() as db:
        roles = {
            (m.org_id, m.user_id): m.role
            for m in (await db.execute(select(OrgMember))).scalars()
        }
        assert roles[(owner_org, tgt.id)] == "owner"       # source owner outranked the target
        assert roles[(member_org, tgt.id)] == "admin"      # target admin outranked source member
        assert roles[(org_proj, tgt.id)] == "member"       # moved; keeps org-project owner a member
        assert not any(uid == src.id for (_, uid) in roles)
        assert (await db.execute(select(Project.owner_id).where(Project.id == proj_id))).scalar_one() == tgt.id
        assert (await db.execute(
            select(func.count()).select_from(Session).where(Session.user_id == tgt.id))).scalar() == 5
        assert (await db.execute(select(Session.alias).where(Session.id == clash.id))).scalar_one() is None
        assert (await db.execute(select(Session.alias).where(Session.id == mine.id))).scalar_one() == "work"
        assert (await db.execute(
            select(func.count()).select_from(SyncWatchlist).where(SyncWatchlist.session_id == mine.id)
        )).scalar() == 1
        assert (await db.execute(
            select(func.count()).select_from(TeamMember).where(TeamMember.team_id == team.id))).scalar() == 1
        assert (await db.execute(
            select(func.count()).select_from(GitLabSettings).where(GitLabSettings.user_id == tgt.id))).scalar() == 1
        assert (await db.execute(
            select(func.count()).select_from(Entitlement).where(
                Entitlement.owner_id == tgt.id, Entitlement.status == "active"))).scalar() == 1
        srow = (await db.execute(select(User).where(User.id == src.id))).scalar_one()
        trow = (await db.execute(select(User).where(User.id == tgt.id))).scalar_one()
        assert srow.is_active is False and trow.storage_used_bytes == 11
        keys = (await db.execute(select(ApiKey).where(ApiKey.user_id == src.id))).scalars().all()
        assert keys and all(not k.is_active for k in keys)
        [entry] = (await db.execute(
            select(AdminAction).where(AdminAction.action == "user_merge"))).scalars().all()
        assert entry.admin_id == admin.id and entry.source == "api"
        assert json.loads(entry.after_state)["merged_into"] == tgt.id
    await engine.dispose()


async def test_concurrent_merges_do_not_deadlock() -> None:
    engine, factory, app = await _setup()
    admin, admin_h = await _user(factory, "admin")
    a, _ = await _user(factory)
    b, _ = await _user(factory)
    c, _ = await _user(factory)
    org = f"org_{uuid.uuid4().hex[:12]}"
    async with factory() as db:
        db.add(Organization(id=org, name="Org", slug=f"s-{uuid.uuid4().hex[:8]}", tier="enterprise"))
        await db.flush()
        for u, role in ((a, "owner"), (b, "admin"), (c, "member")):
            db.add(OrgMember(org_id=org, user_id=u.id, role=role))
        for u in (a, b, c):
            for _ in range(5):
                db.add(_session(u.id))
        await db.commit()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        def go(s, t):
            return client.post(f"/api/v1/admin/users/{s.id}/merge-into/{t.id}",
                               json={"reason": "race", "dry_run": False}, headers=admin_h)

        # same pair twice, an opposite pair, and a chain, all at once
        results = await asyncio.wait_for(
            asyncio.gather(go(a, b), go(a, b), go(b, a), go(c, b)), timeout=90
        )
    assert all(r.status_code in (200, 409) for r in results), [r.text for r in results]
    assert any(r.status_code == 200 for r in results)

    async with factory() as db:
        owners = (await db.execute(
            select(func.count()).select_from(OrgMember).where(
                OrgMember.org_id == org, OrgMember.role == "owner"))).scalar()
        assert owners == 1
        total = (await db.execute(
            select(func.count()).select_from(Session).where(
                Session.user_id.in_([a.id, b.id, c.id])))).scalar()
        assert total == 15  # nothing lost or duplicated
        inactive = (await db.execute(
            select(func.count()).select_from(User).where(User.is_active.is_(False)))).scalar()
        assert inactive >= 1
    await engine.dispose()
