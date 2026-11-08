"""Readiness reads never raise approval requests; the write/trigger paths do
(tk_215621358a664738 item 1).

Reads: GET readiness (what the check_merge_readiness MCP tool calls), loop
status, the GitHub check / GitLab status refresh. Triggers: the merge-check
route, a recorded verdict (the re-evaluation hook), a trusted verdict comment,
GitHub and GitLab PR/MR delivery ingest, and the org recheck after a policy
write. Each trigger raises exactly one request however often it runs.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from sessionfs.server.db.models import (
    AgentRun,
    ApprovalRequest,
    PullRequest,
    Ticket,
    TicketPrLink,
)
from sessionfs.server.services import approvals as svc
from sessionfs.server.services import delivery_ingest, merge_checks
from tests.server.integration.approvals_helpers import (
    HEAD,
    hdrs,
    make_policy,
    make_world,
)


async def _requests(db: AsyncSession) -> list[ApprovalRequest]:
    stmt = select(ApprovalRequest).execution_options(populate_existing=True)
    return list((await db.execute(stmt)).scalars().all())


def _no_detached_rechecks(monkeypatch) -> None:
    monkeypatch.setattr(merge_checks, "schedule_recheck", lambda t: None)
    monkeypatch.setattr(merge_checks, "schedule_org_recheck", lambda o: None)


async def _open_pr(db: AsyncSession, w, *, provider: str = "github") -> None:
    pr = PullRequest(
        id=f"pr_{uuid.uuid4().hex[:16]}", project_id=w.project.id, provider=provider,
        repo=w.project.git_remote_normalized, number=7, title="pr", state="open",
        branch="feat", head_sha=HEAD,
    )
    db.add(pr)
    await db.flush()
    db.add(TicketPrLink(ticket_id=w.ticket.id, pr_id=pr.id, link_source="manual"))
    await db.commit()


# ── reads ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_readiness_route_creates_no_request(
    client: AsyncClient, db_session: AsyncSession, monkeypatch
):
    _no_detached_rechecks(monkeypatch)
    w = await make_world(db_session)  # trusted-clean at HEAD: otherwise ready
    await make_policy(db_session, w.org)
    for _ in range(2):
        resp = await client.get(
            f"/api/v1/projects/{w.project.id}/tickets/{w.ticket.id}/readiness",
            headers=hdrs(w.owner_key), params={"sha": HEAD},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["ready"] is False
        assert body["approvals"][0]["state"] == "not_requested"
        assert body["approvals"][0]["request_id"] is None
    assert await _requests(db_session) == []


@pytest.mark.asyncio
async def test_readiness_read_reports_expiry_without_writing(
    client: AsyncClient, db_session: AsyncSession, monkeypatch
):
    _no_detached_rechecks(monkeypatch)
    w = await make_world(db_session)
    await make_policy(db_session, w.org)
    sha = await svc.raise_ticket_approvals(db_session, w.ticket)
    assert sha == HEAD
    await db_session.commit()
    (req,) = await _requests(db_session)
    req.expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
    await db_session.commit()

    resp = await client.get(
        f"/api/v1/projects/{w.project.id}/tickets/{w.ticket.id}/readiness",
        headers=hdrs(w.owner_key), params={"sha": HEAD},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["approvals"][0]["state"] == "expired"
    rows = await _requests(db_session)
    assert [(r.id, r.state) for r in rows] == [(req.id, "pending")]

    # The next trigger persists the expiry and raises a fresh request.
    await svc.raise_ticket_approvals(db_session, w.ticket)
    await db_session.commit()
    states = sorted(r.state for r in await _requests(db_session))
    assert states == ["expired", "pending"]


@pytest.mark.asyncio
async def test_loop_status_read_creates_no_request(
    client: AsyncClient, db_session: AsyncSession, monkeypatch
):
    _no_detached_rechecks(monkeypatch)
    w = await make_world(db_session)
    parent = Ticket(
        id=f"tk_{uuid.uuid4().hex[:16]}", project_id=w.project.id, title="Loop",
        kind="issue", created_by_user_id=w.owner.id,
        loop_config=json.dumps({"queue_id": "wq_x"}),
    )
    db_session.add(parent)
    child = await db_session.get(Ticket, w.ticket.id)
    assert child is not None
    child.parent_ticket_id = parent.id
    db_session.add(AgentRun(
        id=f"run_{uuid.uuid4().hex[:16]}", project_id=w.project.id, persona_name="atlas",
        role="implementer", ticket_id=child.id, head_sha=HEAD,
        created_at=datetime.now(timezone.utc),
    ))
    await db_session.commit()
    await make_policy(db_session, w.org)

    for url in (
        f"/api/v1/projects/{w.project.id}/loops",
        f"/api/v1/projects/{w.project.id}/loops/{parent.id}",
    ):
        resp = await client.get(url, headers=hdrs(w.owner_key))
        assert resp.status_code == 200, resp.text
    assert await _requests(db_session) == []


@pytest.mark.asyncio
async def test_merge_check_refresh_alone_creates_no_request(
    db_session: AsyncSession, monkeypatch
):
    _no_detached_rechecks(monkeypatch)
    w = await make_world(db_session)
    await _open_pr(db_session, w)
    await make_policy(db_session, w.org)
    await merge_checks.refresh_merge_check(
        db_session, ticket_id=w.ticket.id, project_id=w.project.id
    )
    await merge_checks.refresh_gitlab_statuses(db_session, w.ticket.id)
    await db_session.commit()
    assert await _requests(db_session) == []


# ── triggers: each raises exactly one ──────────────────────────────


@pytest.mark.asyncio
async def test_merge_check_trigger_route_raises_one(
    client: AsyncClient, db_session: AsyncSession, monkeypatch
):
    _no_detached_rechecks(monkeypatch)
    w = await make_world(db_session)
    await make_policy(db_session, w.org)
    for _ in range(2):
        resp = await client.post(
            f"/api/v1/projects/{w.project.id}/tickets/{w.ticket.id}/merge-check",
            headers=hdrs(w.owner_key),
        )
        assert resp.status_code == 200, resp.text
    rows = await _requests(db_session)
    assert [(r.target_id, r.target_fingerprint, r.state) for r in rows] == [
        (w.ticket.id, HEAD, "pending")
    ]


@pytest.mark.asyncio
async def test_verdict_hook_raises_one(db_session: AsyncSession, monkeypatch):
    _no_detached_rechecks(monkeypatch)
    w = await make_world(db_session)
    await make_policy(db_session, w.org)
    for _ in range(2):
        await merge_checks.on_verdict_recorded(db_session, w.ticket.id)
        await db_session.commit()
    assert len(await _requests(db_session)) == 1


@pytest.mark.asyncio
async def test_trusted_verdict_comment_schedules_the_recheck(
    client: AsyncClient, db_session: AsyncSession, monkeypatch
):
    from sessionfs.server.routes import tickets as tickets_route

    seen: list[str] = []
    monkeypatch.setattr(merge_checks, "schedule_recheck", seen.append)

    async def trusted(*_a, **_k) -> bool:
        return True

    monkeypatch.setattr(tickets_route, "is_registered_trusted_reviewer", trusted)
    w = await make_world(db_session)
    url = f"/api/v1/projects/{w.project.id}/tickets/{w.ticket.id}/comments"
    plain = await client.post(url, headers=hdrs(w.reviewer_key), json={"content": "lgtm"})
    assert plain.status_code == 201, plain.text
    assert seen == []
    verdict = await client.post(
        url, headers=hdrs(w.reviewer_key),
        json={
            "content": f"Codex R2 review on {w.ticket.id}: VERIFIED-CLEAN\n\nFindings: none.",
            "reviewed_commit": HEAD,
        },
    )
    assert verdict.status_code == 201, verdict.text
    assert seen == [w.ticket.id]


@pytest.mark.asyncio
async def test_org_recheck_raises_one_for_each_open_pr_ticket(
    db_engine, db_session: AsyncSession, monkeypatch
):
    _no_detached_rechecks(monkeypatch)
    w = await make_world(db_session)
    await _open_pr(db_session, w)
    await make_policy(db_session, w.org)
    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    for _ in range(2):
        assert await merge_checks.recheck_org(w.org.id, session_factory=factory) == 1
    rows = await _requests(db_session)
    assert [(r.target_id, r.target_fingerprint) for r in rows] == [(w.ticket.id, HEAD)]


def _github_pr(w, action: str) -> dict:
    return {
        "action": action,
        "repository": {"clone_url": f"https://github.com/{w.project.git_remote_normalized}"},
        "pull_request": {
            "id": 99, "number": 3, "title": f"Fix {w.ticket.id}", "state": "open",
            "head": {"ref": "feat", "sha": HEAD}, "body": "",
        },
    }


@pytest.mark.asyncio
async def test_github_delivery_pr_ingest_raises_one(db_session: AsyncSession, monkeypatch):
    _no_detached_rechecks(monkeypatch)
    w = await make_world(db_session)
    await make_policy(db_session, w.org)
    for action in ("opened", "synchronize", "edited"):
        result = await delivery_ingest.ingest_event(
            db_session, provider="github", org_id=w.org.id, event="pull_request",
            payload=_github_pr(w, action),
        )
        assert result == {"status": "ok"}, result
    rows = await _requests(db_session)
    assert [(r.target_id, r.target_fingerprint) for r in rows] == [(w.ticket.id, HEAD)]


@pytest.mark.asyncio
async def test_gitlab_delivery_mr_ingest_raises_one(db_session: AsyncSession, monkeypatch):
    _no_detached_rechecks(monkeypatch)
    w = await make_world(db_session)
    await make_policy(db_session, w.org)
    for action in ("open", "update"):
        payload = {
            "object_kind": "merge_request",
            "project": {"git_http_url": f"https://gitlab.com/{w.project.git_remote_normalized}.git"},
            "object_attributes": {
                "id": 5, "iid": 4, "title": f"Fix {w.ticket.id}", "state": "opened",
                "action": action, "source_branch": "feat", "description": "",
                "last_commit": {"id": HEAD},
            },
        }
        result = await delivery_ingest.ingest_event(
            db_session, provider="gitlab", org_id=w.org.id, event="merge_request",
            payload=payload,
        )
        assert result == {"status": "ok"}, result
    rows = await _requests(db_session)
    assert [(r.target_id, r.target_fingerprint) for r in rows] == [(w.ticket.id, HEAD)]


@pytest.mark.asyncio
async def test_closed_pr_ingest_raises_nothing(db_session: AsyncSession, monkeypatch):
    _no_detached_rechecks(monkeypatch)
    w = await make_world(db_session)
    await make_policy(db_session, w.org)
    payload = _github_pr(w, "closed")
    payload["pull_request"]["state"] = "closed"
    await delivery_ingest.ingest_event(
        db_session, provider="github", org_id=w.org.id, event="pull_request",
        payload=payload,
    )
    assert await _requests(db_session) == []


@pytest.mark.asyncio
async def test_failed_approval_raise_keeps_the_ledger_claim(db_session: AsyncSession, monkeypatch):
    """A raise that fails rolls back only its savepoint: the delivery's
    ledger claim (inserted, not yet committed, by the route) survives and is
    committed with the event, so the delivery is neither lost nor re-run."""
    from sqlalchemy import func, select

    from sessionfs.server.db.models import DeliveryWebhookEvent

    _no_detached_rechecks(monkeypatch)
    w = await make_world(db_session)
    await make_policy(db_session, w.org)

    async def _boom(*_a, **_k):
        raise RuntimeError("raise failed")

    monkeypatch.setattr(merge_checks, "raise_approvals_for_commit", _boom)
    assert await delivery_ingest.record_delivery_event(
        db_session, provider="github", org_id=w.org.id, idempotency_key="k-raise-fails"
    )
    result = await delivery_ingest.ingest_event(
        db_session, provider="github", org_id=w.org.id, event="pull_request",
        payload=_github_pr(w, "opened"),
    )
    assert result == {"status": "ok"}, result
    await db_session.commit()
    claims = (
        await db_session.execute(select(func.count()).select_from(DeliveryWebhookEvent))
    ).scalar_one()
    assert claims == 1
    assert await _requests(db_session) == []
