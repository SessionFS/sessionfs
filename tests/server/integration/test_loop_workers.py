"""Review Loop R3 (Brief D3) — worker runs and review rounds."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server.db.models import (
    AgentRun,
    LoopChecklistItem,
    Organization,
    Project,
    Ticket,
    TicketComment,
    TrustedReviewer,
    User,
    WorkQueueItem,
)
from tests.server.integration.test_loops_api import (
    _add_org_member,
    _hdrs,
    _loop_body,
    _make_org,
    _make_project,
    _make_service_key,
    _make_user,
    _start_loop,
)

SHA1 = "a" * 40
SHA2 = "b" * 40
FINDING_NEW = {
    "severity": "high",
    "class": "new",
    "harm": "crash",
    "title": "Unchecked null in the parser",
    "file": "src/p.py",
    "line": 10,
}


class Env:
    owner: User
    owner_key: str
    reviewer: User
    reviewer_key: str
    member: User
    member_key: str
    org: Organization
    project: Project
    loop_id: str
    child_id: str
    queue_id: str


async def _env(
    db: AsyncSession, client: AsyncClient, *, trust_reviewer: bool = True, **loop_extra
) -> Env:
    e = Env()
    e.owner, e.owner_key = await _make_user(db, "owner")
    e.reviewer, e.reviewer_key = await _make_user(db, "rev")
    e.member, e.member_key = await _make_user(db, "mem")
    e.org = await _make_org(db)
    await _add_org_member(db, e.org, e.owner, "admin")
    await _add_org_member(db, e.org, e.reviewer, "member")
    await _add_org_member(db, e.org, e.member, "member")
    e.project = await _make_project(db, e.owner, e.org)
    resp = await _start_loop(
        client,
        e.project,
        e.owner_key,
        _loop_body([{"title": "child"}], title="Loop", **loop_extra),
    )
    assert resp.status_code == 201, resp.text
    e.loop_id = resp.json()["loop_id"]
    e.child_id = resp.json()["tickets"][0]["id"]
    e.queue_id = resp.json()["queue_id"]
    if trust_reviewer:
        db.add(
            TrustedReviewer(
                id=f"tr_{uuid.uuid4().hex[:16]}",
                project_id=e.project.id,
                user_id=e.reviewer.id,
                reviewer_persona="reviewer",
                is_active=True,
                created_by_user_id=e.owner.id,
                created_at=datetime.now(timezone.utc),
            )
        )
        await db.commit()
    return e


def _p(e: Env, tail: str) -> str:
    return f"/api/v1/projects/{e.project.id}/{tail}"


async def _start_worker(client, e: Env, key: str | None = None, **extra):
    return await client.post(
        _p(e, "agent-runs/start-worker"),
        headers=_hdrs(key or e.owner_key),
        json={"ticket_id": e.child_id, "model": "m1", "provider": "p1", **extra},
    )


async def _finish(client, e: Env, run_id: str, key: str | None = None, **over):
    body = {"head_sha": SHA1, "exit_reason": "finished", **over}
    return await client.post(
        _p(e, f"agent-runs/{run_id}/finish"), headers=_hdrs(key or e.owner_key), json=body
    )


async def _round(client, e: Env, *, key: str | None = None, **over):
    body = {
        "ticket_id": e.child_id,
        "round": 1,
        "stage": "code",
        "reviewed_commit": SHA1,
        "verdict": "CHANGES-REQUESTED",
        "findings": [FINDING_NEW],
        **over,
    }
    return await client.post(
        _p(e, f"loops/{e.loop_id}/rounds"), headers=_hdrs(key or e.reviewer_key), json=body
    )


async def _item(db: AsyncSession, e: Env) -> WorkQueueItem:
    return (
        await db.execute(
            select(WorkQueueItem)
            .where(WorkQueueItem.ticket_id == e.child_id)
            .execution_options(populate_existing=True)
        )
    ).scalars().one()


@pytest.mark.asyncio
async def test_start_finish_round_happy_path(client: AsyncClient, db_session: AsyncSession):
    e = await _env(db_session, client)
    r = await _start_worker(client, e)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["loop_id"] == e.loop_id
    assert body["lease_epoch"] == 1
    assert body["ticket_id"] == e.child_id
    run = await db_session.get(AgentRun, body["run_id"])
    assert run.role == "implementer" and run.status == "running"
    item = await _item(db_session, e)
    assert run.work_queue_id == e.queue_id and run.work_queue_item_id == item.id
    child = await db_session.get(Ticket, e.child_id)
    await db_session.refresh(child)
    assert child.status == "in_progress"

    f = await _finish(
        client, e, body["run_id"],
        usage={"input_tokens": 100, "output_tokens": 50, "cost_usd": 0.5, "cost_basis": "reported"},
        summary="done",
    )
    assert f.status_code == 200, f.text
    fj = f.json()
    assert fj["status"] == "passed" and fj["tokens"] == 150 and fj["idempotent"] is False
    assert fj["cost_usd"] == 0.5  # owner is an org admin

    r1 = await _round(client, e)
    assert r1.status_code == 201, r1.text
    j1 = r1.json()
    assert j1["verdict_trusted"] is True and j1["round"] == 1
    comment = await db_session.get(TicketComment, j1["comment_id"])
    assert comment.reviewed_commit == SHA1 and comment.verdict_trusted is True
    assert "CHANGES REQUESTED" in comment.content
    # class=new finding became a proposed checklist item
    items = (await db_session.execute(select(LoopChecklistItem))).scalars().all()
    assert any(i.status == "proposed" for i in items)
    assert (await _item(db_session, e)).item_status != "done"

    r2 = await _round(client, e, round=2, verdict="VERIFIED-CLEAN", findings=[])
    assert r2.status_code == 201, r2.text
    assert r2.json()["item_status"] == "done"


@pytest.mark.asyncio
async def test_finish_idempotent_and_conflicting_sha(client: AsyncClient, db_session: AsyncSession):
    e = await _env(db_session, client)
    run_id = (await _start_worker(client, e)).json()["run_id"]
    usage = {"input_tokens": 10, "output_tokens": 5}
    a = await _finish(client, e, run_id, usage=usage)
    assert a.status_code == 200
    b = await _finish(client, e, run_id, usage=usage)
    assert b.status_code == 200 and b.json()["idempotent"] is True
    assert b.json()["tokens"] == 15
    run = await db_session.get(AgentRun, run_id)
    await db_session.refresh(run)
    assert run.input_tokens == 10 and run.output_tokens == 5
    c = await _finish(client, e, run_id, head_sha=SHA2, usage=usage)
    assert c.status_code == 409


@pytest.mark.asyncio
async def test_round_idempotent_posts_one_verdict(client: AsyncClient, db_session: AsyncSession):
    e = await _env(db_session, client)
    a = await _round(client, e)
    b = await _round(client, e)
    assert a.status_code == 201 and b.status_code == 201
    assert b.json()["idempotent"] is True and b.json()["run_id"] == a.json()["run_id"]
    comments = (
        await db_session.execute(
            select(TicketComment).where(TicketComment.ticket_id == e.child_id)
        )
    ).scalars().all()
    assert len([c for c in comments if "R1 review" in c.content]) == 1


@pytest.mark.asyncio
async def test_untrusted_reviewer_verdict_not_trusted_and_does_not_close(
    client: AsyncClient, db_session: AsyncSession
):
    e = await _env(db_session, client, trust_reviewer=False)
    r = await _round(client, e, verdict="VERIFIED-CLEAN", findings=[])
    assert r.status_code == 201, r.text
    assert r.json()["verdict_trusted"] is False
    assert (await _item(db_session, e)).item_status != "done"


@pytest.mark.asyncio
async def test_same_identity_reviewer_cannot_close_item(
    client: AsyncClient, db_session: AsyncSession
):
    e = await _env(db_session, client)
    # The owner implements AND is registered as the trusted reviewer.
    db_session.add(
        TrustedReviewer(
            id=f"tr_{uuid.uuid4().hex[:16]}",
            project_id=e.project.id,
            user_id=e.owner.id,
            reviewer_persona="reviewer",
            is_active=True,
            created_by_user_id=e.owner.id,
            created_at=datetime.now(timezone.utc),
        )
    )
    await db_session.commit()
    run_id = (await _start_worker(client, e)).json()["run_id"]
    await _finish(client, e, run_id)
    r = await _round(client, e, key=e.owner_key, verdict="VERIFIED-CLEAN", findings=[])
    assert r.status_code == 201 and r.json()["verdict_trusted"] is True
    assert (await _item(db_session, e)).item_status != "done"
    # An independent reviewer's clean round does close it.
    r2 = await _round(client, e, round=2, verdict="VERIFIED-CLEAN", findings=[])
    assert r2.json()["item_status"] == "done"


@pytest.mark.asyncio
async def test_round_cap_parks_item_and_start_refused(client: AsyncClient, db_session: AsyncSession):
    e = await _env(db_session, client, max_review_rounds=1)
    r = await _round(client, e)
    assert r.status_code == 201, r.text
    assert r.json()["needs_human"] is True
    assert (await _item(db_session, e)).item_status == "needs_human"
    s = await _start_worker(client, e)
    assert s.status_code == 409
    assert s.json()["error"]["details"]["error"] == "loop_needs_human"


@pytest.mark.asyncio
async def test_token_budget_parks_on_finish_and_start_refused(
    client: AsyncClient, db_session: AsyncSession
):
    e = await _env(db_session, client, token_budget=100)
    run_id = (await _start_worker(client, e)).json()["run_id"]
    f = await _finish(client, e, run_id, usage={"input_tokens": 200, "output_tokens": 0})
    assert f.status_code == 200 and f.json()["needs_human"] is True
    s = await _start_worker(client, e)
    assert s.status_code == 409 and s.json()["error"]["details"]["error"] == "loop_needs_human"


@pytest.mark.asyncio
async def test_design_round_on_parent(client: AsyncClient, db_session: AsyncSession):
    e = await _env(db_session, client)
    r = await _round(client, e, ticket_id=e.loop_id, stage="design", findings=[], verdict="VERIFIED-CLEAN")
    assert r.status_code == 201, r.text
    assert r.json()["stage"] == "design" and r.json()["item_status"] is None
    comment = await db_session.get(TicketComment, r.json()["comment_id"])
    assert comment.ticket_id == e.loop_id and "doc review" in comment.content
    bad = await _round(client, e, ticket_id=e.loop_id, stage="code", round=2)
    assert bad.status_code == 422


@pytest.mark.asyncio
async def test_outsider_and_cross_org_get_one_404(client: AsyncClient, db_session: AsyncSession):
    e = await _env(db_session, client)
    outsider, okey = await _make_user(db_session, "out")
    a = await _start_worker(client, e, key=okey)
    assert a.status_code == 404
    other_org = await _make_org(db_session)
    await _add_org_member(db_session, other_org, outsider, "admin")
    _, skey = await _make_service_key(
        db_session, other_org, outsider,
        ["loops:write", "tickets:write", "agent_runs:write", "work_queues:write"],
    )
    b = await _start_worker(client, e, key=skey)
    assert b.status_code == 404 and b.json() == a.json()
    c = await _round(client, e, key=skey)
    assert c.status_code == 404 and c.json() == a.json()
    d = await client.post(
        _p(e, "agent-runs/run_nope/finish"),
        headers=_hdrs(e.owner_key),
        json={"head_sha": SHA1, "exit_reason": "finished"},
    )
    assert d.status_code == 404 and d.json() == a.json()


@pytest.mark.asyncio
async def test_service_key_scopes(client: AsyncClient, db_session: AsyncSession):
    e = await _env(db_session, client)
    _, k1 = await _make_service_key(
        db_session, e.org, e.owner, ["loops:write"], project_ids=[e.project.id]
    )
    r = await _start_worker(client, e, key=k1)
    assert r.status_code == 403
    assert r.json()["error"]["details"]["error"] == "insufficient_scope"
    _, k2 = await _make_service_key(
        db_session, e.org, e.owner, ["loops:write", "tickets:write", "agent_runs:write"],
        project_ids=[e.project.id],
    )
    ok = await _start_worker(client, e, key=k2)
    assert ok.status_code == 201, ok.text
    # no loops:write at all
    _, k3 = await _make_service_key(db_session, e.org, e.owner, ["tickets:write"])
    assert (await _start_worker(client, e, key=k3)).status_code == 403
    # a service key never sees dollars
    assert "cost_budget_usd" not in (ok.json()["budget"] or {})


@pytest.mark.asyncio
async def test_dollars_absent_for_members_present_for_admin(
    client: AsyncClient, db_session: AsyncSession
):
    e = await _env(db_session, client, cost_budget_usd=5.0)
    admin = await _start_worker(client, e)
    assert admin.status_code == 201
    assert admin.json()["budget"]["cost_budget_usd"] == 5.0
    mem = await _start_worker(client, e, key=e.member_key)
    assert mem.status_code == 201, mem.text
    assert "cost_budget_usd" not in mem.json()["budget"]
    assert "cost_remaining_usd" not in mem.json()["budget"]
    f = await _finish(
        client, e, mem.json()["run_id"], key=e.member_key,
        usage={"input_tokens": 1, "cost_usd": 1.0},
    )
    assert f.status_code == 200 and "cost_usd" not in f.json() and "budget" in f.json()
    assert "cost_budget_usd" not in f.json()["budget"]


@pytest.mark.asyncio
async def test_nul_and_surrogate_in_text_fields_are_sanitized(
    client: AsyncClient, db_session: AsyncSession
):
    e = await _env(db_session, client)
    raw = (
        '{"ticket_id": "%s", "model": "m\\u0000x\\ud800y", "provider": "p\\u00001",'
        ' "branch": "feat/\\u0000b", "tool": "t\\u0000"}' % e.child_id
    )
    r = await client.post(
        _p(e, "agent-runs/start-worker"),
        headers={**_hdrs(e.owner_key), "content-type": "application/json"},
        content=raw,
    )
    assert r.status_code == 201, r.text
    run = await db_session.get(AgentRun, r.json()["run_id"])
    assert "\x00" not in (run.model + run.provider + run.branch + run.tool)
    assert "\ud800" not in run.model


@pytest.mark.asyncio
async def test_resolver_reviewer_cannot_close_item(
    client: AsyncClient, db_session: AsyncSession
):
    e = await _env(db_session, client)
    run_id = (await _start_worker(client, e)).json()["run_id"]
    await _finish(client, e, run_id)
    ticket = await db_session.get(Ticket, e.child_id)
    ticket.resolver_user_id = e.reviewer.id
    await db_session.commit()
    r = await _round(client, e, verdict="VERIFIED-CLEAN", findings=[])
    assert r.status_code == 201 and r.json()["verdict_trusted"] is True
    assert (await _item(db_session, e)).item_status != "done"


@pytest.mark.asyncio
async def test_unknown_implementer_does_not_auto_close(
    client: AsyncClient, db_session: AsyncSession
):
    e = await _env(db_session, client)
    # No worker ever started: no implementer is recorded -> fail closed.
    r = await _round(client, e, verdict="VERIFIED-CLEAN", findings=[])
    assert r.status_code == 201
    assert (await _item(db_session, e)).item_status != "done"
