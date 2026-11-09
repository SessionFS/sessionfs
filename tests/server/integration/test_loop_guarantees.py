"""Review loop guarantees (site docs: /review-loop-guarantees/).

Each test proves one claim on the "Review loop guarantees" page. The
concurrency claims are proven on PostgreSQL in test_loop_guarantees_pg.py;
these run on SQLite in every test run.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from httpx import AsyncClient
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from sessionfs.server.auth.keys import generate_api_key, hash_api_key
from sessionfs.server.db.models import (
    AgentRun,
    ApiKey,
    Ticket,
    TicketComment,
    TrustedReviewer,
)
from tests.server.integration.test_loop_workers import (
    SHA1,
    SHA2,
    Env,
    _env,
    _finish,
    _item,
    _p,
    _round,
    _start_worker,
)
from tests.server.integration.test_loops_api import _hdrs, _make_service_key

SCOPES = ["loops:write", "loops:read", "tickets:write", "tickets:read",
          "agent_runs:write", "work_queues:write"]


async def _runs(db: AsyncSession, ticket_id: str, role: str = "implementer") -> list[AgentRun]:
    return list(
        (
            await db.execute(
                select(AgentRun)
                .where(AgentRun.ticket_id == ticket_id, AgentRun.role == role)
                .order_by(AgentRun.created_at)
                .execution_options(populate_existing=True)
            )
        ).scalars().all()
    )


async def _ticket(db: AsyncSession, ticket_id: str) -> Ticket:
    return (
        await db.execute(
            select(Ticket)
            .where(Ticket.id == ticket_id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one()


async def _readiness(client: AsyncClient, e: Env, sha: str) -> dict:
    r = await client.get(
        _p(e, f"tickets/{e.child_id}/readiness"), params={"sha": sha},
        headers=_hdrs(e.owner_key),
    )
    assert r.status_code == 200, r.text
    return r.json()


async def _loop_child(client: AsyncClient, e: Env) -> dict:
    r = await client.get(_p(e, f"loops/{e.loop_id}"), headers=_hdrs(e.owner_key))
    assert r.status_code == 200, r.text
    return next(c for c in r.json()["children"] if c["ticket_id"] == e.child_id)


async def _rotate(db: AsyncSession, old: ApiKey, rotator_id: str) -> tuple[ApiKey, str]:
    """What POST …/service-keys/{id}/rotate writes: a new key linked to the
    old one, and the old one revoked."""
    raw = generate_api_key()
    new = ApiKey(
        id=str(uuid.uuid4()),
        user_id=old.user_id,
        key_hash=hash_api_key(raw),
        name=old.name,
        is_active=True,
        key_kind="service",
        org_id=old.org_id,
        scopes=old.scopes,
        created_by_user_id=rotator_id,
        service_key_name=old.service_key_name,
        project_ids=old.project_ids,
        rotated_from_key_id=old.id,
        created_at=datetime.now(timezone.utc),
    )
    db.add(new)
    await db.execute(
        update(ApiKey).where(ApiKey.id == old.id).values(
            is_active=False, revoked_at=datetime.now(timezone.utc)
        )
    )
    await db.commit()
    return new, raw


# ── start worker ──


@pytest.mark.asyncio
async def test_start_bumps_lease_epoch_once_per_start(client: AsyncClient, db_session: AsyncSession):
    """[G-start-1] A fresh start bumps lease_epoch by exactly one and returns it."""
    e = await _env(db_session, client)
    before = (await _ticket(db_session, e.child_id)).lease_epoch
    r = await _start_worker(client, e)
    assert r.status_code == 201, r.text
    assert r.json()["lease_epoch"] == before + 1
    assert (await _ticket(db_session, e.child_id)).lease_epoch == before + 1


@pytest.mark.asyncio
async def test_takeover_is_immediate_and_fences_the_previous_run(
    client: AsyncClient, db_session: AsyncSession
):
    """[G-takeover] There is no lease timeout: any editor's start on an
    in-progress ticket takes the lease at once (epoch + 1), and the previous
    holder's still-running run can no longer record a head commit."""
    e = await _env(db_session, client)
    a = await _start_worker(client, e, key=e.owner_key)
    b = await _start_worker(client, e, key=e.member_key)
    assert b.status_code == 201, b.text
    assert b.json()["lease_epoch"] == a.json()["lease_epoch"] + 1
    assert b.json()["run_id"] != a.json()["run_id"]

    late = await _finish(client, e, a.json()["run_id"], key=e.owner_key, head_sha=SHA1)
    assert late.status_code == 409, late.text
    assert late.json()["error"]["details"]["error"] == "lease_superseded"
    stale = await db_session.get(AgentRun, a.json()["run_id"])
    await db_session.refresh(stale)
    assert stale.head_sha is None and stale.status == "running"

    ok = await _finish(client, e, b.json()["run_id"], key=e.member_key, head_sha=SHA2)
    assert ok.status_code == 200, ok.text


@pytest.mark.asyncio
async def test_superseded_run_can_still_report_usage(
    client: AsyncClient, db_session: AsyncSession
):
    """[G-takeover-usage] A fenced worker's spend is not lost: the usage
    endpoint still adds it to the run, so the budget sees it."""
    e = await _env(db_session, client)
    a = (await _start_worker(client, e, key=e.owner_key)).json()["run_id"]
    await _start_worker(client, e, key=e.member_key)
    r = await client.post(
        _p(e, f"agent-runs/{a}/usage"), headers=_hdrs(e.owner_key),
        json={"input_tokens": 40, "output_tokens": 2},
    )
    assert r.status_code == 200, r.text
    run = await db_session.get(AgentRun, a)
    await db_session.refresh(run)
    assert (run.input_tokens, run.output_tokens) == (40, 2)


@pytest.mark.asyncio
async def test_usage_report_past_the_budget_parks_at_the_next_start(
    client: AsyncClient, db_session: AsyncSession
):
    """[G-budget-usage] The plain usage endpoint records spend but never
    parks: a budget passed that way is enforced by the next start (or finish
    or round), which parks the item and refuses."""
    e = await _env(db_session, client, token_budget=100)
    run = (await _start_worker(client, e)).json()["run_id"]
    r = await client.post(
        _p(e, f"agent-runs/{run}/usage"), headers=_hdrs(e.owner_key),
        json={"input_tokens": 500},
    )
    assert r.status_code == 200, r.text
    assert (await _item(db_session, e)).item_status != "needs_human"
    s = await _start_worker(client, e)
    assert s.status_code == 409
    assert s.json()["error"]["details"]["error"] == "loop_needs_human"
    assert (await _item(db_session, e)).item_status == "needs_human"


@pytest.mark.asyncio
async def test_late_finish_cannot_move_the_ticket_head(
    client: AsyncClient, db_session: AsyncSession
):
    """[G-takeover-head] After a takeover the ticket's head is the newest
    lease's commit, whatever order the finishes arrive in: the loop status
    keeps reporting B's commit."""
    e = await _env(db_session, client)
    a = (await _start_worker(client, e, key=e.owner_key)).json()["run_id"]
    b = (await _start_worker(client, e, key=e.member_key)).json()["run_id"]
    assert (await _finish(client, e, b, key=e.member_key, head_sha=SHA2)).status_code == 200
    await _finish(client, e, a, key=e.owner_key, head_sha=SHA1)
    assert (await _loop_child(client, e))["head_sha"] == SHA2


# ── finish ──


@pytest.mark.asyncio
async def test_finish_replay_adds_usage_once(client: AsyncClient, db_session: AsyncSession):
    """[G-finish-1] Repeating a finish with the same head_sha replays the
    stored result and does not add usage a second time."""
    e = await _env(db_session, client)
    run_id = (await _start_worker(client, e)).json()["run_id"]
    usage = {"input_tokens": 100, "output_tokens": 10}
    first = await _finish(client, e, run_id, usage=usage)
    again = await _finish(client, e, run_id, usage=usage)
    assert first.status_code == again.status_code == 200
    assert first.json()["idempotent"] is False and again.json()["idempotent"] is True
    run = await db_session.get(AgentRun, run_id)
    await db_session.refresh(run)
    assert (run.input_tokens, run.output_tokens) == (100, 10)


# ── review rounds ──


@pytest.mark.asyncio
async def test_round_replay_returns_stored_round_even_with_a_different_body(
    client: AsyncClient, db_session: AsyncSession
):
    """[G-round-1] The idempotency key is (ticket, stage, round, step): a
    retry with the same key returns the stored round, even when the retried
    body differs, and never posts a second verdict."""
    e = await _env(db_session, client)
    first = await _round(client, e)
    again = await _round(client, e, verdict="VERIFIED-CLEAN", findings=[])
    assert first.status_code == again.status_code == 201
    assert again.json()["idempotent"] is True
    assert again.json()["run_id"] == first.json()["run_id"]
    assert (await _item(db_session, e)).item_status != "done"
    verdicts = (
        await db_session.execute(
            select(TicketComment).where(
                TicketComment.ticket_id == e.child_id,
                TicketComment.verdict_trusted.is_(True),
            )
        )
    ).scalars().all()
    assert len(verdicts) == 1


@pytest.mark.asyncio
async def test_late_verdict_for_an_old_head_is_not_ready_at_the_new_head(
    client: AsyncClient, db_session: AsyncSession
):
    """[G-old-sha] A clean verdict bound to an older commit never makes the
    current head ready, even when it arrives last."""
    e = await _env(db_session, client)
    run = (await _start_worker(client, e)).json()["run_id"]
    await _finish(client, e, run, head_sha=SHA2)
    r = await _round(client, e, verdict="VERIFIED-CLEAN", findings=[], reviewed_commit=SHA1)
    assert r.status_code == 201, r.text
    assert (await _readiness(client, e, SHA2))["ready"] is False
    child = await _loop_child(client, e)
    assert child["head_sha"] == SHA2 and child["ready"] is False


@pytest.mark.asyncio
async def test_item_closed_on_an_old_head_is_not_ready_at_the_new_head(
    client: AsyncClient, db_session: AsyncSession
):
    """[G-close-sha] Closing a loop item does not by itself make a commit
    mergeable: a clean round on SHA1 can close the item while the worker has
    already pushed SHA2, and readiness at SHA2 stays false."""
    e = await _env(db_session, client)
    run1 = (await _start_worker(client, e, key=e.member_key)).json()["run_id"]
    await _finish(client, e, run1, key=e.member_key, head_sha=SHA1)
    run2 = (await _start_worker(client, e, key=e.member_key)).json()["run_id"]
    await _finish(client, e, run2, key=e.member_key, head_sha=SHA2)
    r = await _round(client, e, verdict="VERIFIED-CLEAN", findings=[], reviewed_commit=SHA1)
    assert r.status_code == 201 and r.json()["item_status"] == "done"
    assert (await _readiness(client, e, SHA2))["ready"] is False
    assert (await _readiness(client, e, SHA1))["ready"] is True
    child = await _loop_child(client, e)
    assert child["head_sha"] == SHA2 and child["ready"] is False


# ── close item ──


@pytest.mark.asyncio
async def test_close_item_retry_after_success_is_409(client: AsyncClient, db_session: AsyncSession):
    """[G-close-1] A human close is a guarded needs_human → done transition:
    the retry of a close that already landed gets 409, and the item stays
    done with one close record."""
    e = await _env(db_session, client, max_review_rounds=1)
    await _round(client, e)
    assert (await _item(db_session, e)).item_status == "needs_human"
    url = _p(e, f"loops/{e.loop_id}/children/{e.child_id}/close")
    first = await client.post(url, headers=_hdrs(e.owner_key))
    again = await client.post(url, headers=_hdrs(e.owner_key))
    assert first.status_code == 200, first.text
    assert again.status_code == 409
    assert (await _item(db_session, e)).item_status == "done"


# ── key rotation ──


@pytest.mark.asyncio
async def test_rotated_reviewer_key_is_untrusted_until_registered(
    client: AsyncClient, db_session: AsyncSession
):
    """[G-rotate-trust] Reviewer trust is bound to the exact key id: after a
    rotation the new key's verdicts are untrusted (and close nothing) until
    the new key is registered as a trusted reviewer."""
    e = await _env(db_session, client, trust_reviewer=False)
    old, old_raw = await _make_service_key(
        db_session, e.org, e.reviewer, SCOPES, project_ids=[e.project.id]
    )
    db_session.add(
        TrustedReviewer(
            id=f"tr_{uuid.uuid4().hex[:16]}",
            project_id=e.project.id,
            service_key_id=old.id,
            user_id=e.reviewer.id,
            reviewer_persona="reviewer",
            is_active=True,
            created_by_user_id=e.owner.id,
            created_at=datetime.now(timezone.utc),
        )
    )
    await db_session.commit()
    run = (await _start_worker(client, e, key=e.member_key)).json()["run_id"]
    await _finish(client, e, run, key=e.member_key)
    r1 = await _round(client, e, key=old_raw)
    assert r1.status_code == 201 and r1.json()["verdict_trusted"] is True

    _new, new_raw = await _rotate(db_session, old, e.owner.id)
    r2 = await _round(client, e, key=new_raw, round=2, verdict="VERIFIED-CLEAN", findings=[])
    assert r2.status_code == 201, r2.text
    assert r2.json()["verdict_trusted"] is False
    assert (await _item(db_session, e)).item_status != "done"


@pytest.mark.asyncio
async def test_rotated_worker_key_finishes_its_predecessors_run(
    client: AsyncClient, db_session: AsyncSession
):
    """[G-rotate-finish] A worker whose key was rotated mid-run finishes the
    run with the new key."""
    e = await _env(db_session, client)
    old, old_raw = await _make_service_key(
        db_session, e.org, e.member, SCOPES, project_ids=[e.project.id]
    )
    run = (await _start_worker(client, e, key=old_raw)).json()["run_id"]
    _new, new_raw = await _rotate(db_session, old, e.member.id)
    f = await _finish(client, e, run, key=new_raw)
    assert f.status_code == 200, f.text


@pytest.mark.asyncio
async def test_rotator_of_the_implementer_key_cannot_close_the_item(
    client: AsyncClient, db_session: AsyncSession
):
    """[G-rotate-independence] The person who rotated the implementer's key is
    that key's identity too: their trusted clean round does not close the
    item, and readiness stays false."""
    e = await _env(db_session, client)
    old, _old_raw = await _make_service_key(
        db_session, e.org, e.member, SCOPES, project_ids=[e.project.id]
    )
    new, new_raw = await _rotate(db_session, old, e.reviewer.id)
    run = (await _start_worker(client, e, key=new_raw)).json()["run_id"]
    await _finish(client, e, run, key=new_raw)
    r = await _round(client, e, verdict="VERIFIED-CLEAN", findings=[])
    assert r.status_code == 201 and r.json()["verdict_trusted"] is True
    assert (await _item(db_session, e)).item_status != "done"
    assert (await _readiness(client, e, SHA1))["ready"] is False


# ── work-queue settle ──


@pytest.mark.asyncio
async def test_settle_with_a_stale_directive_is_refused(db_session: AsyncSession):
    """[G-settle-lease] Only the item's open directive can be settled: a
    made-up or superseded directive id gets directive_mismatch, posts no
    verdict and leaves the lease open."""
    from sessionfs.server.services import work_queues as wq
    from tests.server.integration.test_work_queue_settle_verdict import (
        R1_CHANGES,
        _emit_directive,
        _setup,
    )

    org, user, project, ticket, queue, item = await _setup(db_session, registered=True)
    d = await _emit_directive(db_session, queue)
    await db_session.commit()
    with pytest.raises(wq.StepEngineError) as exc:
        await wq.complete_work_queue_step(
            db_session, queue=queue, item_id=d["item_id"], directive_id="dir_stale",
            ticket_id=ticket.id, outcome="posted_review", comment_id=None,
            agent_run_id=None, failed=False, verdict_content=R1_CHANGES,
            actor_user_id=user.id, actor_org_id=org.id, actor_service_key_id=None,
            actor_type="user", service_key_name=None,
        )
    assert exc.value.code == "directive_mismatch" and exc.value.http_status == 409
    ticket_id = ticket.id
    await db_session.rollback()
    row = await db_session.get(type(item), d["item_id"])
    await db_session.refresh(row)
    assert row.open_directive_id == d["directive_id"]
    comments = (
        await db_session.execute(
            select(TicketComment).where(TicketComment.ticket_id == ticket_id)
        )
    ).scalars().all()
    assert not [c for c in comments if c.author_persona == "codex-reviewer"]


@pytest.mark.asyncio
async def test_settle_with_a_stale_ticket_lease_is_refused(db_session: AsyncSession):
    """[G-settle-epoch] A settle that passes the ticket lease_epoch it was
    given is refused (stale_lease_epoch) once the ticket's lease moved on, and
    the lease stays open so the directive re-emits."""
    from sessionfs.server.services import work_queues as wq
    from tests.server.integration.test_work_queue_engine import (
        _make_item,
        _make_project,
        _make_queue,
        _make_ticket,
        _make_user_with_key,
    )

    user, _ = await _make_user_with_key(db_session)
    project = await _make_project(db_session, user)
    ticket = await _make_ticket(db_session, project)
    queue = await _make_queue(db_session, project, user)
    await _make_item(db_session, queue, ticket)
    await db_session.commit()
    step = await wq.run_work_queue_step(
        db_session, queue=queue, wake_source="manual", wake_ref=None, max_tickets=None
    )
    await db_session.commit()
    d = step.directives[0]
    epoch = d["ticket_lease_epoch"]
    await db_session.execute(
        update(Ticket).where(Ticket.id == ticket.id).values(lease_epoch=epoch + 1)
    )
    comment = TicketComment(
        id=f"tc_{uuid.uuid4().hex[:16]}", ticket_id=ticket.id, author_user_id=user.id,
        author_persona="atlas", content="done", created_at=datetime.now(timezone.utc),
    )
    db_session.add(comment)
    await db_session.commit()
    with pytest.raises(wq.StepEngineError) as exc:
        await wq.complete_work_queue_step(
            db_session, queue=queue, item_id=d["item_id"], directive_id=d["directive_id"],
            ticket_id=ticket.id, outcome="completed_ticket", comment_id=comment.id,
            agent_run_id=None, failed=False, ticket_lease_epoch=epoch,
        )
    assert exc.value.code == "stale_lease_epoch" and exc.value.http_status == 409
    queue_id = queue.id
    await db_session.rollback()
    queue = await db_session.get(type(queue), queue_id)
    again = await wq.run_work_queue_step(
        db_session, queue=queue, wake_source="manual", wake_ref=None, max_tickets=None
    )
    assert again.directives and again.directives[0]["directive_id"] == d["directive_id"]


# ── merge check post ──


@pytest.mark.asyncio
async def test_merge_check_post_retries_5xx_but_not_4xx(
    client: AsyncClient, db_session: AsyncSession, monkeypatch
):
    """[G-check-retry] A check-run call is retried on a 5xx (up to three
    attempts) and never on a 4xx."""
    import httpx

    from sessionfs.server.services import merge_checks
    from tests.server.integration import test_merge_checks as mc

    gh = mc._CheckRuns()
    posts = {"n": 0, "fail_with": 502}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            posts["n"] += 1
            if posts["n"] == 1:
                return httpx.Response(posts["fail_with"], json={})
        return gh._handle(request)

    async def _token(installation_id: int) -> str:
        return "tok"

    async def _grant(token: str, full_name: str) -> dict:
        return {"full_name": full_name}

    monkeypatch.setattr(merge_checks, "_http_transport", httpx.MockTransport(handler))
    monkeypatch.setattr(merge_checks, "get_installation_token", _token)
    monkeypatch.setattr(merge_checks, "_find_installation_repo", _grant)
    monkeypatch.setattr(merge_checks, "_GITHUB_RETRY_DELAYS", (0.0, 0.0))

    _owner, project, ticket = await mc._ready_ticket(db_session)
    ok = await merge_checks.refresh_merge_check(
        db_session, ticket_id=ticket.id, project_id=project.id
    )
    assert ok["posted"] == "check_run" and posts["n"] == 2

    gh.runs_by_sha.clear()
    posts.update(n=0, fail_with=422)
    refused = await merge_checks.refresh_merge_check(
        db_session, ticket_id=ticket.id, project_id=project.id
    )
    assert refused.get("posted") != "check_run" and posts["n"] == 1


@pytest.mark.asyncio
async def test_stale_merge_check_post_cannot_overwrite_a_newer_one(
    client: AsyncClient, db_engine, db_session: AsyncSession, monkeypatch
):
    """[G-check-order] Two re-evaluations of the same commit: the one that
    computed readiness first must not have the last word."""
    from sessionfs.server.services import merge_checks
    from tests.server.integration import test_merge_checks as mc

    gh = mc._CheckRuns()

    async def _token(installation_id: int) -> str:
        return "tok"

    async def _grant(token: str, full_name: str) -> dict:
        return {"full_name": full_name}

    monkeypatch.setattr(merge_checks, "_http_transport", gh.transport())
    monkeypatch.setattr(merge_checks, "get_installation_token", _token)
    monkeypatch.setattr(merge_checks, "_find_installation_repo", _grant)

    owner, project, ticket = await mc._ready_ticket(db_session)

    real_post = merge_checks._post_or_update_check_run
    first_computed = asyncio.Event()
    newer_posted = asyncio.Event()
    calls = {"n": 0}

    async def ordered_post(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            first_computed.set()
            await asyncio.wait_for(newer_posted.wait(), 10)
            return await real_post(*args, **kwargs)
        out = await real_post(*args, **kwargs)
        newer_posted.set()
        return out

    monkeypatch.setattr(merge_checks, "_post_or_update_check_run", ordered_post)
    factory = async_sessionmaker(db_engine, expire_on_commit=False)

    async def refresh() -> dict:
        async with factory() as s:
            return await merge_checks.refresh_merge_check(
                s, ticket_id=ticket.id, project_id=project.id
            )

    stale = asyncio.create_task(refresh())
    await asyncio.wait_for(first_computed.wait(), 10)
    reviewer = (
        await db_session.execute(
            select(TicketComment.author_user_id).where(TicketComment.ticket_id == ticket.id)
        )
    ).scalar_one()
    db_session.add(
        TicketComment(
            id=f"tc_{uuid.uuid4().hex[:16]}",
            ticket_id=ticket.id,
            author_user_id=reviewer,
            author_persona="codex-reviewer",
            content="Codex R2 review on tk_x: CHANGES REQUESTED\n\nFindings:\n\n - HIGH - x\n",
            verdict_trusted=True,
            reviewed_commit=mc.HEAD,
            created_at=datetime.now(timezone.utc) + timedelta(seconds=1),
        )
    )
    await db_session.commit()
    newer = await refresh()
    assert newer["ready"] is False
    assert (await stale)["ready"] is True
    runs = gh.runs_by_sha[mc.HEAD]
    assert [r["conclusion"] for r in runs][-1] != "success", json.dumps(runs)
