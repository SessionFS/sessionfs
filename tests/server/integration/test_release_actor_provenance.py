"""Actor provenance on release gates and deployments (migration 129).

Every write stamps who recorded the row from the credential, never the
body: a user key records the user, a service key records itself (id + name,
never its minter), and the delivery webhook ingest records ``webhook``. A
gate re-record (e.g. failed→passed) re-attributes the row.
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server.db.models import Deployment, Release, ReleaseGate
from sessionfs.server.services import delivery_ingest

from tests.server.integration.test_releases import (
    _add_org_member,
    _hdrs,
    _make_org,
    _make_project,
    _make_service_key,
    _make_user,
)

pytestmark = pytest.mark.asyncio


async def _release(client: AsyncClient, project, key: str, sha: str = "a" * 40) -> None:
    resp = await client.post(
        f"/api/v1/projects/{project.id}/releases",
        json={"version": "1.0.0", "tag_sha": sha},
        headers=_hdrs(key),
    )
    assert resp.status_code == 200, resp.text


async def _gate_row(db: AsyncSession, gate_id: str) -> ReleaseGate:
    return (
        await db.execute(
            select(ReleaseGate)
            .where(ReleaseGate.id == gate_id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one()


async def test_gate_actor_follows_the_credential_not_the_body(
    client: AsyncClient, db_session: AsyncSession
):
    admin, admin_key = await _make_user(db_session)
    org = await _make_org(db_session)
    await _add_org_member(db_session, org, admin, "admin")
    project = await _make_project(db_session, admin, org=org)
    admin_id = admin.id
    await _release(client, project, admin_key)
    base = f"/api/v1/projects/{project.id}/releases/1.0.0/gates"

    # A user key: recorded as the user. Spoofed actor fields in the body are
    # ignored.
    g1 = await client.post(
        base,
        json={
            "kind": "ci",
            "status": "failed",
            "actor_type": "webhook",
            "actor_user_id": "someone-else",
            "service_key_id": "fake",
        },
        headers=_hdrs(admin_key),
    )
    assert g1.status_code == 200, g1.text
    body = g1.json()
    assert body["actor_type"] == "user"
    assert body["actor_user_id"] == admin_id
    assert body["service_key_id"] is None
    assert "service_key_name" not in body
    row = await _gate_row(db_session, body["id"])
    assert (row.actor_type, row.actor_user_id, row.service_key_id, row.service_key_name) == (
        "user",
        admin_id,
        None,
        None,
    )

    # A service key flips it failed→passed: the flip is attributed to the key,
    # never to the minter.
    svc, svc_key = await _make_service_key(db_session, org, admin, scopes=["loops:write"])
    svc_id, svc_name = svc.id, svc.service_key_name
    g2 = await client.post(base, json={"kind": "ci", "status": "passed"}, headers=_hdrs(svc_key))
    assert g2.status_code == 200, g2.text
    flipped = g2.json()
    assert flipped["id"] == body["id"]
    assert flipped["status"] == "passed"
    assert flipped["actor_type"] == "service_key"
    assert flipped["actor_user_id"] is None
    assert flipped["service_key_id"] == svc_id
    row = await _gate_row(db_session, body["id"])
    assert row.actor_type == "service_key"
    assert row.actor_user_id is None
    assert row.service_key_id == svc_id
    assert row.service_key_name == svc_name


async def test_deployment_actor_for_user_and_service_key(
    client: AsyncClient, db_session: AsyncSession
):
    admin, admin_key = await _make_user(db_session)
    org = await _make_org(db_session)
    await _add_org_member(db_session, org, admin, "admin")
    project = await _make_project(db_session, admin, org=org)
    admin_id = admin.id
    await _release(client, project, admin_key)
    pid = project.id
    base = f"/api/v1/projects/{pid}/releases/1.0.0/deployments"

    d1 = await client.post(
        base,
        json={"environment": "prod", "actor_user_id": "spoof"},
        headers=_hdrs(admin_key),
    )
    assert d1.status_code == 200, d1.text
    assert d1.json()["actor_type"] == "user"
    assert d1.json()["actor_user_id"] == admin_id

    svc, svc_key = await _make_service_key(db_session, org, admin, scopes=["loops:write"])
    svc_id, svc_name = svc.id, svc.service_key_name
    d2 = await client.post(base, json={"environment": "prod"}, headers=_hdrs(svc_key))
    assert d2.status_code == 200, d2.text
    assert d2.json()["actor_type"] == "service_key"
    assert d2.json()["actor_user_id"] is None
    assert d2.json()["service_key_id"] == svc_id

    row = (
        await db_session.execute(
            select(Deployment)
            .where(Deployment.id == d2.json()["id"])
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    assert row.service_key_name == svc_name

    # The authenticated ship report shows the same actor fields.
    report = await client.get(
        f"/api/v1/projects/{pid}/releases/1.0.0/ship-report",
        headers=_hdrs(admin_key),
    )
    assert report.status_code == 200, report.text
    actors = {(d["actor_type"], d["actor_user_id"]) for d in report.json()["deployments"]}
    assert actors == {("user", admin_id), ("service_key", None)}


async def test_webhook_deployment_is_stamped_webhook(client: AsyncClient, db_session: AsyncSession):
    admin, admin_key = await _make_user(db_session)
    org = await _make_org(db_session)
    await _add_org_member(db_session, org, admin, "admin")
    project = await _make_project(db_session, admin, org=org)
    sha = "c" * 40
    await _release(client, project, admin_key, sha=sha)

    result = await delivery_ingest._ingest_gitlab_deployment(
        db_session,
        project,
        {"status": "success", "environment": "staging", "sha": sha},
    )
    assert result == {"status": "ok"}
    release = (
        await db_session.execute(select(Release).where(Release.project_id == project.id))
    ).scalar_one()
    row = (
        await db_session.execute(select(Deployment).where(Deployment.release_id == release.id))
    ).scalar_one()
    assert row.actor_type == "webhook"
    assert row.actor_user_id is None
    assert row.service_key_id is None
    assert row.service_key_name is None
