"""Ordering of merge check posts (GitHub check runs + GitLab commit statuses).

Each evaluation of a commit takes a monotonic sequence before readiness is
read; the post claims ``posted_seq`` with a guarded UPDATE right before it is
sent. An older evaluation is skipped once a newer one posted, a retry of the
same evaluation goes through, and a post overtaken in flight is repaired by
re-sending the newest claimed state.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server import gitlab_client as gc
from sessionfs.server.db.models import MergeCheckPost
from sessionfs.server.services import delivery_ingest, merge_checks
from sessionfs.server.services.merge_check_order import PostGuard, take_eval_seq
from tests.server.integration import test_gitlab_commit_status as gl
from tests.server.integration import test_merge_checks as mc

REPO = "acme/widgets"


@pytest.fixture
def gh(monkeypatch) -> mc._CheckRuns:
    fake = mc._CheckRuns()

    async def _token(installation_id: int) -> str:
        return "tok"

    async def _grant(token: str, full_name: str) -> dict:
        return {"full_name": full_name}

    monkeypatch.setattr(merge_checks, "_http_transport", fake.transport())
    monkeypatch.setattr(merge_checks, "get_installation_token", _token)
    monkeypatch.setattr(merge_checks, "_find_installation_repo", _grant)
    return fake


async def _gh_guards(db: AsyncSession, project_id: str) -> tuple[PostGuard, PostGuard]:
    out = []
    for _ in range(2):
        seq = await take_eval_seq(
            db, project_id=project_id, provider="github", repo=REPO, head_sha=mc.HEAD
        )
        await db.commit()
        out.append(
            PostGuard(
                db=db, project_id=project_id, provider="github", repo=REPO,
                head_sha=mc.HEAD, seq=seq,
            )
        )
    assert out[1].seq == out[0].seq + 1
    return out[0], out[1]


def _conclusions(gh: mc._CheckRuns) -> list[str]:
    return [r["conclusion"] for r in gh.runs_by_sha.get(mc.HEAD, [])]


# ── GitHub ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_github_newer_posts_first_older_is_skipped(
    client: AsyncClient, db_session: AsyncSession, gh: mc._CheckRuns
):
    _, project, _ = await mc._ready_ticket(db_session)
    older, newer = await _gh_guards(db_session, project.id)

    assert await merge_checks._post_or_update_check_run(
        "tok", REPO, mc.HEAD, "action_required", "Not ready", guard=newer
    ) == "check_run"
    assert await merge_checks._post_or_update_check_run(
        "tok", REPO, mc.HEAD, "success", "Ready", guard=older
    ) == "superseded"
    assert _conclusions(gh) == ["action_required"]


@pytest.mark.asyncio
async def test_github_retry_of_the_same_evaluation_posts(
    client: AsyncClient, db_session: AsyncSession, gh: mc._CheckRuns
):
    _, project, _ = await mc._ready_ticket(db_session)
    _, newer = await _gh_guards(db_session, project.id)
    for _ in range(2):
        assert await merge_checks._post_or_update_check_run(
            "tok", REPO, mc.HEAD, "action_required", "Not ready", guard=newer
        ) == "check_run"
    assert _conclusions(gh) == ["action_required"]


@pytest.mark.asyncio
async def test_github_post_overtaken_in_flight_is_repaired(
    client: AsyncClient, db_session: AsyncSession, db_engine, gh: mc._CheckRuns, monkeypatch
):
    """The older evaluation claimed first, but its request landed after the
    newer one: it re-sends the newest claimed state afterwards."""
    from sqlalchemy.ext.asyncio import async_sessionmaker

    _, project, _ = await mc._ready_ticket(db_session)
    older, newer = await _gh_guards(db_session, project.id)
    other = async_sessionmaker(db_engine, expire_on_commit=False)()
    older = dataclasses.replace(older, db=other)

    real_write = merge_checks._write_check_run
    older_claimed = asyncio.Event()
    newer_done = asyncio.Event()

    async def slow_write(token, api, head_sha, conclusion, summary):
        if conclusion == "success" and not newer_done.is_set():
            older_claimed.set()
            await asyncio.wait_for(newer_done.wait(), 10)
        return await real_write(token, api, head_sha, conclusion, summary)

    monkeypatch.setattr(merge_checks, "_write_check_run", slow_write)
    stale = asyncio.create_task(
        merge_checks._post_or_update_check_run(
            "tok", REPO, mc.HEAD, "success", "Ready", guard=older
        )
    )
    await asyncio.wait_for(older_claimed.wait(), 10)
    assert await merge_checks._post_or_update_check_run(
        "tok", REPO, mc.HEAD, "action_required", "Not ready", guard=newer
    ) == "check_run"
    newer_done.set()
    assert await stale == "check_run"
    await other.close()
    assert _conclusions(gh) == ["action_required"]
    patches = [
        r for r in gh.requests if r.method == "PATCH"
    ]
    # success landed after action_required, then the repair re-sent it.
    assert [json.loads(r.content)["conclusion"] for r in patches] == [
        "success", "action_required",
    ]


@pytest.mark.asyncio
async def test_github_refresh_takes_increasing_sequences(
    client: AsyncClient, db_session: AsyncSession, gh: mc._CheckRuns
):
    _, project, ticket = await mc._ready_ticket(db_session)
    for _ in range(2):
        result = await merge_checks.refresh_merge_check(
            db_session, ticket_id=ticket.id, project_id=project.id
        )
        assert result["posted"] == "check_run"
    row = (
        await db_session.execute(
            select(MergeCheckPost).where(MergeCheckPost.project_id == project.id)
        )
    ).scalar_one()
    assert (row.provider, row.eval_seq, row.posted_seq, row.posted_state) == (
        "github", 2, 2, "success",
    )


# ── GitLab ─────────────────────────────────────────────────────────


async def _gl_posts(db: AsyncSession, project, remote: str):
    first = await delivery_ingest.plan_gitlab_status(db, project=project, repo=remote, sha=gl.SHA)
    await db.commit()
    second = await delivery_ingest.plan_gitlab_status(
        db, project=project, repo=remote, sha=gl.SHA
    )
    await db.commit()
    assert first is not None and second is not None
    assert second.seq == first.seq + 1 and first.seq >= 1
    older = dataclasses.replace(first, state="success", description="All review checks passed")
    newer = dataclasses.replace(second, state="pending", description="Review in progress")
    return older, newer


def _record_posts(monkeypatch) -> list[str]:
    calls: list[str] = []

    async def fake_post(self, project_id, sha, state, *, name, description, target_url=None):
        calls.append(state)
        return {"id": 1}

    monkeypatch.setattr(gc.GitLabClient, "post_commit_status", fake_post)
    return calls


@pytest.mark.asyncio
async def test_gitlab_newer_posts_first_older_is_skipped(
    db_session: AsyncSession, monkeypatch
):
    project, remote = await gl._setup(db_session)
    older, newer = await _gl_posts(db_session, project, remote)
    calls = _record_posts(monkeypatch)

    assert await delivery_ingest.send_gitlab_status(newer, db_session) is True
    assert await delivery_ingest.send_gitlab_status(older, db_session) is False
    assert calls == ["pending"]


@pytest.mark.asyncio
async def test_gitlab_retry_of_the_same_evaluation_posts(
    db_session: AsyncSession, monkeypatch
):
    project, remote = await gl._setup(db_session)
    _, newer = await _gl_posts(db_session, project, remote)
    calls = _record_posts(monkeypatch)

    assert await delivery_ingest.send_gitlab_status(newer, db_session) is True
    assert await delivery_ingest.send_gitlab_status(newer, db_session) is True
    assert calls == ["pending", "pending"]


@pytest.mark.asyncio
async def test_gitlab_post_overtaken_in_flight_is_repaired(
    db_session: AsyncSession, db_engine, monkeypatch
):
    from sqlalchemy.ext.asyncio import async_sessionmaker

    project, remote = await gl._setup(db_session)
    older, newer = await _gl_posts(db_session, project, remote)
    calls: list[str] = []
    older_claimed = asyncio.Event()
    newer_done = asyncio.Event()

    async def fake_post(self, project_id, sha, state, *, name, description, target_url=None):
        if state == "success" and not newer_done.is_set():
            older_claimed.set()
            await asyncio.wait_for(newer_done.wait(), 10)
        calls.append(state)
        return {"id": 1}

    monkeypatch.setattr(gc.GitLabClient, "post_commit_status", fake_post)
    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    async with factory() as other:
        stale = asyncio.create_task(delivery_ingest.send_gitlab_status(older, other))
        await asyncio.wait_for(older_claimed.wait(), 10)
        assert await delivery_ingest.send_gitlab_status(newer, db_session) is True
        newer_done.set()
        assert await stale is True
    assert calls == ["pending", "success", "pending"]


@pytest.mark.asyncio
async def test_gitlab_unordered_post_still_sends(db_session: AsyncSession, monkeypatch):
    """A hand-built post (no sequence) keeps the old unordered behaviour."""
    calls = _record_posts(monkeypatch)
    post = delivery_ingest.GitLabStatusPost(
        instance_url="https://gitlab.example.com",
        access_token="t",
        gitlab_project_id=1,
        sha=gl.SHA,
        state="success",
    )
    assert await delivery_ingest.send_gitlab_status(post, db_session) is True
    assert calls == ["success"]
