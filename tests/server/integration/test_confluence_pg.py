"""PG-only Confluence claim-fencing tests (GitHub #99).

Runs when ``SFS_TEST_PG_URL`` points at a scratch PostgreSQL database. The
schema is built by the real migration chain (so migration 099's tables and
constraints are what the claim code runs against), then two independent
sessions race for the same publish claim: exactly one wins, the other gets
409 ``publish_in_progress`` — for a fresh source (unique-constraint insert
race) and for an established linked page (CAS on ``claim_token``).
"""

from __future__ import annotations

import asyncio
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from sessionfs.server.db.models import DocPageLink, Organization, Project, User
from sessionfs.server.routes.confluence import _acquire_publish_claim
from tests.pg_helpers import reset_pg_schema_async

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        not os.environ.get("SFS_TEST_PG_URL"),
        reason="set SFS_TEST_PG_URL to a scratch PostgreSQL database",
    ),
]

_VERSIONS = (
    Path(__file__).resolve().parents[3]
    / "src" / "sessionfs" / "server" / "db" / "migrations" / "versions"
)
_SITE = "https://acme.atlassian.net"


def _parent_present() -> bool:
    text = (_VERSIONS / "099_confluence.py").read_text()
    match = re.search(r'^down_revision\s*=\s*["\'](.+?)["\']', text, re.M)
    assert match
    return any(p.name.startswith(f"{match.group(1)}_") for p in _VERSIONS.iterdir())


async def _setup():
    if not _parent_present():
        pytest.skip("migration 099's parent revision is not merged yet")
    url = os.environ["SFS_TEST_PG_URL"]
    await reset_pg_schema_async(url)
    from tests.pg_helpers import upgrade_pg_to_head

    await asyncio.to_thread(upgrade_pg_to_head, url)
    engine = create_async_engine(url, pool_size=4, max_overflow=0)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as db:
        user = User(
            id=str(uuid.uuid4()), email=f"u-{uuid.uuid4().hex[:8]}@example.com",
            display_name="U", tier="enterprise", email_verified=True,
            created_at=datetime.now(timezone.utc),
        )
        org = Organization(
            id=f"org_{uuid.uuid4().hex[:10]}", name="O", slug=f"o-{uuid.uuid4().hex[:8]}",
            tier="enterprise", created_at=datetime.now(timezone.utc),
        )
        db.add_all([user, org])
        await db.flush()
        project = Project(
            id=f"proj_{uuid.uuid4().hex[:12]}", name="p",
            git_remote_normalized=f"github.com/acme/{uuid.uuid4().hex[:8]}",
            context_document="", owner_id=user.id, org_id=org.id,
        )
        db.add(project)
        await db.commit()
    return engine, factory, project.id, org.id


async def _claim(factory, project_id: str, org_id: str):
    async with factory() as db:
        try:
            _, action, token = await _acquire_publish_claim(
                db, project_id=project_id, org_id=org_id, site_url=_SITE,
                source_kind="wiki_page", source_key="home",
            )
            return action, token
        except HTTPException as exc:
            return "refused", exc.detail["error"]


async def test_concurrent_first_publish_claims_exactly_once():
    engine, factory, project_id, org_id = await _setup()
    try:
        results = await asyncio.gather(*[_claim(factory, project_id, org_id) for _ in range(4)])
        actions = sorted(r[0] for r in results)
        assert actions.count("create") == 1, results
        assert all(r == ("refused", "publish_in_progress") for r in results if r[0] != "create")
    finally:
        await engine.dispose()


async def test_concurrent_update_claims_on_linked_page_exactly_once():
    engine, factory, project_id, org_id = await _setup()
    try:
        async with factory() as db:
            db.add(DocPageLink(
                id=f"dpl_{uuid.uuid4().hex[:20]}", org_id=org_id, project_id=project_id,
                site_url=_SITE, source_kind="wiki_page", source_key="home",
                status="linked", page_id="9001", version=1,
            ))
            await db.commit()
        results = await asyncio.gather(*[_claim(factory, project_id, org_id) for _ in range(4)])
        assert [r[0] for r in results].count("update") == 1, results
        assert all(r == ("refused", "publish_in_progress") for r in results if r[0] != "update")
    finally:
        await engine.dispose()
