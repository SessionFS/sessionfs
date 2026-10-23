"""Concurrent namespace claims cannot exceed the publisher quota (PostgreSQL).

Runs when SFS_TEST_PG_URL points at a scratch PostgreSQL database. Eight
claims for one owner race through ``enforce_claim_quota`` + ``claim_namespace``
in separate sessions; the row locks must let at most MAX_PUBLISHERS_PER_OWNER
commit.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from datetime import datetime, timezone

import pytest
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from tests.pg_helpers import wait_for_lock_waiters
from sessionfs.server.db.models import Base, SkillPublisher, User
from sessionfs.server.services import publishers as svc
from sessionfs.skills.confusables import skeleton

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        not os.environ.get("SFS_TEST_PG_URL"),
        reason="set SFS_TEST_PG_URL to a scratch PostgreSQL database",
    ),
]


async def test_concurrent_claims_respect_the_quota() -> None:
    engine = create_async_engine(os.environ["SFS_TEST_PG_URL"], pool_size=10)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    user_id = str(uuid.uuid4())
    async with factory() as db:
        db.add(User(id=user_id, email=f"{user_id}@example.com", tier="pro",
                    created_at=datetime.now(timezone.utc), email_verified=True))
        await db.commit()

    async def claim(index: int) -> str:
        async with factory() as db:
            try:
                await svc.enforce_claim_quota(
                    db, owner_type="user", owner_id=user_id, claimant_user_id=user_id
                )
                await asyncio.sleep(0.05)  # widen the race window
                await svc.claim_namespace(
                    db, namespace=f"race-{'abcdefgh'[index]}-{user_id[:6]}", owner_type="user",
                    org_id=None, user_id=user_id, display_name=None, created_by_user_id=user_id,
                )
                await db.commit()
                return "ok"
            except HTTPException as exc:
                await db.rollback()
                return str(exc.status_code)

    results = await asyncio.gather(*(claim(i) for i in range(8)))
    async with factory() as db:
        count = len((await db.execute(
            select(SkillPublisher.id).where(SkillPublisher.user_id == user_id)
        )).scalars().all())
    await engine.dispose()
    assert results.count("ok") == svc.MAX_PUBLISHERS_PER_OWNER, results
    assert count == svc.MAX_PUBLISHERS_PER_OWNER


async def _seed_publisher(factory) -> tuple[str, str, str]:
    from sessionfs.server.db.models import SkillPackage, SkillPackageVersion, SkillPublisher

    user_id = str(uuid.uuid4())
    suffix = user_id[:8]
    async with factory() as db:
        db.add(User(id=user_id, email=f"{user_id}@example.com", tier="pro",
                    created_at=datetime.now(timezone.utc), email_verified=True))
        await db.flush()
        db.add(SkillPublisher(id=f"pub_{suffix}", owner_type="user", user_id=user_id,
                              namespace=f"lock-{suffix}",
                              namespace_normalized=skeleton(f"lock-{suffix}")))
        await db.flush()
        db.add(SkillPackage(id=f"spk_{suffix}", owner_type="user", owner_id=user_id, namespace=f"lock-{suffix}",
                            name="pkg", display_name="Pkg", source_type="hosted-upload", source_locator="x",
                            publisher_id=f"pub_{suffix}"))
        await db.flush()
        db.add(SkillPackageVersion(id=f"skv_{suffix}", package_id=f"spk_{suffix}", content_hash=f"sha256:{suffix}",
                                   agent_plugin_schema="s", manifest_json="{}", dlp_status="passed",
                                   blob_key=f"k-{suffix}", blob_size_bytes=1))
        await db.commit()
    return user_id, f"pub_{suffix}", suffix


async def test_suspension_waits_for_an_in_flight_publish_and_collects_its_listing() -> None:
    from sessionfs.server.db.models import SkillMarketplaceListing, SkillPublisher

    engine = create_async_engine(os.environ["SFS_TEST_PG_URL"], pool_size=10)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    user_id, publisher_id, suffix = await _seed_publisher(factory)

    # Transaction A: a publish holds the publisher lock and lists a version.
    publish = factory()
    resolved = await svc.resolve_publish_publisher(
        publish, namespace=f"lock-{suffix}", owner_type="user", owner_id=user_id,
        visibility="user", platform_admin=False, is_new_package=False, package_name="pkg",
    )
    assert resolved is not None and resolved.id == publisher_id
    publish.add(SkillMarketplaceListing(
        id=f"sml_{suffix}", collection="c", package_id=f"spk_{suffix}", version_id=f"skv_{suffix}",
        name="pkg", visibility="public", moderation_state="approved",
        published_at=datetime.now(timezone.utc),
    ))
    await publish.flush()

    # Transaction B: suspension must wait for A.
    async def suspend() -> list[str]:
        async with factory() as db:
            _, ids = await svc.suspend_publisher(db, publisher_id, "race")
            await db.commit()
            return ids

    task = asyncio.create_task(suspend())
    await wait_for_lock_waiters(engine, task=task)
    assert not task.done(), "suspension did not wait for the in-flight publish"
    await publish.commit()
    await publish.close()
    ids = await asyncio.wait_for(task, timeout=10)
    assert ids == [f"sml_{suffix}"]  # it saw, and holds for review, the new listing
    async with factory() as db:
        listing = await db.get(SkillMarketplaceListing, f"sml_{suffix}")
        assert listing.moderation_state == "pending_review"
        # And a publish that starts after the suspension is refused.
        with pytest.raises(HTTPException) as excinfo:
            await svc.resolve_publish_publisher(
                db, namespace=f"lock-{suffix}", owner_type="user", owner_id=user_id,
                visibility="user", platform_admin=False, is_new_package=False, package_name="pkg",
            )
        assert excinfo.value.status_code == 403
        assert (await db.get(SkillPublisher, publisher_id)).auth_generation == 1
    await engine.dispose()


async def _pg():
    engine = create_async_engine(os.environ["SFS_TEST_PG_URL"], pool_size=10)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


async def test_installation_revocation_waits_for_the_publisher_lock() -> None:
    from sessionfs.server.db.models import SkillPublisherVerification

    engine, factory = await _pg()
    _, publisher_id, suffix = await _seed_publisher(factory)
    async with factory() as db:
        db.add(SkillPublisherVerification(
            id=f"spv_{suffix}", publisher_id=publisher_id, method="github_org", subject=f"9{suffix[:5]}",
            subject_display="github.com/x", github_installation_id=7000 + int(suffix[:4], 16) % 1000,
            status="verified",
        ))
        await db.commit()
        installation = (await db.get(SkillPublisherVerification, f"spv_{suffix}")).github_installation_id

    holder = factory()
    await svc.lock_publisher(holder, publisher_id)

    async def revoke() -> int:
        async with factory() as db:
            count = await svc.revoke_installation_verifications(db, installation, "test")
            await db.commit()
            return count

    task = asyncio.create_task(revoke())
    await wait_for_lock_waiters(engine, task=task)
    assert not task.done(), "revocation did not take the publisher lock"
    await holder.commit()
    await holder.close()
    assert await asyncio.wait_for(task, timeout=10) == 1
    await engine.dispose()


async def test_legacy_package_publish_cannot_deadlock_with_moderation(tmp_path) -> None:
    """Moderation locks version -> package. Re-publishing an existing version
    of a legacy package whose publisher_id must change must not lock (or
    autoflush an UPDATE of) the package row before the version row."""
    import io
    import json as _json
    import tarfile
    from types import SimpleNamespace

    from sqlalchemy import text
    from starlette.datastructures import UploadFile

    from sessionfs.server.auth.dependencies import AuthContext
    from sessionfs.server.db.models import SkillPackage, SkillPackageVersion
    from sessionfs.server.routes import marketplace as mp
    from sessionfs.server.storage.local import LocalBlobStore
    from sessionfs.skills.package import AGENT_PLUGIN_SCHEMA

    def archive() -> bytes:
        output = io.BytesIO()
        files = {
            "plugin.json": _json.dumps({"$schema": AGENT_PLUGIN_SCHEMA, "name": "legacy-skill", "version": "1.0.0",
                                        "description": "d", "license": "MIT"}).encode(),
            "skills/legacy-skill/SKILL.md": b"---\nname: legacy-skill\ndescription: d.\n---\n\n# L\n\nBody.\n",
        }
        with tarfile.open(fileobj=output, mode="w:gz") as tar:
            for name, payload in files.items():
                info = tarfile.TarInfo(name)
                info.size = len(payload)
                tar.addfile(info, io.BytesIO(payload))
        return output.getvalue()

    engine, factory = await _pg()
    user_id = str(uuid.uuid4())
    namespace = f"legacy-{user_id[:8]}"
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(blob_store=LocalBlobStore(tmp_path))))
    data = archive()

    async def publish(db) -> dict:
        user = await db.get(User, user_id)
        auth = AuthContext(user=user, api_key_id="k", key_kind="user", scopes=["*"])
        return await mp._publish_package(
            request=request, db=db, auth=auth, owner_type="user", owner_id=user_id, org=None, role="user",
            upload=UploadFile(io.BytesIO(data), filename="p.tar.gz"), namespace=namespace,
            collection=namespace, visibility="user", source_locator=None, source_commit=None, source_subdir="",
        )

    async with factory() as db:
        db.add(User(id=user_id, email=f"{user_id}@example.com", tier="pro",
                    created_at=datetime.now(timezone.utc), email_verified=True))
        await db.commit()
        first = await publish(db)  # legacy: no publisher yet
        assert first["publisher_id"] is None
        publisher, _ = await svc.claim_namespace(
            db, namespace=namespace, owner_type="user", org_id=None, user_id=user_id,
            display_name=None, created_by_user_id=user_id,
        )
        await db.commit()
        publisher_id = publisher.id

    moderation = factory()
    await moderation.execute(text("SET LOCAL lock_timeout = '5s'"))
    await moderation.execute(
        select(SkillPackageVersion.id).where(SkillPackageVersion.id == first["version_id"]).with_for_update()
    )

    async def republish() -> dict:
        async with factory() as db:
            result = await publish(db)
            await db.commit()
            return result

    task = asyncio.create_task(republish())
    await wait_for_lock_waiters(engine, task=task)
    # Moderation's second lock (package) must be free: the publish may not
    # hold it while it waits for the version row.
    await moderation.execute(
        select(SkillPackage.id).where(SkillPackage.id == first["package_id"]).with_for_update()
    )
    await moderation.commit()
    await moderation.close()
    result = await asyncio.wait_for(task, timeout=15)
    assert result["publisher_id"] == publisher_id
    await engine.dispose()


async def test_namespace_claim_and_first_publish_are_serialized(tmp_path) -> None:
    """A claim holding the namespace lock makes a concurrent first publish by
    another owner into that namespace wait, then refuse it (409)."""
    import io
    import json as _json
    import tarfile
    from types import SimpleNamespace

    from starlette.datastructures import UploadFile

    from sessionfs.server.auth.dependencies import AuthContext
    from sessionfs.server.routes import marketplace as mp
    from sessionfs.server.storage.local import LocalBlobStore
    from sessionfs.skills.package import AGENT_PLUGIN_SCHEMA

    engine, factory = await _pg()
    claimer, publisher_user = str(uuid.uuid4()), str(uuid.uuid4())
    namespace = f"race-{claimer[:8]}"
    async with factory() as db:
        for user_id in (claimer, publisher_user):
            db.add(User(id=user_id, email=f"{user_id}@example.com", tier="pro",
                        created_at=datetime.now(timezone.utc), email_verified=True))
        await db.commit()

    output = io.BytesIO()
    files = {
        "plugin.json": _json.dumps({"$schema": AGENT_PLUGIN_SCHEMA, "name": "racer", "version": "1.0.0",
                                    "description": "d", "license": "MIT"}).encode(),
        "skills/racer/SKILL.md": b"---\nname: racer\ndescription: d.\n---\n\n# R\n\nBody.\n",
    }
    with tarfile.open(fileobj=output, mode="w:gz") as tar:
        for name, payload in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            tar.addfile(info, io.BytesIO(payload))
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(blob_store=LocalBlobStore(tmp_path))))

    claim = factory()
    await svc.lock_namespace(claim, namespace)
    await svc.claim_namespace(claim, namespace=namespace, owner_type="user", org_id=None, user_id=claimer,
                              display_name=None, created_by_user_id=claimer)

    async def publish() -> int:
        async with factory() as db:
            user = await db.get(User, publisher_user)
            auth = AuthContext(user=user, api_key_id="k", key_kind="user", scopes=["*"])
            try:
                await mp._publish_package(
                    request=request, db=db, auth=auth, owner_type="user", owner_id=publisher_user, org=None,
                    role="user", upload=UploadFile(io.BytesIO(output.getvalue()), filename="p.tar.gz"),
                    namespace=namespace, collection=namespace, visibility="user", source_locator=None,
                    source_commit=None, source_subdir="",
                )
                await db.commit()
                return 201
            except HTTPException as exc:
                await db.rollback()
                return exc.status_code

    task = asyncio.create_task(publish())
    await wait_for_lock_waiters(engine, task=task)
    assert not task.done(), "the first publish did not wait for the namespace claim"
    await claim.commit()
    await claim.close()
    assert await asyncio.wait_for(task, timeout=15) == 409
    await engine.dispose()


async def test_installation_state_is_serialized_with_verification_creation() -> None:
    engine, factory = await _pg()
    installation_id = int(uuid.uuid4().int % 1_000_000_000)
    webhook = factory()
    await svc.lock_installation(webhook, installation_id)
    await svc.record_installation_state(webhook, installation_id, "suspend")

    async def creator() -> bool:
        async with factory() as db:
            await svc.lock_installation(db, installation_id)
            return await svc.installation_usable(db, installation_id)

    task = asyncio.create_task(creator())
    await wait_for_lock_waiters(engine, task=task)
    assert not task.done(), "verification creation did not wait for the installation lock"
    await webhook.commit()
    await webhook.close()
    assert await asyncio.wait_for(task, timeout=10) is False
    await engine.dispose()


async def test_lookalike_promotions_are_serialized(monkeypatch: pytest.MonkeyPatch) -> None:
    """Two unverified lookalike publishers promoted concurrently: the second
    waits on the global confusable-promotion lock and then refuses."""
    from sessionfs.server.db.models import SkillPublisher, SkillPublisherVerification
    from sessionfs.server.services import domain_verification

    engine, factory = await _pg()
    suffix = uuid.uuid4().hex[:6]
    first_ns, second_ns = f"north{suffix}", f"north{suffix}-official"
    users = [str(uuid.uuid4()), str(uuid.uuid4())]
    async with factory() as db:
        for user_id in users:
            db.add(User(id=user_id, email=f"{user_id}@example.com", tier="pro",
                        created_at=datetime.now(timezone.utc), email_verified=True))
        await db.flush()
        for index, (user_id, namespace) in enumerate(zip(users, (first_ns, second_ns))):
            db.add(SkillPublisher(id=f"pub_{suffix}{index}", owner_type="user", user_id=user_id,
                                  namespace=namespace, namespace_normalized=skeleton(namespace)))
        await db.flush()
        for index in range(2):
            db.add(SkillPublisherVerification(
                id=f"spv_{suffix}{index}", publisher_id=f"pub_{suffix}{index}", method="dns_txt",
                subject=f"d{index}-{suffix}.example", subject_display="d", verification_token="t" * 40,
                status="pending",
            ))
        await db.commit()

    async def resolve(_name: str) -> list[str]:
        return [domain_verification.txt_record("t" * 40)]

    monkeypatch.setattr(domain_verification, "resolve_txt", resolve)
    holder = factory()
    first = await holder.get(SkillPublisherVerification, f"spv_{suffix}0")
    assert await svc.check_verification(holder, first) == "verified"  # holds the global lock

    async def promote_second() -> tuple[str, str | None]:
        async with factory() as db:
            row = await db.get(SkillPublisherVerification, f"spv_{suffix}1")
            status = await svc.check_verification(db, row)
            reason = row.status_reason
            await db.commit()
            return status, reason

    task = asyncio.create_task(promote_second())
    await wait_for_lock_waiters(engine, task=task)
    assert not task.done(), "the second promotion did not wait for the global lock"
    await holder.commit()
    await holder.close()
    status, reason = await asyncio.wait_for(task, timeout=10)
    assert status == "pending" and reason == "namespace_confusable_with_verified_publisher"
    await engine.dispose()


async def test_sso_domain_change_lock_does_not_wait_on_key_share_holders() -> None:
    """A publisher tombstone (or any insert referencing the org, e.g. its
    audit row) holds KEY SHARE on the org row. SSO domain changes lock the
    org FOR NO KEY UPDATE, which does not wait on that; FOR UPDATE would."""
    import secrets as _secrets

    from sqlalchemy import text

    from sessionfs.server.db.models import Organization, OrgAuditEvent
    from sessionfs.server.routes import sso_admin

    engine, factory = await _pg()
    org_id = f"org_{uuid.uuid4().hex[:12]}"
    async with factory() as db:
        db.add(Organization(id=org_id, name="Lock Org", slug=org_id, tier="team"))
        await db.commit()
    holder = factory()
    holder.add(OrgAuditEvent(id=f"oae_{_secrets.token_hex(8)}", org_id=org_id, org_name_snapshot="x",
                             event_type="skill_publisher_tombstoned"))
    await holder.flush()  # KEY SHARE on the org row, held until commit

    async with factory() as db:
        await asyncio.wait_for(sso_admin._lock_org_for_domain_change(db, org_id), timeout=3)
        await db.rollback()
    async with factory() as db:
        await db.execute(text("SET LOCAL lock_timeout = '500ms'"))
        with pytest.raises(Exception):  # the old FOR UPDATE would wait
            await db.execute(select(Organization.id).where(Organization.id == org_id).with_for_update())
        await db.rollback()
    await holder.rollback()
    await holder.close()
    await engine.dispose()


async def _seed_org_admin(factory) -> tuple[str, str]:
    from sessionfs.server.db.models import Organization, OrgMember

    user_id = str(uuid.uuid4())
    org_id = f"org_{uuid.uuid4().hex[:12]}"
    async with factory() as db:
        db.add(User(id=user_id, email=f"{user_id}@example.com", tier="pro",
                    created_at=datetime.now(timezone.utc), email_verified=True))
        db.add(Organization(id=org_id, name="Fence Org", slug=org_id, tier="team"))
        await db.flush()
        db.add(OrgMember(org_id=org_id, user_id=user_id, role="admin"))
        await db.commit()
    return user_id, org_id


async def test_membership_fences_wait_for_a_concurrent_removal() -> None:
    """Trusted-publisher add and SSO domain changes read the caller's
    membership FOR SHARE: an uncommitted removal makes them wait, and they
    then see it gone."""
    from sqlalchemy import delete as sa_delete

    from sessionfs.server.db.models import OrgMember
    from sessionfs.server.routes import marketplace_publishers, sso_admin

    engine, factory = await _pg()
    for check in ("trusted", "sso"):
        user_id, org_id = await _seed_org_admin(factory)
        remover = factory()
        await remover.execute(sa_delete(OrgMember).where(OrgMember.org_id == org_id, OrgMember.user_id == user_id))

        async def fence() -> object:
            async with factory() as db:
                if check == "trusted":
                    return await marketplace_publishers.fenced_org_role(db, org_id, user_id)
                user = await db.get(User, user_id)
                try:
                    return await sso_admin._fence_domain_admin(db, org_id, user)
                except HTTPException as exc:
                    return exc.status_code

        task = asyncio.create_task(fence())
        await wait_for_lock_waiters(engine, task=task)
        assert not task.done(), f"{check} fence did not wait for the membership removal"
        await remover.commit()
        await remover.close()
        assert await asyncio.wait_for(task, timeout=10) in (None, 404)
    await engine.dispose()


async def test_public_publish_waits_for_the_global_promotion_lock(tmp_path) -> None:
    import io
    import json as _json
    import tarfile
    from types import SimpleNamespace

    from starlette.datastructures import UploadFile

    from sessionfs.server.auth.dependencies import AuthContext
    from sessionfs.server.routes import marketplace as mp
    from sessionfs.server.storage.local import LocalBlobStore
    from sessionfs.skills.package import AGENT_PLUGIN_SCHEMA

    engine, factory = await _pg()
    admin_id = str(uuid.uuid4())
    async with factory() as db:
        db.add(User(id=admin_id, email=f"{admin_id}@example.com", tier="admin",
                    created_at=datetime.now(timezone.utc), email_verified=True))
        await db.commit()
    output = io.BytesIO()
    files = {
        "plugin.json": _json.dumps({"$schema": AGENT_PLUGIN_SCHEMA, "name": "globe", "version": "1.0.0",
                                    "description": "d", "license": "MIT"}).encode(),
        "skills/globe/SKILL.md": b"---\nname: globe\ndescription: d.\n---\n\n# G\n\nBody.\n",
    }
    with tarfile.open(fileobj=output, mode="w:gz") as tar:
        for name, payload in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            tar.addfile(info, io.BytesIO(payload))
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(blob_store=LocalBlobStore(tmp_path))))

    promotion = factory()
    await svc.lock_confusable_promotion(promotion)

    async def publish() -> dict:
        async with factory() as db:
            user = await db.get(User, admin_id)
            auth = AuthContext(user=user, api_key_id="k", key_kind="user", scopes=["*"])
            result = await mp._publish_package(
                request=request, db=db, auth=auth, owner_type="user", owner_id=admin_id, org=None,
                role="platform_admin", upload=UploadFile(io.BytesIO(output.getvalue()), filename="p.tar.gz"),
                namespace=f"globe-{admin_id[:6]}", collection="c", visibility="public",
                source_locator=None, source_commit=None, source_subdir="",
            )
            await db.commit()
            return result

    task = asyncio.create_task(publish())
    await wait_for_lock_waiters(engine, task=task)
    assert not task.done(), "a public publish did not wait for the global promotion lock"
    await promotion.commit()
    await promotion.close()
    assert (await asyncio.wait_for(task, timeout=15))["visibility"] == "public"
    await engine.dispose()


async def test_final_publish_fences_hold_membership_until_commit() -> None:
    """The trusted-publish attribution fence, write-mode publisher management
    and the org-publish fence all hold the membership row FOR SHARE: a
    removal waits for them, and they wait for (then see) a removal."""
    from sqlalchemy import delete as sa_delete

    from sessionfs.server.auth.dependencies import AuthContext
    from sessionfs.server.db.models import OrgMember, SkillPublisher
    from sessionfs.server.routes import marketplace as mp
    from sessionfs.server.routes import marketplace_publishers as routes

    engine, factory = await _pg()
    # 1. A held trusted-publish fence blocks the removal.
    from sessionfs.server.db.models import SkillTrustedPublisher

    user_id, org_id = await _seed_org_admin(factory)
    trusted_publisher_id = f"pub_{uuid.uuid4().hex[:12]}"
    trust_id = f"stp_{uuid.uuid4().hex[:12]}"
    async with factory() as db:
        db.add(SkillPublisher(id=trusted_publisher_id, owner_type="org", org_id=org_id,
                              namespace=f"t-{trusted_publisher_id[-8:]}",
                              namespace_normalized=trusted_publisher_id))
        await db.flush()
        db.add(SkillTrustedPublisher(
            id=trust_id, publisher_id=trusted_publisher_id, repository_owner_id=1, repository_id=2,
            repository="o/r", workflow_path=".github/workflows/r.yml", created_by_user_id=user_id,
        ))
        await db.commit()
        generation = (await db.get(SkillPublisher, trusted_publisher_id)).auth_generation or 0
    holder = factory()
    await routes._reauthorize_trusted(
        holder, publisher_id=trusted_publisher_id, trusted_id=trust_id, token_generation=generation,
        publisher_owner_type="org", publisher_org_id=org_id, publisher_user_id=None,
        trusted_creator_id=user_id, attributed_user_id=user_id,
    )

    async def remove() -> None:
        async with factory() as db:
            await db.execute(sa_delete(OrgMember).where(OrgMember.org_id == org_id, OrgMember.user_id == user_id))
            await db.commit()

    task = asyncio.create_task(remove())
    await wait_for_lock_waiters(engine, task=task)
    assert not task.done(), "the removal did not wait for the publish fence"
    await holder.commit()
    await holder.close()
    await asyncio.wait_for(task, timeout=10)

    # 2. Write-mode _manage and the org-publish fence wait for a removal.
    for check in ("manage", "org_publish"):
        user_id, org_id = await _seed_org_admin(factory)
        publisher_id = f"pub_{uuid.uuid4().hex[:12]}"
        async with factory() as db:
            db.add(SkillPublisher(id=publisher_id, owner_type="org", org_id=org_id,
                                  namespace=f"m-{publisher_id[-8:]}", namespace_normalized=publisher_id))
            await db.commit()
        remover = factory()
        await remover.execute(sa_delete(OrgMember).where(OrgMember.org_id == org_id, OrgMember.user_id == user_id))

        async def fenced() -> int:
            async with factory() as db:
                user = await db.get(User, user_id)
                try:
                    if check == "manage":
                        auth = AuthContext(user=user, api_key_id="k", key_kind="user", scopes=["*"])
                        await routes._manage(db, auth, publisher_id)
                    else:
                        await mp._fence_org_publisher(db, org_id=org_id, user_id=user_id)
                    return 200
                except HTTPException as exc:
                    return exc.status_code

        task = asyncio.create_task(fenced())
        await wait_for_lock_waiters(engine, task=task)
        assert not task.done(), f"{check} did not wait for the membership removal"
        await remover.commit()
        await remover.close()
        assert await asyncio.wait_for(task, timeout=10) in (403, 404)
    await engine.dispose()


async def test_offboarding_locks_memberships_before_the_user_row() -> None:
    """Deactivation follows the marketplace lock order (membership before
    user): while a publish fence holds the membership FOR SHARE, offboarding
    waits on the membership lock — it must NOT already hold the user row,
    or the fence's user lock would deadlock with it."""
    from sqlalchemy import delete as sa_delete
    from sqlalchemy import update as sa_update

    from sessionfs.server.db.models import OrgMember
    from sessionfs.server.routes import admin as admin_routes
    from sessionfs.server.routes import marketplace_publishers as routes

    engine, factory = await _pg()
    user_id, org_id = await _seed_org_admin(factory)

    # Transaction A: a marketplace fence holds the membership FOR SHARE.
    fence = factory()
    assert await routes.fenced_org_role(fence, org_id, user_id) == "admin"

    # Transaction B: offboarding (membership locks, then user, then delete).
    async def offboard() -> None:
        async with factory() as db:
            await admin_routes._lock_offboarding_memberships(db, user_id)
            await db.execute(sa_update(User).where(User.id == user_id).values(is_active=False))
            await db.execute(sa_delete(OrgMember).where(OrgMember.user_id == user_id))
            await db.commit()

    task = asyncio.create_task(offboard())
    await wait_for_lock_waiters(engine, task=task)
    assert not task.done(), "offboarding did not wait for the membership fence"
    # The fence can still take the user row: offboarding must not hold it
    # (with the old user-first order this lock would wait, then deadlock).
    await asyncio.wait_for(
        fence.execute(select(User.id).where(User.id == user_id).with_for_update(key_share=True)),
        timeout=5,
    )
    await fence.commit()
    await fence.close()
    await asyncio.wait_for(task, timeout=10)
    async with factory() as db:
        assert (await db.get(User, user_id)).is_active is False
        remaining = (
            await db.execute(
                select(OrgMember.id).where(OrgMember.org_id == org_id, OrgMember.user_id == user_id)
            )
        ).scalars().all()
        assert remaining == []
    await engine.dispose()


async def test_personal_fence_locks_the_org_entitlement_that_grants_the_tier() -> None:
    """A personal publish/claim fence re-checks the tier under the ORG
    entitlement lock when the user's marketplace_access is org-derived: a
    concurrent org-entitlement downgrade waits for the fence, and a fresh fence
    then sees the downgrade and refuses."""
    from sessionfs.server.routes import marketplace as mp
    from sessionfs.server.services.entitlements import apply_entitlement

    engine, factory = await _pg()
    user_id, org_id = await _seed_org_admin(factory)
    async with factory() as db:
        await apply_entitlement("org", org_id, tier="team", source="test", db=db)
        await db.commit()

    fence = factory()
    await mp._fence_personal_tier(fence, user_id=user_id)

    async def downgrade() -> None:
        async with factory() as db:
            await apply_entitlement("org", org_id, tier="free", source="test", db=db)
            await db.commit()

    task = asyncio.create_task(downgrade())
    await wait_for_lock_waiters(engine, task=task)
    assert not task.done(), "the org entitlement downgrade did not wait for the personal fence"
    await fence.commit()
    await fence.close()
    await asyncio.wait_for(task, timeout=10)
    # A fresh fence re-checks the org-derived tier and refuses: the downgrade
    # must be seen, not slipped past by the old user-entitlement-only fence.
    async with factory() as db:
        with pytest.raises(HTTPException) as excinfo:
            await mp._fence_personal_tier(db, user_id=user_id)
        assert excinfo.value.status_code == 403
    await engine.dispose()


async def test_org_publish_fence_locks_the_entitlement_and_rechecks_the_tier() -> None:
    """The final org-publish fence re-runs team_skill_library under the org
    entitlement lock: a concurrent downgrade waits for the fence, and a fresh
    fence then sees the downgrade and refuses."""
    from sessionfs.server.routes import marketplace as mp
    from sessionfs.server.services.entitlements import apply_entitlement

    engine, factory = await _pg()
    user_id, org_id = await _seed_org_admin(factory)
    async with factory() as db:
        await apply_entitlement("org", org_id, tier="team", source="test", db=db)
        await db.commit()

    fence = factory()
    await mp._fence_org_feature(fence, org_id=org_id)

    async def downgrade() -> None:
        async with factory() as db:
            await apply_entitlement("org", org_id, tier="free", source="test", db=db)
            await db.commit()

    task = asyncio.create_task(downgrade())
    await wait_for_lock_waiters(engine, task=task)
    assert not task.done(), "the org entitlement downgrade did not wait for the org feature fence"
    await fence.commit()
    await fence.close()
    await asyncio.wait_for(task, timeout=10)
    async with factory() as db:
        with pytest.raises(HTTPException) as excinfo:
            await mp._fence_org_feature(db, org_id=org_id)
        assert excinfo.value.status_code == 403
    await engine.dispose()


async def test_concurrent_offboarding_serializes_through_the_advisory_lock() -> None:
    """Offboarding takes a transaction-scoped advisory lock before any
    membership/successor lock, so two concurrent offboardings cannot interleave
    their successor scans and deadlock: the second waits for the first."""
    from sessionfs.server.routes import admin as admin_routes

    engine, factory = await _pg()
    admin_id, victim_id = str(uuid.uuid4()), str(uuid.uuid4())
    async with factory() as db:
        db.add(User(id=admin_id, email=f"{admin_id}@example.com", tier="admin",
                    created_at=datetime.now(timezone.utc), email_verified=True))
        db.add(User(id=victim_id, email=f"{victim_id}@example.com", tier="pro",
                    created_at=datetime.now(timezone.utc), email_verified=True))
        await db.commit()
        admin = await db.get(User, admin_id)

    holder = factory()
    await svc.advisory_xact_lock(holder, "offboarding", "global")

    async def offboard() -> None:
        async with factory() as db:
            await admin_routes.delete_user(victim_id, admin, db)

    task = asyncio.create_task(offboard())
    await wait_for_lock_waiters(engine, task=task)
    assert not task.done(), "offboarding did not wait for the offboarding advisory lock"
    await holder.commit()
    await holder.close()
    await asyncio.wait_for(task, timeout=10)
    async with factory() as db:
        assert (await db.get(User, victim_id)).is_active is False
    await engine.dispose()


async def test_reused_version_trust_downgrade_reads_the_listing_provenance() -> None:
    """A version first minted by an untrusted publish but later re-listed by a
    trusted publish keeps its own per-listing provenance; a subsequent untrusted
    publish must still be flagged for trust downgrade (moderation compares the
    previous LISTING's provenance, not the reused version's signature)."""
    import json as _json

    from sessionfs.server.db.models import (
        SkillMarketplaceListing,
        SkillPackage,
        SkillPackageVersion,
        SkillPublisher,
    )

    engine, factory = await _pg()
    user_id = str(uuid.uuid4())
    publisher_id = f"pub_{uuid.uuid4().hex[:12]}"
    package_id = f"spk_{uuid.uuid4().hex[:12]}"
    first_version_id = f"skv_{uuid.uuid4().hex[:12]}"
    second_version_id = f"skv_{uuid.uuid4().hex[:12]}"
    namespace = f"p-{publisher_id[-8:]}"
    async with factory() as db:
        db.add(User(id=user_id, email=f"{user_id}@example.com", tier="pro",
                    created_at=datetime.now(timezone.utc), email_verified=True))
        await db.flush()
        db.add(SkillPublisher(id=publisher_id, owner_type="user", user_id=user_id,
                              namespace=namespace, namespace_normalized=skeleton(namespace)))
        await db.flush()
        db.add(SkillPackage(id=package_id, owner_type="user", owner_id=user_id,
                            namespace=namespace, name="pkg", display_name="Pkg",
                            description="", source_type="hosted-upload", source_locator="x",
                            source_subdir="", publisher_id=publisher_id))
        await db.flush()
        db.add(SkillPackageVersion(
            id=first_version_id, package_id=package_id, content_hash=f"sha256:{first_version_id}",
            agent_plugin_schema="s", manifest_json="{}", dlp_status="passed",
            blob_key=f"k-{first_version_id}", blob_size_bytes=1, risk_summary="{}",
            signature_metadata="{}", scan_summary="{}",
        ))
        db.add(SkillPackageVersion(
            id=second_version_id, package_id=package_id, content_hash=f"sha256:{second_version_id}",
            agent_plugin_schema="s", manifest_json="{}", dlp_status="passed",
            blob_key=f"k-{second_version_id}", blob_size_bytes=1, risk_summary="{}",
            signature_metadata="{}", scan_summary="{}",
        ))
        await db.flush()
        # The trusted re-publish reused the untrusted-minted version; the LISTING
        # carries the trusted provenance even though the version row does not.
        db.add(SkillMarketplaceListing(
            id=f"sml_{uuid.uuid4().hex[:12]}", collection="c", package_id=package_id,
            version_id=first_version_id, name="pkg", visibility="public",
            moderation_state="approved", verification_state="verified",
            signature_metadata=_json.dumps({"kind": "github_oidc"}),
        ))
        await db.commit()

    async with factory() as db:
        publisher = await db.get(SkillPublisher, publisher_id)
        package = await db.get(SkillPackage, package_id)
        version = await db.get(SkillPackageVersion, second_version_id)
        state, reason = await svc.public_moderation_state(
            db, publisher=publisher, package=package, version=version,
            platform_admin=False, provenance={},
        )
        assert state == "pending_review"
        assert "trust_downgrade" in (reason or "")
    await engine.dispose()
