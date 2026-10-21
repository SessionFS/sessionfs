"""Registry polling: Sentinel review fixes (GitHub #69) - transient fetch
failures retry instead of dropping the tag, per-org caps, fair due
selection, the run budget, lease renewal, a generic credential message and
poll-actor recovery on re-save."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server.db.models import (
    Organization,
    OrgMember,
    SkillMarketplaceRegistry,
    SkillRegistryPollRef,
    User,
)
from sessionfs.server.services import registry_poller
from sessionfs.server.services import registry_polling as rp
from sessionfs.skills.sources import GitTransportError
from tests.server.integration.test_registry_polling import SOURCE, _poll, _registry, _versions
from tests.server.integration.test_skill_marketplace import _org_with_project, _second_user_headers
from tests.unit.test_registry_polling import (
    commit_all,
    file_transport,
    git,
    make_repo,
    needs_git,
    plugin_files,
    write_tree,
)

pytestmark = pytest.mark.asyncio


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = make_repo(tmp_path)
    monkeypatch.setattr(rp, "prepare_transport", lambda url, credential, sandbox: file_transport(path, sandbox))
    return path


@needs_git
async def test_a_502_during_fetch_is_retried_not_recorded(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession, test_user: User, blob_store, repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    org, _ = await _org_with_project(db_session, test_user)
    org_id = org.id
    registry = await _registry(client, auth_headers, org_id)
    write_tree(repo, plugin_files("solo"))
    commit_all(repo, "one")
    git(repo, "tag", "v1.0.0")
    real = rp.fetch_ref_packages
    calls = {"n": 0}

    def flaky(transport, ref, *, deadline):
        calls["n"] += 1
        if calls["n"] == 1:
            raise GitTransportError(
                "Git source operation failed: fatal: unable to access 'https://git.example.com/': "
                "The requested URL returned error: 502"
            )
        return real(transport, ref, deadline=deadline)

    monkeypatch.setattr(rp, "fetch_ref_packages", flaky)
    first = await _poll(db_session, blob_store, registry["id"])
    assert first["status"] == "error" and "fetch_failed" in first["summary"]
    recorded = (
        await db_session.execute(
            select(func.count()).select_from(SkillRegistryPollRef).where(
                SkillRegistryPollRef.registry_id == registry["id"]
            )
        )
    ).scalar_one()
    assert recorded == 0  # the tag is NOT written off
    row = await db_session.get(SkillMarketplaceRegistry, registry["id"], populate_existing=True)
    assert row is not None and row.consecutive_failures == 1
    second = await _poll(db_session, blob_store, registry["id"])
    assert second["status"] == "ok" and second["refs"][0]["outcome"] == "published"
    assert len(await _versions(db_session, org_id)) == 1


async def test_per_org_cap_on_polling_registries(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession, test_user: User, monkeypatch
) -> None:
    monkeypatch.setenv("SFS_REGISTRY_POLL_MAX_PER_ORG", "2")
    org, _ = await _org_with_project(db_session, test_user)
    org_id = org.id
    await _registry(client, auth_headers, org_id)
    await _registry(client, auth_headers, org_id)
    third = await client.post(
        f"/api/v1/orgs/{org_id}/skills/registries",
        headers=auth_headers,
        json={"alias": "third", "source_url": SOURCE},
    )
    assert third.status_code == 409 and "poll_registry_limit" in third.text
    # Metadata-only registries do not count and can still be added...
    quiet = await _registry(client, auth_headers, org_id, poll_interval_seconds=0)
    # ...but turning polling on for one past the cap is refused.
    enabled = await client.patch(
        f"/api/v1/orgs/{org_id}/skills/registries/{quiet['id']}",
        headers=auth_headers,
        json={"poll_interval_seconds": 900},
    )
    assert enabled.status_code == 409


async def _seed_due(db: AsyncSession, actor: User, org_registries: dict[str, int]) -> dict[str, list[str]]:
    now = datetime.now(timezone.utc)
    ids: dict[str, list[str]] = {}
    minute = 0
    for org_id, count in org_registries.items():
        db.add(Organization(id=org_id, name=org_id, slug=f"s-{uuid.uuid4().hex[:8]}", tier="team"))
        await db.flush()
        db.add(OrgMember(org_id=org_id, user_id=actor.id, role="owner"))
        for index in range(count):
            minute += 1
            registry_id = f"smr_{uuid.uuid4().hex[:24]}"
            ids.setdefault(org_id, []).append(registry_id)
            db.add(
                SkillMarketplaceRegistry(
                    id=registry_id, owner_type="org", owner_id=org_id, org_id=org_id,
                    alias=f"r{index}", source_url=SOURCE, policy_state="approved",
                    poll_interval_seconds=900, poll_actor_user_id=actor.id, consecutive_failures=0,
                    branch_only_mode=False,
                    # The big org's registries are the most overdue.
                    next_poll_at=now - timedelta(hours=10) + timedelta(minutes=minute),
                )
            )
    await db.commit()
    return ids


async def test_due_selection_is_fair_across_orgs(db_session: AsyncSession, test_user: User) -> None:
    ids = await _seed_due(db_session, test_user, {"org_big": 8, "org_small_a": 1, "org_small_b": 1})
    picked = await registry_poller.due_registry_ids(
        db_session, now=datetime.now(timezone.utc), limit=5, per_org=2
    )
    big = [registry_id for registry_id in picked if registry_id in ids["org_big"]]
    assert len(big) == 2  # never more than per_org from one org
    assert ids["org_small_a"][0] in picked and ids["org_small_b"][0] in picked
    assert picked[0] == ids["org_big"][0]  # most overdue first, then round-robin


async def test_run_stops_claiming_before_the_budget_runs_out(
    db_session: AsyncSession, test_user: User, blob_store, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _seed_due(db_session, test_user, {f"org_{i}": 1 for i in range(4)})
    clock = {"t": 1000.0}
    monkeypatch.setattr(registry_poller.time, "monotonic", lambda: clock["t"])
    monkeypatch.setattr(rp, "run_budget_seconds", lambda: 1200)
    monkeypatch.setattr(rp, "poll_timeout_seconds", lambda: 300)
    polled: list[str] = []

    async def slow_poll(db, registry_id, *, store, owner):
        polled.append(registry_id)
        clock["t"] += 400  # each registry takes 400 s
        await registry_poller._finish(db, registry_id, owner=owner, status="ok", summary=None,
                                      failed=False, interval=900, failures=0)
        return registry_poller.PollResult(registry_id=registry_id, status="ok")

    monkeypatch.setattr(registry_poller, "poll_registry", slow_poll)
    result = await registry_poller.run_poll(db_session, store=blob_store, limit=10)
    # 300 s fetch + 300 s publish slack per registry against a 1200 s budget:
    # claims at t=0 and t=400; at t=800 a third could overrun, so it stops.
    assert len(polled) == 2 and result["budget_exhausted"] is True
    # The unclaimed registries stay due (no lease taken).
    due = await registry_poller.due_registry_ids(db_session, now=datetime.now(timezone.utc), limit=10)
    assert len(due) == 2


async def test_run_budget_below_one_registry_still_polls_one(
    db_session: AsyncSession, test_user: User, blob_store, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _seed_due(db_session, test_user, {f"org_{i}": 1 for i in range(3)})
    clock = {"t": 1000.0}
    monkeypatch.setattr(registry_poller.time, "monotonic", lambda: clock["t"])
    monkeypatch.setattr(rp, "run_budget_seconds", lambda: 500)  # below 300 fetch + 300 slack
    monkeypatch.setattr(rp, "poll_timeout_seconds", lambda: 300)
    polled: list[str] = []

    async def slow_poll(db, registry_id, *, store, owner):
        polled.append(registry_id)
        clock["t"] += 400
        await registry_poller._finish(db, registry_id, owner=owner, status="ok", summary=None,
                                      failed=False, interval=900, failures=0)
        return registry_poller.PollResult(registry_id=registry_id, status="ok")

    monkeypatch.setattr(registry_poller, "poll_registry", slow_poll)
    result = await registry_poller.run_poll(db_session, store=blob_store, limit=10)
    # A misconfigured budget runs one registry per run instead of none.
    assert len(polled) == 1 and result["budget_exhausted"] is True


async def test_lost_lease_stops_the_poll_without_writing_state(
    db_session: AsyncSession, test_user: User, blob_store
) -> None:
    ids = await _seed_due(db_session, test_user, {"org_lease": 1})
    registry_id = ids["org_lease"][0]
    now = datetime.now(timezone.utc)
    assert await registry_poller.claim_registry(db_session, registry_id, owner="poll_a", now=now)
    with pytest.raises(registry_poller._LeaseLost):
        await registry_poller._renew_lease(db_session, registry_id, "poll_b")
    await registry_poller._renew_lease(db_session, registry_id, "poll_a")
    row = await db_session.get(SkillMarketplaceRegistry, registry_id, populate_existing=True)
    assert row is not None and row.poll_lease_owner == "poll_a"
    expires = row.poll_lease_expires_at
    if expires is not None and expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    assert expires is not None and expires - now >= timedelta(
        seconds=rp.poll_timeout_seconds() + rp.PUBLISH_SLACK_SECONDS
    )


async def test_credential_failures_share_one_generic_summary(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession, test_user: User, blob_store, monkeypatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    org, _ = await _org_with_project(db_session, test_user)
    org_id = org.id
    # One name that does not exist, one bound to another org.
    monkeypatch.setenv("SFS_REGISTRY_CRED_OTHER", "tok-other-org-secret")
    monkeypatch.setenv("SFS_REGISTRY_CRED_OTHER_ORG", "org_somebody_else")
    monkeypatch.setenv("SFS_REGISTRY_CRED_OTHER_HOSTS", "git.example.com")
    summaries = []
    for ref in ("env:SFS_REGISTRY_CRED_NOPE", "env:SFS_REGISTRY_CRED_OTHER"):
        registry = await _registry(client, auth_headers, org_id, credential_secret_ref=ref)
        result = await _poll(db_session, blob_store, registry["id"])
        summaries.append(result["summary"])
    assert summaries[0] == summaries[1]
    assert rp.CREDENTIAL_UNAVAILABLE in summaries[0]
    assert "tok-other-org-secret" not in caplog.text
    assert "different organization" in caplog.text  # the operator still sees why


@needs_git
async def test_resaving_settings_recovers_a_lost_poll_actor(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession, test_user: User, blob_store, repo: Path
) -> None:
    org, _ = await _org_with_project(db_session, test_user)
    org_id = org.id
    user_id = test_user.id
    former, former_headers = await _second_user_headers(db_session, email="former-admin@example.com")
    db_session.add(OrgMember(org_id=org_id, user_id=former.id, role="admin"))
    await db_session.commit()
    registry = await _registry(client, former_headers, org_id)
    member = (
        await db_session.execute(select(OrgMember).where(OrgMember.org_id == org_id, OrgMember.user_id == former.id))
    ).scalar_one()
    member.role = "member"
    await db_session.commit()
    write_tree(repo, plugin_files("solo"))
    commit_all(repo, "one")
    git(repo, "tag", "v1.0.0")
    failed = await _poll(db_session, blob_store, registry["id"])
    assert "poll_actor_not_admin" in failed["summary"]
    # Re-saving the SAME settings makes the saving admin the poll actor.
    resaved = await client.patch(
        f"/api/v1/orgs/{org_id}/skills/registries/{registry['id']}",
        headers=auth_headers,
        json={"poll_interval_seconds": registry["poll_interval_seconds"]},
    )
    assert resaved.status_code == 200, resaved.text
    row = await db_session.get(SkillMarketplaceRegistry, registry["id"], populate_existing=True)
    assert row is not None and row.poll_actor_user_id == user_id
    recovered = await _poll(db_session, blob_store, registry["id"])
    assert recovered["status"] == "ok" and len(await _versions(db_session, org_id)) == 1
