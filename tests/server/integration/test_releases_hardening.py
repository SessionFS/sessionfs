"""Hardening regressions for releases routes/services and delivery ingest."""

from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from tests.server.integration.test_releases import (
    _add_org_member,
    _hdrs,
    _make_org,
    _make_project,
    _make_service_key,
    _make_user,
)


@pytest.mark.asyncio
async def test_service_key_cross_org_and_allowlist_get_opaque_404(
    client: AsyncClient, db_session: AsyncSession
):
    admin, _ = await _make_user(db_session)
    org = await _make_org(db_session)
    await _add_org_member(db_session, org, admin, "admin")
    project = await _make_project(db_session, admin, org=org)

    other_org = await _make_org(db_session)
    other_admin, _ = await _make_user(db_session)
    await _add_org_member(db_session, other_org, other_admin, "admin")
    _, cross_key = await _make_service_key(
        db_session, other_org, other_admin, scopes=["loops:read", "loops:write"]
    )
    _, listed_key = await _make_service_key(
        db_session, org, admin, scopes=["loops:read", "loops:write"],
        project_ids=["proj_other"],
    )

    base = "/api/v1/projects"
    for key in (cross_key, listed_key):
        missing = await client.get(f"{base}/proj_nope/releases", headers=_hdrs(key))
        assert missing.status_code == 404
        for method, path, body in (
            ("get", f"{project.id}/releases", None),
            ("get", f"{project.id}/releases/1.0/ship-report", None),
            ("get", f"{project.id}/ship-report?month=2026-01", None),
            ("post", f"{project.id}/releases", {"version": "1", "tag_sha": "a" * 40}),
            ("post", f"{project.id}/pull-requests",
             {"provider": "github", "repo": "o/r", "number": 1, "title": "t"}),
        ):
            kw = {"json": body} if body else {}
            r = await getattr(client, method)(f"{base}/{path}", headers=_hdrs(key), **kw)
            assert r.status_code == 404, (path, r.text)
            assert r.json() == missing.json(), path


# ── service-level replay / order / validation ──


from datetime import datetime, timedelta, timezone  # noqa: E402

from sqlalchemy import select  # noqa: E402

from sessionfs.server.db.models import PullRequest, Release  # noqa: E402
from sessionfs.server.services import releases  # noqa: E402


async def _project(db: AsyncSession):
    u, _ = await _make_user(db)
    org = await _make_org(db)
    await _add_org_member(db, org, u, "admin")
    return await _make_project(db, u, org=org)


async def _pr(db, pid, **kw):
    base = dict(project_id=pid, provider="github", repo="Acme/Widgets", number=7, title="t")
    base.update(kw)
    return await releases.upsert_pull_request(db, **base)


@pytest.mark.asyncio
async def test_merged_pr_never_reverts_and_repo_lowercased(db_session: AsyncSession):
    p = await _project(db_session)
    await _pr(db_session, p.id, state="merged", head_sha="b" * 40)
    await _pr(db_session, p.id, state="open", head_sha="a" * 40)  # delayed older hook
    await _pr(db_session, p.id, repo="acme/widgets", state="closed")
    rows = (await db_session.execute(select(PullRequest))).scalars().all()
    assert len(rows) == 1  # case-insensitive: one row
    assert rows[0].repo == "acme/widgets"
    assert rows[0].state == "merged"
    assert rows[0].head_sha == "b" * 40


@pytest.mark.asyncio
async def test_older_provider_updated_at_skipped(db_session: AsyncSession):
    p = await _project(db_session)
    t1 = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    await _pr(db_session, p.id, title="new", head_sha="b" * 40, provider_updated_at=t1)
    await _pr(
        db_session, p.id, title="old", head_sha="a" * 40,
        provider_updated_at=t1 - timedelta(minutes=5),
    )
    row = (await db_session.execute(select(PullRequest))).scalars().one()
    assert (row.title, row.head_sha) == ("new", "b" * 40)
    await _pr(
        db_session, p.id, title="newer", head_sha="c" * 40,
        provider_updated_at=t1 + timedelta(minutes=5),
    )
    await db_session.refresh(row)
    assert (row.title, row.head_sha) == ("newer", "c" * 40)


@pytest.mark.asyncio
async def test_record_release_does_not_wipe_or_take_over(db_session: AsyncSession):
    p = await _project(db_session)
    when = datetime(2026, 1, 1, tzinfo=timezone.utc)
    await releases.record_release(
        db_session, project_id=p.id, version="1.0", tag_sha="a" * 40,
        previous_tag_sha="9" * 40, released_at=when, source="manual",
    )
    await releases.record_release(
        db_session, project_id=p.id, version="1.0", tag_sha="",
        source="github_release",
    )
    row = (await db_session.execute(select(Release))).scalars().one()
    assert row.tag_sha == "a" * 40
    assert row.previous_tag_sha == "9" * 40
    assert row.released_at is not None
    assert row.source == "manual"


@pytest.mark.asyncio
async def test_unsafe_urls_dropped_and_strings_sanitized(db_session: AsyncSession):
    p = await _project(db_session)
    r = await _pr(
        db_session, p.id, external_url="javascript:alert(1)", branch="fe\x00at\ud800",
    )
    assert r["external_url"] is None
    assert "\x00" not in r["branch"]
    ok = await _pr(db_session, p.id, number=8, external_url="https://x.test/pr/8")
    assert ok["external_url"] == "https://x.test/pr/8"
    long = await _pr(db_session, p.id, number=9, external_url="https://x.test/" + "a" * 2100)
    assert long["external_url"] is None
    with pytest.raises(ValueError):
        await _pr(db_session, p.id, number=2**31)


@pytest.mark.asyncio
async def test_api_rejects_bad_url_number_and_pr_numbers(
    client: AsyncClient, db_session: AsyncSession
):
    u, key = await _make_user(db_session)
    project = await _make_project(db_session, u)
    base = f"/api/v1/projects/{project.id}"
    pr = {"provider": "github", "repo": "o/r", "number": 1, "title": "t"}
    r = await client.post(f"{base}/pull-requests", headers=_hdrs(key),
                          json={**pr, "external_url": "javascript:alert(1)"})
    assert r.status_code == 422
    r = await client.post(f"{base}/pull-requests", headers=_hdrs(key),
                          json={**pr, "number": 2**31})
    assert r.status_code == 422
    r = await client.post(f"{base}/releases", headers=_hdrs(key),
                          json={"version": "1", "tag_sha": "a" * 40, "pr_numbers": list(range(1, 202))})
    assert r.status_code == 422
    r = await client.post(f"{base}/releases", headers=_hdrs(key),
                          json={"version": "1", "tag_sha": "a" * 40})
    assert r.status_code == 200
    r = await client.post(f"{base}/releases/1/gates", headers=_hdrs(key),
                          json={"kind": "ci", "url": "data:text/html,x"})
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_manual_release_requires_full_commit_sha(
    client: AsyncClient, db_session: AsyncSession
):
    owner, key = await _make_user(db_session)
    project = await _make_project(db_session, owner)
    base = f"/api/v1/projects/{project.id}"
    for bad in ("main", "abc1234", "z" * 40):
        r = await client.post(f"{base}/releases", headers=_hdrs(key),
                              json={"version": "1", "tag_sha": bad})
        assert r.status_code == 422, bad
    r = await client.post(f"{base}/releases", headers=_hdrs(key),
                          json={"version": "1", "tag_sha": "A" * 40})
    assert r.status_code == 200, r.text
    assert r.json()["tag_sha"] == "a" * 40
