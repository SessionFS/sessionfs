"""Registry poll leases on real PostgreSQL: two schedulers (API instances)
running at once never poll the same registry twice.

Only runs when SFS_TEST_PG_URL points at a scratch PostgreSQL database.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from sessionfs.server.db.models import Base, Organization, OrgMember, SkillMarketplaceRegistry, User
from sessionfs.server.services import registry_poller
from sessionfs.server.storage.local import LocalBlobStore

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        not os.environ.get("SFS_TEST_PG_URL"),
        reason="set SFS_TEST_PG_URL to a scratch PostgreSQL database",
    ),
]


class _NoLock:
    """Each simulated process has its own (uncontended) single-flight lock."""

    def locked(self) -> bool:
        return False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


async def _seed(factory, count: int) -> tuple[str, list[str]]:
    now = datetime.now(timezone.utc)
    org_id = f"org_{uuid.uuid4().hex[:16]}"
    ids = [f"smr_{uuid.uuid4().hex[:24]}" for _ in range(count)]
    async with factory() as db:
        user = User(id=str(uuid.uuid4()), email=f"{uuid.uuid4().hex[:8]}@example.com", display_name="A",
                    tier="team", email_verified=True, created_at=now)
        db.add(user)
        db.add(Organization(id=org_id, name="Org", slug=f"o-{uuid.uuid4().hex[:8]}", tier="team"))
        await db.flush()
        db.add(OrgMember(org_id=org_id, user_id=user.id, role="owner"))
        for index, registry_id in enumerate(ids):
            db.add(
                SkillMarketplaceRegistry(
                    id=registry_id, owner_type="org", owner_id=org_id, org_id=org_id,
                    alias=f"r{index}-{uuid.uuid4().hex[:6]}", source_url="https://git.example.com/a/b",
                    policy_state="approved", poll_interval_seconds=900, poll_actor_user_id=user.id,
                    next_poll_at=now - timedelta(minutes=1), consecutive_failures=0, branch_only_mode=False,
                )
            )
        await db.commit()
    return org_id, ids


async def test_concurrent_claims_grant_exactly_one_lease() -> None:
    engine = create_async_engine(os.environ["SFS_TEST_PG_URL"], pool_size=10)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    _org, (registry_id,) = await _seed(factory, 1)

    async def claim(owner: str) -> bool:
        async with factory() as db:
            return await registry_poller.claim_registry(
                db, registry_id, owner=owner, now=datetime.now(timezone.utc)
            )

    results = await asyncio.gather(*(claim(f"poll_{i}") for i in range(8)))
    assert sum(results) == 1
    # A forced (admin) poll still respects the live lease.
    async with factory() as db:
        assert not await registry_poller.claim_registry(
            db, registry_id, owner="poll_forced", now=datetime.now(timezone.utc), force=True
        )
        row = (await db.execute(select(SkillMarketplaceRegistry).where(SkillMarketplaceRegistry.id == registry_id))).scalar_one()
        assert row.poll_lease_owner is not None
    await engine.dispose()


async def test_two_schedulers_poll_each_registry_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    engine = create_async_engine(os.environ["SFS_TEST_PG_URL"], pool_size=10)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    _org, ids = await _seed(factory, 6)
    polled: list[str] = []

    async def fake_poll(db, registry_id, *, store, owner):
        polled.append(registry_id)
        await asyncio.sleep(0.05)
        await registry_poller._finish(db, registry_id, owner=owner, status="ok", summary=None,
                                      failed=False, interval=900, failures=0)
        return registry_poller.PollResult(registry_id=registry_id, status="ok")

    monkeypatch.setattr(registry_poller, "poll_registry", fake_poll)
    monkeypatch.setattr(registry_poller, "_RUN_LOCK", _NoLock())
    # One org here; per-org fairness is covered by test_registry_polling_limits.
    monkeypatch.setattr(registry_poller.rp, "max_per_org_per_run", lambda: 20)
    store = LocalBlobStore(tmp_path / "blobs")

    async def scheduler() -> dict:
        async with factory() as db:
            return await registry_poller.run_poll(db, store=store, limit=20)

    await asyncio.gather(scheduler(), scheduler(), scheduler())
    mine = [registry_id for registry_id in polled if registry_id in ids]
    assert len(mine) == len(set(mine))  # none polled twice
    assert sorted(mine) == sorted(ids)  # and every one polled
    async with factory() as db:
        rows = (
            await db.execute(select(SkillMarketplaceRegistry).where(SkillMarketplaceRegistry.id.in_(ids)))
        ).scalars().all()
        assert all(row.poll_lease_owner is None and row.last_poll_status == "ok" for row in rows)
    await engine.dispose()
