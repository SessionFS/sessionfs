"""Per-org delivery webhooks (track F2) — API + ingest tests.

Covers the org-scoped rotate/status endpoints and the public GitHub/GitLab
inbound webhooks end to end against the shared test engine:
- rotate returns the plaintext once (status only ever shows ``secret_last4``);
  member (non-admin) is 403; unknown provider is 422
- GitHub: 401 without a secret; good/bad/missing signature; unknown org answers
  the *same* 401 as a bad signature (no existence oracle); 1 MiB body cap;
  duplicate ``X-GitHub-Delivery``; PR upsert + ticket auto-link; release +
  deployment rows; a cross-org repo is ignored (never mapped)
- GitLab: ``X-Gitlab-Token`` constant-time compare; merge-request → PR
"""

from __future__ import annotations

import hashlib
import hmac
import json
import uuid
from datetime import datetime, timezone

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server.auth.keys import generate_api_key, hash_api_key
from sessionfs.server.db.models import (
    ApiKey,
    Deployment,
    OrgDeliveryWebhook,
    OrgMember,
    Organization,
    Project,
    PullRequest,
    Release,
    Ticket,
    TicketPrLink,
    User,
)
from sessionfs.server.llm_keys import decrypt_secret


# ── helpers ─────────────────────────────────────────────────────────


async def _make_user(db: AsyncSession, name: str = "alice") -> tuple[User, str]:
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


def _hdrs(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


async def _make_org(db: AsyncSession) -> Organization:
    org = Organization(
        id=f"org_{uuid.uuid4().hex[:8]}",
        name=f"Org-{uuid.uuid4().hex[:6]}",
        slug=f"o-{uuid.uuid4().hex[:6]}",
        tier="team",
        created_at=datetime.now(timezone.utc),
    )
    db.add(org)
    await db.commit()
    await db.refresh(org)
    return org


async def _add_org_member(
    db: AsyncSession, org: Organization, user: User, role: str
) -> None:
    db.add(
        OrgMember(
            org_id=org.id,
            user_id=user.id,
            role=role,
            joined_at=datetime.now(timezone.utc),
        )
    )
    await db.commit()


async def _make_project(
    db: AsyncSession, owner: User, org: Organization | None, remote: str
) -> Project:
    project = Project(
        id=f"proj_{uuid.uuid4().hex[:16]}",
        name=f"dw-{uuid.uuid4().hex[:6]}",
        git_remote_normalized=remote,
        context_document="",
        owner_id=owner.id,
        org_id=org.id if org else None,
    )
    db.add(project)
    await db.commit()
    await db.refresh(project)
    return project


async def _make_ticket(
    db: AsyncSession, project: Project, owner: User
) -> Ticket:
    ticket = Ticket(
        id=f"tk_{uuid.uuid4().hex[:16]}",
        project_id=project.id,
        title=f"dw ticket {uuid.uuid4().hex[:6]}",
        description="delivery webhook coverage",
        created_by_user_id=owner.id,
        status="open",
    )
    db.add(ticket)
    await db.commit()
    await db.refresh(ticket)
    return ticket


async def _rotate(
    client: AsyncClient, key: str, org_id: str, provider: str
) -> dict:
    resp = await client.post(
        f"/api/v1/orgs/{org_id}/delivery-webhooks/{provider}/rotate",
        headers=_hdrs(key),
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def _github_sig(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


# ── rotate / status ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_rotate_and_status_returns_secret_once(
    client: AsyncClient, db_session: AsyncSession
):
    owner, key = await _make_user(db_session, "owner")
    org = await _make_org(db_session)
    await _add_org_member(db_session, org, owner, "owner")

    rotated = await _rotate(client, key, org.id, "github")
    secret = rotated["webhook_secret"]
    assert secret
    assert rotated["webhook_secret_last4"] == secret[-4:]
    assert rotated["webhook_url"] == f"http://test/api/v1/webhooks/github/{org.id}"

    # The plaintext is stored Fernet-encrypted, never in cleartext.
    row = (
        await db_session.execute(
            select(OrgDeliveryWebhook).where(
                OrgDeliveryWebhook.org_id == org.id,
                OrgDeliveryWebhook.provider == "github",
            )
        )
    ).scalar_one()
    assert row.encrypted_secret != secret
    assert decrypt_secret(row.encrypted_secret) == secret
    assert row.secret_last4 == secret[-4:]

    status = await client.get(
        f"/api/v1/orgs/{org.id}/delivery-webhooks/github/status", headers=_hdrs(key)
    )
    assert status.status_code == 200, status.text
    body = status.json()
    assert body["has_secret"] is True
    assert body["secret_last4"] == secret[-4:]
    assert "webhook_secret" not in body


@pytest.mark.asyncio
async def test_rotate_member_forbidden(client: AsyncClient, db_session: AsyncSession):
    member, key = await _make_user(db_session, "member")
    org = await _make_org(db_session)
    await _add_org_member(db_session, org, member, "member")

    resp = await client.post(
        f"/api/v1/orgs/{org.id}/delivery-webhooks/github/rotate", headers=_hdrs(key)
    )
    assert resp.status_code == 403, resp.text
    assert resp.json()["error"]["details"]["error"] == "insufficient_role"


@pytest.mark.asyncio
async def test_rotate_unknown_provider_422(client: AsyncClient, db_session: AsyncSession):
    owner, key = await _make_user(db_session, "owner")
    org = await _make_org(db_session)
    await _add_org_member(db_session, org, owner, "owner")

    resp = await client.post(
        f"/api/v1/orgs/{org.id}/delivery-webhooks/bitbucket/rotate", headers=_hdrs(key)
    )
    assert resp.status_code == 422, resp.text


# ── GitHub inbound ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_github_inbound_no_secret_is_same_401_as_unknown_org(
    client: AsyncClient, db_session: AsyncSession
):
    org = await _make_org(db_session)
    resp = await client.post(
        f"/api/v1/webhooks/github/{org.id}",
        content=b"{}",
        headers={"content-type": "application/json", "x-github-event": "ping"},
    )
    assert resp.status_code == 401, resp.text
    unknown = await client.post(
        "/api/v1/webhooks/github/org_does_not_exist",
        content=b"{}",
        headers={"content-type": "application/json", "x-github-event": "ping"},
    )
    assert unknown.status_code == 401
    assert unknown.json() == resp.json()


@pytest.mark.asyncio
async def test_github_inbound_signature_good_bad_missing(
    client: AsyncClient, db_session: AsyncSession
):
    owner, key = await _make_user(db_session, "owner")
    org = await _make_org(db_session)
    await _add_org_member(db_session, org, owner, "owner")
    await _make_project(db_session, owner, org, "acme/widgets")
    secret = (await _rotate(client, key, org.id, "github"))["webhook_secret"]

    payload = json.dumps(
        {
            "action": "opened",
            "repository": {"clone_url": "https://github.com/Acme/widgets"},
            "pull_request": {
                "number": 1,
                "title": "fix",
                "state": "open",
                "head": {"ref": "main", "sha": "deadbeef"},
            },
        }
    ).encode()
    good = _github_sig(secret, payload)

    resp = await client.post(
        f"/api/v1/webhooks/github/{org.id}",
        content=payload,
        headers={
            "content-type": "application/json",
            "x-github-event": "pull_request",
            "x-hub-signature-256": good,
        },
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "ok"

    bad = _github_sig("wrong-secret", payload)
    for sig in (bad, "sha256=" + "0" * 64):
        resp = await client.post(
            f"/api/v1/webhooks/github/{org.id}",
            content=payload,
            headers={
                "content-type": "application/json",
                "x-github-event": "pull_request",
                "x-hub-signature-256": sig,
            },
        )
        assert resp.status_code == 401, resp.text

    # Missing signature fails the same way.
    resp = await client.post(
        f"/api/v1/webhooks/github/{org.id}",
        content=payload,
        headers={"content-type": "application/json", "x-github-event": "pull_request"},
    )
    assert resp.status_code == 401, resp.text


@pytest.mark.asyncio
async def test_github_inbound_unknown_org_matches_bad_signature(
    client: AsyncClient, db_session: AsyncSession
):
    # Unknown org and a bad signature are indistinguishable (no oracle).
    unknown_org = f"org_{uuid.uuid4().hex[:8]}"
    resp = await client.post(
        f"/api/v1/webhooks/github/{unknown_org}",
        content=b"{}",
        headers={
            "content-type": "application/json",
            "x-github-event": "ping",
            "x-hub-signature-256": "sha256=" + "0" * 64,
        },
    )
    assert resp.status_code == 401, resp.text

    owner, key = await _make_user(db_session, "owner")
    org = await _make_org(db_session)
    await _add_org_member(db_session, org, owner, "owner")
    await _rotate(client, key, org.id, "github")
    bad = await client.post(
        f"/api/v1/webhooks/github/{org.id}",
        content=b"{}",
        headers={
            "content-type": "application/json",
            "x-github-event": "ping",
            "x-hub-signature-256": "sha256=" + "0" * 64,
        },
    )
    assert bad.status_code == 401, bad.text
    assert resp.text == bad.text


@pytest.mark.asyncio
async def test_github_inbound_body_cap_413(client: AsyncClient, db_session: AsyncSession):
    owner, key = await _make_user(db_session, "owner")
    org = await _make_org(db_session)
    await _add_org_member(db_session, org, owner, "owner")
    await _rotate(client, key, org.id, "github")

    big = b"x" * (1024 * 1024 + 1)
    resp = await client.post(
        f"/api/v1/webhooks/github/{org.id}",
        content=big,
        headers={"content-type": "application/json", "x-github-event": "ping"},
    )
    assert resp.status_code == 413, resp.text


@pytest.mark.asyncio
async def test_github_inbound_duplicate_delivery(
    client: AsyncClient, db_session: AsyncSession
):
    owner, key = await _make_user(db_session, "owner")
    org = await _make_org(db_session)
    await _add_org_member(db_session, org, owner, "owner")
    await _make_project(db_session, owner, org, "acme/widgets")
    secret = (await _rotate(client, key, org.id, "github"))["webhook_secret"]

    payload = json.dumps(
        {
            "action": "opened",
            "repository": {"clone_url": "https://github.com/Acme/widgets"},
            "pull_request": {
                "number": 1,
                "title": "fix",
                "state": "open",
                "head": {"ref": "main", "sha": "deadbeef"},
            },
        }
    ).encode()
    headers = {
        "content-type": "application/json",
        "x-github-event": "pull_request",
        "x-github-delivery": "delivery-1",
        "x-hub-signature-256": _github_sig(secret, payload),
    }
    first = await client.post(f"/api/v1/webhooks/github/{org.id}", content=payload, headers=headers)
    assert first.status_code == 200, first.text
    second = await client.post(f"/api/v1/webhooks/github/{org.id}", content=payload, headers=headers)
    assert second.status_code == 200, second.text
    assert second.json() == {"status": "duplicate"}


@pytest.mark.asyncio
async def test_github_pr_upsert_and_autolink(
    client: AsyncClient, db_session: AsyncSession
):
    owner, key = await _make_user(db_session, "owner")
    org = await _make_org(db_session)
    await _add_org_member(db_session, org, owner, "owner")
    project = await _make_project(db_session, owner, org, "acme/widgets")
    ticket = await _make_ticket(db_session, project, owner)
    secret = (await _rotate(client, key, org.id, "github"))["webhook_secret"]

    payload = json.dumps(
        {
            "action": "opened",
            "repository": {"clone_url": "https://github.com/Acme/widgets"},
            "pull_request": {
                "id": 12345,
                "number": 7,
                "title": f"ship {ticket.id}",
                "state": "open",
                "head": {"ref": "feat/x", "sha": "aaa111"},
                "html_url": "https://github.com/Acme/widgets/pull/7",
                "body": "closes things",
            },
        }
    ).encode()
    resp = await client.post(
        f"/api/v1/webhooks/github/{org.id}",
        content=payload,
        headers={
            "content-type": "application/json",
            "x-github-event": "pull_request",
            "x-github-delivery": "delivery-pr-1",
            "x-hub-signature-256": _github_sig(secret, payload),
        },
    )
    assert resp.status_code == 200, resp.text

    pr = (
        await db_session.execute(
            select(PullRequest).where(
                PullRequest.project_id == project.id,
                PullRequest.provider == "github",
                PullRequest.number == 7,
            )
        )
    ).scalar_one()
    assert pr.title == f"ship {ticket.id}"
    assert pr.head_sha == "aaa111"
    assert pr.external_id == "12345"

    link = (
        await db_session.execute(
            select(TicketPrLink).where(TicketPrLink.ticket_id == ticket.id, TicketPrLink.pr_id == pr.id)
        )
    ).scalar_one()
    assert link.link_source == "auto_parsed"


@pytest.mark.asyncio
async def test_github_release_and_deployment(
    client: AsyncClient, db_session: AsyncSession
):
    owner, key = await _make_user(db_session, "owner")
    org = await _make_org(db_session)
    await _add_org_member(db_session, org, owner, "owner")
    project = await _make_project(db_session, owner, org, "acme/widgets")
    secret = (await _rotate(client, key, org.id, "github"))["webhook_secret"]
    sha = "beefcafe" * 5

    release_payload = json.dumps(
        {
            "action": "published",
            "repository": {"clone_url": "https://github.com/Acme/widgets"},
            "release": {
                "tag_name": "v1.0.0",
                "target_commitish": sha,
                "published_at": "2026-10-30T12:00:00Z",
            },
        }
    ).encode()
    resp = await client.post(
        f"/api/v1/webhooks/github/{org.id}",
        content=release_payload,
        headers={
            "content-type": "application/json",
            "x-github-event": "release",
            "x-github-delivery": "delivery-rel-1",
            "x-hub-signature-256": _github_sig(secret, release_payload),
        },
    )
    assert resp.status_code == 200, resp.text

    release = (
        await db_session.execute(
            select(Release).where(Release.project_id == project.id, Release.version == "v1.0.0")
        )
    ).scalar_one()
    assert release.source == "github_release"
    assert release.tag_sha == sha

    deploy_payload = json.dumps(
        {
            "action": "created",
            "repository": {"clone_url": "https://github.com/Acme/widgets"},
            "deployment": {"environment": "prod", "sha": sha},
            "deployment_status": {
                "state": "success",
                "environment": "prod",
                "created_at": "2026-10-30T12:05:00Z",
            },
        }
    ).encode()
    resp = await client.post(
        f"/api/v1/webhooks/github/{org.id}",
        content=deploy_payload,
        headers={
            "content-type": "application/json",
            "x-github-event": "deployment_status",
            "x-github-delivery": "delivery-deploy-1",
            "x-hub-signature-256": _github_sig(secret, deploy_payload),
        },
    )
    assert resp.status_code == 200, resp.text

    deployment = (
        await db_session.execute(
            select(Deployment).where(Deployment.release_id == release.id)
        )
    ).scalar_one()
    assert deployment.environment == "prod"


@pytest.mark.asyncio
async def test_cross_org_repo_ignored(
    client: AsyncClient, db_session: AsyncSession
):
    owner, key = await _make_user(db_session, "owner")
    org_a = await _make_org(db_session)
    org_b = await _make_org(db_session)
    await _add_org_member(db_session, org_a, owner, "owner")
    # The repo is owned by org_b; the webhook arrives for org_a.
    await _make_project(db_session, owner, org_b, "acme/widgets")
    secret = (await _rotate(client, key, org_a.id, "github"))["webhook_secret"]

    payload = json.dumps(
        {
            "action": "opened",
            "repository": {"clone_url": "https://github.com/Acme/widgets"},
            "pull_request": {
                "number": 3,
                "title": "fix",
                "state": "open",
                "head": {"ref": "main", "sha": "deadbeef"},
            },
        }
    ).encode()
    resp = await client.post(
        f"/api/v1/webhooks/github/{org_a.id}",
        content=payload,
        headers={
            "content-type": "application/json",
            "x-github-event": "pull_request",
            "x-github-delivery": "delivery-cross-1",
            "x-hub-signature-256": _github_sig(secret, payload),
        },
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"status": "ignored", "reason": "unmapped"}

    count = (
        await db_session.execute(
            select(PullRequest).where(PullRequest.provider == "github")
        )
    ).scalars().all()
    assert count == []


# ── GitLab inbound ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_gitlab_token_compare(client: AsyncClient, db_session: AsyncSession):
    owner, key = await _make_user(db_session, "owner")
    org = await _make_org(db_session)
    await _add_org_member(db_session, org, owner, "owner")
    await _make_project(db_session, owner, org, "acme/widgets")
    secret = (await _rotate(client, key, org.id, "gitlab"))["webhook_secret"]

    payload = json.dumps(
        {
            "object_kind": "merge_request",
            "project": {"git_http_url": "https://gitlab.com/acme/widgets"},
            "object_attributes": {
                "iid": 9,
                "title": "fix",
                "state": "opened",
                "source_branch": "feat/y",
                "last_commit": {"id": "cafe1234"},
            },
        }
    ).encode()

    resp = await client.post(
        f"/api/v1/webhooks/gitlab/{org.id}",
        content=payload,
        headers={
            "content-type": "application/json",
            "x-gitlab-token": secret,
            "x-gitlab-event-uuid": "gl-uuid-1",
        },
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "ok"

    bad = await client.post(
        f"/api/v1/webhooks/gitlab/{org.id}",
        content=payload,
        headers={
            "content-type": "application/json",
            "x-gitlab-token": "wrong-token",
            "x-gitlab-event-uuid": "gl-uuid-2",
        },
    )
    assert bad.status_code == 401, bad.text

    missing = await client.post(
        f"/api/v1/webhooks/gitlab/{org.id}",
        content=payload,
        headers={"content-type": "application/json", "x-gitlab-event-uuid": "gl-uuid-3"},
    )
    assert missing.status_code == 401, missing.text


@pytest.mark.asyncio
async def test_gitlab_mr_creates_pr(client: AsyncClient, db_session: AsyncSession):
    owner, key = await _make_user(db_session, "owner")
    org = await _make_org(db_session)
    await _add_org_member(db_session, org, owner, "owner")
    project = await _make_project(db_session, owner, org, "acme/widgets")
    secret = (await _rotate(client, key, org.id, "gitlab"))["webhook_secret"]

    payload = json.dumps(
        {
            "object_kind": "merge_request",
            "project": {"git_http_url": "https://gitlab.com/acme/widgets"},
            "object_attributes": {
                "id": 99,
                "iid": 9,
                "title": "fix the thing",
                "state": "opened",
                "source_branch": "feat/y",
                "last_commit": {"id": "cafe1234"},
                "url": "https://gitlab.com/acme/widgets/-/merge_requests/9",
                "description": "closes stuff",
            },
        }
    ).encode()
    resp = await client.post(
        f"/api/v1/webhooks/gitlab/{org.id}",
        content=payload,
        headers={
            "content-type": "application/json",
            "x-gitlab-token": secret,
            "x-gitlab-event-uuid": "gl-uuid-4",
        },
    )
    assert resp.status_code == 200, resp.text

    pr = (
        await db_session.execute(
            select(PullRequest).where(
                PullRequest.project_id == project.id,
                PullRequest.provider == "gitlab",
                PullRequest.number == 9,
            )
        )
    ).scalar_one()
    assert pr.title == "fix the thing"
    assert pr.head_sha == "cafe1234"
    assert pr.external_id == "99"


async def _post_github(client, secret, org_id, event, payload_dict, delivery):
    payload = json.dumps(payload_dict).encode()
    return await client.post(
        f"/api/v1/webhooks/github/{org_id}",
        content=payload,
        headers={
            "content-type": "application/json",
            "x-github-event": event,
            "x-github-delivery": delivery,
            "x-hub-signature-256": _github_sig(secret, payload),
        },
    )


@pytest.mark.asyncio
async def test_github_release_only_published_and_hex_sha_only(
    client: AsyncClient, db_session: AsyncSession
):
    owner, key = await _make_user(db_session, "owner")
    org = await _make_org(db_session)
    await _add_org_member(db_session, org, owner, "owner")
    project = await _make_project(db_session, owner, org, "acme/widgets")
    secret = (await _rotate(client, key, org.id, "github"))["webhook_secret"]
    repo = {"clone_url": "https://github.com/Acme/widgets"}

    # Draft / non-published actions are ignored.
    for i, action in enumerate(("created", "edited", "deleted", "prereleased")):
        r = await _post_github(
            client, secret, org.id, "release",
            {"action": action, "repository": repo,
             "release": {"tag_name": "v9", "target_commitish": "main"}},
            f"d-{i}",
        )
        assert r.status_code == 200 and r.json()["status"] == "ignored"
    assert (
        await db_session.execute(select(Release).where(Release.project_id == project.id))
    ).scalars().first() is None

    # A branch name is not a tag sha.
    r = await _post_github(
        client, secret, org.id, "release",
        {"action": "published", "repository": repo,
         "release": {"tag_name": "v1.2", "target_commitish": "main"}},
        "p-1",
    )
    assert r.json()["status"] == "ok"
    rel = (
        await db_session.execute(select(Release).where(Release.project_id == project.id))
    ).scalar_one()
    assert rel.tag_sha == ""

    # A later re-delivery with the real sha fills it in; deployments match.
    sha = "ab" * 20
    r = await _post_github(
        client, secret, org.id, "release",
        {"action": "released", "repository": repo,
         "release": {"tag_name": "v1.2", "target_commitish": sha.upper()}},
        "p-2",
    )
    await db_session.refresh(rel)
    assert rel.tag_sha == sha
    r = await _post_github(
        client, secret, org.id, "deployment_status",
        {"repository": repo, "deployment": {"environment": "prod", "sha": sha},
         "deployment_status": {"state": "success"}},
        "dep-1",
    )
    assert r.json()["status"] == "ok"


@pytest.mark.asyncio
async def test_github_pr_bad_fields_do_not_500(
    client: AsyncClient, db_session: AsyncSession
):
    owner, key = await _make_user(db_session, "owner")
    org = await _make_org(db_session)
    await _add_org_member(db_session, org, owner, "owner")
    project = await _make_project(db_session, owner, org, "acme/widgets")
    secret = (await _rotate(client, key, org.id, "github"))["webhook_secret"]
    repo = {"clone_url": "https://github.com/Acme/widgets"}

    r = await _post_github(
        client, secret, org.id, "pull_request",
        {"repository": repo, "pull_request": {"number": 2**40, "title": "x", "head": {}}},
        "n-1",
    )
    assert r.status_code == 200 and r.json()["status"] == "ignored"

    r = await _post_github(
        client, secret, org.id, "pull_request",
        {"repository": repo,
         "pull_request": {"number": 3, "title": "x\u0000y", "state": "open",
                          "html_url": "javascript:alert(1)",
                          "head": {"ref": "f\u0000b" + "z" * 400, "sha": "a" * 40}}},
        "n-2",
    )
    assert r.status_code == 200, r.text
    pr = (
        await db_session.execute(select(PullRequest).where(PullRequest.project_id == project.id))
    ).scalar_one()
    assert pr.external_url is None
    assert "\x00" not in pr.branch and len(pr.branch) <= 200


async def test_oversized_body_is_413_whatever_the_org(
    client: AsyncClient, db_session: AsyncSession
):
    # An oversized request must not tell a configured org from an unknown one.
    big = b"x" * (1024 * 1024 + 1)
    resp = await client.post(
        "/api/v1/webhooks/github/org_does_not_exist",
        content=big,
        headers={"content-type": "application/json", "x-github-event": "ping"},
    )
    assert resp.status_code == 413, resp.text


async def test_github_pr_non_string_title_does_not_500(
    client: AsyncClient, db_session: AsyncSession
):
    owner, key = await _make_user(db_session, "owner")
    org = await _make_org(db_session)
    await _add_org_member(db_session, org, owner, "owner")
    await _make_project(db_session, owner, org, "acme/widgets")
    secret = (await _rotate(client, key, org.id, "github"))["webhook_secret"]
    r = await _post_github(
        client, secret, org.id, "pull_request",
        {"repository": {"clone_url": "https://github.com/Acme/widgets"},
         "pull_request": {"number": 4, "title": {"x": 1}, "body": 7, "state": "open",
                          "head": {"ref": "b", "sha": "a" * 40}}},
        "t-1",
    )
    assert r.status_code == 200, r.text
