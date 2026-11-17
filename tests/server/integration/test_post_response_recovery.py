"""Post-response work survives a task that never runs (tk_e3eecad8c3874d92).

With Cloud Run CPU billed only during requests, anything started after the
response may get no CPU. Each such job is now either finished before the
response or recorded durably and finished by an existing scheduled sweep.
These tests turn the post-response fast path off (``SFS_POST_RESPONSE_TASKS
=off``: the task is dropped, exactly what a starved task looks like) and show
the sweep completes the job.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import io
import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import BackgroundTasks, FastAPI, Request
from fastapi.responses import JSONResponse
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from sessionfs.server import post_response
from sessionfs.server.auth.keys import hash_api_key
from sessionfs.server.db.engine import get_db
from sessionfs.server.db.models import (
    ApiKey,
    DeferredJob,
    GitHubWebhookDelivery,
    IdempotencyKey,
)
from sessionfs.server.idempotency import IdempotencyMiddleware
from sessionfs.server.services import deferred_jobs, jira_sync, merge_checks
from tests.server.integration.test_platform_sweeps_scope import _sweep_key

pytestmark = pytest.mark.asyncio


@pytest.fixture
def fast_path_off(monkeypatch):
    monkeypatch.setenv("SFS_POST_RESPONSE_TASKS", "off")


def _factory(db_engine):
    return async_sessionmaker(db_engine, expire_on_commit=False)


async def _jobs(db: AsyncSession) -> list[DeferredJob]:
    return list(
        (
            await db.execute(
                select(DeferredJob)
                .order_by(DeferredJob.kind, DeferredJob.subject_id)
                .execution_options(populate_existing=True)
            )
        ).scalars().all()
    )


# ── the switch ────────────────────────────────────────────────────────


async def test_spawn_off_drops_the_coroutine_and_on_runs_it(monkeypatch) -> None:
    ran: list[str] = []

    async def job() -> None:
        ran.append("x")

    monkeypatch.setenv("SFS_POST_RESPONSE_TASKS", "off")
    coro = job()
    assert post_response.spawn(coro, name="t") is None
    assert coro.cr_frame is None  # closed: no "never awaited" warning
    assert ran == []

    monkeypatch.setenv("SFS_POST_RESPONSE_TASKS", "on")
    task = post_response.spawn(job(), name="t")
    assert task is not None
    await post_response.drain()
    assert ran == ["x"]


async def test_jira_drain_with_fast_path_off_starts_nothing(fast_path_off) -> None:
    """The outbox row is the durable record; the jira-sync sweep delivers it
    (end to end in test_jira_sync.test_lost_drain_is_delivered_by_the_sweep)."""
    before = set(post_response._tasks)
    jira_sync.schedule_drain(["evt_1"])
    assert set(post_response._tasks) == before


# ── deferred_jobs protocol ────────────────────────────────────────────


async def test_job_runs_once_and_is_retired(db_engine, db_session, monkeypatch) -> None:
    seen: list[str] = []

    async def handler(subject_id, factory, context):
        seen.append(subject_id)

    monkeypatch.setitem(deferred_jobs._HANDLERS, deferred_jobs.KIND_MERGE_RECHECK, handler)
    await deferred_jobs.request_job(db_session, deferred_jobs.KIND_MERGE_RECHECK, "tk_a")
    await db_session.commit()

    factory = _factory(db_engine)
    assert await deferred_jobs.run_job(
        deferred_jobs.KIND_MERGE_RECHECK, "tk_a", session_factory=factory
    ) == "done"
    assert seen == ["tk_a"]
    assert await _jobs(db_session) == []
    # Nothing left: a second run finds no due row.
    assert await deferred_jobs.run_job(
        deferred_jobs.KIND_MERGE_RECHECK, "tk_a", session_factory=factory
    ) == "busy"


async def test_request_during_a_run_is_not_lost(db_engine, db_session, monkeypatch) -> None:
    factory = _factory(db_engine)
    runs: list[int] = []

    async def handler(subject_id, f, context):
        runs.append(1)
        if len(runs) == 1:
            # A new request lands while the first run is in flight.
            async with f() as db:
                await deferred_jobs.request_job(db, deferred_jobs.KIND_MERGE_RECHECK, subject_id)
                await db.commit()
            # ...and a concurrent run can't take the claimed row.
            assert await deferred_jobs.run_job(
                deferred_jobs.KIND_MERGE_RECHECK, subject_id, session_factory=f
            ) == "busy"

    monkeypatch.setitem(deferred_jobs._HANDLERS, deferred_jobs.KIND_MERGE_RECHECK, handler)
    await deferred_jobs.request_job(db_session, deferred_jobs.KIND_MERGE_RECHECK, "tk_b")
    await db_session.commit()

    outcome = await deferred_jobs.run_job(
        deferred_jobs.KIND_MERGE_RECHECK, "tk_b", session_factory=factory
    )
    # The newer request runs right away in the same call, not at the next sweep.
    assert outcome == "done" and len(runs) == 2
    assert await _jobs(db_session) == []


async def test_cancelled_run_gives_its_claim_back(db_engine, db_session, monkeypatch) -> None:
    factory = _factory(db_engine)
    started = asyncio.Event()

    async def slow(subject_id, f, context):
        started.set()
        await asyncio.sleep(30)

    monkeypatch.setitem(deferred_jobs._HANDLERS, deferred_jobs.KIND_MERGE_RECHECK, slow)
    await deferred_jobs.request_job(db_session, deferred_jobs.KIND_MERGE_RECHECK, "tk_cancel")
    await db_session.commit()
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(
            deferred_jobs.run_job(
                deferred_jobs.KIND_MERGE_RECHECK, "tk_cancel", session_factory=factory
            ),
            timeout=0.2,
        )
    (row,) = await _jobs(db_session)
    # Due again at once, with no attempt counted.
    assert row.claim_token is None and row.available_at is None and row.attempts == 0


async def test_org_recheck_resumes_from_its_checkpoint(
    db_engine, db_session, monkeypatch
) -> None:
    factory = _factory(db_engine)
    tickets = [f"tk_{i:03d}" for i in range(450)]
    seen: list[str] = []
    fail_at = {"tk_250"}

    async def fake_page(db, org_id, *, after=None, limit=200):
        rest = [t for t in tickets if after is None or t > after]
        return rest[:limit]

    async def fake_recheck(db, ticket_id):
        if ticket_id in fail_at:
            fail_at.discard(ticket_id)
            raise RuntimeError("stalled mid-org")
        seen.append(ticket_id)

    monkeypatch.setattr(merge_checks, "org_open_pr_ticket_page", fake_page)
    monkeypatch.setattr(merge_checks, "on_verdict_recorded", fake_recheck)
    await deferred_jobs.request_job(db_session, deferred_jobs.KIND_MERGE_ORG_RECHECK, "org_big")
    await db_session.commit()

    assert await deferred_jobs.run_job(
        deferred_jobs.KIND_MERGE_ORG_RECHECK, "org_big", session_factory=factory
    ) == "failed"
    (row,) = await _jobs(db_session)
    # Checkpointed every 25 tickets, inside a page too: the last one before
    # the failure at tk_250 is tk_249.
    assert row.cursor == "tk_249"
    await db_session.execute(update(DeferredJob).values(available_at=None))
    await db_session.commit()
    seen.clear()
    assert await deferred_jobs.run_job(
        deferred_jobs.KIND_MERGE_ORG_RECHECK, "org_big", session_factory=factory
    ) == "done"
    assert seen[0] == "tk_250" and seen[-1] == "tk_449"  # resumed, nothing redone
    assert await _jobs(db_session) == []


async def test_org_recheck_extends_its_lease_within_a_page(
    db_engine, db_session, monkeypatch
) -> None:
    """A first page of 200 provider-calling rechecks can outlast the 600 s
    lease, so the lease is extended every 25 tickets, not only per page."""
    factory = _factory(db_engine)
    tickets = [f"tk_{i:03d}" for i in range(120)]
    checkpoints: list[str] = []

    async def fake_page(db, org_id, *, after=None, limit=200):
        return [t for t in tickets if after is None or t > after][:limit]

    async def fake_recheck(db, ticket_id):
        return None

    real_checkpoint = deferred_jobs.checkpoint

    async def spy(f, claim, cursor):
        checkpoints.append(cursor)
        return await real_checkpoint(f, claim, cursor)

    monkeypatch.setattr(merge_checks, "org_open_pr_ticket_page", fake_page)
    monkeypatch.setattr(merge_checks, "on_verdict_recorded", fake_recheck)
    monkeypatch.setattr(deferred_jobs, "checkpoint", spy)
    await deferred_jobs.request_job(db_session, deferred_jobs.KIND_MERGE_ORG_RECHECK, "org_one_page")
    await db_session.commit()
    assert await deferred_jobs.run_job(
        deferred_jobs.KIND_MERGE_ORG_RECHECK, "org_one_page", session_factory=factory
    ) == "done"
    # One page of 120 tickets: checkpoints after 25, 50, 75 and 100.
    assert checkpoints == ["tk_024", "tk_049", "tk_074", "tk_099"]


async def test_failures_back_off_then_drop(db_engine, db_session, monkeypatch) -> None:
    factory = _factory(db_engine)

    async def boom(subject_id, f, context):
        raise RuntimeError("provider down")

    monkeypatch.setitem(deferred_jobs._HANDLERS, deferred_jobs.KIND_MERGE_RECHECK, boom)
    await deferred_jobs.request_job(db_session, deferred_jobs.KIND_MERGE_RECHECK, "tk_c")
    await db_session.commit()

    assert await deferred_jobs.run_job(
        deferred_jobs.KIND_MERGE_RECHECK, "tk_c", session_factory=factory
    ) == "failed"
    (row,) = await _jobs(db_session)
    assert row.attempts == 1 and row.last_error == "internal error (RuntimeError)"
    assert row.claim_token is None
    # Backing off: the sweep skips it.
    stats = await deferred_jobs.drain(deferred_jobs.MERGE_KINDS, session_factory=factory)
    assert stats["selected"] == 0

    for _ in range(deferred_jobs.MAX_ATTEMPTS - 1):
        await db_session.execute(update(DeferredJob).values(available_at=None))
        await db_session.commit()
        outcome = await deferred_jobs.run_job(
            deferred_jobs.KIND_MERGE_RECHECK, "tk_c", session_factory=factory
        )
    assert outcome == "dropped"
    assert await _jobs(db_session) == []


async def test_stale_claim_is_taken_over(db_engine, db_session, monkeypatch) -> None:
    """A run whose task stalled mid-job (no CPU) holds its claim only until
    the lease passes; the sweep then runs the job."""
    factory = _factory(db_engine)
    seen: list[str] = []

    async def handler(subject_id, f, context):
        seen.append(subject_id)

    monkeypatch.setitem(deferred_jobs._HANDLERS, deferred_jobs.KIND_MERGE_RECHECK, handler)
    await deferred_jobs.request_job(db_session, deferred_jobs.KIND_MERGE_RECHECK, "tk_d")
    await db_session.commit()
    now = datetime.now(timezone.utc)
    await db_session.execute(
        update(DeferredJob).values(claim_token="stalled", available_at=now + timedelta(minutes=5))
    )
    await db_session.commit()
    assert (await deferred_jobs.drain(deferred_jobs.MERGE_KINDS, session_factory=factory))[
        "selected"
    ] == 0
    await db_session.execute(
        update(DeferredJob).values(available_at=now - timedelta(seconds=1))
    )
    await db_session.commit()
    stats = await deferred_jobs.drain(deferred_jobs.MERGE_KINDS, session_factory=factory)
    assert stats["done"] == 1 and seen == ["tk_d"]
    assert await _jobs(db_session) == []


# ── merge checks: the GitHub webhook delivery sweep ───────────────────


async def test_merge_recheck_is_recovered_by_the_delivery_sweep(
    client: AsyncClient, db_session: AsyncSession, fast_path_off, monkeypatch
) -> None:
    rechecked: list[str] = []

    async def fake_recheck(db, ticket_id):
        rechecked.append(ticket_id)

    monkeypatch.setattr(merge_checks, "on_verdict_recorded", fake_recheck)
    await merge_checks.request_recheck(db_session, "tk_merge1")
    await asyncio.sleep(0)
    assert rechecked == []  # the post-response task never ran
    assert [(j.kind, j.subject_id) for j in await _jobs(db_session)] == [
        ("merge_recheck", "tk_merge1")
    ]

    _, _, _, headers = await _sweep_key(db_session)
    resp = await client.post(
        "/api/v1/admin/github-webhook-deliveries/sweep", headers=headers, json={}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["merge_rechecks_run"] == 1
    assert rechecked == ["tk_merge1"]
    assert await _jobs(db_session) == []


async def test_org_recheck_is_recovered_by_the_delivery_sweep(
    client: AsyncClient, db_session: AsyncSession, fast_path_off, monkeypatch
) -> None:
    rechecked: list[str] = []

    async def fake_recheck_org(org_id, *, session_factory, **kwargs):
        rechecked.append(org_id)
        return 0

    monkeypatch.setattr(merge_checks, "recheck_org", fake_recheck_org)
    await merge_checks.request_org_recheck(db_session, "org_policy1")
    assert rechecked == []

    _, _, _, headers = await _sweep_key(db_session)
    resp = await client.post(
        "/api/v1/admin/github-webhook-deliveries/sweep", headers=headers, json={}
    )
    assert resp.status_code == 200, resp.text
    assert rechecked == ["org_policy1"]
    assert await _jobs(db_session) == []


async def test_merge_recheck_fast_path_retires_the_job(
    db_session: AsyncSession, db_engine, monkeypatch
) -> None:
    import sessionfs.server.db.engine as engine_mod

    rechecked: list[str] = []

    async def fake_recheck(db, ticket_id):
        rechecked.append(ticket_id)

    monkeypatch.setattr(merge_checks, "on_verdict_recorded", fake_recheck)
    monkeypatch.setattr(engine_mod, "_session_factory", _factory(db_engine))
    await merge_checks.request_recheck(db_session, "tk_fast")
    await post_response.drain()
    assert rechecked == ["tk_fast"]
    assert await _jobs(db_session) == []


# ── knowledge: the kb-compile sweep ───────────────────────────────────


async def test_push_extraction_is_recovered_by_the_kb_compile_sweep(
    client: AsyncClient,
    db_session: AsyncSession,
    auth_headers,
    sample_sfs_tar: bytes,
    test_user,
    fast_path_off,
    monkeypatch,
) -> None:
    from sessionfs.server.routes import sessions as sessions_mod

    calls: list[tuple] = []

    async def fake_extract(session_id, archive, remote, user_id):
        calls.append((session_id, archive, remote, user_id))

    monkeypatch.setattr(sessions_mod, "_auto_extract_knowledge", fake_extract)
    session_id = f"ses_deferred{uuid.uuid4().hex[:8]}"
    resp = await client.put(
        f"/api/v1/sessions/{session_id}/sync",
        headers=auth_headers,
        files={"file": ("session.tar.gz", io.BytesIO(sample_sfs_tar), "application/gzip")},
    )
    assert resp.status_code == 201, resp.text
    assert calls == []  # the post-response task never ran
    assert [(j.kind, j.subject_id) for j in await _jobs(db_session)] == [
        ("knowledge_extract", session_id)
    ]

    _, _, _, headers = await _sweep_key(db_session)
    resp = await client.post("/api/v1/admin/sweeps/kb-compile", headers=headers, json={})
    assert resp.status_code == 200, resp.text
    assert resp.json()["knowledge_jobs_run"] == 1
    assert len(calls) == 1
    sid, archive, remote, user_id = calls[0]
    assert sid == session_id and archive == sample_sfs_tar  # re-read from the blob store
    assert remote and user_id == test_user.id
    assert await _jobs(db_session) == []


async def test_push_extraction_fast_path_still_runs(
    client: AsyncClient, db_session: AsyncSession, auth_headers, sample_sfs_tar, monkeypatch
) -> None:
    from sessionfs.server.routes import sessions as sessions_mod

    calls: list[str] = []

    async def fake_extract(session_id, archive, remote, user_id):
        calls.append(session_id)

    monkeypatch.setattr(sessions_mod, "_auto_extract_knowledge", fake_extract)
    session_id = f"ses_fastpath{uuid.uuid4().hex[:8]}"
    resp = await client.put(
        f"/api/v1/sessions/{session_id}/sync",
        headers=auth_headers,
        files={"file": ("session.tar.gz", io.BytesIO(sample_sfs_tar), "application/gzip")},
    )
    assert resp.status_code == 201, resp.text
    assert calls == [session_id]
    assert await _jobs(db_session) == []


async def test_project_backfill_is_recovered_by_the_kb_compile_sweep(
    client: AsyncClient, db_session: AsyncSession, auth_headers, test_user, fast_path_off,
    monkeypatch,
) -> None:
    from sessionfs.server.routes import projects as projects_mod

    calls: list[tuple] = []

    async def fake_backfill(project_id, remote, user_id, blob_store):
        calls.append((project_id, remote, user_id, blob_store is not None))

    monkeypatch.setattr(projects_mod, "_backfill_knowledge_for_project", fake_backfill)
    remote = f"github.com/acme/deferred-{uuid.uuid4().hex[:8]}"
    resp = await client.post(
        "/api/v1/projects/",
        headers=auth_headers,
        json={"name": "deferred", "git_remote_normalized": remote},
    )
    assert resp.status_code == 201, resp.text
    project_id = resp.json()["id"]
    assert calls == []
    assert [(j.kind, j.subject_id) for j in await _jobs(db_session)] == [
        ("knowledge_backfill", project_id)
    ]

    _, _, _, headers = await _sweep_key(db_session)
    resp = await client.post("/api/v1/admin/sweeps/kb-compile", headers=headers, json={})
    assert resp.status_code == 200, resp.text
    assert calls == [(project_id, remote, test_user.id, True)]
    assert await _jobs(db_session) == []


# ── GitHub pull_request deliveries: the delivery sweep ─────────────────


async def test_github_pr_delivery_is_recovered_by_the_delivery_sweep(
    client: AsyncClient, db_session: AsyncSession, fast_path_off, monkeypatch
) -> None:
    from sessionfs.server.routes import webhooks

    monkeypatch.setattr(webhooks, "GITHUB_WEBHOOK_SECRET", "whsec")
    monkeypatch.delenv("SFS_GITHUB_WEBHOOK_ALLOW_UNSIGNED", raising=False)
    handled: list[dict] = []

    async def fake_handle(payload, fence=None):
        handled.append(payload)
        return True

    monkeypatch.setattr(webhooks, "_handle_pr_event", fake_handle)
    body = json.dumps({
        "action": "opened",
        "pull_request": {"head": {"ref": "x"}, "number": 7},
        "repository": {"full_name": "acme/deferred", "clone_url": "https://github.com/acme/deferred.git"},
        "installation": {"id": 1},
    }).encode()
    headers = {
        "X-GitHub-Event": "pull_request",
        "Content-Type": "application/json",
        "X-Hub-Signature-256": "sha256="
        + hmac.new(b"whsec", body, hashlib.sha256).hexdigest(),
    }
    resp = await client.post("/webhooks/github", content=body, headers=headers)
    assert resp.status_code == 200 and resp.json()["status"] == "ok"
    await asyncio.sleep(0)
    assert handled == []  # the post-response task never ran
    (row,) = (
        await db_session.execute(
            select(GitHubWebhookDelivery).execution_options(populate_existing=True)
        )
    ).scalars().all()
    # Handed back as due at once (not left 'processing' for the 10-minute
    # stale window), without counting an attempt: the next sweep runs it.
    assert row.status == "failed" and row.attempts == 0
    _, _, _, sweep_headers = await _sweep_key(db_session)
    resp = await client.post(
        "/api/v1/admin/github-webhook-deliveries/sweep", headers=sweep_headers, json={}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["retries_succeeded"] == 1
    assert len(handled) == 1
    (row,) = (
        await db_session.execute(
            select(GitHubWebhookDelivery).execution_options(populate_existing=True)
        )
    ).scalars().all()
    assert row.status == "completed"


# ── GitLab MR comments: finished before the response ──────────────────


def _mr_body() -> bytes:
    return json.dumps({
        "object_kind": "merge_request",
        "object_attributes": {"action": "open", "iid": 1, "source_branch": "x"},
        "project": {"id": 1, "git_http_url": "https://gitlab.com/a/b.git"},
    }).encode()


async def test_gitlab_mr_comment_runs_before_the_response(
    client: AsyncClient, fast_path_off, monkeypatch
) -> None:
    from sessionfs.server.routes import webhooks

    monkeypatch.setenv("SFS_GITLAB_WEBHOOK_SECRET", "glsecret")
    done: list[str] = []

    async def fake_handle(payload, request, auth_user_id):
        await asyncio.sleep(0.01)
        done.append("posted")

    monkeypatch.setattr(webhooks, "_handle_gitlab_mr", fake_handle)
    resp = await client.post(
        "/webhooks/gitlab", content=_mr_body(), headers={"X-Gitlab-Token": "glsecret"}
    )
    assert resp.status_code == 200
    assert done == ["posted"]  # finished before the response, fast path off or not


async def test_gitlab_mr_comment_past_the_deadline_is_left_for_the_sweep(
    client: AsyncClient, db_session: AsyncSession, monkeypatch
) -> None:
    from sessionfs.server.routes import webhooks

    monkeypatch.setenv("SFS_GITLAB_WEBHOOK_SECRET", "glsecret")
    monkeypatch.setattr(webhooks, "_GITLAB_MR_DEADLINE_S", 0.05)
    calls: list[tuple] = []
    slow_once = {"on": True}

    async def handle(payload, request, auth_user_id):
        if slow_once["on"]:
            slow_once["on"] = False
            await asyncio.sleep(5)
        calls.append((payload["object_attributes"]["iid"], auth_user_id))

    monkeypatch.setattr(webhooks, "_handle_gitlab_mr", handle)
    loop = asyncio.get_running_loop()
    started = loop.time()
    resp = await client.post(
        "/webhooks/gitlab", content=_mr_body(), headers={"X-Gitlab-Token": "glsecret"}
    )
    assert resp.status_code == 200 and resp.json()["status"] == "processing"
    assert loop.time() - started < 2
    assert calls == []
    (row,) = await _jobs(db_session)
    assert row.kind == "gitlab_mr_comment" and row.claim_token is None  # due again

    _, _, _, headers = await _sweep_key(db_session)
    resp = await client.post(
        "/api/v1/admin/github-webhook-deliveries/sweep", headers=headers, json={}
    )
    assert resp.status_code == 200, resp.text
    assert calls == [(1, None)]  # global secret: no per-user binding
    assert await _jobs(db_session) == []


async def test_gitlab_mr_note_cut_off_before_recording_is_adopted(
    db_session: AsyncSession, db_engine, test_user, monkeypatch
) -> None:
    """A note posted by an attempt that was cancelled before it recorded the
    note id carries the hidden marker; the retry edits it instead of posting."""
    import sessionfs.server.db.engine as engine_mod
    from sessionfs.server.db.models import GitLabSettings, PRComment, Session
    from sessionfs.server.llm_keys import encrypt_secret
    from sessionfs.server.routes import webhooks

    monkeypatch.setattr(engine_mod, "_session_factory", _factory(db_engine))
    now = datetime.now(timezone.utc)
    db_session.add(Session(
        id="ses_mr1", user_id=test_user.id, title="t", tags="[]", source_tool="claude-code",
        blob_key="k", blob_size_bytes=1, etag="e", git_remote_normalized="a/b",
        git_branch="x", created_at=now, updated_at=now, uploaded_at=now,
    ))
    db_session.add(GitLabSettings(
        user_id=test_user.id, instance_url="https://gitlab.com",
        encrypted_access_token=encrypt_secret("glpat"), webhook_secret="s",
    ))
    await db_session.commit()
    calls: list[tuple] = []

    class FakeClient:
        def __init__(self, base_url, token):
            pass

        async def find_mr_note_with_marker(self, project_id, mr_iid, marker):
            calls.append(("find", marker))
            return 777

        async def update_mr_comment(self, project_id, mr_iid, note_id, body):
            calls.append(("update", note_id, "<!-- sessionfs-mr:a/b:1 -->" in body))
            return {"id": note_id}

        async def post_mr_comment(self, project_id, mr_iid, body):
            calls.append(("post",))
            return {"id": 1}

    monkeypatch.setattr("sessionfs.server.gitlab_client.GitLabClient", FakeClient)
    await webhooks._handle_gitlab_mr(json.loads(_mr_body()), None, None)
    assert calls == [
        ("find", "<!-- sessionfs-mr:a/b:1 -->"),
        ("update", 777, True),
    ]
    (row,) = (
        await db_session.execute(
            select(PRComment).execution_options(populate_existing=True)
        )
    ).scalars().all()
    assert row.comment_id == 777


# ── audit: large sessions run inside the request ──────────────────────


async def test_large_session_audit_completes_inside_the_request(
    client: AsyncClient, auth_headers, sample_sfs_tar, monkeypatch
) -> None:
    from sessionfs.judge.report import JudgeReport
    from sessionfs.server.routes import audit as audit_mod

    session_id = f"ses_bigaudit{uuid.uuid4().hex[:8]}"
    resp = await client.put(
        f"/api/v1/sessions/{session_id}/sync",
        headers=auth_headers,
        files={"file": ("session.tar.gz", io.BytesIO(sample_sfs_tar), "application/gzip")},
    )
    assert resp.status_code == 201, resp.text

    async def many_messages(blob_store, blob_key):
        return [{"role": "user", "content": f"m{i}"} for i in range(600)]

    async def fake_pipeline(sid, messages, model, key, provider, base_url=None):
        assert len(messages) == 600
        return JudgeReport(session_id=sid, model=model, timestamp="2026-11-16T00:00:00Z")

    monkeypatch.setattr(audit_mod, "_extract_messages_from_archive", many_messages)
    monkeypatch.setattr(audit_mod, "_run_judge_pipeline", fake_pipeline)
    resp = await client.post(
        f"/api/v1/sessions/{session_id}/audit",
        headers=auth_headers,
        json={"model": "m", "provider": "openai", "llm_api_key": "sk-caller"},
    )
    assert resp.status_code == 200, resp.text  # not 202 + a detached task
    stored = await client.get(f"/api/v1/sessions/{session_id}/audit", headers=auth_headers)
    assert stored.status_code == 200
    status = await client.get(
        f"/api/v1/sessions/{session_id}/audit/status", headers=auth_headers
    )
    assert status.json()["status"] == "completed"


async def _pushed(client: AsyncClient, auth_headers, sample_sfs_tar) -> str:
    session_id = f"ses_audittx{uuid.uuid4().hex[:8]}"
    resp = await client.put(
        f"/api/v1/sessions/{session_id}/sync",
        headers=auth_headers,
        files={"file": ("session.tar.gz", io.BytesIO(sample_sfs_tar), "application/gzip")},
    )
    assert resp.status_code == 201, resp.text
    return session_id


async def test_audit_holds_no_transaction_during_the_judge_calls(
    client: AsyncClient, auth_headers, sample_sfs_tar, db_engine, monkeypatch
) -> None:
    from sessionfs.judge.report import JudgeReport
    from sessionfs.server.db.models import AuditReport
    from sessionfs.server.routes import audit as audit_mod

    session_id = await _pushed(client, auth_headers, sample_sfs_tar)
    factory = _factory(db_engine)
    request_sessions: list[AsyncSession] = []

    async def tracking_get_db():
        async with factory() as session:
            request_sessions.append(session)
            yield session

    app = client._transport.app  # type: ignore[attr-defined]
    app.dependency_overrides[get_db] = tracking_get_db
    seen: list[bool] = []

    async def fake_pipeline(sid, messages, model, key, provider, base_url=None):
        seen.append(any(s.in_transaction() for s in request_sessions))
        return JudgeReport(session_id=sid, model=model, timestamp="2026-11-16T00:00:00Z")

    monkeypatch.setattr(audit_mod, "_run_judge_pipeline", fake_pipeline)
    resp = await client.post(
        f"/api/v1/sessions/{session_id}/audit",
        headers=auth_headers,
        json={"model": "m", "provider": "openai", "llm_api_key": "sk-caller"},
    )
    assert resp.status_code == 200, resp.text
    assert seen == [False]  # no transaction open across the LLM calls
    async with factory() as s:
        rows = (
            await s.execute(select(AuditReport).where(AuditReport.session_id == session_id))
        ).scalars().all()
    assert len(rows) == 1  # written afterwards in its own short transaction


async def test_audit_of_a_session_purged_mid_run_stores_nothing(
    client: AsyncClient, auth_headers, sample_sfs_tar, db_engine, monkeypatch
) -> None:
    from sqlalchemy import delete

    from sessionfs.judge.report import JudgeReport
    from sessionfs.server.db.models import AuditReport, Session
    from sessionfs.server.routes import audit as audit_mod

    session_id = await _pushed(client, auth_headers, sample_sfs_tar)
    factory = _factory(db_engine)

    async def purge_then_report(sid, messages, model, key, provider, base_url=None):
        async with factory() as s:
            await s.execute(delete(DeferredJob))
            await s.execute(delete(Session).where(Session.id == sid))
            await s.commit()
        return JudgeReport(session_id=sid, model=model, timestamp="2026-11-16T00:00:00Z")

    monkeypatch.setattr(audit_mod, "_run_judge_pipeline", purge_then_report)
    resp = await client.post(
        f"/api/v1/sessions/{session_id}/audit",
        headers=auth_headers,
        json={"model": "m", "provider": "openai", "llm_api_key": "sk-caller"},
    )
    assert resp.status_code == 404
    async with factory() as s:
        assert (
            await s.execute(select(AuditReport).where(AuditReport.session_id == session_id))
        ).scalars().all() == []


# ── idempotency keys: settled before the last byte ────────────────────


async def test_idempotency_key_is_settled_before_post_response_work(
    db_engine, db_session: AsyncSession, test_user
) -> None:
    db_session.add(
        ApiKey(
            id=str(uuid.uuid4()),
            user_id=test_user.id,
            key_hash=hash_api_key("good-a"),
            name="good-a",
            created_at=datetime.now(timezone.utc),
        )
    )
    await db_session.commit()
    factory = _factory(db_engine)
    app = FastAPI()
    app.add_middleware(IdempotencyMiddleware)

    async def override_get_db():
        async with factory() as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db
    seen_status: list[str | None] = []

    async def after_response() -> None:
        # Runs after the last byte: with CPU only during requests this may
        # never run, so the key must already be settled here.
        async with factory() as s:
            seen_status.append(
                (await s.execute(select(IdempotencyKey.status))).scalar_one_or_none()
            )

    @app.post("/things")
    async def create_thing(request: Request, background_tasks: BackgroundTasks):
        request.state.auth_context = object()
        background_tasks.add_task(after_response)
        return JSONResponse({"ok": True}, status_code=201)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        resp = await c.post(
            "/things",
            headers={"Authorization": "Bearer good-a", "Idempotency-Key": "k-settle"},
            json={},
        )
    assert resp.status_code == 201
    assert seen_status == ["completed"]


# ── a job that can't be recorded still runs (the old path) ────────────


async def test_unrecorded_merge_recheck_still_runs_directly(
    db_session: AsyncSession, db_engine, fast_path_off, monkeypatch
) -> None:
    import sessionfs.server.db.engine as engine_mod

    rechecked: list[str] = []

    async def fake_recheck(db, ticket_id):
        rechecked.append(ticket_id)

    async def broken_request_job(*a, **k):
        raise RuntimeError("deferred_jobs unavailable")

    monkeypatch.setattr(engine_mod, "_session_factory", _factory(db_engine))
    monkeypatch.setattr(merge_checks, "on_verdict_recorded", fake_recheck)
    monkeypatch.setattr(deferred_jobs, "request_job", broken_request_job)
    await merge_checks.request_recheck(db_session, "tk_norow")
    await post_response.drain()
    # No durable row, so the detached recheck is the only copy of the work:
    # it runs even with the fast path off, as it did before.
    assert rechecked == ["tk_norow"]
    assert await _jobs(db_session) == []


async def test_unrecorded_push_extraction_still_runs_directly(
    client: AsyncClient, db_session: AsyncSession, auth_headers, sample_sfs_tar,
    fast_path_off, monkeypatch,
) -> None:
    from sessionfs.server.routes import sessions as sessions_mod

    calls: list[str] = []

    async def fake_extract(session_id, archive, remote, user_id):
        calls.append(session_id)

    async def broken_request_job(*a, **k):
        raise RuntimeError("deferred_jobs unavailable")

    monkeypatch.setattr(sessions_mod, "_auto_extract_knowledge", fake_extract)
    monkeypatch.setattr(deferred_jobs, "request_job", broken_request_job)
    session_id = f"ses_norow{uuid.uuid4().hex[:8]}"
    resp = await client.put(
        f"/api/v1/sessions/{session_id}/sync",
        headers=auth_headers,
        files={"file": ("session.tar.gz", io.BytesIO(sample_sfs_tar), "application/gzip")},
    )
    assert resp.status_code == 201, resp.text  # the push never fails for it
    assert calls == [session_id]
    assert await _jobs(db_session) == []


# ── the org helper slot is freed inside the request ───────────────────


async def test_helper_slot_release_awaits_the_org_slot() -> None:
    from sessionfs.server.routes import helper as helper_mod

    released: list[str] = []

    class FakeOrgSlot:
        async def release(self):
            await asyncio.sleep(0)
            released.append("db")

        def release_soon(self):  # pragma: no cover - must not be used
            raise AssertionError("release_soon used")

    helper_mod._acquire("u_slot")
    slot = helper_mod._InFlightSlot("u_slot")
    slot.org_slot = FakeOrgSlot()
    await slot.release_async()
    assert released == ["db"]
    await slot.release_async()  # idempotent
    slot.release()
    assert released == ["db"]
