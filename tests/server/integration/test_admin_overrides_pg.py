"""GH #84 — admin overrides on real PostgreSQL.

Runs when SFS_TEST_PG_URL points at a scratch PostgreSQL database: the
project force-transfer (advisory scope locks, owner reassignment, VARCHAR
limits on the audit rows), the operator-record endpoint and the audit-log
filters against the real dialect. Behaviour coverage lives in
test_admin_overrides.py (SQLite).
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from sessionfs.server.auth.keys import generate_api_key, hash_api_key
from sessionfs.server.db.models import (
    AdminAction,
    ApiKey,
    Base,
    OrgAuditEvent,
    OrgMember,
    Organization,
    Project,
    User,
)

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        not os.environ.get("SFS_TEST_PG_URL"),
        reason="set SFS_TEST_PG_URL to a scratch PostgreSQL database",
    ),
]


async def _user(factory, tier: str = "team") -> tuple[User, dict]:
    raw = generate_api_key()
    user = User(id=str(uuid.uuid4()), email=f"{uuid.uuid4().hex}@example.com", tier=tier,
                created_at=datetime.now(timezone.utc), email_verified=True)
    async with factory() as db:
        db.add(user)
        await db.flush()
        db.add(ApiKey(id=str(uuid.uuid4()), user_id=user.id, key_hash=hash_api_key(raw),
                      name="k", created_at=datetime.now(timezone.utc)))
        await db.commit()
    return user, {"Authorization": f"Bearer {raw}"}


async def test_admin_overrides_on_postgres() -> None:
    from sessionfs.server.app import create_app
    from sessionfs.server.config import ServerConfig
    from sessionfs.server.db.engine import get_db

    engine = create_async_engine(os.environ["SFS_TEST_PG_URL"], pool_size=10)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    app = create_app(ServerConfig(database_url="sqlite+aiosqlite://"))

    async def override_get_db():
        async with factory() as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db

    admin, admin_headers = await _user(factory, "admin")
    owner, _ = await _user(factory)
    lead, _ = await _user(factory)
    src_id = f"org_{uuid.uuid4().hex[:12]}"
    dest_id = f"org_{uuid.uuid4().hex[:12]}"
    project_id = f"proj_{uuid.uuid4().hex[:12]}"
    async with factory() as db:
        for org_id in (src_id, dest_id):
            db.add(Organization(id=org_id, name="Org", slug=f"s-{uuid.uuid4().hex[:8]}",
                                tier="enterprise"))
        await db.flush()
        db.add(OrgMember(org_id=dest_id, user_id=lead.id, role="member"))
        db.add(Project(id=project_id, name="p", git_remote_normalized=f"github.com/x/{project_id}",
                       owner_id=owner.id, org_id=src_id))
        await db.commit()

    url = f"/api/v1/admin/projects/{project_id}/force-transfer"
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        refused = await client.post(
            url, json={"to_org_id": dest_id, "reason": "owner left"}, headers=admin_headers
        )
        assert refused.status_code == 409, refused.text
        assert refused.json()["error"]["code"] == "owner_not_member"

        moved = await client.post(
            url,
            json={"to_org_id": dest_id, "reason": "owner left", "new_owner_id": lead.id,
                  "expected_org_id": src_id},
            headers=admin_headers,
        )
        assert moved.status_code == 200, moved.text

        replay = await client.post(
            url,
            json={"to_org_id": dest_id, "reason": "owner left", "new_owner_id": lead.id,
                  "expected_org_id": src_id},
            headers=admin_headers,
        )
        assert replay.status_code == 409
        assert replay.json()["error"]["code"] == "unexpected_source_scope"

        recorded = await client.post(
            "/api/v1/admin/audit-log",
            json={"action": "user_merge", "target_type": "user", "target_id": owner.id,
                  "reason": "duplicate signup", "before_state": {"rows": list(range(500))},
                  "after_state": {"merged_into": lead.id}},
            headers=admin_headers,
        )
        assert recorded.status_code == 201, recorded.text

        listing = await client.get(
            "/api/v1/admin/audit-log",
            params={"target_type": "project", "target_id": project_id, "admin_id": admin.id},
            headers=admin_headers,
        )
        assert listing.status_code == 200, listing.text
        [row] = listing.json()["actions"]
        assert row["action"] == "project_transfer_override"
        assert row["before_state"]["owner_id"] == owner.id
        assert row["after_state"]["owner_id"] == lead.id
        manual = await client.get(
            "/api/v1/admin/audit-log", params={"source": "operator_record"},
            headers=admin_headers,
        )
        assert [a["action"] for a in manual.json()["actions"]] == ["user_merge"]

    async with factory() as db:
        project = (await db.execute(select(Project).where(Project.id == project_id))).scalar_one()
        assert (project.org_id, project.owner_id) == (dest_id, lead.id)
        actions = (await db.execute(select(AdminAction))).scalars().all()
        assert {a.source for a in actions} == {"api", "operator_record"}
        events = (
            await db.execute(select(OrgAuditEvent).where(OrgAuditEvent.target_id == project_id))
        ).scalars().all()
        assert {e.org_id for e in events} == {src_id, dest_id}
        assert all(json.loads(e.after)["owner_id"] == lead.id for e in events)
    await engine.dispose()
