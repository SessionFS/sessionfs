"""Server-side polling of registered Git marketplace registries (GitHub #69):
API settings, the scheduler endpoint and the poller end to end against a
local repository (the transport is the production one, except that the
protocol allowlist names ``file``)."""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server.auth.keys import generate_api_key, hash_api_key
from sessionfs.server.db.models import (
    ApiKey,
    OrgMember,
    SkillMarketplaceListing,
    SkillMarketplaceRegistry,
    SkillPackage,
    SkillPackageVersion,
    SkillRegistryPollRef,
    User,
)
from sessionfs.server.services import registry_poller
from sessionfs.server.services import registry_polling as rp
from sessionfs.skills.models import SkillError
from tests.server.integration.test_skill_marketplace import (
    _org_with_project,
    _platform_admin_headers,
    _second_user_headers,
)
from tests.unit.test_registry_polling import (
    commit_all,
    file_transport,
    git,
    make_repo,
    marketplace_tree,
    needs_git,
    plugin_files,
    write_tree,
)

pytestmark = pytest.mark.asyncio

SOURCE = "https://git.example.com/acme/skills.git"


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = make_repo(tmp_path)
    monkeypatch.setattr(rp, "prepare_transport", lambda url, credential, sandbox: file_transport(path, sandbox))
    return path


async def _registry(client: AsyncClient, headers: dict, org_id: str, **extra) -> dict:
    body = {"alias": f"team-{uuid.uuid4().hex[:6]}", "source_url": SOURCE, **extra}
    response = await client.post(f"/api/v1/orgs/{org_id}/skills/registries", headers=headers, json=body)
    assert response.status_code == 201, response.text
    return response.json()


async def _poll(db: AsyncSession, blob_store, registry_id: str) -> dict:
    result = await registry_poller.run_poll(db, store=blob_store, registry_ids=[registry_id])
    assert result["polled"] == 1, result
    return result["results"][0]


async def _versions(db: AsyncSession, org_id: str) -> list[SkillPackageVersion]:
    return list(
        (
            await db.execute(
                select(SkillPackageVersion)
                .join(SkillPackage, SkillPackage.id == SkillPackageVersion.package_id)
                .where(SkillPackage.org_id == org_id)
                .order_by(SkillPackageVersion.publication_seq)
            )
        ).scalars().all()
    )


# ---------------------------------------------------------------------------
# Settings API
# ---------------------------------------------------------------------------


async def test_org_registry_defaults_and_settings_validation(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession, test_user: User
) -> None:
    org, _ = await _org_with_project(db_session, test_user)
    org_id = org.id  # the poller's rollbacks expire ORM instances
    created = await _registry(client, auth_headers, org_id)
    assert created["poll_interval_seconds"] == 900
    assert created["server_fetch_enabled"] is True
    assert created["tag_pattern"] == rp.DEFAULT_TAG_PATTERN
    assert created["next_poll_at"] and created["last_poll_status"] is None
    ssh = await _registry(client, auth_headers, org_id, source_url="ssh://git@git.example.com/acme/skills")
    assert ssh["poll_interval_seconds"] == 0 and ssh["server_fetch_enabled"] is False
    for bad in (
        {"poll_interval_seconds": 30},
        {"poll_interval_seconds": 120},
        {"source_url": "https://%6deta.evil.com/acme/x"},
        {"source_url": "https://b\u00fccher.example/acme/x"},
        {"poll_interval_seconds": True},
        {"tag_pattern": "(a+)+$"},
        {"credential_secret_ref": "ghp_" + "a" * 36},
        {"credential_secret_ref": "env:SFS_DATABASE_URL"},
        {"branch_only_mode": True},
        {"branch": "a..b", "branch_only_mode": True},
        {"source_url": "ssh://git@git.example.com/acme/x", "poll_interval_seconds": 900},
        {"source_url": "https://10.0.0.5/acme/x", "poll_interval_seconds": 900},
        {"source_url": "https://[::1]/acme/x"},
    ):
        response = await client.post(
            f"/api/v1/orgs/{org_id}/skills/registries",
            headers=auth_headers,
            json={"alias": f"bad-{uuid.uuid4().hex[:6]}", "source_url": SOURCE, **bad},
        )
        assert response.status_code == 422, (bad, response.text)
    personal = await client.post(
        "/api/v1/users/me/skills/registries",
        headers=auth_headers,
        json={"alias": "mine", "source_url": SOURCE, "poll_interval_seconds": 900},
    )
    assert personal.status_code == 422
    personal = await client.post(
        "/api/v1/users/me/skills/registries", headers=auth_headers, json={"alias": "mine", "source_url": SOURCE}
    )
    assert personal.status_code == 201 and personal.json()["server_fetch_enabled"] is False


async def test_patch_is_admin_only_cross_org_404_and_audited(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession, test_user: User
) -> None:
    org, _ = await _org_with_project(db_session, test_user)
    org_id = org.id  # the poller's rollbacks expire ORM instances
    registry = await _registry(client, auth_headers, org_id)
    url = f"/api/v1/orgs/{org_id}/skills/registries/{registry['id']}"
    patched = await client.patch(
        url,
        headers=auth_headers,
        json={"tag_pattern": r"release-\d+", "credential_secret_ref": "env:SFS_REGISTRY_CRED_ACME",
              "poll_interval_seconds": 3600},
    )
    assert patched.status_code == 200, patched.text
    body = patched.json()
    assert body["tag_pattern"] == r"release-\d+" and body["poll_interval_seconds"] == 3600
    assert body["credential_secret_ref"] == "env:SFS_REGISTRY_CRED_ACME"
    cleared = await client.patch(url, headers=auth_headers, json={"tag_pattern": "", "poll_interval_seconds": 0})
    assert cleared.json()["tag_pattern"] == rp.DEFAULT_TAG_PATTERN
    assert cleared.json()["server_fetch_enabled"] is False and cleared.json()["next_poll_at"] is None
    # A member (not admin) cannot change it; another org's admin sees 404.
    member, member_headers = await _second_user_headers(db_session, email="member@example.com")
    db_session.add(OrgMember(org_id=org_id, user_id=member.id, role="member"))
    await db_session.commit()
    assert (await client.patch(url, headers=member_headers, json={"poll_interval_seconds": 900})).status_code == 403
    other, other_headers = await _second_user_headers(db_session, email="other-admin@example.com")
    other_org, _ = await _org_with_project(db_session, other)
    foreign = await client.patch(
        f"/api/v1/orgs/{other_org.id}/skills/registries/{registry['id']}",
        headers=other_headers,
        json={"poll_interval_seconds": 900},
    )
    assert foreign.status_code == 404
    assert (
        await client.post(f"/api/v1/orgs/{other_org.id}/skills/registries/{registry['id']}/poll", headers=other_headers)
    ).status_code == 404


async def test_poll_now_schedules_without_fetching_and_is_rate_limited(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession, test_user: User, monkeypatch
) -> None:
    def explode(*_args, **_kwargs):
        raise AssertionError("poll-now must never fetch in the request")

    monkeypatch.setattr(rp, "prepare_transport", explode)
    org, _ = await _org_with_project(db_session, test_user)
    org_id = org.id  # the poller's rollbacks expire ORM instances
    registry = await _registry(client, auth_headers, org_id)
    url = f"/api/v1/orgs/{org_id}/skills/registries/{registry['id']}/poll"
    first = await client.post(url, headers=auth_headers)
    assert first.status_code == 202, first.text
    assert first.json()["poll_requested_at"]
    second = await client.post(url, headers=auth_headers)
    assert second.status_code == 429 and second.headers.get("Retry-After")
    off = await _registry(client, auth_headers, org_id, poll_interval_seconds=0)
    disabled = await client.post(f"/api/v1/orgs/{org_id}/skills/registries/{off['id']}/poll", headers=auth_headers)
    assert disabled.status_code == 409


async def test_scheduler_endpoint_is_platform_admin_or_minted_poll_key(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession, test_user: User, monkeypatch
) -> None:
    # An ordinary user (or org admin) key is refused.
    assert (await client.post("/api/v1/admin/skills/registries/poll", headers=auth_headers, json={})).status_code == 403
    admin, admin_headers = await _platform_admin_headers(db_session)
    response = await client.post("/api/v1/admin/skills/registries/poll", headers=admin_headers, json={"limit": 2})
    assert response.status_code == 200, response.text
    assert response.json()["busy"] is False
    # A skills_registry:poll service key minted by the platform admin works;
    # the same scope on a key whose owner is not a platform admin is refused.
    org, _ = await _org_with_project(db_session, test_user)
    org_id = org.id  # the poller's rollbacks expire ORM instances
    keys = {}
    for owner in (admin, test_user):
        raw = generate_api_key()
        db_session.add(
            ApiKey(id=str(uuid.uuid4()), user_id=owner.id, key_hash=hash_api_key(raw), name="poller",
                   created_at=datetime.now(timezone.utc), key_kind="service", org_id=org_id,
                   scopes=json.dumps(["skills_registry:poll"]), service_key_name="poller",
                   created_by_user_id=owner.id)
        )
        keys[owner.id] = {"Authorization": f"Bearer {raw}"}
    await db_session.commit()
    ok = await client.post("/api/v1/admin/skills/registries/poll", headers=keys[admin.id], json={})
    assert ok.status_code == 200, ok.text
    refused = await client.post("/api/v1/admin/skills/registries/poll", headers=keys[test_user.id], json={})
    assert refused.status_code == 403
    # The scope cannot be minted by a non platform admin.
    minted = await client.post(
        f"/api/v1/orgs/{org_id}/service-keys",
        headers=auth_headers,
        json={"name": "poller", "scopes": ["skills_registry:poll"]},
    )
    assert minted.status_code in (403, 404), minted.text
    monkeypatch.setenv("SFS_REGISTRY_POLLING_ENABLED", "false")
    off = await client.post("/api/v1/admin/skills/registries/poll", headers=admin_headers, json={})
    assert off.status_code == 503


# ---------------------------------------------------------------------------
# Poller end to end
# ---------------------------------------------------------------------------


@needs_git
async def test_new_tags_publish_immutable_versions_idempotently(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession, test_user: User, blob_store, repo: Path
) -> None:
    org, _ = await _org_with_project(db_session, test_user)
    org_id = org.id  # the poller's rollbacks expire ORM instances
    registry = await _registry(client, auth_headers, org_id)
    write_tree(repo, marketplace_tree({"alpha": plugin_files("alpha", version="1.0.0")}))
    first_commit = commit_all(repo, "v1")
    git(repo, "tag", "v1.0.0")
    git(repo, "tag", "nightly-1")  # never matches the semver pattern

    result = await _poll(db_session, blob_store, registry["id"])
    assert result["status"] == "ok", result
    assert [ref["ref"] for ref in result["refs"]] == ["refs/tags/v1.0.0"]
    versions = await _versions(db_session, org_id)
    assert len(versions) == 1 and versions[0].source_commit == first_commit
    first_hash, first_id = versions[0].content_hash, versions[0].id
    listing = (
        await db_session.execute(select(SkillMarketplaceListing).where(SkillMarketplaceListing.version_id == first_id))
    ).scalar_one()
    assert listing.visibility == "org" and listing.org_id == org_id
    signature = json.loads(versions[0].signature_metadata)
    assert signature == {"commit": first_commit, "ref": "refs/tags/v1.0.0", "registry_id": registry["id"],
                         "source": "registry_poll"}

    # Re-polling the same commit is a no-op.
    again = await _poll(db_session, blob_store, registry["id"])
    assert again["status"] == "ok" and again["refs"] == []
    assert len(await _versions(db_session, org_id)) == 1

    # A new tag with new content: a new version.
    write_tree(repo, marketplace_tree({"alpha": plugin_files("alpha", version="1.1.0", body="Second.")}))
    commit_all(repo, "v1.1")
    git(repo, "tag", "v1.1.0")
    await _poll(db_session, blob_store, registry["id"])
    assert len(await _versions(db_session, org_id)) == 2

    # Force-moving v1.0.0 to new content creates a NEW version; the original
    # version row is never mutated.
    write_tree(repo, marketplace_tree({"alpha": plugin_files("alpha", version="1.0.1", body="Moved.")}))
    moved = commit_all(repo, "moved")
    git(repo, "tag", "-f", "v1.0.0")
    result = await _poll(db_session, blob_store, registry["id"])
    assert [(ref["ref"], ref["commit"]) for ref in result["refs"]] == [("refs/tags/v1.0.0", moved)]
    versions = await _versions(db_session, org_id)
    assert len(versions) == 3
    original = (
        await db_session.execute(
            select(SkillPackageVersion).where(SkillPackageVersion.id == first_id).execution_options(populate_existing=True)
        )
    ).scalar_one()
    assert original.content_hash == first_hash and original.source_commit == first_commit
    assert versions[-1].content_hash != first_hash
    refs = (
        await db_session.execute(
            select(func.count()).select_from(SkillRegistryPollRef).where(
                SkillRegistryPollRef.registry_id == registry["id"]
            )
        )
    ).scalar_one()
    assert refs == 3
    history = await client.get(
        f"/api/v1/orgs/{org_id}/skills/registries/{registry['id']}/poll-refs", headers=auth_headers
    )
    assert history.status_code == 200 and len(history.json()["items"]) == 3
    listed = (await client.get(f"/api/v1/orgs/{org_id}/skills/registries", headers=auth_headers)).json()["items"]
    row = next(item for item in listed if item["id"] == registry["id"])
    assert row["last_poll_status"] == "ok" and row["consecutive_failures"] == 0 and row["last_poll_at"]


@needs_git
async def test_branch_mode_publishes_the_branch_head(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession, test_user: User, blob_store, repo: Path
) -> None:
    org, _ = await _org_with_project(db_session, test_user)
    org_id = org.id  # the poller's rollbacks expire ORM instances
    registry = await _registry(client, auth_headers, org_id, branch_only_mode=True, branch="main")
    write_tree(repo, plugin_files("solo"))
    head = commit_all(repo, "one")
    result = await _poll(db_session, blob_store, registry["id"])
    assert result["status"] == "ok", result
    assert result["refs"][0]["ref"] == "refs/heads/main"
    assert result["refs"][0]["commit"] == head
    assert len(await _versions(db_session, org_id)) == 1


@needs_git
async def test_dlp_and_scanner_refusals_are_recorded_not_published(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession, test_user: User, blob_store, repo: Path
) -> None:
    org, _ = await _org_with_project(db_session, test_user)
    org_id = org.id  # the poller's rollbacks expire ORM instances
    registry = await _registry(client, auth_headers, org_id)
    leaky = plugin_files("leaky", body="Credential: AKIAABCDEFGHIJKLMNOP")
    write_tree(repo, marketplace_tree({"clean": plugin_files("clean"), "leaky": leaky}))
    commit_all(repo, "mixed")
    git(repo, "tag", "v1.0.0")
    result = await _poll(db_session, blob_store, registry["id"])
    assert result["status"] == "partial", result
    ref = result["refs"][0]
    assert ref["outcome"] == "partial"
    outcomes = {item["name"]: item for item in ref["packages"]}
    assert outcomes["clean"]["outcome"] == "published"
    assert outcomes["leaky"]["outcome"] == "rejected" and outcomes["leaky"]["code"]
    names = {
        name
        for name in (
            await db_session.execute(select(SkillPackage.name).where(SkillPackage.org_id == org_id))
        ).scalars()
    }
    assert names == {"clean"}
    # The refused ref is not retried until it moves.
    again = await _poll(db_session, blob_store, registry["id"])
    assert again["refs"] == []
    # A registry-level "partial" is not a failure: no backoff.
    row = await db_session.get(SkillMarketplaceRegistry, registry["id"], populate_existing=True)
    assert row is not None and row.consecutive_failures == 0 and row.last_poll_status == "partial"


@needs_git
async def test_metadata_only_registries_are_never_fetched(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession, test_user: User, blob_store, monkeypatch
) -> None:
    def explode(*_args, **_kwargs):
        raise AssertionError("an interval-0 registry must never be fetched")

    monkeypatch.setattr(rp, "prepare_transport", explode)
    org, _ = await _org_with_project(db_session, test_user)
    org_id = org.id  # the poller's rollbacks expire ORM instances
    registry = await _registry(client, auth_headers, org_id, poll_interval_seconds=0)
    due = await registry_poller.run_poll(db_session, store=blob_store)
    assert due["polled"] == 0
    forced = await registry_poller.run_poll(db_session, store=blob_store, registry_ids=[registry["id"]])
    assert forced["polled"] == 0


async def test_failures_back_off_and_never_leak_the_credential(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession, test_user: User, blob_store, monkeypatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    token = "glpat-TOPSECRETTOKEN0000000"
    org, _ = await _org_with_project(db_session, test_user)
    org_id = org.id  # the poller's rollbacks expire ORM instances
    monkeypatch.setenv("SFS_REGISTRY_CRED_ACME", token)
    monkeypatch.setenv("SFS_REGISTRY_CRED_ACME_ORG", org_id)
    monkeypatch.setenv("SFS_REGISTRY_CRED_ACME_HOSTS", "git.example.com")
    registry = await _registry(client, auth_headers, org_id, credential_secret_ref="env:SFS_REGISTRY_CRED_ACME")

    def failing_listing(transport, sandbox, *, branch=None):
        raise SkillError(f"Git source operation failed: fatal: https://x:{token}@git.example.com refused {token}")

    monkeypatch.setattr(rp, "prepare_transport", lambda url, credential, sandbox: rp.GitTransport(
        url=SOURCE, env={}, config=(), secrets=credential.secrets()))
    monkeypatch.setattr(rp, "list_remote_refs", failing_listing)
    caplog.set_level(logging.DEBUG)
    before = datetime.now(timezone.utc)
    first = await _poll(db_session, blob_store, registry["id"])
    assert first["status"] == "error" and token not in json.dumps(first)
    row = await db_session.get(SkillMarketplaceRegistry, registry["id"], populate_existing=True)
    assert row is not None and row.consecutive_failures == 1
    next_at = row.next_poll_at if row.next_poll_at.tzinfo else row.next_poll_at.replace(tzinfo=timezone.utc)
    assert next_at - before >= timedelta(seconds=1700)
    second = await _poll(db_session, blob_store, registry["id"])
    assert second["status"] == "error"
    row = await db_session.get(SkillMarketplaceRegistry, registry["id"], populate_existing=True)
    assert row is not None and row.consecutive_failures == 2
    assert token not in (row.last_poll_error_summary or "")
    listed = await client.get(f"/api/v1/orgs/{org_id}/skills/registries", headers=auth_headers)
    assert token not in listed.text
    item = next(item for item in listed.json()["items"] if item["id"] == registry["id"])
    assert item["backoff"] is True and item["last_poll_status"] == "error"
    assert token not in caplog.text

    # A credential bound to another org fails closed, with no token in sight.
    monkeypatch.setenv("SFS_REGISTRY_CRED_ACME_ORG", "org_someone_else")
    third = await _poll(db_session, blob_store, registry["id"])
    assert third["status"] == "error" and "credential_unavailable" in third["summary"]
    assert token not in json.dumps(third)


@needs_git
async def test_poll_actor_who_lost_admin_stops_publishing(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession, test_user: User, blob_store, repo: Path
) -> None:
    org, _ = await _org_with_project(db_session, test_user)
    org_id = org.id  # the poller's rollbacks expire ORM instances
    registry = await _registry(client, auth_headers, org_id)
    write_tree(repo, plugin_files("solo"))
    commit_all(repo, "one")
    git(repo, "tag", "v1.0.0")
    member = (
        await db_session.execute(
            select(OrgMember).where(OrgMember.org_id == org_id, OrgMember.user_id == test_user.id)
        )
    ).scalar_one()
    member.role = "member"
    await db_session.commit()
    result = await _poll(db_session, blob_store, registry["id"])
    assert result["status"] == "error" and "poll_actor_not_admin" in result["summary"]
    assert await _versions(db_session, org_id) == []


@needs_git
async def test_backlog_beyond_the_per_poll_cap_is_skipped_oldest_first(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession, test_user: User, blob_store, repo: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(rp, "MAX_NEW_REFS_PER_POLL", 2)
    org, _ = await _org_with_project(db_session, test_user)
    org_id = org.id  # the poller's rollbacks expire ORM instances
    registry = await _registry(client, auth_headers, org_id)
    for minor in range(4):
        write_tree(repo, plugin_files("solo", version=f"1.{minor}.0", body=f"Body {minor}."))
        commit_all(repo, f"v1.{minor}")
        git(repo, "tag", f"v1.{minor}.0")
    result = await _poll(db_session, blob_store, registry["id"])
    outcomes = {ref["ref"]: ref["outcome"] for ref in result["refs"]}
    assert outcomes == {
        "refs/tags/v1.0.0": "skipped",
        "refs/tags/v1.1.0": "skipped",
        "refs/tags/v1.2.0": "published",
        "refs/tags/v1.3.0": "published",
    }
    package = (await db_session.execute(select(SkillPackage).where(SkillPackage.org_id == org_id))).scalar_one()
    current = await db_session.get(SkillPackageVersion, package.current_version_id)
    assert current is not None and current.version_label == "1.3.0"
