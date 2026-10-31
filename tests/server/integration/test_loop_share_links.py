"""Review Loop R3 (Brief C3) — loop share links tests.

Covers the editor mint/list/revoke routes end to end and the unauthenticated
public view: the raw token is returned once and never stored, the public story
is DLP-redacted with dollars and every identity stripped, every public miss is
one identical 404, service-key scope + cross-org boundary hold, and validation
rejects malformed bodies.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server.auth.keys import generate_api_key, hash_api_key
from sessionfs.server.db.models import (
    AgentRun,
    ApiKey,
    LoopShareLink,
    Organization,
    Project,
    Session,
    Ticket,
    User,
)

EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
NOT_FOUND = {"error": {"code": "404", "message": "Share link not found", "details": {}}}


def _hdrs(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


async def _make_user(
    db: AsyncSession, name: str = "alice"
) -> tuple[User, str]:
    user = User(
        id=str(uuid.uuid4()),
        email=f"{name}-{uuid.uuid4().hex[:6]}@example.com",
        display_name=name.capitalize(),
        tier="team",
        email_verified=True,
        created_at=datetime.now(timezone.utc),
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    raw = generate_api_key()
    db.add(
        ApiKey(
            id=str(uuid.uuid4()),
            user_id=user.id,
            key_hash=hash_api_key(raw),
            name=f"{name}-key",
            created_at=datetime.now(timezone.utc),
        )
    )
    await db.commit()
    return user, raw


async def _make_project(
    db: AsyncSession, owner: User, *, org_id: str | None = None
) -> Project:
    project = Project(
        id=f"proj_{uuid.uuid4().hex[:16]}",
        name=f"share-{uuid.uuid4().hex[:6]}",
        git_remote_normalized=f"acme/share-{uuid.uuid4().hex[:6]}",
        context_document="",
        owner_id=owner.id,
        org_id=org_id,
    )
    db.add(project)
    await db.commit()
    await db.refresh(project)
    return project


async def _make_ticket(
    db: AsyncSession, project: Project, owner: User, *, title: str
) -> Ticket:
    ticket = Ticket(
        id=f"tk_{uuid.uuid4().hex[:16]}",
        project_id=project.id,
        title=title,
        kind="task",
        status="open",
        created_by_user_id=owner.id,
    )
    db.add(ticket)
    await db.commit()
    await db.refresh(ticket)
    return ticket


async def _make_run(
    db: AsyncSession,
    project: Project,
    ticket_id: str,
    *,
    result_summary: str | None = None,
    findings: str = "[]",
) -> AgentRun:
    run = AgentRun(
        id=f"run_{uuid.uuid4().hex[:16]}",
        project_id=project.id,
        persona_name="atlas",
        role="implementer",
        ticket_id=ticket_id,
        input_tokens=100,
        result_summary=result_summary,
        findings=findings,
        created_at=datetime.now(timezone.utc),
    )
    db.add(run)
    await db.commit()
    await db.refresh(run)
    return run


def _all_strings(obj):
    """Yield every key and every string value in the payload, recursively."""
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for key, value in obj.items():
            yield key
            yield from _all_strings(value)
    elif isinstance(obj, list):
        for item in obj:
            yield from _all_strings(item)


# ── Mint + public view ──


@pytest.mark.asyncio
async def test_create_story_link_returns_token_once_and_url(
    client: AsyncClient, db_session: AsyncSession
):
    owner, key = await _make_user(db_session)
    project = await _make_project(db_session, owner)
    ticket = await _make_ticket(db_session, project, owner, title="Fix a leak")

    resp = await client.post(
        f"/api/v1/projects/{project.id}/loop-share-links",
        headers=_hdrs(key),
        json={"kind": "story", "ticket_id": ticket.id, "expires_days": 7},
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["kind"] == "story"
    assert body["ticket_id"] == ticket.id
    assert body["token"]
    assert body["url"].endswith(f"/share/loop/{body['token']}")
    assert "expires_at" in body

    # The raw token is never stored — only its SHA-256.
    link = (
        await db_session.execute(select(LoopShareLink).where(LoopShareLink.id == body["id"]))
    ).scalar_one()
    assert link.token_hash == _hash(body["token"])
    assert link.token_hash != body["token"]
    assert link.created_by_user_id == owner.id
    assert link.created_actor_type == "user"

    # Listing never returns the token.
    listing = (
        await client.get(
            f"/api/v1/projects/{project.id}/loop-share-links", headers=_hdrs(key)
        )
    ).json()
    assert len(listing) == 1
    assert listing[0]["id"] == body["id"]
    assert "token" not in listing[0]
    assert "token_hash" not in listing[0]


@pytest.mark.asyncio
async def test_public_view_serves_redacted_story(
    client: AsyncClient, db_session: AsyncSession
):
    owner, key = await _make_user(db_session)
    project = await _make_project(db_session, owner)
    ticket = await _make_ticket(
        db_session, project, owner, title="Fix alice@example.com auth leak"
    )
    await _make_run(
        db_session,
        project,
        ticket.id,
        result_summary="Reviewed bob@example.com credentials",
        findings=json.dumps(
            [{"title": "carol@example.com in logs", "round": 1, "status": "fixed"}]
        ),
    )

    resp = await client.post(
        f"/api/v1/projects/{project.id}/loop-share-links",
        headers=_hdrs(key),
        json={"kind": "story", "ticket_id": ticket.id},
    )
    token = resp.json()["token"]

    public = await client.get(f"/api/v1/share/loop/{token}")
    assert public.status_code == 200, public.text
    assert public.headers["cache-control"] == "no-store"
    assert public.headers["x-robots-tag"] == "noindex"
    body = public.json()

    # No auth needed; the story structure is the existing ticket_story shape.
    assert body["ticket_id"] == ticket.id
    assert isinstance(body["events"], list)

    # Walk the WHOLE payload: no raw email, no dollar/cost keys, no user ids.
    for string in _all_strings(body):
        assert not EMAIL_RE.search(string), f"raw email leaked: {string!r}"
        assert string != owner.id, "user id leaked"

    def _keys(obj):
        if isinstance(obj, dict):
            for k, v in obj.items():
                yield k
                yield from _keys(v)
        elif isinstance(obj, list):
            for item in obj:
                yield from _keys(item)

    for key in _keys(body):
        lowered = key.lower()
        assert "usd" not in lowered and "cost" not in lowered, key

    # The emails were redacted, not silently dropped.
    assert "[REDACTED:email]" in json.dumps(body)


@pytest.mark.asyncio
async def test_public_view_constant_404_for_every_miss(
    client: AsyncClient, db_session: AsyncSession
):
    owner, key = await _make_user(db_session)
    project = await _make_project(db_session, owner)
    ticket = await _make_ticket(db_session, project, owner, title="A ticket")

    # Unknown token.
    resp = await client.get("/api/v1/share/loop/does-not-exist")
    assert resp.status_code == 404
    assert resp.json() == NOT_FOUND

    # Revoked.
    created = (
        await client.post(
            f"/api/v1/projects/{project.id}/loop-share-links",
            headers=_hdrs(key),
            json={"kind": "story", "ticket_id": ticket.id},
        )
    ).json()
    revoked_token = created["token"]
    await client.delete(
        f"/api/v1/projects/{project.id}/loop-share-links/{created['id']}",
        headers=_hdrs(key),
    )
    resp = await client.get(f"/api/v1/share/loop/{revoked_token}")
    assert resp.status_code == 404
    assert resp.json() == NOT_FOUND

    # Expired.
    expired_token = "expired-token-1"
    db_session.add(
        LoopShareLink(
            id=f"lsl_{uuid.uuid4().hex[:16]}",
            project_id=project.id,
            kind="story",
            ticket_id=ticket.id,
            token_hash=_hash(expired_token),
            created_by_user_id=owner.id,
            expires_at=datetime.now(timezone.utc) - timedelta(days=1),
        )
    )
    await db_session.commit()
    resp = await client.get(f"/api/v1/share/loop/{expired_token}")
    assert resp.status_code == 404
    assert resp.json() == NOT_FOUND

    # Ship report: no F-track releases table in this branch → same 404.
    ship = (
        await client.post(
            f"/api/v1/projects/{project.id}/loop-share-links",
            headers=_hdrs(key),
            json={"kind": "ship_report", "release_version": "0.30.0"},
        )
    ).json()
    resp = await client.get(f"/api/v1/share/loop/{ship['token']}")
    assert resp.status_code == 404
    assert resp.json() == NOT_FOUND


@pytest.mark.asyncio
async def test_revoke_guarded_update_and_ticket_deleted_404(
    client: AsyncClient, db_session: AsyncSession
):
    owner, key = await _make_user(db_session)
    project = await _make_project(db_session, owner)
    ticket = await _make_ticket(db_session, project, owner, title="Gone soon")

    created = (
        await client.post(
            f"/api/v1/projects/{project.id}/loop-share-links",
            headers=_hdrs(key),
            json={"kind": "story", "ticket_id": ticket.id},
        )
    ).json()

    # Revoking a link that isn't ours (or doesn't exist) is the same 404.
    resp = await client.delete(
        f"/api/v1/projects/{project.id}/loop-share-links/lsl_missing",
        headers=_hdrs(key),
    )
    assert resp.status_code == 404

    # A second revoke of the same link is also a miss (already revoked).
    await client.delete(
        f"/api/v1/projects/{project.id}/loop-share-links/{created['id']}",
        headers=_hdrs(key),
    )
    resp = await client.delete(
        f"/api/v1/projects/{project.id}/loop-share-links/{created['id']}",
        headers=_hdrs(key),
    )
    assert resp.status_code == 404

    # Deleting the ticket soft-nulls ticket_id → the public link now 404s.
    project2 = await _make_project(db_session, owner)
    ticket2 = await _make_ticket(db_session, project2, owner, title="Deleted")
    created2 = (
        await client.post(
            f"/api/v1/projects/{project2.id}/loop-share-links",
            headers=_hdrs(key),
            json={"kind": "story", "ticket_id": ticket2.id},
        )
    ).json()
    await db_session.delete(ticket2)
    await db_session.commit()
    resp = await client.get(f"/api/v1/share/loop/{created2['token']}")
    assert resp.status_code == 404
    assert resp.json() == NOT_FOUND


# ── Authorization ──


@pytest.mark.asyncio
async def test_editor_authorization(
    client: AsyncClient, db_session: AsyncSession
):
    owner, owner_key = await _make_user(db_session, name="owner")
    project = await _make_project(db_session, owner)
    ticket = await _make_ticket(db_session, project, owner, title="Private")

    # A captured-session reader can read the project but not mint a link.
    reader, reader_key = await _make_user(db_session, name="reader")
    db_session.add(
        Session(
            id=f"ses_{uuid.uuid4().hex[:16]}",
            user_id=reader.id,
            title="synced from the same repo",
            tags="[]",
            source_tool="claude-code",
            blob_key="x",
            blob_size_bytes=0,
            etag="x",
            git_remote_normalized=project.git_remote_normalized,
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
            uploaded_at=datetime.now(timezone.utc),
        )
    )
    await db_session.commit()

    resp = await client.post(
        f"/api/v1/projects/{project.id}/loop-share-links",
        headers=_hdrs(reader_key),
        json={"kind": "story", "ticket_id": ticket.id},
    )
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "project_edit_forbidden"

    # An outsider gets the byte-identical existence-oracle 404.
    outsider, outsider_key = await _make_user(db_session, name="outsider")
    resp = await client.post(
        f"/api/v1/projects/{project.id}/loop-share-links",
        headers=_hdrs(outsider_key),
        json={"kind": "story", "ticket_id": ticket.id},
    )
    assert resp.status_code == 404
    assert resp.json()["error"]["message"] == "Project not found"

    # The owner passes.
    resp = await client.post(
        f"/api/v1/projects/{project.id}/loop-share-links",
        headers=_hdrs(owner_key),
        json={"kind": "story", "ticket_id": ticket.id},
    )
    assert resp.status_code == 201, resp.text


async def _make_service_key(
    db: AsyncSession, admin: User, org: Organization, scopes: list[str]
) -> str:
    raw = generate_api_key()
    db.add(
        ApiKey(
            id=str(uuid.uuid4()),
            user_id=admin.id,
            key_hash=hash_api_key(raw),
            name="svc-key",
            is_active=True,
            key_kind="service",
            org_id=org.id,
            scopes=json.dumps(scopes),
            created_by_user_id=admin.id,
            service_key_name="svc-key",
            created_at=datetime.now(timezone.utc),
        )
    )
    await db.commit()
    return raw


@pytest.mark.asyncio
async def test_service_key_scope_and_cross_org(
    client: AsyncClient, db_session: AsyncSession
):
    admin, _ = await _make_user(db_session, name="admin")
    org = Organization(
        id=f"org_{uuid.uuid4().hex[:8]}",
        name="Org",
        slug=f"o-{uuid.uuid4().hex[:6]}",
        tier="team",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(org)
    await db_session.commit()

    project = await _make_project(db_session, admin, org_id=org.id)
    ticket = await _make_ticket(db_session, project, admin, title="In org")

    # Same-org key with loops:write mints; actor provenance is the key.
    svc_key = await _make_service_key(
        db_session, admin, org, ["loops:read", "loops:write"]
    )
    resp = await client.post(
        f"/api/v1/projects/{project.id}/loop-share-links",
        headers=_hdrs(svc_key),
        json={"kind": "story", "ticket_id": ticket.id},
    )
    assert resp.status_code == 201, resp.text
    link = (
        await db_session.execute(
            select(LoopShareLink).where(LoopShareLink.id == resp.json()["id"])
        )
    ).scalar_one()
    assert link.created_by_user_id is None
    assert link.created_actor_type == "service_key"
    assert link.created_service_key_name == "svc-key"

    # A cross-org service key gets the opaque 404 on the read/list path.
    other_org = Organization(
        id=f"org_{uuid.uuid4().hex[:8]}",
        name="Other",
        slug=f"o-{uuid.uuid4().hex[:6]}",
        tier="team",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(other_org)
    await db_session.commit()
    cross_key = await _make_service_key(
        db_session, admin, other_org, ["loops:read", "loops:write"]
    )
    resp = await client.get(
        f"/api/v1/projects/{project.id}/loop-share-links", headers=_hdrs(cross_key)
    )
    assert resp.status_code == 404
    assert resp.json()["error"]["message"] == "Project not found"

    # A service key without loops:write cannot mint (scope gate).
    readonly_key = await _make_service_key(db_session, admin, org, ["loops:read"])
    resp = await client.post(
        f"/api/v1/projects/{project.id}/loop-share-links",
        headers=_hdrs(readonly_key),
        json={"kind": "story", "ticket_id": ticket.id},
    )
    assert resp.status_code == 403
    assert resp.json()["error"]["details"]["error"] == "insufficient_scope"


# ── Validation ──


@pytest.mark.asyncio
async def test_create_validation(
    client: AsyncClient, db_session: AsyncSession
):
    owner, key = await _make_user(db_session)
    project = await _make_project(db_session, owner)
    ticket = await _make_ticket(db_session, project, owner, title="T")

    base = f"/api/v1/projects/{project.id}/loop-share-links"

    # story needs ticket_id, and rejects release_version/month.
    resp = await client.post(base, headers=_hdrs(key), json={"kind": "story"})
    assert resp.status_code == 422, resp.text
    resp = await client.post(
        base,
        headers=_hdrs(key),
        json={"kind": "story", "ticket_id": ticket.id, "month": "2026-10"},
    )
    assert resp.status_code == 422, resp.text

    # ship_report needs release_version or month, and rejects ticket_id.
    resp = await client.post(base, headers=_hdrs(key), json={"kind": "ship_report"})
    assert resp.status_code == 422, resp.text
    resp = await client.post(
        base,
        headers=_hdrs(key),
        json={"kind": "ship_report", "ticket_id": ticket.id, "release_version": "0.30.0"},
    )
    assert resp.status_code == 422, resp.text

    # Bad kind, bad month shape, and expires_days out of range.
    resp = await client.post(
        base,
        headers=_hdrs(key),
        json={"kind": "nope", "ticket_id": ticket.id},
    )
    assert resp.status_code == 422, resp.text
    resp = await client.post(
        base,
        headers=_hdrs(key),
        json={"kind": "ship_report", "month": "2026/10"},
    )
    assert resp.status_code == 422, resp.text
    resp = await client.post(
        base,
        headers=_hdrs(key),
        json={"kind": "story", "ticket_id": ticket.id, "expires_days": 91},
    )
    assert resp.status_code == 422, resp.text

    # A ship_report mint succeeds (the public view is what 404s it).
    resp = await client.post(
        base,
        headers=_hdrs(key),
        json={"kind": "ship_report", "month": "2026-10"},
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["month"] == "2026-10"


@pytest.mark.asyncio
async def test_public_view_serves_ship_report_without_dollars_or_identities(
    client: AsyncClient, db_session: AsyncSession
):
    from sessionfs.server.services import releases

    owner, key = await _make_user(db_session)
    project = await _make_project(db_session, owner)
    ticket = await _make_ticket(
        db_session, project, owner, title="Ship dave@example.com fix"
    )
    await releases.upsert_pull_request(
        db_session,
        project_id=project.id,
        provider="github",
        repo="acme/widgets",
        number=7,
        title=f"Fix auth ({ticket.id})",
        state="merged",
        author_user_id=owner.id,
        head_sha="a" * 40,
    )
    await releases.record_release(
        db_session,
        project_id=project.id,
        version="0.30.0",
        tag_sha="b" * 40,
        pr_numbers=[7],
        created_by_user_id=owner.id,
    )
    await db_session.commit()

    resp = await client.post(
        f"/api/v1/projects/{project.id}/loop-share-links",
        headers=_hdrs(key),
        json={"kind": "ship_report", "release_version": "0.30.0"},
    )
    assert resp.status_code == 201, resp.text
    public = await client.get(f"/api/v1/share/loop/{resp.json()['token']}")
    assert public.status_code == 200, public.text
    body = public.json()
    assert body["version"] == "0.30.0"
    assert body["prs"] and body["prs"][0]["number"] == 7

    payload = json.dumps(body)
    assert owner.id not in payload
    assert "author_user_id" not in payload and "created_by_user_id" not in payload
    assert "usd" not in payload.lower()
    for string in _all_strings(body):
        assert not EMAIL_RE.search(string), f"raw email leaked: {string!r}"

    # A ship report for a release that doesn't exist is the constant 404.
    missing = await client.post(
        f"/api/v1/projects/{project.id}/loop-share-links",
        headers=_hdrs(key),
        json={"kind": "ship_report", "release_version": "9.9.9"},
    )
    gone = await client.get(f"/api/v1/share/loop/{missing.json()['token']}")
    assert gone.status_code == 404
    assert gone.json() == NOT_FOUND


@pytest.mark.asyncio
async def test_public_404_keeps_no_store_headers_and_merged_project_refused(
    client: AsyncClient, db_session: AsyncSession
):
    owner, key = await _make_user(db_session)
    project = await _make_project(db_session, owner)
    ticket = await _make_ticket(db_session, project, owner, title="A ticket")

    resp = await client.get("/api/v1/share/loop/does-not-exist")
    assert resp.status_code == 404
    assert resp.headers["cache-control"] == "no-store"
    assert resp.headers["x-robots-tag"] == "noindex"

    created = (
        await client.post(
            f"/api/v1/projects/{project.id}/loop-share-links",
            headers=_hdrs(key),
            json={"kind": "story", "ticket_id": ticket.id},
        )
    ).json()
    ok = await client.get(f"/api/v1/share/loop/{created['token']}")
    assert ok.status_code == 200

    other = await _make_project(db_session, owner)
    project.merged_into_project_id = other.id
    await db_session.commit()
    resp = await client.get(f"/api/v1/share/loop/{created['token']}")
    assert resp.status_code == 404
    assert resp.json() == NOT_FOUND
    assert resp.headers["cache-control"] == "no-store"
