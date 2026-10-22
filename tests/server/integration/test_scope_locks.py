"""GH #70 — project/org scope locks (services/scope_locks.py).

The SQLite path is exercised by every persona/ticket/transfer route test
(the writer-lock UPDATE shows up in the statement-order tests in
test_org_personas.py). The PostgreSQL advisory-lock path needs two real
connections; it runs when SFS_TEST_PG_URL points at a scratch database.
"""

from __future__ import annotations

import asyncio
import os

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from sessionfs.server.services.scope_locks import advisory_key, lock_scopes
from tests.pg_helpers import upgrade_pg_to_head


@pytest.fixture(autouse=True, scope="module")
def _pg_migrated_to_head():
    """The PostgreSQL tests below need the schema at head, and the shared
    database starts every file empty (tests/conftest.py)."""
    if os.environ.get("SFS_TEST_PG_URL"):
        upgrade_pg_to_head()
    yield


def test_advisory_key_is_stable_and_scoped():
    assert advisory_key("project", "p1") == advisory_key("project", "p1")
    assert advisory_key("project", "p1") != advisory_key("org", "p1")
    assert advisory_key("project", "p1") != advisory_key("project", "p2")
    assert -(2**63) <= advisory_key("org", "x") < 2**63


@pytest.mark.asyncio
async def test_lock_project_scope_404s_on_concurrent_hard_delete(db_session, test_user):
    """delete_project hard-deletes WITHOUT taking the scope lock, so a writer
    that waited on the lock can find the row gone by the time it runs.
    lock_project_scope must re-SELECT and raise a clean 404 rather than let
    `db.refresh` raise an unhandled ObjectDeletedError (a 500)."""
    import uuid

    from fastapi import HTTPException

    from sessionfs.server.db.models import Project
    from sessionfs.server.services.scope_locks import lock_project_scope

    project = Project(
        id=f"proj_{uuid.uuid4().hex[:12]}",
        name="p",
        owner_id=test_user.id,
        git_remote_normalized=f"github.com/x/{uuid.uuid4().hex[:8]}",
    )
    db_session.add(project)
    await db_session.commit()

    # Out-of-band hard delete (no scope lock), as delete_project does.
    await db_session.execute(text("DELETE FROM projects WHERE id = :id"), {"id": project.id})

    with pytest.raises(HTTPException) as exc:
        await lock_project_scope(db_session, project)
    assert exc.value.status_code == 404


@pytest.mark.skipif(
    not os.environ.get("SFS_TEST_PG_URL"),
    reason="set SFS_TEST_PG_URL to a scratch PostgreSQL database",
)
@pytest.mark.asyncio
async def test_pg_project_lock_serializes_two_transactions():
    """Transaction A holds the project lock; B blocks until A commits, then
    acquires it. Deterministic: B's wait is observed while A still holds."""
    engine = create_async_engine(os.environ["SFS_TEST_PG_URL"])
    try:
        async with AsyncSession(engine) as a, AsyncSession(engine) as b:
            await lock_scopes(a, project_ids=["proj_lock_test"], org_ids=["org_lock_test"])

            # While A holds it, a non-blocking try from B fails.
            got = (
                await b.execute(
                    text("SELECT pg_try_advisory_xact_lock(:k)"),
                    {"k": advisory_key("project", "proj_lock_test")},
                )
            ).scalar_one()
            assert got is False
            await b.rollback()

            order: list[str] = []

            async def b_takes_lock() -> None:
                await lock_scopes(b, project_ids=["proj_lock_test"])
                order.append("b-acquired")
                await b.commit()

            task = asyncio.create_task(b_takes_lock())
            await asyncio.sleep(0.3)
            assert order == []  # still blocked behind A
            order.append("a-commit")
            await a.commit()
            await asyncio.wait_for(task, timeout=5)
            assert order == ["a-commit", "b-acquired"]

            # Org lock is independent of the project lock key.
            await lock_scopes(a, org_ids=["org_lock_test"])
            got_project = (
                await b.execute(
                    text("SELECT pg_try_advisory_xact_lock(:k)"),
                    {"k": advisory_key("project", "proj_lock_test")},
                )
            ).scalar_one()
            assert got_project is True
            await a.rollback()
            await b.rollback()
    finally:
        await engine.dispose()


@pytest.mark.skipif(
    not os.environ.get("SFS_TEST_PG_URL"),
    reason="set SFS_TEST_PG_URL to a scratch PostgreSQL database (migrated to head)",
)
@pytest.mark.asyncio
async def test_pg_merge_execute_with_audit_sessions_and_advisory_locks():
    """Merge execute on real PostgreSQL: advisory scope locks on the primary
    session, started/completed audit rows from separate sessions."""
    import uuid
    from datetime import datetime, timezone

    from sqlalchemy.ext.asyncio import async_sessionmaker

    from sessionfs.server.db.models import (
        AgentPersona,
        Project,
        ProjectRepo,
        User,
    )
    from sessionfs.server.services.merge import merge_projects

    engine = create_async_engine(os.environ["SFS_TEST_PG_URL"])
    factory = async_sessionmaker(engine, expire_on_commit=False)
    tag = uuid.uuid4().hex[:8]
    now = datetime.now(timezone.utc)
    try:
        async with factory() as db:
            exists = (
                await db.execute(text("SELECT to_regclass('public.project_merge_audit')"))
            ).scalar_one()
            if exists is None:
                pytest.skip("scratch PG database is not migrated to head")
            user = User(id=f"u-{tag}", email=f"{tag}@x.com", tier="pro", created_at=now)
            db.add(user)
            await db.flush()
            for side in ("src", "tgt"):
                db.add(Project(
                    id=f"p-{side}-{tag}", name=side, owner_id=user.id,
                    git_remote_normalized=f"github.com/x/{side}-{tag}",
                ))
            await db.flush()
            for side in ("src", "tgt"):
                db.add(ProjectRepo(
                    id=f"r-{side}-{tag}", project_id=f"p-{side}-{tag}",
                    git_remote_normalized=f"github.com/x/{side}-{tag}", is_primary=True,
                ))
            db.add(AgentPersona(
                id=f"per-{tag}", project_id=f"p-src-{tag}", name="Atlas", role="r",
                created_by=user.id,
            ))
            db.add(AgentPersona(
                id=f"per2-{tag}", project_id=f"p-tgt-{tag}", name="atlas", role="r",
                created_by=user.id,
            ))
            await db.commit()

            result = await asyncio.wait_for(
                merge_projects(
                    db=db, source_id=f"p-src-{tag}", target_id=f"p-tgt-{tag}",
                    user_id=user.id, dry_run=False, persona_policy="rename",
                    session_factory=factory,
                ),
                timeout=30,
            )
        async with factory() as check:
            status = (await check.execute(
                text("SELECT status FROM project_merge_audit WHERE id = :i"),
                {"i": result["audit_id"]},
            )).scalar_one()
            names = (await check.execute(
                text("SELECT name FROM agent_personas WHERE project_id = :p"),
                {"p": f"p-tgt-{tag}"},
            )).scalars().all()
        assert status == "completed"
        assert len({n.lower() for n in names}) == len(names) == 2
    finally:
        await engine.dispose()


@pytest.mark.skipif(
    not os.environ.get("SFS_TEST_PG_URL"),
    reason="set SFS_TEST_PG_URL to a scratch PostgreSQL database",
)
@pytest.mark.asyncio
async def test_pg_scope_then_row_order_has_no_deadlock():
    """Two transactions that both follow the global order (scope lock, then
    a row write on the same row) serialize instead of deadlocking — the
    update_ticket fix (scope lock before the lease-epoch UPDATE)."""
    engine = create_async_engine(os.environ["SFS_TEST_PG_URL"])
    try:
        async with engine.begin() as conn:
            await conn.execute(text("CREATE TABLE IF NOT EXISTS lock_probe (id int primary key, n int)"))
            await conn.execute(text("DELETE FROM lock_probe"))
            await conn.execute(text("INSERT INTO lock_probe VALUES (1, 0)"))

        order: list[str] = []

        async def worker(tag: str) -> None:
            async with AsyncSession(engine) as s:
                await lock_scopes(s, project_ids=["proj_order"], org_ids=["org_order"])
                await s.execute(text("UPDATE lock_probe SET n = n + 1 WHERE id = 1"))
                order.append(f"{tag}-row")
                await asyncio.sleep(0.2)
                await s.commit()
                order.append(f"{tag}-commit")

        await asyncio.wait_for(asyncio.gather(worker("a"), worker("b")), timeout=10)
        # Fully serialized: one worker's row write + commit precede the other's.
        assert order[1].endswith("-commit") and order[0][0] == order[1][0]
        async with engine.begin() as conn:
            n = (await conn.execute(text("SELECT n FROM lock_probe WHERE id = 1"))).scalar_one()
            await conn.execute(text("DROP TABLE lock_probe"))
        assert n == 2
    finally:
        await engine.dispose()



@pytest.mark.skipif(
    not os.environ.get("SFS_TEST_PG_URL"),
    reason="set SFS_TEST_PG_URL to a scratch PostgreSQL database (migrated to head)",
)
@pytest.mark.asyncio
async def test_pg_merge_waits_for_concurrent_demotion_then_refuses():
    """Codex R5 — a role change holds the org scope lock; a merge by the
    admin being demoted blocks on that lock, then re-reads standing and is
    refused (403) instead of merging on the stale admin role."""
    import uuid
    from datetime import datetime, timezone

    from fastapi import HTTPException
    from sqlalchemy import update
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from sessionfs.server.db.models import (
        OrgMember,
        Organization,
        Project,
        ProjectMergeAudit,
        ProjectRepo,
        User,
    )
    from sessionfs.server.services.merge import merge_projects

    engine = create_async_engine(os.environ["SFS_TEST_PG_URL"])
    factory = async_sessionmaker(engine, expire_on_commit=False)
    tag = uuid.uuid4().hex[:8]
    now = datetime.now(timezone.utc)
    org_id = f"org-{tag}"
    try:
        async with factory() as setup:
            exists = (
                await setup.execute(text("SELECT to_regclass('public.project_merge_audit')"))
            ).scalar_one()
            if exists is None:
                pytest.skip("scratch PG database is not migrated to head")
            owner = User(id=f"own-{tag}", email=f"o{tag}@x.com", tier="team", created_at=now)
            admin = User(id=f"adm-{tag}", email=f"a{tag}@x.com", tier="team", created_at=now)
            setup.add_all([owner, admin])
            setup.add(Organization(id=org_id, name=org_id, slug=org_id, tier="team", created_at=now))
            await setup.flush()
            setup.add(OrgMember(org_id=org_id, user_id=admin.id, role="admin", joined_at=now))
            for side in ("src", "tgt"):
                setup.add(Project(
                    id=f"p-{side}-{tag}", name=side, owner_id=owner.id, org_id=org_id,
                    git_remote_normalized=f"github.com/y/{side}-{tag}",
                ))
            await setup.flush()
            for side in ("src", "tgt"):
                setup.add(ProjectRepo(
                    id=f"r-{side}-{tag}", project_id=f"p-{side}-{tag}",
                    git_remote_normalized=f"github.com/y/{side}-{tag}", is_primary=True,
                ))
            await setup.commit()

        async with factory() as demoter, factory() as merger:
            # The role change: org lock held, admin demoted, not committed.
            await lock_scopes(demoter, org_ids=[org_id])
            await demoter.execute(
                update(OrgMember)
                .where(OrgMember.org_id == org_id, OrgMember.user_id == f"adm-{tag}")
                .values(role="member")
            )

            task = asyncio.create_task(
                merge_projects(
                    db=merger, source_id=f"p-src-{tag}", target_id=f"p-tgt-{tag}",
                    user_id=f"adm-{tag}", dry_run=False, persona_policy="rename",
                    session_factory=factory,
                )
            )
            await asyncio.sleep(0.5)
            assert not task.done()  # blocked on the org lock
            await demoter.commit()
            with pytest.raises(HTTPException) as exc:
                await asyncio.wait_for(task, timeout=10)
            assert exc.value.status_code == 403

        async with factory() as check:
            merged = (await check.execute(
                text("SELECT merged_into_project_id FROM projects WHERE id = :i"),
                {"i": f"p-src-{tag}"},
            )).scalar_one()
            status = (await check.execute(
                text("SELECT status FROM project_merge_audit WHERE source_project_id = :i"),
                {"i": f"p-src-{tag}"},
            )).scalar_one()
        assert merged is None
        assert status == "failed"
        assert ProjectMergeAudit.__tablename__ == "project_merge_audit"
    finally:
        await engine.dispose()



@pytest.mark.skipif(
    not os.environ.get("SFS_TEST_PG_URL"),
    reason="set SFS_TEST_PG_URL to a scratch PostgreSQL database (migrated to head)",
)
@pytest.mark.asyncio
async def test_pg_force_transfer_waits_for_org_lock_then_rereads():
    """Codex R7 — a membership change holds the org scope lock and removes
    the force-transfer target; the platform-admin force-transfer blocks on
    the org lock, then re-reads membership and refuses (404) instead of
    promoting a user who is no longer a member."""
    import uuid
    from datetime import datetime, timezone

    from fastapi import HTTPException
    from sqlalchemy import delete
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from sessionfs.server.db.models import OrgMember, Organization, User
    from sessionfs.server.routes.admin import ForceTransferRequest, force_transfer_owner

    engine = create_async_engine(os.environ["SFS_TEST_PG_URL"])
    factory = async_sessionmaker(engine, expire_on_commit=False)
    tag = uuid.uuid4().hex[:8]
    now = datetime.now(timezone.utc)
    org_id = f"orgft-{tag}"
    try:
        async with factory() as setup:
            exists = (
                await setup.execute(text("SELECT to_regclass('public.org_members')"))
            ).scalar_one()
            if exists is None:
                pytest.skip("scratch PG database is not migrated to head")
            platform = User(id=f"pa-{tag}", email=f"pa{tag}@x.com", tier="admin", created_at=now)
            owner = User(id=f"ow-{tag}", email=f"ow{tag}@x.com", tier="team", created_at=now)
            target = User(id=f"tg-{tag}", email=f"tg{tag}@x.com", tier="team", created_at=now)
            setup.add_all([platform, owner, target])
            setup.add(Organization(id=org_id, name=org_id, slug=org_id, tier="team", created_at=now))
            await setup.flush()
            setup.add(OrgMember(org_id=org_id, user_id=owner.id, role="owner", joined_at=now))
            setup.add(OrgMember(org_id=org_id, user_id=target.id, role="admin", joined_at=now))
            await setup.commit()

        async with factory() as remover, factory() as admin_db:
            await lock_scopes(remover, org_ids=[org_id])
            await remover.execute(
                delete(OrgMember).where(
                    OrgMember.org_id == org_id, OrgMember.user_id == f"tg-{tag}"
                )
            )
            platform_admin = await admin_db.get(User, f"pa-{tag}")
            task = asyncio.create_task(
                force_transfer_owner(
                    org_id=org_id,
                    body=ForceTransferRequest(to_user_id=f"tg-{tag}"),
                    admin=platform_admin,
                    db=admin_db,
                )
            )
            await asyncio.sleep(0.5)
            assert not task.done()  # blocked on the org lock
            await remover.commit()
            with pytest.raises(HTTPException) as exc:
                await asyncio.wait_for(task, timeout=10)
            assert exc.value.status_code == 404

        async with factory() as check:
            owner_now = (await check.execute(
                text("SELECT user_id FROM org_members WHERE org_id = :o AND role = 'owner'"),
                {"o": org_id},
            )).scalar_one()
        assert owner_now == f"ow-{tag}"
    finally:
        await engine.dispose()



async def _pg_org_setup(factory, tag: str, members: dict[str, str]):
    """Create users + an org with the given {user_suffix: role} members.
    Returns org_id, or None when the scratch DB is not migrated."""
    from datetime import datetime, timezone

    from sessionfs.server.db.models import OrgMember, Organization, User

    now = datetime.now(timezone.utc)
    org_id = f"org8-{tag}"
    async with factory() as setup:
        exists = (
            await setup.execute(text("SELECT to_regclass('public.org_members')"))
        ).scalar_one()
        if exists is None:
            return None
        for suffix in members:
            setup.add(User(
                id=f"{suffix}-{tag}", email=f"{suffix}{tag}@x.com", tier="team",
                created_at=now,
            ))
        setup.add(Organization(id=org_id, name=org_id, slug=org_id, tier="team", created_at=now))
        await setup.flush()
        for suffix, role in members.items():
            setup.add(OrgMember(org_id=org_id, user_id=f"{suffix}-{tag}", role=role, joined_at=now))
        await setup.commit()
    return org_id


@pytest.mark.skipif(
    not os.environ.get("SFS_TEST_PG_URL"),
    reason="set SFS_TEST_PG_URL to a scratch PostgreSQL database (migrated to head)",
)
@pytest.mark.asyncio
async def test_pg_owner_transfer_accept_waits_for_concurrent_demotion():
    """Codex R8 — a role change holds the org lock and demotes the transfer
    target; accept_owner_transfer blocks on the org lock, then re-reads the
    roles and refuses (409), so the org never ends up without an owner."""
    import uuid
    from datetime import datetime, timedelta, timezone

    from fastapi import HTTPException
    from sqlalchemy import update
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from sessionfs.server.db.models import OrgMember, OrgOwnerTransfer, User
    from sessionfs.server.routes.org_members import accept_owner_transfer

    engine = create_async_engine(os.environ["SFS_TEST_PG_URL"])
    factory = async_sessionmaker(engine, expire_on_commit=False)
    tag = uuid.uuid4().hex[:8]
    try:
        org_id = await _pg_org_setup(factory, tag, {"own": "owner", "tgt": "admin"})
        if org_id is None:
            pytest.skip("scratch PG database is not migrated to head")
        async with factory() as setup:
            transfer = OrgOwnerTransfer(
                org_id=org_id, from_user_id=f"own-{tag}", to_user_id=f"tgt-{tag}",
                status="pending",
                expires_at=datetime.now(timezone.utc) + timedelta(days=7),
            )
            setup.add(transfer)
            await setup.commit()
            transfer_id = transfer.id

        async with factory() as demoter, factory() as acceptor:
            await lock_scopes(demoter, org_ids=[org_id])
            await demoter.execute(
                update(OrgMember)
                .where(OrgMember.org_id == org_id, OrgMember.user_id == f"tgt-{tag}")
                .values(role="member")
            )
            target_user = await acceptor.get(User, f"tgt-{tag}")
            await acceptor.commit()
            task = asyncio.create_task(
                accept_owner_transfer(
                    org_id=org_id, transfer_id=transfer_id, request=None,  # type: ignore[arg-type]
                    user=target_user, db=acceptor,
                )
            )
            await asyncio.sleep(0.5)
            assert not task.done()  # blocked on the org lock
            await demoter.commit()
            with pytest.raises(HTTPException) as exc:
                await asyncio.wait_for(task, timeout=10)
            assert exc.value.status_code == 409

        async with factory() as check:
            owners = (await check.execute(
                text("SELECT user_id FROM org_members WHERE org_id = :o AND role = 'owner'"),
                {"o": org_id},
            )).scalars().all()
        assert owners == [f"own-{tag}"]
    finally:
        await engine.dispose()


@pytest.mark.skipif(
    not os.environ.get("SFS_TEST_PG_URL"),
    reason="set SFS_TEST_PG_URL to a scratch PostgreSQL database (migrated to head)",
)
@pytest.mark.asyncio
async def test_pg_delete_user_serializes_with_concurrent_join():
    """Codex R8 — a membership add for the user holds the USER lock; the
    user delete waits for it, then snapshots the full org set (now
    including the new membership) and removes every membership — none is
    missed."""
    import uuid
    from datetime import datetime, timezone

    from sqlalchemy.ext.asyncio import async_sessionmaker

    from sessionfs.server.db.models import OrgMember, Organization, User
    from sessionfs.server.routes.admin import delete_user

    engine = create_async_engine(os.environ["SFS_TEST_PG_URL"])
    factory = async_sessionmaker(engine, expire_on_commit=False)
    tag = uuid.uuid4().hex[:8]
    try:
        org_id = await _pg_org_setup(factory, tag, {"vic": "member", "own": "owner"})
        if org_id is None:
            pytest.skip("scratch PG database is not migrated to head")
        other_org = f"org8b-{tag}"
        now = datetime.now(timezone.utc)
        async with factory() as setup:
            setup.add(User(id=f"pa-{tag}", email=f"pa{tag}@x.com", tier="admin", created_at=now))
            setup.add(Organization(id=other_org, name=other_org, slug=other_org, tier="team", created_at=now))
            await setup.commit()

        async with factory() as joiner, factory() as deleter:
            # A join in progress: user lock (then org lock), insert, not committed.
            await lock_scopes(joiner, user_ids=[f"vic-{tag}"], org_ids=[other_org])
            joiner.add(OrgMember(org_id=other_org, user_id=f"vic-{tag}", role="member", joined_at=now))
            await joiner.flush()

            admin_user = await deleter.get(User, f"pa-{tag}")
            await deleter.commit()
            task = asyncio.create_task(
                delete_user(user_id=f"vic-{tag}", admin=admin_user, db=deleter)
            )
            await asyncio.sleep(0.5)
            assert not task.done()  # waiting on the user lock
            await joiner.commit()
            await asyncio.wait_for(task, timeout=10)

        async with factory() as check:
            left = (await check.execute(
                text("SELECT org_id FROM org_members WHERE user_id = :u"),
                {"u": f"vic-{tag}"},
            )).scalars().all()
        assert left == []
    finally:
        await engine.dispose()


@pytest.mark.skipif(
    not os.environ.get("SFS_TEST_PG_URL"),
    reason="set SFS_TEST_PG_URL to a scratch PostgreSQL database (migrated to head)",
)
@pytest.mark.asyncio
async def test_pg_create_project_waits_for_member_removal_then_refuses():
    """GH #70 — project creation takes the caller's user lock + the target
    org lock BEFORE checking membership. A concurrent member removal holds
    the org lock and deletes the OrgMember row; creation blocks, re-reads
    the membership under the locks, and refuses (403) instead of inserting
    a project owned by a now-removed member."""
    import uuid
    from types import SimpleNamespace

    from fastapi import BackgroundTasks, HTTPException, Request
    from sqlalchemy import delete
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from sessionfs.server.db.models import OrgMember, User
    from sessionfs.server.routes.projects import CreateProjectRequest, create_project

    engine = create_async_engine(os.environ["SFS_TEST_PG_URL"])
    factory = async_sessionmaker(engine, expire_on_commit=False)
    tag = uuid.uuid4().hex[:8]
    try:
        org_id = await _pg_org_setup(factory, tag, {"vic": "member", "own": "owner"})
        if org_id is None:
            pytest.skip("scratch PG database is not migrated to head")

        fake_request = Request({
            "type": "http",
            "app": SimpleNamespace(state=SimpleNamespace(blob_store=None)),
        })
        body = CreateProjectRequest(
            name=f"p-{tag}",
            git_remote_normalized=f"github.com/x/{tag}",
            org_id=org_id,
        )

        async with factory() as remover, factory() as creator:
            # T1: member removal holds the org lock (+ the target user lock)
            # and deletes the OrgMember row, uncommitted.
            await lock_scopes(remover, user_ids=[f"vic-{tag}"], org_ids=[org_id])
            await remover.execute(
                delete(OrgMember).where(
                    OrgMember.org_id == org_id,
                    OrgMember.user_id == f"vic-{tag}",
                )
            )

            creator_user = await creator.get(User, f"vic-{tag}")
            await creator.commit()
            task = asyncio.create_task(
                create_project(
                    body=body,
                    request=fake_request,
                    background_tasks=BackgroundTasks(),
                    user=creator_user,
                    ctx=None,  # type: ignore[arg-type]
                    db=creator,
                )
            )
            await asyncio.sleep(0.5)
            assert not task.done()  # blocked on the org/user lock
            await remover.commit()
            with pytest.raises(HTTPException) as exc:
                await asyncio.wait_for(task, timeout=10)
            assert exc.value.status_code == 403
            await creator.rollback()

        async with factory() as check:
            n = (await check.execute(
                text("SELECT count(*) FROM projects WHERE org_id = :o AND owner_id = :u"),
                {"o": org_id, "u": f"vic-{tag}"},
            )).scalar_one()
        assert n == 0  # no project owned by the removed member
    finally:
        await engine.dispose()


@pytest.mark.skipif(
    not os.environ.get("SFS_TEST_PG_URL"),
    reason="set SFS_TEST_PG_URL to a scratch PostgreSQL database (migrated to head)",
)
@pytest.mark.asyncio
async def test_pg_handoff_create_waits_for_persona_delete_then_refuses():
    """GH #70 R10 — a persona delete holds the project scope lock; a
    persona-bearing handoff create blocks on that lock, then re-reads the
    persona and refuses (422) instead of anchoring a handoff to a persona
    that no longer resolves."""
    import uuid
    from datetime import datetime, timezone
    from types import SimpleNamespace

    from fastapi import HTTPException, Request
    from sqlalchemy import update
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from sessionfs.server.auth.dependencies import AuthContext
    from sessionfs.server.db.models import AgentPersona, Project, Session, User
    from sessionfs.server.routes.handoffs import create_handoff
    from sessionfs.server.schemas.handoffs import CreateHandoffRequest
    from sessionfs.server.tier_gate import UserContext
    from sessionfs.server.tiers import Tier

    engine = create_async_engine(os.environ["SFS_TEST_PG_URL"])
    factory = async_sessionmaker(engine, expire_on_commit=False)
    tag = uuid.uuid4().hex[:8]
    now = datetime.now(timezone.utc)
    pid = f"p-hc-{tag}"
    try:
        async with factory() as setup:
            exists = (
                await setup.execute(text("SELECT to_regclass('public.handoffs')"))
            ).scalar_one()
            if exists is None:
                pytest.skip("scratch PG database is not migrated to head")
            sender = User(id=f"sd-{tag}", email=f"sd{tag}@x.com", tier="pro", created_at=now)
            setup.add(sender)
            await setup.flush()
            setup.add(Project(
                id=pid, name="hc", owner_id=sender.id,
                git_remote_normalized=f"github.com/z/hc-{tag}",
            ))
            await setup.flush()
            setup.add(AgentPersona(
                id=f"per-{tag}", project_id=pid, name="atlas", role="r",
                created_by=sender.id, is_active=True,
            ))
            setup.add(Session(
                id=f"ses-{tag}", user_id=sender.id, project_id=pid,
                git_remote_normalized=f"github.com/z/hc-{tag}",
                source_tool="claude-code", blob_key=f"b/{tag}", etag="e",
            ))
            await setup.commit()

        fake_request = Request({
            "type": "http",
            "app": SimpleNamespace(state=SimpleNamespace(
                blob_store=None, email_service=None, dashboard_url=None,
            )),
        })

        async with factory() as deleter, factory() as creator:
            # Persona delete in flight: project lock held, soft-delete
            # uncommitted.
            await lock_scopes(deleter, project_ids=[pid])
            await deleter.execute(
                update(AgentPersona)
                .where(AgentPersona.id == f"per-{tag}")
                .values(is_active=False)
            )

            sender_user = await creator.get(User, f"sd-{tag}")
            auth = AuthContext(user=sender_user, api_key_id="k", key_kind="user")
            ctx = UserContext(
                user=sender_user, effective_tier=Tier.PRO, org=None,
                role=None, is_org_user=False,
            )
            task = asyncio.create_task(
                create_handoff(
                    body=CreateHandoffRequest(
                        session_id=f"ses-{tag}",
                        recipient_email="rcpt@x.com",
                        persona_name="atlas",
                    ),
                    request=fake_request,
                    auth=auth,
                    ctx=ctx,
                    db=creator,
                )
            )
            await asyncio.sleep(0.5)
            assert not task.done()  # blocked on the project scope lock
            await deleter.commit()
            with pytest.raises(HTTPException) as exc:
                await asyncio.wait_for(task, timeout=10)
            assert exc.value.status_code == 422
            await creator.rollback()

        async with factory() as check:
            n = (await check.execute(
                text("SELECT count(*) FROM handoffs WHERE session_id = :s"),
                {"s": f"ses-{tag}"},
            )).scalar_one()
        assert n == 0  # no handoff anchored to the deleted persona
    finally:
        await engine.dispose()


@pytest.mark.skipif(
    not os.environ.get("SFS_TEST_PG_URL"),
    reason="set SFS_TEST_PG_URL to a scratch PostgreSQL database (migrated to head)",
)
@pytest.mark.asyncio
async def test_pg_handoff_claim_waits_for_merge_rewrite_then_refuses():
    """GH #70 R10 — a merge holds both project locks and rewrites a pending
    handoff (persona rename + anchor move); a claim blocks on the source
    project lock, then re-reads the handoff and refuses (409
    handoff_project_changed) instead of returning the stale persona."""
    import uuid
    from datetime import datetime, timedelta, timezone
    from types import SimpleNamespace

    from fastapi import HTTPException, Request
    from sqlalchemy import update
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from sessionfs.server.auth.dependencies import AuthContext
    from sessionfs.server.db.models import Handoff, Project, Session, User
    from sessionfs.server.routes.handoffs import claim_handoff

    engine = create_async_engine(os.environ["SFS_TEST_PG_URL"])
    factory = async_sessionmaker(engine, expire_on_commit=False)
    tag = uuid.uuid4().hex[:8]
    now = datetime.now(timezone.utc)
    src_id = f"p-cs-{tag}"
    tgt_id = f"p-ct-{tag}"
    try:
        async with factory() as setup:
            exists = (
                await setup.execute(text("SELECT to_regclass('public.handoffs')"))
            ).scalar_one()
            if exists is None:
                pytest.skip("scratch PG database is not migrated to head")
            sender = User(id=f"cs-{tag}", email=f"cs{tag}@x.com", tier="pro", created_at=now)
            recipient = User(id=f"cr-{tag}", email=f"cr{tag}@x.com", tier="pro", created_at=now)
            setup.add_all([sender, recipient])
            await setup.flush()
            for pid, side in ((src_id, "cs"), (tgt_id, "ct")):
                setup.add(Project(
                    id=pid, name=side, owner_id=sender.id,
                    git_remote_normalized=f"github.com/z/{side}-{tag}",
                ))
            await setup.flush()
            setup.add(Session(
                id=f"ses-{tag}", user_id=sender.id, project_id=src_id,
                git_remote_normalized=f"github.com/z/cs-{tag}",
                source_tool="claude-code", blob_key=f"b/{tag}", etag="e",
            ))
            await setup.flush()
            setup.add(Handoff(
                id=f"hnd_{tag}", session_id=f"ses-{tag}", sender_id=sender.id,
                recipient_user_id=recipient.id, status="pending",
                persona_name="atlas", source_project_id=src_id,
                created_at=now,
                expires_at=now + timedelta(days=1),
            ))
            await setup.commit()

        fake_request = Request({
            "type": "http",
            "app": SimpleNamespace(state=SimpleNamespace(
                blob_store=None, email_service=None, dashboard_url=None,
            )),
        })

        async with factory() as merger, factory() as claimer:
            # Merge in flight: BOTH project locks held (global order),
            # persona rewrite + anchor move uncommitted.
            await lock_scopes(merger, project_ids=sorted([src_id, tgt_id]))
            await merger.execute(
                update(Handoff)
                .where(Handoff.id == f"hnd_{tag}")
                .values(persona_name=f"atlas-{tag[:8]}", source_project_id=tgt_id)
            )

            recipient_user = await claimer.get(User, f"cr-{tag}")
            auth = AuthContext(user=recipient_user, api_key_id="k", key_kind="user")
            task = asyncio.create_task(
                claim_handoff(
                    handoff_id=f"hnd_{tag}",
                    request=fake_request,
                    auth=auth,
                    db=claimer,
                )
            )
            await asyncio.sleep(0.5)
            assert not task.done()  # blocked on the source project lock
            await merger.commit()
            with pytest.raises(HTTPException) as exc:
                await asyncio.wait_for(task, timeout=10)
            assert exc.value.status_code == 409
            assert exc.value.detail["code"] == "handoff_project_changed"
            await claimer.rollback()

        async with factory() as check:
            status = (await check.execute(
                text("SELECT status FROM handoffs WHERE id = :i"),
                {"i": f"hnd_{tag}"},
            )).scalar_one()
        assert status == "pending"  # the claim never went through
    finally:
        await engine.dispose()
