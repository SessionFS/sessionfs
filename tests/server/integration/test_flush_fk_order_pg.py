"""PostgreSQL: rows added in one flush with a child pointing at a parent that
is also new. Runs only with SFS_TEST_PG_URL.

db/models.py declares no relationship(), so the unit of work orders inserts
by class name, not by foreign key: a child whose class sorts before its
parent's is inserted first. SQLite never enforced the foreign key; PostgreSQL
rejects the flush (GitHub #124, tk_59579f33997047c8).

The tk_59579f33997047c8 audit found every such write path guarded (an explicit
flush after the parent, or a query that autoflushes it); the static guard in
tests/server/unit/test_flush_fk_order_static.py keeps new ones out. The API
tests re-run below cover the guarded paths most likely to regress:

- chat upload: ConversationMessageMeta -> ConversationSource (flush first);
- org create: OrgMember -> Organization (flush first, GitHub #51);
- SSO JIT: ExternalIdentity / OrgMember -> User (the identity lookup's
  SELECT autoflushes the new user);
- skill publish: SkillScanFinding -> SkillVersionScan (rows added after the
  scan is flushed).
"""

from __future__ import annotations

import os

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from sessionfs.server.db.models import Base, SkillScanFinding, SkillVersionScan
from tests.server.integration.test_chat_threads_api import (  # noqa: F401 - re-run on PostgreSQL
    test_project_slack_upload_get_list_kind_filter,
)
from tests.server.integration.test_skill_marketplace import _hooky_tar
from tests.server.integration import test_sso_login as _sso
from tests.server.integration.test_sso_login import (  # noqa: F401 - fixtures for the JIT re-run
    idp,
    org,
    rsa_key,
    verified_domain,
)

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


class TestSsoJitOnPostgres:
    """The SSO JIT callback adds a new User, its ExternalIdentity and its
    OrgMember in one request (both children sort before User)."""

    teardown_method = _sso.TestSsoLoginFlow.teardown_method
    _make_id_token = _sso.TestSsoLoginFlow._make_id_token
    test_jit_provision_new_user = _sso.TestSsoLoginFlow.test_jit_provision_new_user


async def test_create_org_without_stripe_on_postgres(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """POST /api/v1/org adds the Organization and the owner's OrgMember
    (OrgMember sorts first) on the no-Stripe path, which has no autoflushing
    UPDATE in between (GitHub #51)."""
    import uuid
    from datetime import datetime, timezone

    from sessionfs.server.auth.keys import generate_api_key, hash_api_key
    from sessionfs.server.db.models import ApiKey, OrgMember, User

    user = User(
        id=str(uuid.uuid4()), email="ent-no-stripe@example.com", tier="enterprise",
        email_verified=True, created_at=datetime.now(timezone.utc),
    )
    db_session.add(user)
    await db_session.commit()
    raw = generate_api_key()
    db_session.add(ApiKey(
        id=str(uuid.uuid4()), user_id=user.id, key_hash=hash_api_key(raw), name="k",
        created_at=datetime.now(timezone.utc),
    ))
    await db_session.commit()
    resp = await client.post(
        "/api/v1/org", headers={"Authorization": f"Bearer {raw}"},
        json={"name": "No Stripe", "slug": "no-stripe"},
    )
    assert resp.status_code == 200, resp.text
    member = (
        await db_session.execute(
            select(OrgMember).where(OrgMember.org_id == resp.json()["org_id"])
        )
    ).scalar_one()
    assert (member.user_id, member.role) == (user.id, "owner")
