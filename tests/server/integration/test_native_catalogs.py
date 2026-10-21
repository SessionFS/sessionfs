"""Native per-tool catalogs over dumb-HTTP git and "works in" evidence
(marketplace v2 §7, tk_86084118c8ee453c)."""

from __future__ import annotations

import base64
import json
import pytest
from httpx import AsyncClient
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server.db.models import (
    Organization,
    SkillFeedCatalog,
    SkillFeedEntry,
    SkillPackage,
    SkillToolVerification,
    SkillVerificationJob,
    User,
)
from sessionfs.server.services import native_catalogs as service
from sessionfs.server.services import publishers as publishers_service
from tests.server.integration.test_marketplace_moderation import (
    _act,
    _org,
    _publish_org,
    _publish_personal,
    _service,
    _user,
)

pytestmark = pytest.mark.asyncio

PUBLIC_GIT = "/api/v1/git/public/sessionfs.git"


def _clear_service_state() -> None:
    service._fresh_until.clear()
    service._state_cache.clear()
    service._build_failures.clear()
    service._build_locks.clear()


@pytest.fixture(autouse=True)
def _reset_caches():
    _clear_service_state()
    yield
    _clear_service_state()


def _basic(headers: dict) -> dict:
    key = headers["Authorization"].removeprefix("Bearer ")
    token = base64.b64encode(f"sessionfs:{key}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


async def _system_public(client: AsyncClient, db: AsyncSession, owner: User) -> tuple[dict, dict]:
    """A platform-admin public listing owned by the platform publisher."""
    org, _ = await _org(db, owner)
    _, platform_headers = await _user(db, tier="admin")
    pkg = await _publish_org(client, platform_headers, org, visibility="public", namespace="pub",
                             collection="pub", body="Public.")
    system = await publishers_service.ensure_system_publisher(db)
    await db.execute(update(SkillPackage).where(SkillPackage.id == pkg["package_id"]).values(
        publisher_id=system.id))
    await db.commit()
    return pkg, platform_headers


async def _refs(client: AsyncClient, base: str, headers: dict) -> dict[str, str]:
    response = await client.get(f"{base}/info/refs", headers=headers)
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    refs = {}
    for line in response.text.splitlines():
        sha, ref = line.split("\t")
        refs[ref] = sha
    return refs


async def _tree_paths(client: AsyncClient, base: str, headers: dict) -> dict[str, bytes]:
    """Parse the served pack (no git binary needed) into path -> bytes."""
    import zlib

    refs = await _refs(client, base, headers)  # a client always reads info/refs first
    packs = (await client.get(f"{base}/objects/info/packs", headers=headers)).text
    name = packs.split()[1].removesuffix(".pack")
    pack = (await client.get(f"{base}/objects/pack/{name}.pack", headers=headers)).content
    index = (await client.get(f"{base}/objects/pack/{name}.idx", headers=headers)).content
    assert pack[:4] == b"PACK" and index[:4] == b"\xfftOc"
    count = int.from_bytes(pack[8:12], "big")
    objects: dict[str, tuple[str, bytes]] = {}
    import hashlib

    position = 12
    kinds = {1: "commit", 2: "tree", 3: "blob"}
    for _ in range(count):
        byte = pack[position]
        kind = (byte >> 4) & 7
        position += 1
        while byte & 0x80:
            byte = pack[position]
            position += 1
        decompressor = zlib.decompressobj()
        payload = decompressor.decompress(pack[position:])
        position = len(pack) - len(decompressor.unused_data)
        raw = f"{kinds[kind]} {len(payload)}".encode() + b"\0" + payload
        objects[hashlib.sha1(raw).hexdigest()] = (kinds[kind], payload)
    commit = objects[refs["refs/heads/main"]][1].decode()
    tree = commit.split("\n")[0].split()[1]
    files: dict[str, bytes] = {}

    def walk(tree_id: str, prefix: str) -> None:
        data = objects[tree_id][1]
        while data:
            head, _, rest = data.partition(b"\0")
            mode, name = head.decode().split(" ", 1)
            sha = rest[:20].hex()
            data = rest[20:]
            if mode == "40000":
                walk(sha, f"{prefix}{name}/")
            else:
                assert mode == "100644"
                files[f"{prefix}{name}"] = objects[sha][1]

    walk(tree, "")
    return files


async def test_public_catalog_serves_only_approved_and_drops_blocked(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession, test_user: User
) -> None:
    pkg, platform_headers = await _system_public(client, db_session, test_user)
    # A personal package never enters the public catalog.
    await _publish_personal(client, auth_headers)

    unauth = await client.get(f"{PUBLIC_GIT}/info/refs")
    assert unauth.status_code == 401
    assert unauth.headers["www-authenticate"].startswith("Basic ")
    bad = await client.get(f"{PUBLIC_GIT}/info/refs", headers={"Authorization": "Basic !!!"})
    assert bad.status_code == 401

    refs = await _refs(client, PUBLIC_GIT, _basic(auth_headers))
    files = await _tree_paths(client, PUBLIC_GIT, _basic(auth_headers))
    catalog = json.loads(files[".claude-plugin/marketplace.json"])
    assert catalog["name"] == "sessionfs"
    assert [entry["name"] for entry in catalog["plugins"]] == ["pub--review-helper"]
    assert catalog["plugins"][0]["source"] == "./plugins/pub--review-helper"
    assert "plugins/pub--review-helper/skills/review-helper/SKILL.md" in files
    index = json.loads(files["SESSIONFS-CATALOG.json"])
    assert index["plugins"][0]["version_id"] == pkg["version_id"]
    assert index["plugins"][0]["content_hash"] == pkg["content_hash"]
    for path in (".github/plugin/marketplace.json", ".agents/plugins/marketplace.json",
                 ".cursor-plugin/marketplace.json"):
        assert json.loads(files[path])["plugins"][0]["name"] == "pub--review-helper"

    # Bearer works too, and unknown paths are 404 (no directory listing).
    head = await client.get(f"{PUBLIC_GIT}/HEAD", headers=auth_headers)
    assert head.text == "ref: refs/heads/main\n"
    assert (await client.get(f"{PUBLIC_GIT}/config", headers=auth_headers)).status_code == 404
    assert (await client.get(f"{PUBLIC_GIT}/objects/ab/" + "c" * 38, headers=auth_headers)).status_code == 404

    # Gemini per-plugin repository (public scope only).
    plugin_git = "/api/v1/git/public/plugins/pub--review-helper.git"
    plugin_refs = await _refs(client, plugin_git, auth_headers)
    assert "refs/tags/v1.2.3" in plugin_refs
    plugin_files = await _tree_paths(client, plugin_git, auth_headers)
    assert json.loads(plugin_files["gemini-extension.json"])["name"] == "pub--review-helper"

    entries = (await db_session.execute(select(SkillFeedEntry))).scalars().all()
    assert {entry.tool for entry in entries} >= {"claude-code", "codex", "copilot", "cursor", "gemini"}

    # Block -> the very next fetch rebuilds without it (no stale feed).
    old_pack = (await client.get(f"{PUBLIC_GIT}/objects/info/packs", headers=auth_headers)).text.split()[1]
    blocked = await _act(client, platform_headers, "versions", pkg["version_id"], "block")
    assert blocked.status_code == 200, blocked.text
    new_refs = await _refs(client, PUBLIC_GIT, auth_headers)
    assert new_refs["refs/heads/main"] != refs["refs/heads/main"]
    files = await _tree_paths(client, PUBLIC_GIT, auth_headers)
    assert json.loads(files[".claude-plugin/marketplace.json"])["plugins"] == []
    assert not any(path.startswith("plugins/") for path in files)
    # The old pack (which held the blocked bytes) is no longer served.
    gone = await client.get(f"{PUBLIC_GIT}/objects/pack/{old_pack}", headers=auth_headers)
    assert gone.status_code == 404
    assert (await client.get(f"{plugin_git}/info/refs", headers=auth_headers)).status_code == 404


async def test_catalog_commit_is_deterministic(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession, test_user: User
) -> None:
    await _system_public(client, db_session, test_user)
    first = await _refs(client, PUBLIC_GIT, auth_headers)
    catalog = (await db_session.execute(select(SkillFeedCatalog))).scalars().one()
    await db_session.execute(update(SkillFeedCatalog).values(input_digest=None, current_commit_sha=None))
    await db_session.commit()
    service._fresh_until.clear()
    service._state_cache.clear()
    second = await _refs(client, PUBLIC_GIT, auth_headers)
    assert first == second
    assert catalog.scope_key == "public"


async def test_public_catalog_requires_verified_publisher_and_scan(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession, test_user: User
) -> None:
    org, _ = await _org(db_session, test_user)
    _, platform_headers = await _user(db_session, tier="admin")
    unverified = await _publish_org(client, platform_headers, org, visibility="public",
                                    namespace="pub", collection="pub", body="Public.")
    package = await db_session.get(SkillPackage, unverified["package_id"])
    assert package is not None and package.owner_type != "system"
    await db_session.execute(update(SkillPackage).values(publisher_id=None))
    await db_session.commit()
    files = await _tree_paths(client, PUBLIC_GIT, auth_headers)
    assert json.loads(files[".claude-plugin/marketplace.json"])["plugins"] == []

    assert service.scan_ok_for_public(json.dumps({"ruleset": "r", "max_severity": "medium"}))
    assert not service.scan_ok_for_public(json.dumps({"ruleset": "r", "max_severity": "high"}))
    assert service.scan_ok_for_public(
        json.dumps({"ruleset": "r", "max_severity": "high", "open_max_severity": "low"})
    )
    assert not service.scan_ok_for_public("{}")


async def test_org_catalog_assigned_only_and_tier_gated(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession, test_user: User
) -> None:
    org, _ = await _org(db_session, test_user)
    pkg = await _publish_org(client, auth_headers, org)
    base = f"/api/v1/git/org/{org.id}/catalog.git"
    files = await _tree_paths(client, base, auth_headers)
    # Published but not assigned: not in the native catalog (native installs skip consent).
    assert json.loads(files[".claude-plugin/marketplace.json"])["plugins"] == []
    assigned = await client.post(
        f"/api/v1/orgs/{org.id}/skills/assignments",
        headers=auth_headers,
        json={"version_id": pkg["version_id"], "content_hash": pkg["content_hash"], "target_type": "org"},
    )
    assert assigned.status_code == 201, assigned.text
    files = await _tree_paths(client, base, auth_headers)
    catalog = json.loads(files[".claude-plugin/marketplace.json"])
    assert catalog["name"] == f"sessionfs-org-{org.id.replace('_', '-')}"
    assert [entry["name"] for entry in catalog["plugins"]] == ["mod--review-helper"]
    # Gemini is public-mirror only.
    assert "plugins/mod--review-helper/gemini-extension.json" not in files

    # Outsider: constant 404. Downgraded org: 403 upgrade_required.
    _, outsider_headers = await _user(db_session)
    assert (await client.get(f"{base}/info/refs", headers=outsider_headers)).status_code == 404
    await db_session.execute(update(Organization).where(Organization.id == org.id).values(tier="starter"))
    await db_session.commit()
    downgraded = await client.get(f"{base}/info/refs", headers=auth_headers)
    assert downgraded.status_code == 403
    assert downgraded.json()["error"]["code"] == "upgrade_required"


async def test_user_catalog_tier_gate(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    free_user, free_headers = await _user(db_session, tier="free")
    response = await client.get(f"/api/v1/git/user/{free_user.id}/catalog.git/info/refs", headers=free_headers)
    assert response.status_code == 403
    other, other_headers = await _user(db_session)
    hidden = await client.get(f"/api/v1/git/user/{free_user.id}/catalog.git/info/refs", headers=other_headers)
    assert hidden.status_code == 404
    own = await client.get(f"/api/v1/git/user/{other.id}/catalog.git/info/refs", headers=other_headers)
    assert own.status_code == 200, own.text


async def test_catalog_listing_and_feed_url(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession, test_user: User
) -> None:
    org, _ = await _org(db_session, test_user)
    listing = await client.get("/api/v1/marketplace/catalogs", headers=auth_headers)
    assert listing.status_code == 200, listing.text
    body = listing.json()
    scopes = [(item["scope"], item["scope_id"]) for item in body["items"]]
    assert scopes == [("public", None), ("user", test_user.id), ("org", org.id)]
    public = body["items"][0]
    assert public["clone_url"].endswith("/api/v1/git/public/sessionfs.git")
    assert public["tools"]["claude-code"]["add"] == f"/plugin marketplace add {public['clone_url']}"
    assert "gemini" in public["tools"] and "gemini" not in body["items"][2]["tools"]
    feed = await client.get("/api/v1/marketplace/feeds/claude-code", headers=auth_headers)
    assert feed.json()["native_catalog_url"].endswith("/api/v1/git/public/sessionfs.git")
    amp = await client.get("/api/v1/marketplace/feeds/amp", headers=auth_headers)
    assert amp.json()["native_catalog_url"] is None


async def test_works_in_l0_and_private_gate(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession, test_user: User
) -> None:
    pkg, _ = await _system_public(client, db_session, test_user)
    response = await client.get(f"/api/v1/marketplace/versions/{pkg['version_id']}/works-in",
                                headers=auth_headers)
    assert response.status_code == 200, response.text
    tools = response.json()["tools"]
    assert tools["claude-code"]["badge"] == "format-valid"
    assert tools["claude-code"]["levels"]["L0"]["status"] == "pass"
    assert set(tools) == {"claude-code", "copilot", "codex", "cursor", "gemini", "antigravity"}
    # Idempotent: viewing again writes nothing new.
    count = len((await db_session.execute(select(SkillToolVerification))).scalars().all())
    await client.get(f"/api/v1/marketplace/versions/{pkg['version_id']}/works-in", headers=auth_headers)
    assert len((await db_session.execute(select(SkillToolVerification))).scalars().all()) == count

    # Personal package of a Pro (below Team, no org) user: works-in is Team+.
    _, pro_headers = await _user(db_session, tier="pro")
    personal = await _publish_personal(client, pro_headers)
    gated = await client.get(f"/api/v1/marketplace/versions/{personal['version_id']}/works-in",
                             headers=pro_headers)
    assert gated.status_code == 403
    assert gated.json()["error"]["code"] == "upgrade_required"


async def test_harness_protocol_and_regression(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession, test_user: User
) -> None:
    pkg, platform_headers = await _system_public(client, db_session, test_user)
    # Non-admins can neither queue nor claim.
    assert (await client.post("/api/v1/admin/skills/verify", headers=auth_headers, json={})).status_code == 403
    assert (await client.post("/api/v1/admin/skills/verify/jobs/claim", headers=auth_headers,
                              json={"tool": "claude-code"})).status_code == 403

    queued = await client.post("/api/v1/admin/skills/verify", headers=platform_headers,
                               json={"version_ids": [pkg["version_id"]], "tools": ["claude-code"]})
    assert queued.status_code == 200, queued.text
    assert queued.json()["queued"] == 1
    again = await client.post("/api/v1/admin/skills/verify", headers=platform_headers,
                              json={"version_ids": [pkg["version_id"]], "tools": ["claude-code"]})
    assert again.json()["queued"] == 0  # one open job per (version, tool, level)

    async def run(status: str, tool_version: str) -> dict:
        claim = await client.post("/api/v1/admin/skills/verify/jobs/claim", headers=platform_headers,
                                  json={"tool": "claude-code"})
        [job] = claim.json()["jobs"]
        no_token = await client.get(job["projection_path"], headers=platform_headers)
        assert no_token.status_code == 404
        bundle = await client.get(job["projection_path"],
                                  headers={**platform_headers, "X-SessionFS-Claim-Token": job["claim_token"]})
        assert bundle.status_code == 200
        assert bundle.headers["x-sessionfs-plugin"] == "pub--review-helper"
        result = await client.post(
            f"/api/v1/admin/skills/verify/jobs/{job['job_id']}/result",
            headers=platform_headers,
            json={"claim_token": job["claim_token"], "status": status, "tool_version": tool_version,
                  "evidence": {"exit_code": 0 if status == "pass" else 1}},
        )
        assert result.status_code == 200, result.text
        replay = await client.post(
            f"/api/v1/admin/skills/verify/jobs/{job['job_id']}/result",
            headers=platform_headers,
            json={"claim_token": job["claim_token"], "status": "pass", "tool_version": tool_version},
        )
        assert replay.status_code == 404
        return result.json()

    assert (await run("pass", "2.1.240"))["status"] == "pass"
    works = (await client.get(f"/api/v1/marketplace/versions/{pkg['version_id']}/works-in",
                              headers=auth_headers)).json()
    assert works["tools"]["claude-code"]["badge"] == "verified"
    assert works["tools"]["claude-code"]["levels"]["L1"]["tool_version"] == "2.1.240"

    await client.post("/api/v1/admin/skills/verify", headers=platform_headers,
                      json={"version_ids": [pkg["version_id"]], "tools": ["claude-code"]})
    assert (await run("fail", "2.2.0"))["status"] == "regressed"
    works = (await client.get(f"/api/v1/marketplace/versions/{pkg['version_id']}/works-in",
                              headers=auth_headers)).json()
    assert works["tools"]["claude-code"]["badge"] == "regressed"


async def test_org_verify_quota(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession, test_user: User,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SFS_WORKS_IN_ORG_DAILY_QUOTA", "2")
    org, _ = await _org(db_session, test_user)
    pkg = await _publish_org(client, auth_headers, org)
    ok = await client.post(f"/api/v1/orgs/{org.id}/skills/verify", headers=auth_headers,
                           json={"version_ids": [pkg["version_id"]], "tools": ["claude-code", "copilot"]})
    assert ok.status_code == 200, ok.text
    assert len(ok.json()["queued"]) == 2
    over = await client.post(f"/api/v1/orgs/{org.id}/skills/verify", headers=auth_headers,
                             json={"version_ids": [pkg["version_id"]], "tools": ["codex"]})
    assert over.status_code == 429
    jobs = (await db_session.execute(select(SkillVerificationJob))).scalars().all()
    assert {job.owner_org_id for job in jobs} == {org.id}
    # Someone else's version: 404, nothing queued.
    other_org, _ = await _org(db_session, test_user)
    foreign = await client.post(f"/api/v1/orgs/{other_org.id}/skills/verify", headers=auth_headers,
                                json={"version_ids": [pkg["version_id"]], "tools": ["codex"]})
    assert foreign.status_code == 404


async def test_org_export_target(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession, test_user: User
) -> None:
    org, _ = await _org(db_session, test_user)
    bad = await client.post(f"/api/v1/orgs/{org.id}/skills/catalog/export", headers=auth_headers,
                            json={"remote": "https://user:pw@github.com/acme/catalog.git"})
    assert bad.status_code == 422
    ok = await client.post(f"/api/v1/orgs/{org.id}/skills/catalog/export", headers=auth_headers,
                           json={"remote": "https://github.com/acme/catalog.git"})
    assert ok.status_code == 200, ok.text
    assert ok.json()["command"] == f"sfs catalog export --org {org.id} --to https://github.com/acme/catalog.git"
    listing = (await client.get("/api/v1/marketplace/catalogs", headers=auth_headers)).json()
    assert listing["items"][-1]["export_remote"] == "https://github.com/acme/catalog.git"


async def test_scheduler_regenerate_and_sweep(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession, test_user: User
) -> None:
    pkg, platform_headers = await _system_public(client, db_session, test_user)
    assert (await client.post("/api/v1/admin/skills/catalogs/regenerate", headers=auth_headers,
                              json={})).status_code == 403
    regenerated = await client.post("/api/v1/admin/skills/catalogs/regenerate", headers=platform_headers,
                                    json={})
    assert regenerated.status_code == 200, regenerated.text
    [public] = regenerated.json()["catalogs"]
    assert public["scope"] == "public" and public["plugins"] == 1 and len(public["commit"]) == 40
    swept = await client.post("/api/v1/admin/skills/verify", headers=platform_headers,
                              json={"tools": ["claude-code", "gemini"]})
    assert swept.status_code == 200, swept.text
    assert swept.json()["queued"] == 2
    jobs = (await db_session.execute(select(SkillVerificationJob))).scalars().all()
    assert {(job.version_id, job.tool, job.trigger) for job in jobs} == {
        (pkg["version_id"], "claude-code", "sweep"), (pkg["version_id"], "gemini", "sweep")
    }


async def test_retired_packs_are_collected_after_one_generation(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession, test_user: User, blob_store
) -> None:
    pkg, platform_headers = await _system_public(client, db_session, test_user)

    def packs() -> set[str]:
        return {path.name for path in blob_store.root.rglob("pack-*.pack")}

    await _refs(client, PUBLIC_GIT, auth_headers)
    first = packs()
    assert (await _act(client, platform_headers, "versions", pkg["version_id"], "block")).status_code == 200
    await _refs(client, PUBLIC_GIT, auth_headers)
    second = packs()
    assert first < second  # the old generation is retired, not yet deleted
    org, _ = await _org(db_session, test_user)
    await _publish_org(client, platform_headers, org, visibility="public", namespace="pub2",
                       collection="pub2", body="Another.")
    system = await publishers_service.ensure_system_publisher(db_session)
    await db_session.execute(
        update(SkillPackage).where(SkillPackage.namespace == "pub2").values(publisher_id=system.id)
    )
    await db_session.commit()
    await _refs(client, PUBLIC_GIT, auth_headers)
    third = packs()
    assert not (first & third)  # the first generation's packs are gone
    assert (second - first) <= third


# ---------------------------------------------------------------------------
# Review fixes: failure isolation, CAS rebuilds, harness queue, harness key
# ---------------------------------------------------------------------------


async def test_build_failure_serves_the_last_good_commit(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession, test_user: User,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pkg, platform_headers = await _system_public(client, db_session, test_user)
    good = await _refs(client, PUBLIC_GIT, auth_headers)
    calls = 0

    def broken(*args, **kwargs):
        nonlocal calls
        calls += 1
        raise RuntimeError("boom")

    monkeypatch.setattr(service, "build", broken)
    # The inputs change (a second public package), the rebuild fails: the
    # scope keeps serving its last good commit, pack included.
    org, _ = await _org(db_session, test_user)
    await _publish_org(client, platform_headers, org, visibility="public", namespace="pub2",
                       collection="pub2", body="Another.")
    system = await publishers_service.ensure_system_publisher(db_session)
    await db_session.execute(update(SkillPackage).where(SkillPackage.namespace == "pub2")
                             .values(publisher_id=system.id))
    await db_session.commit()
    assert await _refs(client, PUBLIC_GIT, auth_headers) == good
    files = await _tree_paths(client, PUBLIC_GIT, auth_headers)
    assert "plugins/pub--review-helper/plugin.json" in files
    assert calls == 1
    # Within the retry window the failed inputs are not rebuilt on every fetch.
    assert await _refs(client, PUBLIC_GIT, auth_headers) == good
    assert calls == 1
    # A scope that never built successfully answers 503 (retryable), not 500.
    await db_session.execute(update(SkillFeedCatalog).values(current_commit_sha=None, input_digest=None))
    await db_session.commit()
    _clear_service_state()
    unavailable = await client.get(f"{PUBLIC_GIT}/info/refs", headers=auth_headers)
    assert unavailable.status_code == 503
    assert unavailable.headers.get("retry-after") == "30"
    assert pkg


async def test_build_failure_never_keeps_serving_a_blocked_version(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession, test_user: User,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A takedown fails closed: if the rebuild that would drop a blocked
    version fails, the stale commit carrying it is not served (503)."""
    pkg, platform_headers = await _system_public(client, db_session, test_user)
    await _refs(client, PUBLIC_GIT, auth_headers)

    def broken(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(service, "build", broken)
    assert (await _act(client, platform_headers, "versions", pkg["version_id"], "block")).status_code == 200
    _clear_service_state()
    for _ in range(2):  # the failed build, then the cached-failure path
        response = await client.get(f"{PUBLIC_GIT}/info/refs", headers=auth_headers)
        assert response.status_code == 503, response.text


async def test_rebuild_loser_of_the_swap_discards_its_build(
    client: AsyncClient, db_session: AsyncSession, test_user: User, blob_store, db_engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The build runs outside any lock; if another builder moved the row in
    the meantime, this one's compare-and-swap fails and it writes nothing."""
    from sqlalchemy.ext.asyncio import async_sessionmaker

    await _system_public(client, db_session, test_user)
    original = service._put_packs
    other = async_sessionmaker(db_engine, expire_on_commit=False)

    async def racing_put_packs(store, catalog, built):
        state = await original(store, catalog, built)
        async with other() as session:  # "another process" wins first
            await session.execute(update(SkillFeedCatalog).where(SkillFeedCatalog.id == catalog.id)
                                  .values(current_commit_sha="f" * 40, input_digest="e" * 64, plugin_count=7))
            await session.commit()
        return state

    monkeypatch.setattr(service, "_put_packs", racing_put_packs)
    catalog = await service.ensure_catalog(db_session, blob_store, "public", None, homepage_base="https://x")
    assert catalog.current_commit_sha == "f" * 40 and catalog.plugin_count == 7
    assert (await db_session.execute(select(SkillFeedEntry))).scalars().all() == []
    assert not list(blob_store.root.rglob("state.json"))  # the loser wrote no state

    monkeypatch.setattr(service, "_put_packs", original)
    rebuilt = await service.ensure_catalog(db_session, blob_store, "public", None, homepage_base="https://x")
    assert rebuilt.current_commit_sha != "f" * 40 and rebuilt.plugin_count == 1
    assert (await db_session.execute(select(SkillFeedEntry))).scalars().all()


async def _queue_and_claim(client: AsyncClient, headers: dict, version_id: str) -> dict:
    await client.post("/api/v1/admin/skills/verify", headers=headers,
                      json={"version_ids": [version_id], "tools": ["claude-code"]})
    claim = await client.post("/api/v1/admin/skills/verify/jobs/claim", headers=headers,
                              json={"tool": "claude-code"})
    assert claim.status_code == 200, claim.text
    [job] = claim.json()["jobs"]
    return job


async def test_undeliverable_job_is_skipped_not_stuck(
    client: AsyncClient, db_session: AsyncSession, test_user: User
) -> None:
    pkg, platform_headers = await _system_public(client, db_session, test_user)
    job = await _queue_and_claim(client, platform_headers, pkg["version_id"])
    assert (await _act(client, platform_headers, "versions", pkg["version_id"], "block")).status_code == 200
    gone = await client.get(job["projection_path"],
                            headers={**platform_headers, "X-SessionFS-Claim-Token": job["claim_token"]})
    assert gone.status_code == 410
    assert gone.json()["error"]["code"] == "job_skipped"
    row = await db_session.get(SkillVerificationJob, job["job_id"])
    await db_session.refresh(row)
    assert (row.status, row.outcome) == ("done", "skipped")
    assert (await db_session.execute(select(SkillToolVerification).where(
        SkillToolVerification.level == "L1"))).scalars().all() == []
    again = await client.post("/api/v1/admin/skills/verify/jobs/claim", headers=platform_headers,
                              json={"tool": "claude-code"})
    assert again.json()["jobs"] == []


async def test_harness_errors_requeue_until_the_attempt_cap(
    client: AsyncClient, db_session: AsyncSession, test_user: User
) -> None:
    from datetime import datetime, timedelta, timezone

    from sessionfs.server.routes import native_catalogs as routes

    pkg, platform_headers = await _system_public(client, db_session, test_user)
    job = await _queue_and_claim(client, platform_headers, pkg["version_id"])
    for attempt in range(1, routes.MAX_JOB_ATTEMPTS + 1):
        if attempt > 1:
            claim = await client.post("/api/v1/admin/skills/verify/jobs/claim", headers=platform_headers,
                                      json={"tool": "claude-code"})
            [job] = claim.json()["jobs"]
        result = await client.post(
            f"/api/v1/admin/skills/verify/jobs/{job['job_id']}/result", headers=platform_headers,
            json={"claim_token": job["claim_token"], "status": "error", "tool_version": "n/a",
                  "reason": "503 blob unavailable"},
        )
        assert result.status_code == 200, result.text
        assert result.json()["requeued"] is (attempt < routes.MAX_JOB_ATTEMPTS)
    row = await db_session.get(SkillVerificationJob, job["job_id"])
    await db_session.refresh(row)
    assert (row.status, row.outcome, row.attempts) == ("done", "errored", routes.MAX_JOB_ATTEMPTS)

    # A harness that could not prove its sandbox clean ends the job at once.
    final = await _queue_and_claim(client, platform_headers, pkg["version_id"])
    ended = await client.post(
        f"/api/v1/admin/skills/verify/jobs/{final['job_id']}/result", headers=platform_headers,
        json={"claim_token": final["claim_token"], "status": "error", "tool_version": "n/a",
              "reason": "sandbox breach", "final": True},
    )
    assert ended.json()["requeued"] is False
    row = await db_session.get(SkillVerificationJob, final["job_id"])
    await db_session.refresh(row)
    assert (row.status, row.outcome, row.attempts) == ("done", "errored", 1)

    # A harness that died mid-job after the last attempt: the stale claim is
    # closed out as errored instead of being handed out forever.
    second = await _queue_and_claim(client, platform_headers, pkg["version_id"])
    await db_session.execute(update(SkillVerificationJob).where(SkillVerificationJob.id == second["job_id"])
                             .values(attempts=routes.MAX_JOB_ATTEMPTS,
                                     claimed_at=datetime.now(timezone.utc) - timedelta(hours=2)))
    await db_session.commit()
    claim = await client.post("/api/v1/admin/skills/verify/jobs/claim", headers=platform_headers,
                              json={"tool": "claude-code"})
    assert claim.json()["jobs"] == []
    row = await db_session.get(SkillVerificationJob, second["job_id"])
    await db_session.refresh(row)
    assert (row.status, row.outcome) == ("done", "errored")


async def test_harness_service_key_is_narrow_and_platform_minted(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession, test_user: User
) -> None:
    pkg, platform_headers = await _system_public(client, db_session, test_user)
    platform = (await db_session.execute(select(User).where(User.tier == "admin"))).scalars().first()
    platform_org, _ = await _org(db_session, platform)
    minted = await client.post(f"/api/v1/orgs/{platform_org.id}/service-keys", headers=platform_headers,
                               json={"name": "works-in harness", "scopes": ["skills_verify:run"]})
    assert minted.status_code == 201, minted.text
    harness = {"Authorization": f"Bearer {minted.json()['key']}"}
    await client.post("/api/v1/admin/skills/verify", headers=platform_headers,
                      json={"version_ids": [pkg["version_id"]], "tools": ["claude-code"]})
    claim = await client.post("/api/v1/admin/skills/verify/jobs/claim", headers=harness,
                              json={"tool": "claude-code"})
    assert claim.status_code == 200, claim.text
    [job] = claim.json()["jobs"]
    bundle = await client.get(job["projection_path"],
                              headers={**harness, "X-SessionFS-Claim-Token": job["claim_token"]})
    assert bundle.status_code == 200
    posted = await client.post(f"/api/v1/admin/skills/verify/jobs/{job['job_id']}/result", headers=harness,
                               json={"claim_token": job["claim_token"], "status": "pass", "tool_version": "2.1"})
    assert posted.status_code == 200, posted.text
    # The harness key reaches nothing else: no catalogs, no admin sweeps.
    assert (await client.get(f"{PUBLIC_GIT}/info/refs", headers=harness)).status_code == 403
    assert (await client.post("/api/v1/admin/skills/verify", headers=harness, json={})).status_code == 403
    assert (await client.get("/api/v1/marketplace/catalogs", headers=harness)).status_code == 403

    # An org admin who is not a platform admin can neither mint one...
    org_admin, org_admin_headers = await _user(db_session, tier="pro")
    org, _ = await _org(db_session, org_admin)
    refused = await client.post(f"/api/v1/orgs/{org.id}/service-keys", headers=org_admin_headers,
                                json={"name": "h", "scopes": ["skills_verify:run"]})
    assert refused.status_code == 403, refused.text
    assert "platform_admin_scope" in refused.text
    # ...nor use one that exists anyway (jobs span every org's packages).
    sneaky = await _service(db_session, user=org_admin, org_id=org.id, scopes=["skills_verify:run"])
    assert (await client.post("/api/v1/admin/skills/verify/jobs/claim", headers=sneaky,
                              json={"tool": "claude-code"})).status_code == 403
    # Rotating the platform-minted key needs a platform admin too.
    co_admin, co_admin_headers = await _user(db_session, tier="pro")
    await _join_admin(db_session, platform_org, co_admin)
    rotate = await client.post(
        f"/api/v1/orgs/{platform_org.id}/service-keys/{minted.json()['id']}/rotate", headers=co_admin_headers)
    assert rotate.status_code == 403, rotate.text
    assert "platform_admin_scope" in rotate.text


async def _join_admin(db: AsyncSession, org: Organization, user: User) -> None:
    from sessionfs.server.db.models import OrgMember

    db.add(OrgMember(org_id=org.id, user_id=user.id, role="admin"))
    await db.commit()


async def test_platform_sweeps_do_not_consume_the_org_quota(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession, test_user: User,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SFS_WORKS_IN_ORG_DAILY_QUOTA", "2")
    org, _ = await _org(db_session, test_user)
    pkg = await _publish_org(client, auth_headers, org)
    _, platform_headers = await _user(db_session, tier="admin")
    swept = await client.post("/api/v1/admin/skills/verify", headers=platform_headers,
                              json={"tools": ["codex", "cursor", "antigravity"]})
    assert swept.status_code == 200 and swept.json()["queued"] == 3
    jobs = (await db_session.execute(select(SkillVerificationJob))).scalars().all()
    assert {(job.owner_org_id, job.trigger) for job in jobs} == {(org.id, "sweep")}
    ok = await client.post(f"/api/v1/orgs/{org.id}/skills/verify", headers=auth_headers,
                           json={"version_ids": [pkg["version_id"]], "tools": ["claude-code", "copilot"]})
    assert ok.status_code == 200, ok.text
    assert ok.json()["used"] == 2


async def test_a_waiting_builder_never_swaps_older_inputs_over_a_newer_commit(
    client: AsyncClient, db_session: AsyncSession, test_user: User, blob_store,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A builder read the approved set (old), then waited while another
    builder committed a NEWER set. Its swap baseline is taken after that
    commit, so it must re-read the inputs rather than build the old set."""
    _, platform_headers = await _system_public(client, db_session, test_user)
    old_rows = await service.approved_rows(db_session, "public", None)
    await db_session.commit()
    org, _ = await _org(db_session, test_user)
    await _publish_org(client, platform_headers, org, visibility="public", namespace="pub2",
                       collection="pub2", body="Another.")
    system = await publishers_service.ensure_system_publisher(db_session)
    await db_session.execute(update(SkillPackage).where(SkillPackage.namespace == "pub2")
                             .values(publisher_id=system.id))
    await db_session.commit()
    newer = await service.ensure_catalog(db_session, blob_store, "public", None, homepage_base="https://x")
    assert newer.plugin_count == 2
    newer_commit = newer.current_commit_sha

    real = service.approved_rows
    calls = 0

    async def stale_first(db, scope_type, scope_id):
        nonlocal calls
        calls += 1
        return old_rows if calls == 1 else await real(db, scope_type, scope_id)

    monkeypatch.setattr(service, "approved_rows", stale_first)
    _clear_service_state()
    after = await service.ensure_catalog(db_session, blob_store, "public", None, homepage_base="https://x")
    assert calls == 2  # re-read under the lock, after the baseline
    assert after.current_commit_sha == newer_commit and after.plugin_count == 2


async def test_loader_charges_only_admitted_plugins(
    client: AsyncClient, db_session: AsyncSession, test_user: User, blob_store,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """catalog_inputs (which bounds memory) drops an oversized package without
    charging it, so the next package still loads under the byte budget."""
    _, platform_headers = await _system_public(client, db_session, test_user)
    org, _ = await _org(db_session, test_user)
    await _publish_org(client, platform_headers, org, visibility="public", namespace="pub2",
                       collection="pub2", body="Another.")
    system = await publishers_service.ensure_system_publisher(db_session)
    await db_session.execute(update(SkillPackage).where(SkillPackage.namespace == "pub2")
                             .values(publisher_id=system.id))
    await db_session.commit()
    rows = await service.approved_rows(db_session, "public", None)
    assert [row[1].namespace for row in rows] == ["pub", "pub2"]
    real = service._extract_files

    def inflate_first(payload):
        files = real(payload)
        if not getattr(inflate_first, "done", False):
            inflate_first.done = True  # type: ignore[attr-defined]
            files["skills/huge.bin"] = b"x" * 50_000
        return files

    monkeypatch.setattr(service, "_extract_files", inflate_first)
    inputs = await service.catalog_inputs(blob_store, rows, homepage_base="https://x", public=True,
                                          max_bytes=20_000, max_plugin_bytes=20_000)
    assert inputs[0].skip_reason and inputs[0].skip_reason.startswith("plugin_too_large")
    assert inputs[0].files == {}
    assert inputs[1].skip_reason is None and inputs[1].files


async def test_public_mirror_leads_add_commands_and_rebuilds_the_readme(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession, test_user: User
) -> None:
    """SFS_PUBLIC_CATALOG_MIRROR (D4b): the public catalog leads with the
    keyless GitHub form, keeps the hosted URL, and the next fetch rebuilds
    the README to name the mirror. Org catalogs are unaffected."""
    org, _ = await _org(db_session, test_user)
    await _system_public(client, db_session, test_user)
    before = await _refs(client, PUBLIC_GIT, auth_headers)
    plain_readme = (await _tree_paths(client, PUBLIC_GIT, auth_headers))["README.md"].decode()
    assert "github.com" not in plain_readme
    listing = (await client.get("/api/v1/marketplace/catalogs", headers=auth_headers)).json()
    assert all(item["mirror"] is None for item in listing["items"])

    app = client._transport.app  # type: ignore[attr-defined]
    from sessionfs.server.config import ServerConfig

    app.state.config = ServerConfig(database_url="sqlite+aiosqlite://", public_catalog_mirror="SessionFS/marketplace")
    try:
        listing = (await client.get("/api/v1/marketplace/catalogs", headers=auth_headers)).json()
        public = listing["items"][0]
        assert public["mirror"] == {"repo": "SessionFS/marketplace",
                                    "url": "https://github.com/SessionFS/marketplace", "branch": "main"}
        assert public["tools"]["claude-code"]["add"] == "/plugin marketplace add SessionFS/marketplace"
        assert public["tools"]["claude-code"]["add_hosted"] == f"/plugin marketplace add {public['clone_url']}"
        assert public["tools"]["codex"]["add"] == "codex plugin marketplace add SessionFS/marketplace"
        assert public["tools"]["gemini"]["install"].startswith("gemini extensions install http")
        org_items = [item for item in listing["items"] if item["scope"] == "org"]
        assert org.id in {item["scope_id"] for item in org_items}
        assert all(item["mirror"] is None for item in org_items)
        assert "SessionFS/marketplace" not in json.dumps([item["tools"] for item in org_items])

        service._fresh_until.clear()
        after = await _refs(client, PUBLIC_GIT, auth_headers)
        assert after["refs/heads/main"] != before["refs/heads/main"]
        readme = (await _tree_paths(client, PUBLIC_GIT, auth_headers))["README.md"].decode()
        assert readme.startswith(plain_readme)
        assert "`/plugin marketplace add SessionFS/marketplace`" in readme
    finally:
        del app.state.config
    service._fresh_until.clear()
    assert await _refs(client, PUBLIC_GIT, auth_headers) == before


async def test_input_digest_is_unchanged_without_a_mirror() -> None:
    assert service.input_digest("public", []) == service.input_digest("public", [], mirror=None)
    assert service.input_digest("public", []) != service.input_digest("public", [], mirror="SessionFS/marketplace")
