"""Native catalog generation on REAL PostgreSQL: concurrent first fetches of
one scope create exactly one catalog row (ON CONFLICT) and agree on the
commit, whether builds are single-flight or race through the
compare-and-swap; the org / user approved-set queries (correlated
subqueries) run on PostgreSQL."""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from sessionfs.server.db.models import (
    Base,
    SkillFeedCatalog,
    SkillFeedEntry,
    SkillMarketplaceListing,
    SkillPackage,
    SkillPackageVersion,
    SkillToolVerification,
    User,
)
from sessionfs.server.services import native_catalogs as service
from sessionfs.server.storage.local import LocalBlobStore
from tests.server.integration.test_marketplace_moderation import _tar

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        not os.environ.get("SFS_TEST_PG_URL"),
        reason="set SFS_TEST_PG_URL to a scratch PostgreSQL database",
    ),
]


@pytest.fixture(autouse=True)
def _reset_service_state():
    for state in (service._fresh_until, service._state_cache, service._build_failures, service._build_locks):
        state.clear()
    yield
    for state in (service._fresh_until, service._state_cache, service._build_failures, service._build_locks):
        state.clear()


@pytest.mark.parametrize("cross_process", [False, True], ids=["one-process", "cas-across-processes"])
async def test_concurrent_first_fetches_build_one_catalog(
    tmp_path: Path, cross_process: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One process: builds are single-flight. Across processes (simulated by
    giving every builder its own lock): every builder builds outside any
    database lock and the compare-and-swap lets exactly one win; losers
    discard, so entries and L0 rows are written once."""
    if cross_process:
        monkeypatch.setattr(service, "_build_lock", lambda key: asyncio.Lock())
    builds = 0
    real_build = service.build

    def counting_build(*args, **kwargs):
        nonlocal builds
        builds += 1
        return real_build(*args, **kwargs)

    monkeypatch.setattr(service, "build", counting_build)
    engine = create_async_engine(os.environ["SFS_TEST_PG_URL"], pool_size=10)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    store = LocalBlobStore(tmp_path / "blobs")
    suffix = uuid.uuid4().hex[:24]
    user_id = str(uuid.uuid4())
    blob_key = f"skills/{suffix}.tar.gz"
    await store.put(blob_key, _tar())
    async with factory() as db:
        # Earlier runs in this scratch database must not leak into the catalog.
        await db.execute(SkillFeedCatalog.__table__.delete())
        await db.execute(SkillMarketplaceListing.__table__.delete())
        db.add(User(id=user_id, email=f"{user_id}@example.com", tier="pro",
                    created_at=datetime.now(timezone.utc), email_verified=True))
        db.add(SkillPackage(id=f"spk_{suffix}", owner_type="system", owner_id="system",
                            namespace=f"pg{suffix[:6]}", name="review-helper", display_name="Review",
                            source_type="upload", source_locator="upload:x"))
        await db.flush()
        db.add(SkillPackageVersion(
            id=f"skv_{suffix}", package_id=f"spk_{suffix}", version_label="1.2.3",
            content_hash=f"sha256:{suffix}", agent_plugin_schema="s", manifest_json="{}",
            dlp_status="passed", blob_key=blob_key, blob_size_bytes=1,
            scan_summary=json.dumps({"ruleset": "r", "max_severity": "none"}),
        ))
        await db.flush()
        db.add(SkillMarketplaceListing(
            id=f"sml_{suffix}", package_id=f"spk_{suffix}", version_id=f"skv_{suffix}",
            name="review-helper", visibility="public", collection="pg",
        ))
        await db.commit()

    async def fetch() -> str:
        async with factory() as db:
            catalog = await service.ensure_catalog(db, store, "public", None, homepage_base="https://x")
            return catalog.current_commit_sha or ""

    commits = await asyncio.gather(*(fetch() for _ in range(6)))
    assert len(set(commits)) == 1 and commits[0]
    # Single-flight in one process; across processes several builders race
    # and all but one lose the swap.
    assert builds == 1 if not cross_process else builds > 1
    async with factory() as db:
        rows = (await db.execute(select(SkillFeedCatalog))).scalars().all()
        assert [row.scope_key for row in rows] == ["public"]
        assert rows[0].plugin_count == 1
        l0 = (await db.execute(select(SkillToolVerification).where(
            SkillToolVerification.version_id == f"skv_{suffix}"))).scalars().all()
        assert sorted(row.tool for row in l0) == sorted(
            ["claude-code", "copilot", "codex", "cursor", "gemini", "antigravity"]
        )
        entries = (await db.execute(select(SkillFeedEntry).where(
            SkillFeedEntry.catalog_id == rows[0].id))).scalars().all()
        assert len(entries) == 6
        # The org / user queries compile and run on PostgreSQL.
        assert await service.approved_rows(db, "org", "org_none") == []
        assert await service.approved_rows(db, "user", user_id) == []
    await engine.dispose()
