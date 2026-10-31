"""Release recording, share-link creation, merge-check, the Review loops inbox
and inbound PR/release webhooks require the Team plan (``agent_tickets``).

Plan errors only reach callers who can see the project (an outsider keeps the
plain 404); reads, share list/revoke and the public share view stay open so a
downgraded project keeps its data.
"""

from __future__ import annotations

import json

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server.db.models import (
    LoopShareLink,
    Organization,
    PullRequest,
    Release,
)
from sessionfs.server.routes import webhooks
from tests.server.integration import test_delivery_webhooks as dw
from tests.server.integration import test_github_webhook_ingest as gi
from tests.server.integration import test_loop_needs_you as ny
from tests.server.integration import test_loop_share_links as sl


def _plan_error(resp) -> None:
    assert resp.status_code == 403, resp.text
    assert "agent_tickets" in resp.text


async def _free_project(db: AsyncSession):
    user, key = await sl._make_user(db, "free")
    user.tier = "free"
    await db.commit()
    return user, key, await sl._make_project(db, user)


@pytest.mark.asyncio
async def test_release_writes_gated_reads_open(
    client: AsyncClient, db_session: AsyncSession
):
    owner, key, project = await _free_project(db_session)
    base = f"/api/v1/projects/{project.id}"
    h = sl._hdrs(key)

    for path, body in (
        ("/pull-requests", {"provider": "github", "repo": "a/b", "number": 1, "title": "t"}),
        ("/releases", {"version": "v1", "source": "manual", "tag_sha": "a" * 40}),
        ("/releases/v1/gates", {"kind": "ci", "status": "passed"}),
        ("/releases/v1/deployments", {"environment": "prod", "source": "manual"}),
        ("/releases/v1/tickets", {"ticket_ids": ["tk_x"]}),
    ):
        _plan_error(await client.post(base + path, json=body, headers=h))

    assert (await client.get(base + "/releases", headers=h)).status_code == 200
    assert (await client.get(base + "/ship-report", headers=h)).status_code != 403

    # Outsider keeps the plain 404, not the plan error.
    _, other = await sl._make_user(db_session, "outsider")
    r = await client.post(
        base + "/releases", json={"version": "v1", "source": "manual", "tag_sha": "a" * 40}, headers=sl._hdrs(other)
    )
    assert r.status_code == 404, r.text
    assert "agent_tickets" not in r.text


@pytest.mark.asyncio
async def test_merge_check_gated(client: AsyncClient, db_session: AsyncSession):
    owner, key, project = await _free_project(db_session)
    r = await client.post(
        f"/api/v1/projects/{project.id}/tickets/tk_x/merge-check", headers=sl._hdrs(key)
    )
    _plan_error(r)
    _, other = await sl._make_user(db_session, "outsider")
    r = await client.post(
        f"/api/v1/projects/{project.id}/tickets/tk_x/merge-check", headers=sl._hdrs(other)
    )
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_share_create_gated_list_revoke_open(
    client: AsyncClient, db_session: AsyncSession
):
    owner, key, project = await _free_project(db_session)
    h = sl._hdrs(key)
    url = f"/api/v1/projects/{project.id}/loop-share-links"
    _plan_error(
        await client.post(url, json={"kind": "ship_report", "month": "2026-10"}, headers=h)
    )
    assert (await client.get(url, headers=h)).json() == []

    # A link minted before a downgrade can still be listed, revoked and viewed.
    import hashlib

    link = LoopShareLink(
        id="lsl_downgrade0001",
        project_id=project.id,
        kind="ship_report",
        month="2026-10",
        token_hash=hashlib.sha256(b"tok").hexdigest(),
        expires_at=__import__("datetime").datetime.now(__import__("datetime").timezone.utc)
        + __import__("datetime").timedelta(days=1),
    )
    db_session.add(link)
    await db_session.commit()
    assert len((await client.get(url, headers=h)).json()) == 1
    pub = await client.get("/api/v1/share/loop/tok")
    assert pub.status_code == 200, pub.text
    assert (await client.delete(f"{url}/{link.id}", headers=h)).status_code == 204

    _, other = await sl._make_user(db_session, "outsider")
    r = await client.post(
        url, json={"kind": "ship_report", "month": "2026-10"}, headers=sl._hdrs(other)
    )
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_needs_you_excludes_free_projects(
    client: AsyncClient, db_session: AsyncSession
):
    owner, key = await ny._make_user(db_session)  # team
    team_project = await ny._make_project(db_session, owner)
    _, children = await ny._make_parent(
        db_session, team_project, owner, {"reviewer_persona": "reviewer"}, n=1
    )
    await ny._seed_ready_child(db_session, team_project, owner, children[0])
    await ny._make_proposed_item(db_session, team_project, slug="paid")

    # A project the same user can read whose owner is on the free plan.
    free_owner, _ = await ny._make_user(db_session, name="freebie", tier="free")
    free_project = await ny._make_project(db_session, free_owner)
    # Same user key reads it only through ownership; use the free owner's key.
    _, fchildren = await ny._make_parent(
        db_session, free_project, free_owner, {"reviewer_persona": "reviewer"}, n=1
    )
    await ny._seed_ready_child(db_session, free_project, free_owner, fchildren[0])
    await ny._make_proposed_item(db_session, free_project, slug="unpaid")

    body = (await client.get("/api/v1/me/loops/needs-you", headers=ny._hdrs(key))).json()
    assert {i["project_id"] for i in body["items"]} == {team_project.id}
    assert body["counts"]["total"] == 2


@pytest.mark.asyncio
async def test_needs_you_free_user_sees_nothing(
    client: AsyncClient, db_session: AsyncSession
):
    owner, key = await ny._make_user(db_session, name="freeone", tier="free")
    project = await ny._make_project(db_session, owner)
    _, children = await ny._make_parent(
        db_session, project, owner, {"reviewer_persona": "reviewer"}, n=1
    )
    await ny._seed_ready_child(db_session, project, owner, children[0])
    body = (await client.get("/api/v1/me/loops/needs-you", headers=ny._hdrs(key))).json()
    assert body["items"] == []
    assert body["counts"]["total"] == 0


@pytest.mark.asyncio
async def test_app_webhooks_skip_free_projects(
    client: AsyncClient, db_session: AsyncSession
):
    owner, _ = await gi._make_user(db_session)
    owner.tier = "free"
    await db_session.commit()
    project = await gi._make_project(db_session, owner)
    ticket = await gi._make_ticket(db_session, project, owner)

    assert await webhooks._handle_pr_ingest(gi._pr_payload(ticket.id)) is True
    assert (await db_session.execute(select(PullRequest))).scalars().all() == []

    rel = gi._release_payload("v1.0.0", gi.HEAD)
    assert await webhooks._handle_release_event(rel) is True
    assert (await db_session.execute(select(Release))).scalars().all() == []


@pytest.mark.asyncio
async def test_delivery_webhook_skips_free_org(
    client: AsyncClient, db_session: AsyncSession
):
    owner, key = await dw._make_user(db_session, "owner")
    org = await dw._make_org(db_session)
    await dw._add_org_member(db_session, org, owner, "owner")
    project = await dw._make_project(db_session, owner, org, "acme/widgets")
    ticket = await dw._make_ticket(db_session, project, owner)
    secret = (await dw._rotate(client, key, org.id, "github"))["webhook_secret"]

    # Downgrade the org after setup.
    row = await db_session.get(Organization, org.id)
    row.tier = "free"
    await db_session.commit()

    payload = json.dumps(
        {
            "action": "opened",
            "repository": {"clone_url": "https://github.com/Acme/widgets"},
            "pull_request": {
                "id": 1,
                "number": 7,
                "title": f"ship {ticket.id}",
                "state": "open",
                "head": {"ref": "feat/x", "sha": "aaa111"},
                "html_url": "https://github.com/Acme/widgets/pull/7",
                "body": "",
            },
        }
    ).encode()
    resp = await client.post(
        f"/api/v1/webhooks/github/{org.id}",
        content=payload,
        headers={
            "content-type": "application/json",
            "x-github-event": "pull_request",
            "x-github-delivery": "free-1",
            "x-hub-signature-256": dw._github_sig(secret, payload),
        },
    )
    assert resp.status_code == 200, resp.text
    assert (await db_session.execute(select(PullRequest))).scalars().all() == []
