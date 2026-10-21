"""Marketplace discovery and freshness (marketplace v2 §9, migration 076):
filters and categories, deterministic ranking with stable pagination, safe
README and package images, install counting with the k-floor, the freshness
sweeper and works-in intake, publisher analytics and org usage."""

from __future__ import annotations

import io
import json
import tarfile
from datetime import date, datetime, timedelta, timezone

import pytest
from httpx import AsyncClient
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server.db.models import (
    SkillInstallDaily,
    SkillInstallDedupe,
    SkillMarketplaceListing,
    SkillPackage,
    SkillPackageFreshness,
    SkillPackageToolFreshness,
    SkillPackageVersion,
    SkillPublisherVerification,
)
from sessionfs.server.services import skill_discovery
from sessionfs.skills.package import AGENT_PLUGIN_SCHEMA
from tests.server.integration.test_marketplace_moderation import _join, _org, _publish_org, _user
from tests.server.integration.test_marketplace_publishers import (
    _claim,
    _fresh_publisher_limiters,  # noqa: F401 - autouse fixture
    _verify_dns,
)

pytestmark = pytest.mark.asyncio

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


def _tar(
    name: str,
    *,
    description: str = "A helper.",
    license: str | None = "MIT",
    scripts: bool = False,
    version: str = "1.0.0",
    extra: dict[str, bytes] | None = None,
    body: str = "Do the thing.",
) -> bytes:
    manifest = {"$schema": AGENT_PLUGIN_SCHEMA, "name": name, "version": version, "description": description}
    if license is not None:
        manifest["license"] = license
    files = {
        "plugin.json": json.dumps(manifest).encode(),
        f"skills/{name}/SKILL.md": (
            f"---\nname: {name}\ndescription: {description}\n---\n\n# {name}\n\n{body}\n"
        ).encode(),
    }
    if scripts:
        files[f"skills/{name}/scripts/run.sh"] = b"#!/bin/sh\necho hi\n"
    files.update(extra or {})
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz") as archive:
        for path, payload in files.items():
            info = tarfile.TarInfo(path)
            info.size = len(payload)
            archive.addfile(info, io.BytesIO(payload))
    return output.getvalue()


async def _publish(client: AsyncClient, headers: dict, name: str, *, namespace: str = "testing", **kwargs) -> dict:
    response = await client.post(
        "/api/v1/users/me/skills/packages",
        headers=headers,
        files={"package": (f"{name}.tar.gz", _tar(name, **kwargs), "application/gzip")},
        data={"namespace": namespace, "collection": namespace},
    )
    assert response.status_code == 201, response.text
    return response.json()


async def _search(client: AsyncClient, headers: dict, **params) -> dict:
    response = await client.get("/api/v1/marketplace/packages", headers=headers, params=params)
    assert response.status_code == 200, response.text
    return response.json()


def _names(body: dict) -> list[str]:
    return [item["name"] for item in body["items"]]


# ---------------------------------------------------------------------------
# Filters and categories
# ---------------------------------------------------------------------------


async def test_filters_run_in_sql_with_facets(client: AsyncClient, auth_headers: dict, db_session: AsyncSession) -> None:
    await _publish(client, auth_headers, "alpha-mit", license="MIT")
    await _publish(client, auth_headers, "beta-apache", license="Apache-2.0", scripts=True)
    gamma = await _publish(client, auth_headers, "gamma-none", license=None)

    assert sorted(_names(await _search(client, auth_headers, license="mit"))) == ["alpha-mit"]
    assert _names(await _search(client, auth_headers, license="apache-2.0")) == ["beta-apache"]
    assert _names(await _search(client, auth_headers, license="none")) == ["gamma-none"]
    assert _names(await _search(client, auth_headers, capability="scripts")) == ["beta-apache"]
    assert sorted(_names(await _search(client, auth_headers, capability="instructions_only"))) == ["alpha-mit", "gamma-none"]

    # Severity facet follows the stored scan summary (kept in sync on flush).
    version = await db_session.get(SkillPackageVersion, gamma["version_id"])
    version.scan_summary = json.dumps({"max_severity": "high", "open_max_severity": "high"})
    await db_session.commit()
    await db_session.refresh(version)
    assert version.scan_severity_rank == 3
    assert "gamma-none" not in _names(await _search(client, auth_headers, max_severity="medium"))
    assert "gamma-none" in _names(await _search(client, auth_headers, max_severity="high"))

    # updated_within uses the listing's publication time.
    await db_session.execute(
        update(SkillMarketplaceListing)
        .where(SkillMarketplaceListing.version_id == gamma["version_id"])
        .values(published_at=datetime.now(timezone.utc) - timedelta(days=40))
    )
    await db_session.commit()
    assert "gamma-none" not in _names(await _search(client, auth_headers, updated_within=30))
    assert "gamma-none" in _names(await _search(client, auth_headers, updated_within=60))

    for params in ({"category": "nope"}, {"capability": "rootkit"}, {"works_in": "notatool"}, {"max_severity": "bad"}):
        response = await client.get("/api/v1/marketplace/packages", headers=auth_headers, params=params)
        assert response.status_code == 422, params


async def test_attested_filter_uses_listing_provenance(client: AsyncClient, auth_headers: dict, db_session: AsyncSession) -> None:
    plain = await _publish(client, auth_headers, "plain-pub")
    await _publish(client, auth_headers, "oidc-pub")
    listing = (
        await db_session.execute(select(SkillMarketplaceListing).where(SkillMarketplaceListing.name == "oidc-pub"))
    ).scalar_one()
    listing.signature_metadata = json.dumps({"kind": "github_oidc", "verification": "oidc_token"})
    await db_session.commit()
    await db_session.refresh(listing)
    assert listing.provenance_attested is True
    assert _names(await _search(client, auth_headers, attested="true")) == ["oidc-pub"]
    assert _names(await _search(client, auth_headers, attested="false")) == [plain["name"]]


async def test_categories_publisher_and_verified_filters(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    categories = await client.get("/api/v1/marketplace/categories", headers=auth_headers)
    assert categories.status_code == 200
    slugs = [item["slug"] for item in categories.json()["items"]]
    assert "review" in slugs and len(slugs) == 15

    publisher = await _claim(client, auth_headers, "discoco")
    owned = await _publish(client, auth_headers, "owned-skill", namespace="discoco")
    await _publish(client, auth_headers, "loose-skill")
    patch = await client.patch(
        f"/api/v1/publishers/{publisher['id']}/packages/{owned['package_id']}",
        headers=auth_headers,
        json={"categories": ["review", "testing"]},
    )
    assert patch.status_code == 200, patch.text
    assert patch.json()["categories"] == ["review", "testing"]

    assert _names(await _search(client, auth_headers, category="review")) == ["owned-skill"]
    assert _names(await _search(client, auth_headers, category="docs")) == []
    assert _names(await _search(client, auth_headers, publisher="discoco")) == ["owned-skill"]
    assert _names(await _search(client, auth_headers, publisher=publisher["id"])) == ["owned-skill"]
    assert _names(await _search(client, auth_headers, verified="true")) == []
    await _verify_dns(client, auth_headers, publisher["id"], "disco.example", monkeypatch)
    assert _names(await _search(client, auth_headers, verified="true")) == ["owned-skill"]
    assert _names(await _search(client, auth_headers, verified="false")) == ["loose-skill"]

    item = (await _search(client, auth_headers, q="owned"))["items"][0]
    assert item["categories"] == ["review", "testing"]
    assert item["freshness"]["stale"] is False
    assert item["installs"]["total"] == {"count": 0, "display": "0"}

    too_many = await client.patch(
        f"/api/v1/publishers/{publisher['id']}/packages/{owned['package_id']}",
        headers=auth_headers,
        json={"categories": ["review", "testing", "docs", "data"]},
    )
    assert too_many.status_code == 422
    unknown = await client.patch(
        f"/api/v1/publishers/{publisher['id']}/packages/{owned['package_id']}",
        headers=auth_headers,
        json={"categories": ["warez"]},
    )
    assert unknown.status_code == 422


async def test_package_settings_are_publisher_managed(client: AsyncClient, auth_headers: dict, db_session: AsyncSession) -> None:
    publisher = await _claim(client, auth_headers, "settingsco")
    package = await _publish(client, auth_headers, "set-skill", namespace="settingsco")
    _, other_headers = await _user(db_session)
    denied = await client.patch(
        f"/api/v1/publishers/{publisher['id']}/packages/{package['package_id']}",
        headers=other_headers,
        json={"categories": ["review"]},
    )
    assert denied.status_code == 404
    loose = await _publish(client, auth_headers, "loose-two")
    not_owned = await client.patch(
        f"/api/v1/publishers/{publisher['id']}/packages/{loose['package_id']}",
        headers=auth_headers,
        json={"categories": ["review"]},
    )
    assert not_owned.status_code == 404
    bad_path = await client.patch(
        f"/api/v1/publishers/{publisher['id']}/packages/{package['package_id']}",
        headers=auth_headers,
        json={"readme_path": "../etc/passwd"},
    )
    assert bad_path.status_code == 422


# ---------------------------------------------------------------------------
# Deprecation and successor
# ---------------------------------------------------------------------------


async def test_deprecation_successor_must_share_publisher_and_ranks_last(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession
) -> None:
    publisher = await _claim(client, auth_headers, "deprecco")
    old = await _publish(client, auth_headers, "helper-old", namespace="deprecco")
    new = await _publish(client, auth_headers, "helper-new", namespace="deprecco")
    other_pub = await _claim(client, auth_headers, "otherco")
    foreign = await _publish(client, auth_headers, "helper-foreign", namespace="otherco")
    assert other_pub["id"] != publisher["id"]

    hijack = await client.patch(
        f"/api/v1/publishers/{publisher['id']}/packages/{old['package_id']}",
        headers=auth_headers,
        json={"deprecated": True, "successor_package_id": foreign["package_id"]},
    )
    assert hijack.status_code == 422
    assert hijack.json()["error"]["code"] == "successor_not_owned"
    itself = await client.patch(
        f"/api/v1/publishers/{publisher['id']}/packages/{old['package_id']}",
        headers=auth_headers,
        json={"deprecated": True, "successor_package_id": old["package_id"]},
    )
    assert itself.status_code == 422
    ok = await client.patch(
        f"/api/v1/publishers/{publisher['id']}/packages/{old['package_id']}",
        headers=auth_headers,
        json={"deprecated": True, "deprecation_message": "Use helper-new", "successor_package_id": new["package_id"]},
    )
    assert ok.status_code == 200, ok.text
    assert ok.json()["deprecated_at"] is not None

    ranked = await _search(client, auth_headers, q="helper", sort="relevance")
    names = _names(ranked)
    assert names[-1] == "helper-old"
    deprecated = ranked["items"][-1]["deprecation"]
    assert deprecated["message"] == "Use helper-new"
    assert deprecated["successor"]["package_id"] == new["package_id"]
    assert "helper-old" not in _names(await _search(client, auth_headers, q="helper", include_deprecated="false"))

    # The successor block is only shown to callers who can see it.
    detail = await client.get(f"/api/v1/marketplace/packages/{old['package_id']}", headers=auth_headers)
    assert detail.json()["deprecation"]["successor"]["name"] == "helper-new"

    undo = await client.patch(
        f"/api/v1/publishers/{publisher['id']}/packages/{old['package_id']}",
        headers=auth_headers,
        json={"deprecated": False},
    )
    assert undo.json()["deprecated_at"] is None and undo.json()["successor_package_id"] is None


# ---------------------------------------------------------------------------
# Ranking and pagination
# ---------------------------------------------------------------------------


async def test_relevance_ranking_is_deterministic_and_paginates_stably(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in ("lint-extra", "lint", "my-lint", "lintkit", "other-tool"):
        await _publish(client, auth_headers, name, description="lint helper")
    publisher = await _claim(client, auth_headers, "rankco")
    await _publish(client, auth_headers, "lintverified", namespace="rankco", description="lint helper")
    await _verify_dns(client, auth_headers, publisher["id"], "rank.example", monkeypatch)

    first = await _search(client, auth_headers, q="lint", sort="relevance", limit=100)
    names = _names(first)
    # exact > prefix (verified first among prefixes) > contains.
    assert names[0] == "lint"
    assert names[1] == "lintverified"
    assert set(names[2:4]) == {"lint-extra", "lintkit"}
    assert names[4] == "other-tool" or names[4] == "my-lint"
    assert names.index("my-lint") < names.index("other-tool")
    assert first["sort"] == "relevance"
    again = await _search(client, auth_headers, q="lint", sort="relevance", limit=100)
    assert _names(again) == names

    walked: list[str] = []
    cursor = None
    for _ in range(10):
        params = {"q": "lint", "sort": "relevance", "limit": 2}
        if cursor:
            params["cursor"] = cursor
        page = await _search(client, auth_headers, **params)
        walked.extend(_names(page))
        cursor = page["next_cursor"]
        if not page["has_more"]:
            break
    assert walked == names

    # A recency cursor is refused by the relevance sort, and vice versa.
    recent = await _search(client, auth_headers, limit=1)
    wrong = await client.get(
        "/api/v1/marketplace/packages", headers=auth_headers,
        params={"sort": "relevance", "cursor": recent["next_cursor"]},
    )
    assert wrong.status_code == 422


async def test_recent_sort_pagination_with_filter_is_never_short(client: AsyncClient, auth_headers: dict) -> None:
    for index in range(5):
        await _publish(client, auth_headers, f"pager-{index}", scripts=index % 2 == 0)
    walked: list[str] = []
    cursor = None
    while True:
        params = {"capability": "scripts", "limit": 1}
        if cursor:
            params["cursor"] = cursor
        page = await _search(client, auth_headers, **params)
        assert page["count"] == 1
        walked.extend(_names(page))
        cursor = page["next_cursor"]
        if not page["has_more"]:
            break
    assert sorted(walked) == ["pager-0", "pager-2", "pager-4"]


# ---------------------------------------------------------------------------
# README and images
# ---------------------------------------------------------------------------


async def test_readme_is_returned_as_untrusted_text_with_visible_hidden_unicode(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession
) -> None:
    readme = (
        "# Hello\n\n<script>alert(1)</script>\n<img src=\"https://tracker.example/p.gif\">\n"
        "[x](javascript:alert(1)) safe‮text\n![logo](assets/logo.png)\n"
    ).encode()
    package = await _publish(
        client, auth_headers, "readme-skill",
        extra={"README.md": readme, "assets/logo.png": PNG, "assets/evil.svg": b"<svg onload=alert(1)/>"},
    )
    response = await client.get(f"/api/v1/marketplace/packages/{package['package_id']}/readme", headers=auth_headers)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["source"] == "readme" and body["path"] == "README.md"
    assert body["untrusted"] is True
    # The server never renders: markup stays inert text for a no-raw-HTML renderer.
    assert "<script>" in body["content"]
    assert "‮" not in body["content"] and "⟦U+202E⟧" in body["content"]
    assert body["hidden_unicode"] == [{"line": 5, "column": 30, "code_point": "U+202E", "kind": "bidi"}]
    prefix = body["image_path_prefix"]
    image = await client.get(prefix.replace("/api/v1", "/api/v1") + "assets/logo.png", headers=auth_headers)
    assert image.status_code == 200
    assert image.headers["content-type"] == "image/png"
    assert image.headers["x-content-type-options"] == "nosniff"
    assert "sandbox" in image.headers["content-security-policy"]
    for path in ("assets/evil.svg", "../plugin.json", "plugin.json", "assets/missing.png"):
        refused = await client.get(
            f"/api/v1/marketplace/versions/{package['version_id']}/assets", headers=auth_headers, params={"path": path}
        )
        assert refused.status_code == 404, path

    _, other_headers = await _user(db_session)
    hidden = await client.get(f"/api/v1/marketplace/packages/{package['package_id']}/readme", headers=other_headers)
    assert hidden.status_code == 404
    hidden_image = await client.get(prefix + "assets/logo.png", headers=other_headers)
    assert hidden_image.status_code == 404


async def test_readme_falls_back_to_skill_md_and_honours_readme_path(client: AsyncClient, auth_headers: dict) -> None:
    package = await _publish(client, auth_headers, "fallback-skill", body="Body text here.")
    body = (await client.get(f"/api/v1/marketplace/packages/{package['package_id']}/readme", headers=auth_headers)).json()
    assert body["source"] == "skill_md"
    assert body["path"] == "skills/fallback-skill/SKILL.md"
    assert body["content"].startswith("# fallback-skill")
    assert "description:" not in body["content"]

    publisher = await _claim(client, auth_headers, "readmeco")
    custom = await _publish(
        client, auth_headers, "custom-readme", namespace="readmeco", extra={"docs/GUIDE.md": b"# Guide\n"}
    )
    await client.patch(
        f"/api/v1/publishers/{publisher['id']}/packages/{custom['package_id']}",
        headers=auth_headers, json={"readme_path": "docs/GUIDE.md"},
    )
    guide = (await client.get(f"/api/v1/marketplace/packages/{custom['package_id']}/readme", headers=auth_headers)).json()
    assert guide["path"] == "docs/GUIDE.md" and guide["content"] == "# Guide\n"


# ---------------------------------------------------------------------------
# Install events and counts
# ---------------------------------------------------------------------------


async def test_install_events_dedupe_and_k_floor(client: AsyncClient, auth_headers: dict, db_session: AsyncSession) -> None:
    package = await _publish(client, auth_headers, "counted-skill")
    url = f"/api/v1/marketplace/versions/{package['version_id']}/events"
    first = await client.post(url, headers=auth_headers, json={"event": "install", "tools": ["claude-code", "codex"]})
    assert first.status_code == 202 and first.json()["counted"] == 2
    repeat = await client.post(url, headers=auth_headers, json={"event": "install", "tools": ["claude-code"]})
    assert repeat.json()["counted"] == 0
    bad = await client.post(url, headers=auth_headers, json={"event": "install", "tools": ["notepad"]})
    assert bad.status_code == 422

    item = (await _search(client, auth_headers, q="counted"))["items"][0]
    assert item["installs"]["by_tool"]["claude-code"] == {"count": None, "display": "<10"}
    assert item["installs"]["total"]["display"] == "<10"

    # Ten distinct principals clear the floor.
    for index in range(9):
        assert await skill_discovery.record_event(
            db_session, package_id=package["package_id"], version_id=package["version_id"], tool="claude-code",
            event="install", source="download", actor_type="user", principal_id=f"user-{index}", org_id=None,
        )
    await db_session.commit()
    item = (await _search(client, auth_headers, q="counted"))["items"][0]
    # Ten distinct principals clear the floor, but the codex cell (1) is
    # still hidden, so every shown count is coarsened.
    assert item["installs"]["by_tool"]["claude-code"] == {"count": 10, "display": "10+", "approximate": True}
    assert item["installs"]["by_tool"]["codex"] == {"count": None, "display": "<10"}
    rows = (await db_session.execute(select(SkillInstallDaily))).scalars().all()
    assert {row.count for row in rows if row.tool == "claude-code"} == {10}

    # Uninstall of a version the caller cannot see: accepted, not counted.
    _, other_headers = await _user(db_session)
    invisible = await client.post(url, headers=other_headers, json={"event": "uninstall", "tools": ["codex"]})
    assert invisible.status_code == 202 and invisible.json()["counted"] == 0
    denied = await client.post(url, headers=other_headers, json={"event": "install", "tools": ["codex"]})
    assert denied.status_code == 404


# ---------------------------------------------------------------------------
# Freshness sweeper and works-in intake
# ---------------------------------------------------------------------------


async def test_freshness_sweeper_flags_stale_and_unmaintained(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    publisher = await _claim(client, auth_headers, "freshco")
    package = await _publish(client, auth_headers, "aging-skill", namespace="freshco")
    _, admin_headers = await _user(db_session, tier="admin")

    forbidden = await client.post("/api/v1/admin/marketplace/freshness/sweep", headers=auth_headers, json={})
    assert forbidden.status_code == 403

    results = await client.post(
        "/api/v1/admin/marketplace/works-in-results", headers=admin_headers,
        json={"results": [{"version_id": package["version_id"], "tool": "claude-code", "passed": True}]},
    )
    assert results.status_code == 200 and results.json()["results"][0]["verify_status"] == "passing"
    assert _names(await _search(client, auth_headers, works_in="claude-code")) == ["aging-skill"]
    failed = await client.post(
        "/api/v1/admin/marketplace/works-in-results", headers=admin_headers,
        json={"results": [{"version_id": package["version_id"], "tool": "claude-code", "passed": False, "detail": "skill not loaded"}]},
    )
    assert failed.json()["results"][0]["verify_status"] == "regressed"
    assert _names(await _search(client, auth_headers, works_in="claude-code")) == []

    swept = await client.post("/api/v1/admin/marketplace/freshness/sweep", headers=admin_headers, json={})
    assert swept.status_code == 200 and swept.json()["done"] is True
    fresh = await db_session.get(SkillPackageFreshness, package["package_id"])
    await db_session.refresh(fresh)
    assert fresh.works_in_regressed is True and fresh.stale is False  # recent version

    # Age the only version past 180 days: regressed + old = stale.
    old = datetime.now(timezone.utc) - timedelta(days=200)
    await db_session.execute(
        update(SkillPackageVersion).where(SkillPackageVersion.id == package["version_id"]).values(created_at=old)
    )
    # Lapse the publisher's only verification: unmaintained.
    await _verify_dns(client, auth_headers, publisher["id"], "fresh.example", monkeypatch)
    await db_session.execute(
        update(SkillPublisherVerification)
        .where(SkillPublisherVerification.publisher_id == publisher["id"])
        .values(status="lapsed")
    )
    db_session.add(
        SkillInstallDedupe(
            principal_hash="a" * 64, version_id=package["version_id"], tool="codex", event="install",
            day=date.today() - timedelta(days=45), package_id=package["package_id"],
        )
    )
    await db_session.commit()
    swept = (await client.post("/api/v1/admin/marketplace/freshness/sweep", headers=admin_headers, json={})).json()
    assert swept["stale"] == 1 and swept["unmaintained"] == 1 and swept["pruned_dedupe_rows"] == 1
    item = (await _search(client, auth_headers, q="aging"))["items"][0]
    assert item["freshness"]["stale"] is True
    assert item["freshness"]["unmaintained"] is True
    tool = item["freshness"]["tools"]["claude-code"]
    assert tool["verify_status"] == "regressed" and tool["stale"] is True
    assert tool["last_verified_at"] is not None and tool["last_updated_at"] is not None
    assert item["freshness"]["tools"]["codex"]["verify_status"] == "unknown"


async def test_works_in_results_ignore_non_current_versions(client: AsyncClient, auth_headers: dict, db_session: AsyncSession) -> None:
    v1 = await _publish(client, auth_headers, "versioned", version="1.0.0")
    v2 = await _publish(client, auth_headers, "versioned", version="2.0.0", body="v2")
    assert v1["package_id"] == v2["package_id"]
    _, admin_headers = await _user(db_session, tier="admin")
    response = await client.post(
        "/api/v1/admin/marketplace/works-in-results", headers=admin_headers,
        json={"results": [
            {"version_id": v1["version_id"], "tool": "codex", "passed": True},
            {"version_id": "skv_missing", "tool": "codex", "passed": True},
        ]},
    )
    reasons = {r["version_id"]: r.get("reason") for r in response.json()["results"]}
    assert reasons[v1["version_id"]] == "not_current_version"
    assert reasons["skv_missing"] == "unknown_version"
    assert await db_session.get(SkillPackageToolFreshness, (v1["package_id"], "codex")) is None


async def test_source_archived_only_counts_for_verified_github_publishers(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession
) -> None:
    package = await _publish(client, auth_headers, "archived-skill")
    _, admin_headers = await _user(db_session, tier="admin")
    await db_session.execute(
        update(SkillPackageVersion).where(SkillPackageVersion.id == package["version_id"])
        .values(created_at=datetime.now(timezone.utc) - timedelta(days=365))
    )
    await db_session.commit()
    marked = await client.post(
        "/api/v1/admin/marketplace/source-status", headers=admin_headers,
        json={"items": [{"package_id": package["package_id"], "archived": True}]},
    )
    assert marked.json()["updated"] == 1
    swept = (await client.post("/api/v1/admin/marketplace/freshness/sweep", headers=admin_headers, json={})).json()
    assert swept["stale"] == 0  # no verified GitHub publisher behind it


# ---------------------------------------------------------------------------
# Publisher analytics and org usage
# ---------------------------------------------------------------------------


async def test_publisher_analytics_is_manager_only_floored_and_tier_gated(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession, test_user
) -> None:
    publisher = await _claim(client, auth_headers, "statsco")
    package = await _publish(client, auth_headers, "stats-skill", namespace="statsco")
    for index in range(12):
        await skill_discovery.record_event(
            db_session, package_id=package["package_id"], version_id=package["version_id"], tool="codex",
            event="install", source="download", actor_type="user", principal_id=f"p{index}", org_id=None,
        )
    for index in range(3):
        await skill_discovery.record_event(
            db_session, package_id=package["package_id"], version_id=package["version_id"], tool="codex",
            event="uninstall", source="download", actor_type="user", principal_id=f"p{index}", org_id="org_x",
        )
    await skill_discovery.record_event(
        db_session, package_id=package["package_id"], version_id=package["version_id"], tool="cursor",
        event="translation_failure", source="download", actor_type="user", principal_id="p1", org_id=None,
    )
    await skill_discovery.record_event(
        db_session, package_id=package["package_id"], version_id=package["version_id"], tool="codex",
        event="install", source="reconcile", actor_type="service", principal_id="svc1", org_id="org_x",
    )
    await db_session.commit()

    response = await client.get(f"/api/v1/publishers/{publisher['id']}/analytics", headers=auth_headers)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["totals"]["installs"] == {"count": 12, "display": "12"}
    assert body["totals"]["uninstalls"]["display"] == "<10"
    # 3 uninstalls is under the floor: no rate (it would reveal them).
    assert body["totals"]["uninstall_rate"] is None
    assert body["totals"]["translation_failures"]["display"] == "<10"
    assert body["totals"]["active_installs"] == {"count": 12, "display": "12"}
    assert body["totals"]["service_installs"]["display"] == "<10"
    codex = next(row for row in body["by_tool"] if row["tool"] == "codex")
    assert codex["installs"]["count"] == 12
    assert len(body["by_day"]) == 30
    pkg = body["packages"][0]
    assert pkg["installs_by_version"][0]["installs"]["count"] == 12
    assert pkg["translation_failures_by_tool"] == {"cursor": {"count": None, "display": "<10"}}
    assert pkg["categories"] == [] and pkg["successor_package_id"] is None
    assert "org_x" not in response.text  # consuming orgs are never shown

    _, other_headers = await _user(db_session)
    assert (await client.get(f"/api/v1/publishers/{publisher['id']}/analytics", headers=other_headers)).status_code == 404

    # Personal publisher analytics need Starter+ (marketplace_access).
    test_user.tier = "free"
    db_session.add(test_user)
    await db_session.commit()
    gated = await client.get(f"/api/v1/publishers/{publisher['id']}/analytics", headers=auth_headers)
    assert gated.status_code == 403


async def test_org_usage_is_admin_only_and_never_shows_private_packages(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession, test_user
) -> None:
    org, project = await _org(db_session, test_user)
    member, member_headers = await _user(db_session)
    await _join(db_session, org, member, "member")
    outsider, outsider_headers = await _user(db_session)
    org_package = await _publish_org(client, auth_headers, org)
    sent = await client.post(
        f"/api/v1/marketplace/versions/{org_package['version_id']}/events", headers=member_headers,
        json={"event": "install", "tools": ["codex"], "project_id": project.id},
    )
    assert sent.json()["counted"] == 1
    # A member's PERSONAL package counts toward no org.
    personal = await _publish(client, auth_headers, "my-private")
    await client.post(
        f"/api/v1/marketplace/versions/{personal['version_id']}/events", headers=auth_headers,
        json={"event": "install", "tools": ["codex"], "project_id": project.id},
    )
    # Another org cannot pull this org's private package into its usage,
    # even with a stored attribution pointing at it.
    other_org, _ = await _org(db_session, outsider)
    await skill_discovery.record_event(
        db_session, package_id=org_package["package_id"], version_id=org_package["version_id"], tool="cursor",
        event="install", source="download", actor_type="user", principal_id="x", org_id=other_org.id,
    )
    await db_session.commit()
    usage = await client.get(f"/api/v1/orgs/{org.id}/skills/usage", headers=auth_headers)
    assert usage.status_code == 200, usage.text
    items = usage.json()["items"]
    assert [item["package_id"] for item in items] == [org_package["package_id"]]
    assert items[0]["installs"] == 1
    leaked = await client.get(f"/api/v1/orgs/{other_org.id}/skills/usage", headers=outsider_headers)
    assert leaked.status_code == 200 and leaked.json()["items"] == []
    assert (await client.get(f"/api/v1/orgs/{org.id}/skills/usage", headers=member_headers)).status_code == 403
    assert (await client.get(f"/api/v1/orgs/{org.id}/skills/usage", headers=outsider_headers)).status_code == 404


async def test_totals_are_rounded_when_a_part_is_hidden(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession
) -> None:
    publisher = await _claim(client, auth_headers, "roundco")
    package = await _publish(client, auth_headers, "round-skill", namespace="roundco")
    for tool, count in (("codex", 12), ("cursor", 3)):
        for index in range(count):
            await skill_discovery.record_event(
                db_session, package_id=package["package_id"], version_id=package["version_id"], tool=tool,
                event="install", source="download", actor_type="user", principal_id=f"{tool}{index}", org_id=None,
            )
    await db_session.commit()
    body = (await client.get(f"/api/v1/publishers/{publisher['id']}/analytics", headers=auth_headers)).json()
    # codex 12 exact, cursor hidden: an exact 15 would reveal cursor = 3.
    assert body["totals"]["installs"] == {"count": 10, "display": "10+", "approximate": True}
    pkg = body["packages"][0]
    assert pkg["installs"]["approximate"] is True
    assert pkg["installs_by_tool"]["cursor"] == {"count": None, "display": "<10"}
    item = (await _search(client, auth_headers, q="round-skill"))["items"][0]
    assert item["installs"]["total"] == {"count": 10, "display": "10+", "approximate": True}


async def _installs(db_session, package: dict, *, tool: str, count: int, prefix: str,
                    version_id: str | None = None, day: date | None = None) -> None:
    for index in range(count):
        await skill_discovery.record_event(
            db_session, package_id=package["package_id"], version_id=version_id or package["version_id"],
            tool=tool, event="install", source="download", actor_type="user",
            principal_id=f"{prefix}{index}", org_id=None, day=day,
        )
    await db_session.commit()


async def test_listing_and_analytics_cannot_be_differenced(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession
) -> None:
    """Reviewer scenario: codex 18 on the listing vs v2 = 15 / v1 = <10 in
    analytics would reveal v1 = 3. One hidden cell coarsens both views."""
    publisher = await _claim(client, auth_headers, "diffco")
    v1 = await _publish(client, auth_headers, "diff-skill", namespace="diffco", version="1.0.0")
    v2 = await _publish(client, auth_headers, "diff-skill", namespace="diffco", version="2.0.0", body="v2")
    await _installs(db_session, v1, tool="codex", count=3, prefix="old", version_id=v1["version_id"])
    await _installs(db_session, v2, tool="codex", count=15, prefix="new", version_id=v2["version_id"])
    item = (await _search(client, auth_headers, q="diff-skill"))["items"][0]
    assert item["installs"]["total"] == {"count": 10, "display": "10+", "approximate": True}
    assert item["installs"]["by_tool"]["codex"] == {"count": 10, "display": "10+", "approximate": True}
    body = (await client.get(f"/api/v1/publishers/{publisher['id']}/analytics", headers=auth_headers)).json()
    versions = {row["version_id"]: row["installs"] for row in body["packages"][0]["installs_by_version"]}
    assert versions[v2["version_id"]] == {"count": 10, "display": "10+", "approximate": True}
    assert versions[v1["version_id"]] == {"count": None, "display": "<10"}
    assert body["packages"][0]["installs_by_tool"]["codex"]["display"] == "10+"
    assert body["totals"]["installs"]["display"] == "10+"


async def test_package_totals_and_active_installs_are_coarsened_together(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession
) -> None:
    """Reviewer scenario: A = 15 and B = 3 under one publisher; an exact
    total of 18 (installs or active accounts) would reveal B."""
    publisher = await _claim(client, auth_headers, "pairco")
    a = await _publish(client, auth_headers, "pair-a", namespace="pairco")
    b = await _publish(client, auth_headers, "pair-b", namespace="pairco")
    await _installs(db_session, a, tool="codex", count=15, prefix="a")
    await _installs(db_session, b, tool="codex", count=3, prefix="b")
    body = (await client.get(f"/api/v1/publishers/{publisher['id']}/analytics", headers=auth_headers)).json()
    packages = {item["name"]: item for item in body["packages"]}
    assert packages["pair-a"]["installs"]["display"] == "10+"
    assert packages["pair-b"]["installs"]["display"] == "<10"
    assert body["totals"]["installs"] == {"count": 10, "display": "10+", "approximate": True}
    assert body["totals"]["active_installs"] == {"count": 10, "display": "10+", "approximate": True}
    codex = next(row for row in body["by_tool"] if row["tool"] == "codex")
    assert codex["installs"]["display"] == "10+"


async def test_analytics_windows_are_fixed_and_a_hidden_day_coarsens_the_window(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession
) -> None:
    publisher = await _claim(client, auth_headers, "windowco")
    package = await _publish(client, auth_headers, "window-skill", namespace="windowco")
    url = f"/api/v1/publishers/{publisher['id']}/analytics"
    for days in (1, 14, 29, 31, 365):
        response = await client.get(url, headers=auth_headers, params={"days": days})
        assert response.status_code == 422, days
    await _installs(db_session, package, tool="codex", count=12, prefix="recent")
    await _installs(db_session, package, tool="codex", count=3, prefix="older",
                    day=date.today() - timedelta(days=20))
    week = (await client.get(url, headers=auth_headers, params={"days": 7})).json()
    month = (await client.get(url, headers=auth_headers, params={"days": 30})).json()
    assert week["totals"]["installs"] == {"count": 12, "display": "12"}
    # 30 days holds a hidden day (3): an exact 15 minus the week's 12 would
    # reveal it, so the month is approximate.
    assert month["totals"]["installs"] == {"count": 10, "display": "10+", "approximate": True}
    hidden_days = [row for row in month["by_day"] if row["installs"]["display"] == "<10"]
    assert len(hidden_days) == 1


async def test_works_in_is_bound_to_the_current_version(client: AsyncClient, auth_headers: dict, db_session: AsyncSession) -> None:
    v1 = await _publish(client, auth_headers, "bound-skill", version="1.0.0")
    _, admin_headers = await _user(db_session, tier="admin")
    await client.post(
        "/api/v1/admin/marketplace/works-in-results", headers=admin_headers,
        json={"results": [{"version_id": v1["version_id"], "tool": "codex", "passed": True}]},
    )
    assert _names(await _search(client, auth_headers, works_in="codex")) == ["bound-skill"]
    v2 = await _publish(client, auth_headers, "bound-skill", version="2.0.0", body="v2")
    assert v2["package_id"] == v1["package_id"]
    assert _names(await _search(client, auth_headers, works_in="codex")) == []
    detail = (await client.get(f"/api/v1/marketplace/packages/{v1['package_id']}", headers=auth_headers)).json()
    assert detail["freshness"]["tools"]["codex"]["verify_status"] == "unknown"
    # A failure of the new version after the old one passed is a regression.
    await client.post(
        "/api/v1/admin/marketplace/works-in-results", headers=admin_headers,
        json={"results": [{"version_id": v2["version_id"], "tool": "codex", "passed": False}]},
    )
    detail = (await client.get(f"/api/v1/marketplace/packages/{v1['package_id']}", headers=auth_headers)).json()
    assert detail["freshness"]["tools"]["codex"]["verify_status"] == "regressed"


async def test_deprecation_message_refuses_control_and_bidi_characters(client: AsyncClient, auth_headers: dict) -> None:
    publisher = await _claim(client, auth_headers, "cleanco")
    package = await _publish(client, auth_headers, "clean-skill", namespace="cleanco")
    url = f"/api/v1/publishers/{publisher['id']}/packages/{package['package_id']}"
    for message in ("evil\x1b[2Jclear", "rtl‮trick", "zero​width", "two\nlines", "c1\x9bx", "tag\U000e0041"):
        response = await client.patch(url, headers=auth_headers, json={"deprecated": True, "deprecation_message": message})
        assert response.status_code == 422, message
    ok = await client.patch(url, headers=auth_headers, json={"deprecated": True, "deprecation_message": "Use v2 — thanks ❤️"})
    assert ok.status_code == 200 and ok.json()["deprecation_message"] == "Use v2 — thanks ❤️"


async def test_archive_reads_are_streamed_cached_and_admission_limited(
    client: AsyncClient, auth_headers: dict, blob_store, monkeypatch: pytest.MonkeyPatch
) -> None:
    package = await _publish(
        client, auth_headers, "cached-skill", extra={"README.md": b"# Cached\n", "assets/logo.png": PNG},
    )
    fetches: list[str] = []
    original_get = blob_store.get

    async def counting_get(key: str):
        fetches.append(key)
        return await original_get(key)

    monkeypatch.setattr(blob_store, "get", counting_get)
    readme_url = f"/api/v1/marketplace/packages/{package['package_id']}/readme"
    asset_url = f"/api/v1/marketplace/versions/{package['version_id']}/assets"
    for _ in range(3):
        assert (await client.get(readme_url, headers=auth_headers)).json()["content"] == "# Cached\n"
        assert (await client.get(asset_url, headers=auth_headers, params={"path": "assets/logo.png"})).status_code == 200
        assert (await client.get(asset_url, headers=auth_headers, params={"path": "assets/none.png"})).status_code == 404
    # One archive read per (version, file), including the negative lookup.
    assert len(fetches) == 3
    monkeypatch.setattr(skill_discovery, "ARCHIVE_READ_SLOTS", 0)
    busy = await client.get(asset_url, headers=auth_headers, params={"path": "assets/other.png"})
    assert busy.status_code == 503
    assert busy.headers["retry-after"] == str(skill_discovery.DISCOVERY_RETRY_AFTER_SECONDS)
    assert busy.json()["error"]["code"] == "discovery_busy"
    # Cached results are still served while saturated.
    assert (await client.get(readme_url, headers=auth_headers)).status_code == 200


async def test_package_facets_follow_the_json_columns(db_session: AsyncSession, client: AsyncClient, auth_headers: dict) -> None:
    package = await _publish(client, auth_headers, "facet-skill", scripts=True, license=" GPL-3.0  only ")
    version = await db_session.get(SkillPackageVersion, package["version_id"])
    assert version.license_key == "gpl-3.0 only"
    assert version.capability_flags == ",scripts,"
    pkg = await db_session.get(SkillPackage, package["package_id"])
    assert pkg.deprecated_at is None
