"""Held-update count on real PostgreSQL.

Runs when SFS_TEST_PG_URL points at a scratch PostgreSQL database: the
set-based count (correlated EXISTS over listings, the shared delivery
predicates, the cap) must agree with the queue on PostgreSQL too, and its
statement count must not grow with the number of assignments.
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from sessionfs.server.auth.keys import generate_api_key, hash_api_key
from sessionfs.server.db.models import (
    ApiKey,
    Base,
    Organization,
    OrgMember,
    SkillMarketplaceListing,
    User,
)
from sessionfs.server.storage.local import LocalBlobStore
from tests.pg_helpers import reset_pg_schema_async
from tests.server.integration.test_skill_disclosure_api import _tar

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        not os.environ.get("SFS_TEST_PG_URL"),
        reason="set SFS_TEST_PG_URL to a scratch PostgreSQL database",
    ),
]


async def test_held_updates_count_on_postgres(tmp_path: Path) -> None:
    from sessionfs.server.app import create_app
    from sessionfs.server.config import ServerConfig
    from sessionfs.server.db.engine import get_db
    from sessionfs.server.routes import marketplace_disclosure

    await reset_pg_schema_async()
    engine = create_async_engine(os.environ["SFS_TEST_PG_URL"], pool_size=5)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        app = create_app(ServerConfig(database_url="sqlite+aiosqlite://"))

        async def override_get_db():
            async with factory() as session:
                yield session

        app.dependency_overrides[get_db] = override_get_db
        app.state.blob_store = LocalBlobStore(tmp_path / "blobs")

        raw = generate_api_key()
        org_id = f"org_{uuid.uuid4().hex[:16]}"
        other_id = f"org_{uuid.uuid4().hex[:16]}"
        async with factory() as db:
            user = User(id=str(uuid.uuid4()), email=f"{uuid.uuid4().hex}@example.com", tier="team",
                        created_at=datetime.now(timezone.utc), email_verified=True)
            db.add_all([
                user,
                Organization(id=org_id, name="PG Org", slug=f"pg-{uuid.uuid4().hex[:8]}", tier="team"),
                Organization(id=other_id, name="Other", slug=f"pg-{uuid.uuid4().hex[:8]}", tier="team"),
            ])
            await db.flush()
            db.add(OrgMember(org_id=org_id, user_id=user.id, role="owner"))
            db.add(ApiKey(id=str(uuid.uuid4()), user_id=user.id, key_hash=hash_api_key(raw), name="k",
                          created_at=datetime.now(timezone.utc)))
            await db.commit()
        headers = {"Authorization": f"Bearer {raw}"}
        base = f"/api/v1/orgs/{org_id}/skills"

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            async def pair(namespace: str) -> tuple[dict, dict]:
                versions = []
                for body, version in (("One.", "1.0.0"), ("Two.", "2.0.0")):
                    response = await client.post(
                        f"{base}/packages", headers=headers,
                        files={"package": ("p.tar.gz", _tar(body=body, version=version), "application/gzip")},
                        data={"namespace": namespace, "collection": "kit", "visibility": "org"},
                    )
                    assert response.status_code == 201, response.text
                    versions.append(response.json())
                created = await client.post(f"{base}/assignments", headers=headers, json={
                    "version_id": versions[0]["version_id"], "content_hash": versions[0]["content_hash"],
                })
                assert created.status_code == 201, created.text
                return versions[1], created.json()

            async def count() -> tuple[int, dict]:
                statements: list[str] = []

                def _before(conn, cursor, statement, params, context, executemany):
                    statements.append(statement)

                event.listen(engine.sync_engine, "before_cursor_execute", _before)
                try:
                    response = await client.get(f"{base}/assignments/updates/count", headers=headers)
                finally:
                    event.remove(engine.sync_engine, "before_cursor_execute", _before)
                assert response.status_code == 200, response.text
                return len(statements), response.json()

            expected = set()
            _v2, assignment = await pair("pga")
            expected.add(assignment["id"])
            first_statements, body = await count()
            assert body == {"count": 1, "has_more": False}

            for name in ("pgb", "pgc", "pgd"):
                _v2, assignment = await pair(name)
                expected.add(assignment["id"])
            yanked, _a = await pair("pgyank")
            foreign, _a = await pair("pgforeign")
            async with factory() as db:
                for listing in (await db.execute(select(SkillMarketplaceListing))).scalars():
                    if listing.version_id == yanked["version_id"]:
                        listing.is_yanked = True
                    if listing.version_id == foreign["version_id"]:
                        listing.org_id = other_id
                await db.commit()

            later_statements, body = await count()
            assert body == {"count": 4, "has_more": False}
            assert later_statements == first_statements

            listed = await client.get(f"{base}/assignments/updates", headers=headers)
            assert listed.status_code == 200, listed.text
            assert {item["assignment"]["id"] for item in listed.json()["items"]} == expected

            original = marketplace_disclosure.MAX_UPDATES_COUNT
            marketplace_disclosure.MAX_UPDATES_COUNT = 2
            try:
                _n, capped = await count()
            finally:
                marketplace_disclosure.MAX_UPDATES_COUNT = original
            assert capped == {"count": 2, "has_more": True}
    finally:
        await engine.dispose()
        await reset_pg_schema_async()
