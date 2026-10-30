"""The CLI's hosted-marketplace client against the REAL FastAPI app.

The client's error parsing (`sessionfs.skills.hosted.parse_api_error`) must
understand the envelope the server actually emits, not a hand-written body:
a service key calling `/api/v1/auth/me` is rejected with
`service_key_not_allowed`, and the client must recognise it as a service
identity.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

import httpx
import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server.auth.keys import generate_api_key, hash_api_key
from sessionfs.server.db.models import ApiKey, Organization, User
from sessionfs.skills.hosted import (
    HostedClient,
    HostedUnavailable,
    ServiceKeyNotAllowed,
    parse_api_error,
)


async def _service_key(db_session: AsyncSession, user: User) -> str:
    org = Organization(
        id=f"org_{uuid.uuid4().hex[:8]}",
        name="Hosted Client Org",
        slug=f"o-{uuid.uuid4().hex[:6]}",
        tier="team",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(org)
    await db_session.flush()
    raw = generate_api_key()
    db_session.add(
        ApiKey(
            id=str(uuid.uuid4()),
            user_id=user.id,
            key_hash=hash_api_key(raw),
            name="svc",
            is_active=True,
            key_kind="service",
            org_id=org.id,
            scopes=json.dumps(["marketplace:read", "skills:read"]),
            created_by_user_id=user.id,
            service_key_name="svc",
            created_at=datetime.now(timezone.utc),
        )
    )
    await db_session.commit()
    return raw


def _replay(status: int, body: bytes, content_type: str) -> HostedClient:
    """A client whose transport replays the real app's captured response."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, content=body, headers={"content-type": content_type})

    return HostedClient("http://test", "k", transport=httpx.MockTransport(handler))


@pytest.mark.asyncio
async def test_real_service_key_rejection_is_recognised(
    client: AsyncClient, db_session: AsyncSession, test_user: User
):
    raw = await _service_key(db_session, test_user)
    response = await client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {raw}"})
    assert response.status_code == 403
    assert parse_api_error(response).code == "service_key_not_allowed"
    hosted = _replay(
        response.status_code, response.content, response.headers.get("content-type", "")
    )
    assert hosted.identity() == {"kind": "service", "user_id": None}


@pytest.mark.asyncio
async def test_real_user_key_identity_and_service_refusal_on_user_routes(
    client: AsyncClient, db_session: AsyncSession, test_user: User, auth_headers: dict
):
    me = await client.get("/api/v1/auth/me", headers=auth_headers)
    assert me.status_code == 200
    hosted = _replay(me.status_code, me.content, me.headers.get("content-type", ""))
    assert hosted.identity() == {"kind": "user", "user_id": test_user.id}

    # The older project-by-remote lookup now admits service keys holding
    # knowledge:read; a marketplace-only key gets a 403 insufficient_scope,
    # which the client raises as an authorization failure (never a silent
    # "no project").
    raw = await _service_key(db_session, test_user)
    lookup = await client.get(
        "/api/v1/projects/github.com/acme/repo", headers={"Authorization": f"Bearer {raw}"}
    )
    assert lookup.status_code == 403
    assert "insufficient_scope" in lookup.text
    replay = _replay(lookup.status_code, lookup.content, lookup.headers.get("content-type", ""))
    with pytest.raises(HostedUnavailable) as raised:
        replay._legacy_project_id_for_remote("github.com/acme/repo")
    assert not isinstance(raised.value, ServiceKeyNotAllowed)

    # The user-only rejection still maps to ServiceKeyNotAllowed.
    refused = _replay(
        403,
        b'{"detail": {"code": "service_key_not_allowed", "error": "service_key_not_allowed"}}',
        "application/json",
    )
    with pytest.raises(ServiceKeyNotAllowed):
        refused._legacy_project_id_for_remote("github.com/acme/repo")


@pytest.mark.asyncio
async def test_real_route_not_found_is_told_apart_from_project_not_found(client: AsyncClient):
    from sessionfs.skills.hosted import _is_route_missing

    missing = await client.get("/api/v1/no-such-route-for-hosted-client")
    assert missing.status_code == 404
    assert _is_route_missing(missing)
    not_found = httpx.Response(404, json={"error": {"code": "404", "message": "Project not found"}})
    assert not _is_route_missing(not_found)


@pytest.mark.asyncio
async def test_real_listings_flag_omitted_grants_and_the_client_reads_it(
    client: AsyncClient, db_session: AsyncSession, test_user: User, auth_headers: dict
):
    """The server omits grants whose stored targeting is unreadable and says
    so (`incomplete` / `omitted_count`); the client carries that flag to
    pull, which then treats the source as incomplete."""
    from sessionfs.server.db.models import OrgSkillAssignment, SkillInstallRequest
    from tests.server.integration.test_marketplace_moderation import _org, _publish_org

    org, project = await _org(db_session, test_user)
    org_id, project_id = org.id, project.id
    pkg = await _publish_org(client, auth_headers, org)
    created = await client.post(
        f"/api/v1/orgs/{org_id}/skills/assignments",
        headers=auth_headers,
        json={"version_id": pkg["version_id"], "content_hash": pkg["content_hash"],
              "target_type": "org"},
    )
    assert created.status_code == 201, created.text
    requests_url = f"/api/v1/projects/{project_id}/skill-install-requests"
    filed = await client.post(
        requests_url, headers=auth_headers,
        json={"version_id": pkg["version_id"], "content_hash": pkg["content_hash"]},
    )
    assert filed.status_code == 201, filed.text

    def _capture(response: httpx.Response) -> HostedClient:
        return _replay(
            response.status_code, response.content, response.headers.get("content-type", "")
        )

    # Clean records: complete listings.
    listing = _capture(
        await client.get("/api/v1/users/me/skills/assignments", headers=auth_headers)
    ).assignments()
    assert (listing.server_incomplete, listing.omitted_count) == (False, 0)
    assert len(listing) == 1

    assignment = await db_session.get(OrgSkillAssignment, created.json()["id"])
    assignment.target_tools = "null"
    request = await db_session.get(SkillInstallRequest, filed.json()["id"])
    request.requested_capabilities = "not json"
    await db_session.commit()

    listing = _capture(
        await client.get("/api/v1/users/me/skills/assignments", headers=auth_headers)
    ).assignments()
    assert list(listing) == []
    assert (listing.server_incomplete, listing.omitted_count) == (True, 1)
    requests = _capture(await client.get(requests_url, headers=auth_headers)).approved_install_requests(
        project_id
    )
    assert (requests.server_incomplete, requests.omitted_count) == (True, 1)
    # A direct read of the corrupt record is the server's 500 envelope.
    single = await client.get(f"{requests_url}/{filed.json()['id']}", headers=auth_headers)
    assert single.status_code == 500
    assert parse_api_error(single).code == "grant_record_corrupt"


@pytest.mark.asyncio
async def test_the_client_pages_through_the_real_grant_listing(
    client: AsyncClient, db_session: AsyncSession, test_user: User, auth_headers: dict,
    monkeypatch: pytest.MonkeyPatch,
):
    """Real pages (limit=1) replayed through the hosted client: it follows
    next_cursor until has_more is false and collects every row."""
    from sessionfs.skills import hosted
    from tests.server.integration.test_marketplace_moderation import _org, _publish_org

    org, _project = await _org(db_session, test_user)
    pkg = await _publish_org(client, auth_headers, org)
    for target in ({"target_type": "user", "target_id": test_user.id}, {"target_type": "org"}):
        created = await client.post(
            f"/api/v1/orgs/{org.id}/skills/assignments",
            headers=auth_headers,
            json={"version_id": pkg["version_id"], "content_hash": pkg["content_hash"],
                  "target_tools": ["codex"], **target},
        )
        assert created.status_code == 201, created.text
    pages: dict[str | None, httpx.Response] = {}
    cursor: str | None = None
    while True:
        params = {"limit": "1", **({"cursor": cursor} if cursor else {})}
        response = await client.get(
            "/api/v1/users/me/skills/assignments", headers=auth_headers, params=params
        )
        assert response.status_code == 200, response.text
        pages[cursor] = response
        body = response.json()
        assert set(body) >= {"items", "incomplete", "omitted_count", "has_more", "next_cursor"}
        if not body["has_more"]:
            break
        cursor = body["next_cursor"]
    assert len(pages) == 2

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params.get("limit") == "1"
        real = pages[request.url.params.get("cursor")]
        return httpx.Response(
            real.status_code, content=real.content,
            headers={"content-type": real.headers.get("content-type", "")},
        )

    monkeypatch.setattr(hosted, "GRANT_PAGE_LIMIT", 1)
    replay = HostedClient("http://test", "k", transport=httpx.MockTransport(handler))
    listing = replay.assignments()
    assert len(listing) == 2 and not listing.truncated and not listing.server_incomplete
