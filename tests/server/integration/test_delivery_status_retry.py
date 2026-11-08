"""Bounded retry for the GitLab commit status and the GitHub check-run calls.

A transport error or a 5xx retries (3 attempts in all); a 4xx is final. The
status is computed inside the caller's transaction but sent only after it is
committed, so no transaction is open while the post waits between attempts.
"""

from __future__ import annotations

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server import gitlab_client as gc
from sessionfs.server.services import delivery_ingest, merge_checks

from tests.server.integration.test_gitlab_commit_status import (
    SHA,
    _make_project,
    _make_repo,
    _make_settings,
    _make_ticket_and_pr,
    _make_user,
)


def _post(state: str = "pending") -> delivery_ingest.GitLabStatusPost:
    return delivery_ingest.GitLabStatusPost(
        instance_url="https://gitlab.example.com",
        access_token="glpat-test",
        gitlab_project_id=7,
        sha=SHA,
        state=state,
        description="Review in progress",
    )


def _status_error(code: int) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "https://gitlab.example.com/api/v4/x")
    return httpx.HTTPStatusError(
        "boom", request=request, response=httpx.Response(code, request=request)
    )


@pytest.fixture
def no_wait(monkeypatch):
    sleeps: list[float] = []

    async def fake_sleep(delay):
        sleeps.append(delay)

    monkeypatch.setattr(delivery_ingest.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(merge_checks.asyncio, "sleep", fake_sleep)
    return sleeps


def _scripted_post(monkeypatch, outcomes: list):
    calls: list[str] = []

    async def fake_post(self, project_id, sha, state, *, name, description, target_url=None):
        calls.append(state)
        outcome = outcomes[len(calls) - 1]
        if isinstance(outcome, BaseException):
            raise outcome
        return {"id": 1}

    monkeypatch.setattr(gc.GitLabClient, "post_commit_status", fake_post)
    return calls


@pytest.mark.asyncio
async def test_gitlab_status_retries_transport_error_then_succeeds(monkeypatch, no_wait):
    calls = _scripted_post(
        monkeypatch, [httpx.ConnectError("down"), _status_error(502), None]
    )
    assert await delivery_ingest.send_gitlab_status(_post()) is True
    assert len(calls) == 3
    assert no_wait == list(delivery_ingest._STATUS_RETRY_DELAYS)


@pytest.mark.asyncio
async def test_gitlab_status_gives_up_after_three_attempts(monkeypatch, no_wait):
    calls = _scripted_post(monkeypatch, [_status_error(503)] * 5)
    assert await delivery_ingest.send_gitlab_status(_post()) is False
    assert len(calls) == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("code", [400, 401, 403, 404, 422])
async def test_gitlab_status_never_retries_4xx(monkeypatch, no_wait, code):
    calls = _scripted_post(monkeypatch, [_status_error(code)] * 3)
    assert await delivery_ingest.send_gitlab_status(_post()) is False
    assert len(calls) == 1
    assert no_wait == []


@pytest.mark.asyncio
async def test_gitlab_status_never_retries_guard_refusal(monkeypatch, no_wait):
    calls = _scripted_post(monkeypatch, [ValueError("private host")] * 3)
    assert await delivery_ingest.send_gitlab_status(_post()) is False
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_post_gitlab_status_commits_before_sending(
    db_session: AsyncSession, monkeypatch, no_wait
):
    owner = await _make_user(db_session, "owner")
    reviewer = await _make_user(db_session, "reviewer")
    project = await _make_project(db_session, owner, "acme/widgets")
    await _make_repo(db_session, project, "acme/widgets", "12345")
    await _make_settings(db_session, owner)
    await _make_ticket_and_pr(
        db_session, project, owner, "acme/widgets", reviewer=reviewer, reviewed_commit=SHA
    )

    in_txn: list[bool] = []
    outcomes = [httpx.ConnectError("down"), None]

    async def fake_post(self, project_id, sha, state, *, name, description, target_url=None):
        in_txn.append(db_session.in_transaction())
        outcome = outcomes[len(in_txn) - 1]
        if isinstance(outcome, BaseException):
            raise outcome
        return {"id": 1}

    monkeypatch.setattr(gc.GitLabClient, "post_commit_status", fake_post)

    await delivery_ingest.post_gitlab_status(
        db_session, project=project, repo="acme/widgets", sha=SHA
    )
    # Two attempts, neither with the session's transaction open.
    assert in_txn == [False, False]


@pytest.mark.asyncio
async def test_github_request_retries_5xx_and_transport(monkeypatch, no_wait):
    seen: list[int] = []
    script = ["raise", 500, 201]

    def handler(request):
        step = script[len(seen)]
        seen.append(len(seen))
        if step == "raise":
            raise httpx.ConnectError("down", request=request)
        return httpx.Response(step, json={})

    monkeypatch.setattr(merge_checks, "_http_transport", httpx.MockTransport(handler))
    resp = await merge_checks._github_request(
        "POST", "https://api.github.com/repos/a/b/check-runs", token="t", json_body={}
    )
    assert resp is not None and resp.status_code == 201
    assert len(seen) == 3


@pytest.mark.asyncio
async def test_github_request_never_retries_4xx(monkeypatch, no_wait):
    seen: list[int] = []

    def handler(request):
        seen.append(1)
        return httpx.Response(403, json={})

    monkeypatch.setattr(merge_checks, "_http_transport", httpx.MockTransport(handler))
    resp = await merge_checks._github_request(
        "GET", "https://api.github.com/repos/a/b/commits/x/check-runs", token="t"
    )
    assert resp is not None and resp.status_code == 403
    assert seen == [1]
    assert no_wait == []


@pytest.mark.asyncio
async def test_github_request_bounded_on_persistent_5xx(monkeypatch, no_wait):
    seen: list[int] = []

    def handler(request):
        seen.append(1)
        return httpx.Response(503, json={})

    monkeypatch.setattr(merge_checks, "_http_transport", httpx.MockTransport(handler))
    resp = await merge_checks._github_request(
        "GET", "https://api.github.com/repos/a/b/commits/x/check-runs", token="t"
    )
    assert resp is not None and resp.status_code == 503
    assert len(seen) == 3
