"""Org skills policy on real PostgreSQL (marketplace v2 §8).

Runs only when SFS_TEST_PG_URL points at a scratch PostgreSQL database; the
schema is dropped and recreated. Re-runs the API tests against PostgreSQL
(the compliance query, revision lock, report upserts, retention and the
per-user device cap) and proves that
two concurrent writers holding the same ETag cannot both publish.
"""

from __future__ import annotations

import asyncio
import os

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from sessionfs.server.db.models import Base, OrgSkillPolicy, User
from tests.server.integration.test_skill_policy_api import (  # noqa: F401 - re-run on PostgreSQL
    NO_SCRIPTS,
    _enterprise,
    test_assignment_and_install_request_enforced,
    _roomy_report_limiter,
    test_compliance_pages_devices_and_counts_all_of_them,
    test_device_policy_reports_and_compliance,
    test_device_reports_are_bounded_per_device_day_and_user,
    test_downgraded_org_keeps_its_policy_and_may_only_relax_it,
    test_reconcile_listing_withholds_assignments_the_current_policy_refuses,
    test_revisions_etag_acknowledgement_and_audit,
)

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        not os.environ.get("SFS_TEST_PG_URL"),
        reason="set SFS_TEST_PG_URL to a scratch PostgreSQL database to run",
    ),
]


@pytest.fixture
async def db_engine():
    engine = create_async_engine(os.environ["SFS_TEST_PG_URL"])
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


async def test_concurrent_writers_with_one_etag_publish_once(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession, test_user: User
) -> None:
    org, _ = await _enterprise(db_session, test_user)
    url = f"/api/v1/orgs/{org.id}/skills/policy"

    async def put(mode: str):
        return await client.put(
            url,
            headers={**auth_headers, "If-Match": '"osp-0"'},
            json={"policy": {"skills": {**NO_SCRIPTS, "mode": mode}}},
        )

    results = await asyncio.gather(put("open"), put("allowlist"))
    assert sorted(r.status_code for r in results) == [200, 409], [r.text for r in results]
    rows = (await db_session.execute(select(OrgSkillPolicy).where(OrgSkillPolicy.org_id == org.id))).scalars().all()
    assert [row.revision for row in rows] == [1]
