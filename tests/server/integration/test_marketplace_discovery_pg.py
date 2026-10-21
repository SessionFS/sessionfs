"""Marketplace discovery on real PostgreSQL (marketplace v2 §9).

Runs when SFS_TEST_PG_URL points at a scratch PostgreSQL database: the
dialect upserts behind install counting (concurrent events for one
principal count once), and the relevance keyset (row-value comparison,
scalar-subquery rank keys), filters, sweeper and analytics SQL end to end.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from sessionfs.server.auth.keys import generate_api_key, hash_api_key
from sessionfs.server.db.models import ApiKey, Base, SkillInstallDaily, User
from sessionfs.server.services import skill_discovery
from sessionfs.server.storage.local import LocalBlobStore
from tests.server.integration.test_marketplace_discovery import _tar

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        not os.environ.get("SFS_TEST_PG_URL"),
        reason="set SFS_TEST_PG_URL to a scratch PostgreSQL database",
    ),
]


async def _user(factory, tier: str) -> tuple[User, dict]:
    raw = generate_api_key()
    user = User(id=str(uuid.uuid4()), email=f"{uuid.uuid4().hex}@example.com", tier=tier,
                created_at=datetime.now(timezone.utc), email_verified=True)
    async with factory() as db:
        db.add(user)
        await db.flush()
        db.add(ApiKey(id=str(uuid.uuid4()), user_id=user.id, key_hash=hash_api_key(raw), name="k",
                      created_at=datetime.now(timezone.utc)))
        await db.commit()
    return user, {"Authorization": f"Bearer {raw}"}


async def test_discovery_end_to_end_on_postgres(tmp_path: Path) -> None:
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
    app.state.blob_store = LocalBlobStore(tmp_path / "blobs")
    _, headers = await _user(factory, "pro")
    _, admin = await _user(factory, "admin")
    suffix = uuid.uuid4().hex[:6]
    namespace = f"pg{suffix}"
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        published = []
        for name in (f"lint{suffix}", f"lint{suffix}-x", f"my-lint{suffix}", f"zz{suffix}"):
            response = await client.post(
                "/api/v1/users/me/skills/packages", headers=headers,
                files={"package": ("p.tar.gz", _tar(name, description=f"lint{suffix} helper", scripts=name.startswith("my")), "application/gzip")},
                data={"namespace": namespace, "collection": namespace},
            )
            assert response.status_code == 201, response.text
            published.append(response.json())

        ranked = await client.get("/api/v1/marketplace/packages", headers=headers,
                                  params={"q": f"lint{suffix}", "sort": "relevance", "limit": 100})
        assert ranked.status_code == 200, ranked.text
        names = [item["name"] for item in ranked.json()["items"]]
        assert names[0] == f"lint{suffix}" and len(names) == 4
        walked: list[str] = []
        cursor = None
        while True:
            params = {"q": f"lint{suffix}", "sort": "relevance", "limit": 1}
            if cursor:
                params["cursor"] = cursor
            page = (await client.get("/api/v1/marketplace/packages", headers=headers, params=params)).json()
            walked += [item["name"] for item in page["items"]]
            cursor = page["next_cursor"]
            if not page["has_more"]:
                break
        assert walked == names

        filtered = await client.get("/api/v1/marketplace/packages", headers=headers, params={
            "q": f"lint{suffix}", "capability": "scripts", "license": "MIT", "max_severity": "low",
            "updated_within": 7, "verified": "false", "attested": "false", "sort": "relevance",
        })
        assert [item["name"] for item in filtered.json()["items"]] == [f"my-lint{suffix}"]

        version_id = published[0]["version_id"]
        package_id = published[0]["package_id"]
        events = await client.post(f"/api/v1/marketplace/versions/{version_id}/events", headers=headers,
                                   json={"event": "install", "tools": ["codex", "cursor"]})
        assert events.json()["counted"] == 2

        works = await client.post("/api/v1/admin/marketplace/works-in-results", headers=admin,
                                  json={"results": [{"version_id": version_id, "tool": "codex", "passed": True}]})
        assert works.status_code == 200, works.text
        swept = await client.post("/api/v1/admin/marketplace/freshness/sweep", headers=admin, json={"limit": 2000})
        assert swept.status_code == 200, swept.text
        detail = (await client.get(f"/api/v1/marketplace/packages/{package_id}", headers=headers)).json()
        assert detail["freshness"]["tools"]["codex"]["verify_status"] == "passing"
        assert detail["installs"]["by_tool"]["codex"]["display"] == "<10"

    # Concurrent identical events from one principal count once.
    async def one(principal: str) -> bool:
        async with factory() as db:
            counted = await skill_discovery.record_event(
                db, package_id=package_id, version_id=version_id, tool="gemini-cli", event="install",
                source="reconcile", actor_type="user", principal_id=principal, org_id=None,
            )
            await db.commit()
            return counted

    results = await asyncio.gather(*(one("same") for _ in range(6)), *(one(f"p{i}") for i in range(5)))
    assert sum(results) == 6
    async with factory() as db:
        total = (await db.execute(
            select(func.sum(SkillInstallDaily.count)).where(
                SkillInstallDaily.package_id == package_id, SkillInstallDaily.tool == "gemini-cli"
            )
        )).scalar_one()
    await engine.dispose()
    assert total == 6
