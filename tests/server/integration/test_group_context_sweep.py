"""Scheduled group-context recompile (GitHub #101, phase 2).

The KB compile sweep recompiles an opted-in group when it was never compiled
or when any member project compiled since the group did (in this sweep or
outside it), attributing the write to the group creator — but only while the
creator is active and still holds org-admin / group-admin standing, and the
org's plan still includes project groups. At most ``limit`` groups compile
per run. Uses a
file-backed engine (the sweep opens one session per project/group, which the
shared in-memory engine cannot service on a single connection).
"""

from __future__ import annotations

import asyncio
import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from sessionfs.server.db.models import (
    ContextCompilation,
    KnowledgeEntry,
    Organization,
    OrgMember,
    Project,
    ProjectGroup,
    ProjectGroupAccess,
    User,
)
from sessionfs.server.services.scheduled_compile import (
    GROUP_CLAIM_LEASE,
    claim_group,
    run_kb_compile_sweep,
)

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def file_engine(tmp_path):
    from sessionfs.server.db.engine import install_sqlite_unicode_lower
    from sessionfs.server.db.models import Base

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'sweep.db'}")
    install_sqlite_unicode_lower(engine)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest.fixture
async def sweep_session(file_engine):
    factory = async_sessionmaker(file_engine, expire_on_commit=False)
    async with factory() as session:
        yield session


@pytest.fixture
def sweep_factory(file_engine):
    return async_sessionmaker(file_engine, expire_on_commit=False)


async def _seed_sweep_org(
    sweep_session: AsyncSession,
    *,
    creator_role: str = "owner",
    compile_enabled: bool = True,
    org_tier: str = "team",
    schedule: str = "nightly",
    group_id: str | None = None,
) -> dict:
    """An org with one nightly due project in one group, plus the group's
    creator (a separate user whose org role drives whether the recompile is
    attributed to them or skipped)."""
    now = datetime.now(timezone.utc)

    def _mk_user(name: str) -> User:
        user = User(
            id=str(uuid.uuid4()),
            email=f"{name}-{uuid.uuid4().hex[:6]}@example.com",
            display_name=name,
            tier="pro",
            created_at=now,
            email_verified=True,
        )
        sweep_session.add(user)
        return user

    owner = _mk_user("owner")
    creator = _mk_user("creator")
    org = Organization(
        id=f"org_{uuid.uuid4().hex[:12]}",
        name="Sweep Org",
        slug=f"gor-{uuid.uuid4().hex[:8]}",
        tier=org_tier,
        seats_limit=20,
        settings="{}",
    )
    sweep_session.add(org)
    await sweep_session.flush()
    sweep_session.add(OrgMember(org_id=org.id, user_id=owner.id, role="owner"))
    sweep_session.add(OrgMember(org_id=org.id, user_id=creator.id, role=creator_role))

    project = Project(
        id=f"proj_{uuid.uuid4().hex[:16]}",
        name="Sweep Project",
        git_remote_normalized=f"github.com/example/{uuid.uuid4().hex}",
        owner_id=owner.id,
        org_id=org.id,
        compile_schedule=schedule,
    )
    sweep_session.add(project)

    group = ProjectGroup(
        id=group_id or f"pg_{uuid.uuid4().hex[:12]}",
        org_id=org.id,
        slug="core",
        name="Core",
        created_by_user_id=creator.id,
        created_at=now,
        updated_at=now,
        compile_enabled=compile_enabled,
    )
    sweep_session.add(group)
    await sweep_session.flush()
    sweep_session.add(
        ProjectGroupAccess(
            group_id=group.id,
            project_id=project.id,
            added_by_user_id=owner.id,
            created_at=now,
        )
    )
    sweep_session.add(
        KnowledgeEntry(
            project_id=project.id,
            session_id=f"ses_{uuid.uuid4().hex[:8]}",
            user_id=owner.id,
            entry_type="decision",
            content=f"Claim for {project.id}",
            confidence=0.9,
            claim_class="claim",
            freshness_class="current",
        )
    )
    await sweep_session.commit()
    return {"owner": owner, "creator": creator, "org": org, "project": project, "group": group}


async def _refetch_group(db: AsyncSession, group_id: str) -> ProjectGroup:
    return (
        await db.execute(
            select(ProjectGroup)
            .where(ProjectGroup.id == group_id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one()


async def test_sweep_recompiles_opted_in_group(sweep_session: AsyncSession, sweep_factory):
    seeded = await _seed_sweep_org(sweep_session, creator_role="admin")
    group_id = seeded["group"].id
    creator_id = seeded["creator"].id
    project_id = seeded["project"].id
    fixed = datetime(2026, 2, 3, 4, 5, 6, tzinfo=timezone.utc)

    result = await run_kb_compile_sweep(
        sweep_session, limit=10, session_factory=sweep_factory, now=fixed
    )

    assert result["compiled"] == 1
    assert result["groups_selected"] == 1
    assert result["groups_compiled"] == 1
    assert result["groups_skipped"] == 0
    assert result["groups_failed"] == 0

    loaded = await _refetch_group(sweep_session, group_id)
    assert loaded.context_compiled_at is not None
    assert loaded.compiled_by_user_id == creator_id
    assert loaded.context_sources == [project_id]
    assert "Shaped by 1 claims from 1 projects" in loaded.context_document


async def test_sweep_skips_group_when_creator_lacks_admin(sweep_session: AsyncSession, sweep_factory):
    seeded = await _seed_sweep_org(sweep_session, creator_role="member")
    group_id = seeded["group"].id
    fixed = datetime(2026, 2, 3, 4, 5, 6, tzinfo=timezone.utc)

    result = await run_kb_compile_sweep(
        sweep_session, limit=10, session_factory=sweep_factory, now=fixed
    )

    assert result["compiled"] == 1
    assert result["groups_selected"] == 1
    assert result["groups_compiled"] == 0
    assert result["groups_skipped"] == 1
    assert result["groups_failed"] == 0

    loaded = await _refetch_group(sweep_session, group_id)
    assert loaded.context_compiled_at is None


async def test_sweep_ignores_group_not_enabled(sweep_session: AsyncSession, sweep_factory):
    seeded = await _seed_sweep_org(sweep_session, creator_role="admin", compile_enabled=False)
    group_id = seeded["group"].id
    fixed = datetime(2026, 2, 3, 4, 5, 6, tzinfo=timezone.utc)

    result = await run_kb_compile_sweep(
        sweep_session, limit=10, session_factory=sweep_factory, now=fixed
    )

    assert result["compiled"] == 1
    assert result["groups_selected"] == 0
    assert result["groups_compiled"] == 0
    assert result["groups_skipped"] == 0
    assert result["groups_failed"] == 0

    loaded = await _refetch_group(sweep_session, group_id)
    assert loaded.context_compiled_at is None


async def _mark_group_compiled(db: AsyncSession, group_id: str, at: datetime) -> None:
    group = await _refetch_group(db, group_id)
    group.context_compiled_at = at
    group.context_document = "old"
    await db.commit()


async def test_sweep_recompiles_group_after_member_compiled_outside_sweep(
    sweep_session: AsyncSession, sweep_factory
):
    # The member project is not due (schedule off), so the sweep compiles no
    # project; it must still notice the member's manual compile.
    seeded = await _seed_sweep_org(sweep_session, creator_role="admin", schedule="off")
    group_id = seeded["group"].id
    project_id = seeded["project"].id
    owner_id = seeded["owner"].id
    t0 = datetime(2026, 3, 1, tzinfo=timezone.utc)
    await _mark_group_compiled(sweep_session, group_id, t0)

    # Nothing newer than the group's compile → not due.
    result = await run_kb_compile_sweep(sweep_session, limit=10, session_factory=sweep_factory)
    assert result["compiled"] == 0
    assert result["groups_selected"] == 0
    assert (await _refetch_group(sweep_session, group_id)).context_document == "old"

    sweep_session.add(
        ContextCompilation(
            project_id=project_id,
            user_id=owner_id,
            entries_compiled=1,
            compiled_at=t0 + timedelta(hours=1),
        )
    )
    await sweep_session.commit()

    result = await run_kb_compile_sweep(sweep_session, limit=10, session_factory=sweep_factory)
    assert result["compiled"] == 0
    assert result["groups_selected"] == 1
    assert result["groups_compiled"] == 1
    loaded = await _refetch_group(sweep_session, group_id)
    assert "Shaped by 1 claims from 1 projects" in loaded.context_document

    # Up to date again: the next run leaves it alone.
    result = await run_kb_compile_sweep(sweep_session, limit=10, session_factory=sweep_factory)
    assert result["groups_selected"] == 0


async def test_sweep_bounds_groups_per_run(sweep_session: AsyncSession, sweep_factory):
    first = await _seed_sweep_org(sweep_session, creator_role="admin", schedule="off")
    second = await _seed_sweep_org(sweep_session, creator_role="admin", schedule="off")
    group_ids = [first["group"].id, second["group"].id]

    result = await run_kb_compile_sweep(sweep_session, limit=1, session_factory=sweep_factory)
    assert result["groups_compiled"] == 1
    compiled = [
        (await _refetch_group(sweep_session, gid)).context_compiled_at is not None
        for gid in group_ids
    ]
    assert sorted(compiled) == [False, True]

    result = await run_kb_compile_sweep(sweep_session, limit=1, session_factory=sweep_factory)
    assert result["groups_compiled"] == 1
    for gid in group_ids:
        assert (await _refetch_group(sweep_session, gid)).context_compiled_at is not None


async def test_sweep_skips_group_when_creator_inactive(sweep_session: AsyncSession, sweep_factory):
    seeded = await _seed_sweep_org(sweep_session, creator_role="admin", schedule="off")
    group_id = seeded["group"].id
    seeded["creator"].is_active = False
    await sweep_session.commit()

    result = await run_kb_compile_sweep(sweep_session, limit=10, session_factory=sweep_factory)
    assert result["groups_selected"] == 1
    assert result["groups_skipped"] == 1
    assert result["groups_compiled"] == 0
    assert (await _refetch_group(sweep_session, group_id)).context_compiled_at is None


async def test_sweep_skips_group_when_org_lacks_feature(sweep_session: AsyncSession, sweep_factory):
    seeded = await _seed_sweep_org(
        sweep_session, creator_role="admin", schedule="off", org_tier="free"
    )
    group_id = seeded["group"].id

    result = await run_kb_compile_sweep(sweep_session, limit=10, session_factory=sweep_factory)
    assert result["groups_selected"] == 1
    assert result["groups_skipped"] == 1
    assert result["groups_compiled"] == 0
    assert (await _refetch_group(sweep_session, group_id)).context_compiled_at is None


async def test_skipped_groups_rotate_behind_healthy_ones(sweep_session: AsyncSession, sweep_factory):
    # Six groups the sweep must skip, with ids that sort before the healthy
    # one: limit=1 selects 1 x 5 candidates, all of them skipped ones.
    for i in range(6):
        await _seed_sweep_org(
            sweep_session, creator_role="member", schedule="off", group_id=f"pg_a{i}"
        )
    await _seed_sweep_org(sweep_session, creator_role="admin", schedule="off", group_id="pg_z")

    first = await run_kb_compile_sweep(sweep_session, limit=1, session_factory=sweep_factory)
    assert first["groups_skipped"] == 5
    assert first["groups_compiled"] == 0
    skipped = await _refetch_group(sweep_session, "pg_a0")
    assert skipped.compile_attempted_at is not None

    # The skipped groups were stamped, so the next run reaches the healthy one.
    second = await run_kb_compile_sweep(sweep_session, limit=1, session_factory=sweep_factory)
    assert second["groups_compiled"] == 1
    assert (await _refetch_group(sweep_session, "pg_z")).context_compiled_at is not None


async def test_failed_group_is_stamped_and_rotates(sweep_session: AsyncSession, sweep_factory, monkeypatch):
    from sessionfs.server.services import group_compiler

    await _seed_sweep_org(sweep_session, creator_role="admin", schedule="off", group_id="pg_bad")

    async def _boom(*a, **k):
        raise RuntimeError("compile exploded")

    monkeypatch.setattr(group_compiler, "compile_group_context", _boom)
    result = await run_kb_compile_sweep(sweep_session, limit=10, session_factory=sweep_factory)
    assert result["groups_failed"] == 1
    loaded = await _refetch_group(sweep_session, "pg_bad")
    assert loaded.compile_attempted_at is not None  # the claim survived the failure
    assert loaded.context_compiled_at is None

    # Within the lease it is not retried (and so cannot hog the batch).
    result = await run_kb_compile_sweep(sweep_session, limit=10, session_factory=sweep_factory)
    assert result["groups_selected"] == 0


async def test_claim_is_exclusive_within_the_lease(sweep_session: AsyncSession, sweep_factory):
    await _seed_sweep_org(sweep_session, creator_role="admin", schedule="off", group_id="pg_claim")
    now = datetime.now(timezone.utc)

    async with sweep_factory() as one, sweep_factory() as two:
        assert await claim_group(one, "pg_claim", now) is True
        assert await claim_group(two, "pg_claim", now) is False
        # After the lease another sweep may claim it again.
        later = now + GROUP_CLAIM_LEASE + timedelta(seconds=1)
        assert await claim_group(two, "pg_claim", later) is True


async def test_overlapping_sweep_skips_a_claimed_group(sweep_session: AsyncSession, sweep_factory):
    await _seed_sweep_org(sweep_session, creator_role="admin", schedule="off", group_id="pg_busy")
    async with sweep_factory() as other_sweep:
        assert await claim_group(other_sweep, "pg_busy", datetime.now(timezone.utc))

    result = await run_kb_compile_sweep(sweep_session, limit=10, session_factory=sweep_factory)
    assert result["groups_selected"] == 0
    assert result["groups_compiled"] == 0
    assert (await _refetch_group(sweep_session, "pg_busy")).context_compiled_at is None


@pytest.mark.skipif(not os.environ.get("SFS_TEST_PG_URL"), reason="SFS_TEST_PG_URL not set")
async def test_sweep_and_concurrent_claims_on_postgres():
    """The real migrated schema on PostgreSQL: two sweeps racing for one
    group claim it exactly once, and a full sweep compiles it."""
    from tests.pg_helpers import reset_pg_schema_async, upgrade_pg_to_head

    url = os.environ["SFS_TEST_PG_URL"]
    await reset_pg_schema_async(url)
    await asyncio.to_thread(upgrade_pg_to_head, url)
    engine = create_async_engine(url)
    try:
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            await _seed_sweep_org(session, creator_role="admin", schedule="off", group_id="pg_race")
        now = datetime.now(timezone.utc)

        async def _claim() -> bool:
            async with factory() as racer:
                return await claim_group(racer, "pg_race", now)

        outcomes = await asyncio.gather(*[_claim() for _ in range(4)])
        assert sorted(outcomes) == [False, False, False, True]

        async with factory() as session:
            await session.execute(
                ProjectGroup.__table__.update()
                .where(ProjectGroup.id == "pg_race")
                .values(compile_attempted_at=None)
            )
            await session.commit()
        async with factory() as session:
            result = await run_kb_compile_sweep(session, limit=10, session_factory=factory)
        assert result["groups_compiled"] == 1
        async with factory() as session:
            loaded = await _refetch_group(session, "pg_race")
            assert "Shaped by 1 claims from 1 projects" in loaded.context_document
    finally:
        await engine.dispose()
        await reset_pg_schema_async(url)
