"""PostgreSQL: rows added in one flush with a child pointing at a parent that
is also new. Runs only with SFS_TEST_PG_URL.

db/models.py declares no relationship(), so the unit of work orders inserts
by class name, not by foreign key: a child whose class sorts before its
parent's is inserted first. SQLite never enforced the foreign key; PostgreSQL
rejects the flush (GitHub #124, tk_59579f33997047c8).
"""

from __future__ import annotations

import os

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from sessionfs.server.db.models import Base, SkillScanFinding, SkillVersionScan
from tests.server.integration.test_skill_marketplace import _hooky_tar

pytestmark = pytest.mark.skipif(
    not os.environ.get("SFS_TEST_PG_URL"), reason="SFS_TEST_PG_URL not set"
)


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


async def test_publish_with_scan_findings_on_postgres(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession
) -> None:
    """The publish-time scan and its findings are both new rows; the findings
    (SkillScanFinding sorts before SkillVersionScan) must not share its flush,
    or the version flush fails and the publish is treated as a lost race."""
    resp = await client.post(
        "/api/v1/users/me/skills/packages",
        headers=auth_headers,
        files={"package": ("hooky.tar.gz", _hooky_tar(), "application/gzip")},
    )
    assert resp.status_code == 201, resp.text
    version_id = resp.json()["version_id"]
    scan = (
        await db_session.execute(
            select(SkillVersionScan).where(SkillVersionScan.version_id == version_id)
        )
    ).scalar_one()
    assert scan.finding_count > 0
    stored = (
        await db_session.execute(
            select(func.count()).select_from(SkillScanFinding).where(SkillScanFinding.scan_id == scan.id)
        )
    ).scalar_one()
    assert stored == scan.finding_count
