"""Domain auto-join vs. domain revocation on real PostgreSQL (two sessions).

Auto-join locks the user, then the org, then the verification row. Deleting
a verified domain must take the org lock first too; otherwise a delete that
has already removed the row and then inserts its audit event (which needs a
key-share lock on the org) deadlocks against an auto-join holding the org.

Only runs when SFS_TEST_PG_URL points at a scratch PostgreSQL database
(row locks are no-ops on SQLite). The schema is dropped and recreated.
"""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from sessionfs.server.db.models import (
    Base,
    OrgDomainVerification,
    OrgMember,
    Organization,
    User,
)

pytestmark = pytest.mark.skipif(
    not os.environ.get("SFS_TEST_PG_URL"),
    reason="set SFS_TEST_PG_URL to a scratch PostgreSQL database to run",
)


@pytest.mark.asyncio
async def test_domain_delete_and_auto_join_serialize_instead_of_deadlocking(monkeypatch):
    from sessionfs.server.routes import sso_admin
    from sessionfs.server.services import auto_join as auto_join_module

    engine = create_async_engine(os.environ["SFS_TEST_PG_URL"])
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)

    now = datetime.now(timezone.utc)
    org_id = f"org_{uuid.uuid4().hex[:12]}"
    async with sessions() as db:
        admin = User(id=str(uuid.uuid4()), email="admin@corp.com", display_name="A",
                     tier="team", email_verified=True, created_at=now)
        joiner = User(id=str(uuid.uuid4()), email="new@corp.com", display_name="N",
                      tier="team", email_verified=True, created_at=now)
        db.add_all([admin, joiner])
        db.add(Organization(id=org_id, name="Corp", slug=f"c-{uuid.uuid4().hex[:6]}",
                            tier="team", seats_limit=10,
                            settings=json.dumps({"general": {"auto_join_domains": ["corp.com"]}})))
        await db.flush()
        db.add(OrgMember(org_id=org_id, user_id=admin.id, role="owner"))
        domain_id = f"odv_{secrets.token_hex(12)}"
        db.add(OrgDomainVerification(id=domain_id, org_id=org_id, domain="corp.com",
                                     method="dns_txt", verification_token="t",
                                     status="verified", verified_at=now))
        await db.commit()

    async def entitled(_db, _org_id, _user):
        return None

    monkeypatch.setattr(sso_admin, "_require_oidc_sso", entitled)

    async def has_sso(_org, _db):
        return True

    monkeypatch.setattr(auto_join_module, "org_has_oidc_sso", has_sso)

    # Pause the delete after the row is gone, just before its audit insert.
    deleted_row = asyncio.Event()
    resume_delete = asyncio.Event()
    real_emit = sso_admin._emit_audit

    async def paused_emit(db, **kwargs):
        deleted_row.set()
        await resume_delete.wait()
        return await real_emit(db, **kwargs)

    monkeypatch.setattr(sso_admin, "_emit_audit", paused_emit)

    async def run_delete():
        async with sessions() as db:
            return await sso_admin.delete_domain(org_id, domain_id, user=admin, db=db)

    async def run_join():
        async with sessions() as db:
            await auto_join_module._auto_join_impl(joiner.id, db)

    delete_task = asyncio.create_task(run_delete())
    await asyncio.wait_for(deleted_row.wait(), timeout=10)
    join_task = asyncio.create_task(run_join())
    await asyncio.sleep(0.5)  # let auto-join reach its lock waits
    resume_delete.set()

    results = await asyncio.wait_for(
        asyncio.gather(delete_task, join_task, return_exceptions=True), timeout=30
    )
    errors = [r for r in results if isinstance(r, BaseException)]
    assert not errors, errors
    assert results[0]["deleted"] is True

    async with sessions() as db:
        members = (
            await db.execute(select(OrgMember.user_id).where(OrgMember.org_id == org_id))
        ).scalars().all()
    # The domain was revoked first, so the newcomer must not have joined.
    assert joiner.id not in members
    await engine.dispose()


@pytest.mark.asyncio
async def test_domain_verify_and_delete_serialize_instead_of_deadlocking(monkeypatch):
    """Verifying a pending domain must take the org lock before the domain
    row, like delete; otherwise verify holds the row, delete holds the org,
    and verify's audit insert (key-share on the org) deadlocks."""
    from sessionfs.server.routes import sso_admin

    engine = create_async_engine(os.environ["SFS_TEST_PG_URL"])
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)

    now = datetime.now(timezone.utc)
    org_id = f"org_{uuid.uuid4().hex[:12]}"
    async with sessions() as db:
        admin = User(id=str(uuid.uuid4()), email="admin@corp.com", display_name="A",
                     tier="team", email_verified=True, created_at=now)
        db.add(admin)
        db.add(Organization(id=org_id, name="Corp", slug=f"c-{uuid.uuid4().hex[:6]}",
                            tier="team", seats_limit=10))
        await db.flush()
        db.add(OrgMember(org_id=org_id, user_id=admin.id, role="owner"))
        domain_id = f"odv_{secrets.token_hex(12)}"
        db.add(OrgDomainVerification(id=domain_id, org_id=org_id, domain="corp.com",
                                     method="dns_txt", verification_token="tok",
                                     status="pending"))
        await db.commit()

    async def entitled(_db, _org_id, _user):
        return None

    async def txt(_domain):
        return ["sessionfs-verification=tok"]

    monkeypatch.setattr(sso_admin, "_require_oidc_sso", entitled)
    monkeypatch.setattr(sso_admin, "_resolve_txt", txt)

    # Pause verify just before its audit insert (after its row update).
    reached_audit = asyncio.Event()
    resume_verify = asyncio.Event()
    real_emit = sso_admin._emit_audit
    paused = {"done": False}

    async def paused_emit(db, **kwargs):
        if not paused["done"] and kwargs.get("event_type", "").startswith("sso_domain_verif"):
            paused["done"] = True
            reached_audit.set()
            await resume_verify.wait()
        return await real_emit(db, **kwargs)

    monkeypatch.setattr(sso_admin, "_emit_audit", paused_emit)

    async def run_verify():
        async with sessions() as db:
            return await sso_admin.verify_domain(org_id, domain_id, user=admin, db=db)

    async def run_delete():
        async with sessions() as db:
            return await sso_admin.delete_domain(org_id, domain_id, user=admin, db=db)

    verify_task = asyncio.create_task(run_verify())
    await asyncio.wait_for(reached_audit.wait(), timeout=10)
    delete_task = asyncio.create_task(run_delete())
    await asyncio.sleep(0.5)  # let delete reach its lock wait
    resume_verify.set()

    results = await asyncio.wait_for(
        asyncio.gather(verify_task, delete_task, return_exceptions=True), timeout=30
    )
    errors = [r for r in results if isinstance(r, BaseException)]
    assert not errors, errors
    assert results[1]["deleted"] is True
    await engine.dispose()
