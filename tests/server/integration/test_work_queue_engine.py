"""WQ-P3 (tk_3de50bf7bb73418b) — work-queue STEP ENGINE + safety envelope.

Covers run_work_queue_step / complete_work_queue_step (services + routes):

- open-directive re-emit returns the SAME directive_id, no second claim/run.
- fresh-claim happy path returns a bounded directive with expected fields.
- complete advances last_acked_* (and ONLY complete does); settles the lease;
  links agent_run_id.
- duplicate complete for the same directive_id is idempotent (no double-apply).
- crash simulation: run (SEEN advanced, lease open) then a second run WITHOUT
  complete → re-emits same directive (no new claim); then complete → ACKED
  advances.
- failure path: attempts increment, next_eligible_at set by backoff,
  item_status='failed' after max_attempts_per_item.
- cadence: a wake before cadence_seconds elapsed is a no-op.
- max_tickets_per_run respected; auto_adopt materializes capped by
  max_adopt_per_wake.
- scope/permission: missing work_queues:write → 403; review_until_clean step →
  not-available (422).
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server.auth.keys import generate_api_key, hash_api_key
from sessionfs.server.db.models import (
    ApiKey,
    Organization,
    Project,
    Ticket,
    TicketComment,
    User,
    WorkQueue,
    WorkQueueItem,
    WorkQueueRun,
)
from sessionfs.server.services import work_queues as wq_engine


# ── helpers ──


def _hdrs(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


async def _make_user_with_key(
    db: AsyncSession, scopes: list[str] | None = None, tier: str = "team"
) -> tuple[User, str]:
    user = User(
        id=str(uuid.uuid4()),
        email=f"u-{uuid.uuid4().hex[:6]}@example.com",
        display_name="U",
        tier=tier,
        email_verified=True,
        created_at=datetime.now(timezone.utc),
    )
    db.add(user)
    await db.flush()
    raw = generate_api_key()
    db.add(
        ApiKey(
            id=str(uuid.uuid4()),
            user_id=user.id,
            key_hash=hash_api_key(raw),
            name="user-key",
            is_active=True,
            key_kind="user",
            scopes=json.dumps(scopes if scopes is not None else ["*"]),
            created_at=datetime.now(timezone.utc),
        )
    )
    await db.commit()
    return user, raw


async def _make_org(db: AsyncSession) -> Organization:
    org = Organization(
        id=f"org_{uuid.uuid4().hex[:8]}",
        name=f"Org-{uuid.uuid4().hex[:6]}",
        slug=f"o-{uuid.uuid4().hex[:6]}",
        tier="team",
        created_at=datetime.now(timezone.utc),
    )
    db.add(org)
    await db.flush()
    return org


async def _make_service_key(
    db: AsyncSession, org_id: str, minter: User, scopes: list[str]
) -> str:
    raw = generate_api_key()
    db.add(
        ApiKey(
            id=str(uuid.uuid4()),
            user_id=minter.id,
            key_hash=hash_api_key(raw),
            name=f"svc-{uuid.uuid4().hex[:6]}",
            is_active=True,
            key_kind="service",
            org_id=org_id,
            scopes=json.dumps(scopes),
            created_by_user_id=minter.id,
            service_key_name=f"svc-{uuid.uuid4().hex[:6]}",
            created_at=datetime.now(timezone.utc),
        )
    )
    await db.commit()
    return raw


async def _make_project(db: AsyncSession, owner: User) -> Project:
    project = Project(
        id=f"proj_{uuid.uuid4().hex[:12]}",
        name=f"wq-{uuid.uuid4().hex[:6]}",
        git_remote_normalized=f"github.com/acme/{uuid.uuid4().hex[:8]}",
        context_document="",
        owner_id=owner.id,
        created_at=datetime.now(timezone.utc),
    )
    db.add(project)
    await db.commit()
    await db.refresh(project)
    return project


async def _make_ticket(db: AsyncSession, project: Project, status: str = "open",
                       priority: str = "medium") -> Ticket:
    t = Ticket(
        id=f"tk_{uuid.uuid4().hex[:16]}",
        project_id=project.id,
        title="A ticket",
        status=status,
        kind="task",
        priority=priority,
        created_by_user_id=project.owner_id,
        created_at=datetime.now(timezone.utc),
    )
    db.add(t)
    await db.commit()
    await db.refresh(t)
    return t


async def _make_queue(
    db: AsyncSession,
    project: Project,
    owner: User,
    *,
    mode: str = "implement_until_done",
    cadence: int = 120,
    max_tickets: int = 1,
    max_attempts: int = 3,
    auto_adopt: bool = False,
    max_adopt_per_wake: int = 5,
    selector: dict | None = None,
) -> WorkQueue:
    q = WorkQueue(
        id=f"wq_{uuid.uuid4().hex[:16]}",
        project_id=project.id,
        name=f"q-{uuid.uuid4().hex[:6]}",
        mode=mode,
        selector=json.dumps(selector or {}),
        auto_adopt=auto_adopt,
        max_adopt_per_wake=max_adopt_per_wake,
        stop_condition="queue_empty",
        cadence_seconds=cadence,
        max_tickets_per_run=max_tickets,
        max_attempts_per_item=max_attempts,
        status="active",
        lease_epoch=0,
        created_by_user_id=owner.id,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )
    db.add(q)
    await db.flush()
    return q


async def _make_item(db: AsyncSession, queue: WorkQueue, ticket: Ticket,
                     status: str = "pending") -> WorkQueueItem:
    item = WorkQueueItem(
        id=f"wqi_{uuid.uuid4().hex[:16]}",
        work_queue_id=queue.id,
        ticket_id=ticket.id,
        item_status=status,
        attempts=0,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )
    db.add(item)
    await db.flush()
    return item


# ── fresh-claim happy path ──


async def test_fresh_claim_returns_bounded_directive(
    db_session: AsyncSession,
):
    user, _ = await _make_user_with_key(db_session)
    project = await _make_project(db_session, user)
    ticket = await _make_ticket(db_session, project)
    queue = await _make_queue(db_session, project, user)
    item = await _make_item(db_session, queue, ticket)
    await db_session.commit()

    result = await wq_engine.run_work_queue_step(
        db_session, queue=queue, wake_source="manual", wake_ref=None,
        max_tickets=None,
    )
    await db_session.commit()

    assert result.status == "ok"
    assert result.work_queue_run_id is not None
    assert len(result.directives) == 1
    d = result.directives[0]
    # bounded directive shape
    assert d["directive_id"].startswith("dir_")
    assert d["item_id"] == item.id
    assert d["ticket_id"] == ticket.id
    assert d["intent"] == "implement"
    assert d["ticket_lease_epoch"] == ticket.lease_epoch
    assert "comment_delta" in d
    assert "expand_hints" in d
    assert d["writeback_contract"]["author_persona_is_server_derived"] is True

    # the item now holds an open directive lease + a run row exists
    refreshed = (
        await db_session.execute(
            select(WorkQueueItem).where(WorkQueueItem.id == item.id)
        )
    ).scalar_one()
    assert refreshed.open_directive_id == d["directive_id"]
    assert refreshed.item_status == "active"
    assert refreshed.attempts == 1  # EMITTED directive counts
    runs = (
        await db_session.execute(
            select(WorkQueueRun).where(WorkQueueRun.work_queue_id == queue.id)
        )
    ).scalars().all()
    assert len(runs) == 1
    assert runs[0].directive_id == d["directive_id"]
    assert runs[0].outcome is None  # not yet settled


# ── open-lease re-emit ──


async def test_open_directive_re_emits_same_id_no_second_claim(
    db_session: AsyncSession,
):
    user, _ = await _make_user_with_key(db_session)
    project = await _make_project(db_session, user)
    ticket = await _make_ticket(db_session, project)
    queue = await _make_queue(db_session, project, user, cadence=120)
    await _make_item(db_session, queue, ticket)
    await db_session.commit()

    first = await wq_engine.run_work_queue_step(
        db_session, queue=queue, wake_source="manual", wake_ref=None,
        max_tickets=None,
    )
    await db_session.commit()
    did = first.directives[0]["directive_id"]
    run_id = first.directives[0]["work_queue_run_id"]

    # Re-run without completing → re-emit the SAME directive_id + run id.
    second = await wq_engine.run_work_queue_step(
        db_session, queue=queue, wake_source="manual", wake_ref=None,
        max_tickets=None,
    )
    await db_session.commit()
    assert second.status == "ok"
    assert len(second.directives) == 1
    assert second.directives[0]["directive_id"] == did
    assert second.directives[0]["work_queue_run_id"] == run_id

    # No second run row was minted, attempts did not double-count.
    runs = (
        await db_session.execute(
            select(WorkQueueRun).where(WorkQueueRun.work_queue_id == queue.id)
        )
    ).scalars().all()
    assert len(runs) == 1
    item = (
        await db_session.execute(
            select(WorkQueueItem).where(WorkQueueItem.ticket_id == ticket.id)
        )
    ).scalar_one()
    assert item.attempts == 1


# ── complete advances ACKED + settles + links agent_run_id ──


async def test_complete_advances_acked_and_settles(db_session: AsyncSession):
    user, _ = await _make_user_with_key(db_session)
    project = await _make_project(db_session, user)
    ticket = await _make_ticket(db_session, project)
    queue = await _make_queue(db_session, project, user)
    await _make_item(db_session, queue, ticket)
    await db_session.commit()

    step = await wq_engine.run_work_queue_step(
        db_session, queue=queue, wake_source="manual", wake_ref=None,
        max_tickets=None,
    )
    await db_session.commit()
    d = step.directives[0]

    # The agent posts a comment.
    comment = TicketComment(
        id=f"tc_{uuid.uuid4().hex[:16]}",
        ticket_id=ticket.id,
        author_user_id=user.id,
        author_persona="atlas",
        content="Posted progress",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(comment)
    await db_session.commit()

    before = (
        await db_session.execute(
            select(WorkQueueItem).where(WorkQueueItem.id == d["item_id"])
        )
    ).scalar_one()
    assert before.last_acked_comment_id is None  # not advanced by run_step

    res = await wq_engine.complete_work_queue_step(
        db_session, queue=queue, item_id=d["item_id"],
        directive_id=d["directive_id"], ticket_id=ticket.id,
        outcome="posted_progress", comment_id=comment.id,
        agent_run_id="run_abc", failed=False,
    )
    await db_session.commit()

    assert res.status == "settled"
    item = (
        await db_session.execute(
            select(WorkQueueItem).where(WorkQueueItem.id == d["item_id"])
        )
    ).scalar_one()
    # ACKED advanced (ONLY complete advances it).
    assert item.last_acked_comment_id == comment.id
    # lease settled.
    assert item.open_directive_id is None
    assert item.open_directive_run_id is None
    assert item.item_status == "waiting"
    assert item.last_agent_run_id == "run_abc"
    # run row outcome stamped + agent_run linked.
    run = (
        await db_session.execute(
            select(WorkQueueRun).where(
                WorkQueueRun.directive_id == d["directive_id"]
            )
        )
    ).scalar_one()
    assert run.outcome is not None
    assert run.agent_run_id == "run_abc"


# ── idempotent duplicate complete ──


async def test_duplicate_complete_is_idempotent(db_session: AsyncSession):
    user, _ = await _make_user_with_key(db_session)
    project = await _make_project(db_session, user)
    ticket = await _make_ticket(db_session, project)
    queue = await _make_queue(db_session, project, user)
    await _make_item(db_session, queue, ticket)
    await db_session.commit()

    step = await wq_engine.run_work_queue_step(
        db_session, queue=queue, wake_source="manual", wake_ref=None,
        max_tickets=None,
    )
    await db_session.commit()
    d = step.directives[0]

    comment = TicketComment(
        id=f"tc_{uuid.uuid4().hex[:16]}",
        ticket_id=ticket.id,
        author_user_id=user.id,
        author_persona="atlas",
        content="done",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(comment)
    await db_session.commit()

    first = await wq_engine.complete_work_queue_step(
        db_session, queue=queue, item_id=d["item_id"],
        directive_id=d["directive_id"], ticket_id=ticket.id,
        outcome="completed_ticket", comment_id=comment.id,
        agent_run_id=None, failed=False,
    )
    await db_session.commit()
    assert first.status == "settled"
    assert first.item_terminal is True

    second = await wq_engine.complete_work_queue_step(
        db_session, queue=queue, item_id=d["item_id"],
        directive_id=d["directive_id"], ticket_id=ticket.id,
        outcome="completed_ticket", comment_id=comment.id,
        agent_run_id=None, failed=False,
    )
    await db_session.commit()
    assert second.status == "idempotent_replay"
    assert second.item_status == "done"


# ── crash simulation ──


async def test_crash_replay_then_complete(db_session: AsyncSession):
    user, _ = await _make_user_with_key(db_session)
    project = await _make_project(db_session, user)
    ticket = await _make_ticket(db_session, project)
    queue = await _make_queue(db_session, project, user)
    await _make_item(db_session, queue, ticket)
    await db_session.commit()

    # Wake 1: directive emitted, SEEN advanced, lease open, NO complete (crash).
    step1 = await wq_engine.run_work_queue_step(
        db_session, queue=queue, wake_source="manual", wake_ref=None,
        max_tickets=None,
    )
    await db_session.commit()
    did = step1.directives[0]["directive_id"]

    item_after1 = (
        await db_session.execute(
            select(WorkQueueItem).where(WorkQueueItem.ticket_id == ticket.id)
        )
    ).scalar_one()
    assert item_after1.last_acked_comment_id is None  # ACKED not moved

    # Wake 2 (no complete in between) → re-emits same directive, no new claim.
    step2 = await wq_engine.run_work_queue_step(
        db_session, queue=queue, wake_source="manual", wake_ref=None,
        max_tickets=None,
    )
    await db_session.commit()
    assert step2.directives[0]["directive_id"] == did

    # Now the agent posts + completes → ACKED advances.
    comment = TicketComment(
        id=f"tc_{uuid.uuid4().hex[:16]}",
        ticket_id=ticket.id,
        author_user_id=user.id,
        author_persona="atlas",
        content="recovered",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(comment)
    await db_session.commit()

    await wq_engine.complete_work_queue_step(
        db_session, queue=queue, item_id=item_after1.id,
        directive_id=did, ticket_id=ticket.id,
        outcome="posted_progress", comment_id=comment.id,
        agent_run_id=None, failed=False,
    )
    await db_session.commit()
    item = (
        await db_session.execute(
            select(WorkQueueItem).where(WorkQueueItem.id == item_after1.id)
        )
    ).scalar_one()
    assert item.last_acked_comment_id == comment.id
    assert item.open_directive_id is None


# ── failure path / backoff / failed-after-max ──


async def test_failure_backoff_and_failed_after_max_attempts(
    db_session: AsyncSession,
):
    user, _ = await _make_user_with_key(db_session)
    project = await _make_project(db_session, user)
    ticket = await _make_ticket(db_session, project)
    queue = await _make_queue(db_session, project, user, max_attempts=3)
    await _make_item(db_session, queue, ticket)
    await db_session.commit()

    last_eligible = None
    for attempt in range(1, 4):
        # Clear next_eligible so each wake can re-claim (simulate backoff
        # elapsed) AND age any prior wake rows past the cadence interval.
        await db_session.execute(
            WorkQueueItem.__table__.update()
            .where(WorkQueueItem.ticket_id == ticket.id)
            .values(next_eligible_at=None)
        )
        await db_session.execute(
            WorkQueueRun.__table__.update()
            .where(WorkQueueRun.work_queue_id == queue.id)
            .values(
                created_at=datetime.now(timezone.utc) - timedelta(hours=1)
            )
        )
        await db_session.commit()

        step = await wq_engine.run_work_queue_step(
            db_session, queue=queue, wake_source="manual", wake_ref=None,
            max_tickets=None,
        )
        await db_session.commit()
        assert step.status == "ok", f"attempt {attempt}: {step.reason}"
        d = step.directives[0]

        res = await wq_engine.complete_work_queue_step(
            db_session, queue=queue, item_id=d["item_id"],
            directive_id=d["directive_id"], ticket_id=ticket.id,
            outcome="waited", comment_id=None, agent_run_id=None,
            failed=True,
        )
        await db_session.commit()
        assert res.attempts == attempt
        if attempt < 3:
            assert res.item_status == "waiting"
            assert res.next_eligible_at is not None
            last_eligible = res.next_eligible_at
        else:
            assert res.item_status == "failed"
            assert res.item_terminal is True
            assert res.next_eligible_at is None
    assert last_eligible is not None

    # A failed item is NOT re-picked.
    await db_session.execute(
        WorkQueueItem.__table__.update()
        .where(WorkQueueItem.ticket_id == ticket.id)
        .values(next_eligible_at=None)
    )
    await db_session.execute(
        WorkQueueRun.__table__.update()
        .where(WorkQueueRun.work_queue_id == queue.id)
        .values(created_at=datetime.now(timezone.utc) - timedelta(hours=1))
    )
    await db_session.commit()
    step = await wq_engine.run_work_queue_step(
        db_session, queue=queue, wake_source="manual", wake_ref=None,
        max_tickets=None,
    )
    await db_session.commit()
    assert step.status == "stopped"
    assert step.reason == "queue_empty"


# ── cadence gate ──


async def test_cadence_gate_no_op_before_interval(db_session: AsyncSession):
    user, _ = await _make_user_with_key(db_session)
    project = await _make_project(db_session, user)
    ticket = await _make_ticket(db_session, project)
    queue = await _make_queue(db_session, project, user, cadence=300)
    await _make_item(db_session, queue, ticket)
    await db_session.commit()

    # Insert a recent wake row so the cadence gate trips.
    db_session.add(
        WorkQueueRun(
            id=f"wqr_{uuid.uuid4().hex[:16]}",
            work_queue_id=queue.id,
            work_queue_item_id=None,
            directive_id=None,
            outcome="noop",
            created_at=datetime.now(timezone.utc) - timedelta(seconds=30),
        )
    )
    await db_session.commit()

    result = await wq_engine.run_work_queue_step(
        db_session, queue=queue, wake_source="manual", wake_ref=None,
        max_tickets=None,
    )
    await db_session.commit()
    assert result.status == "idle"
    assert result.reason == "cadence"
    assert result.directives == []


# ── max_tickets_per_run ──


async def test_max_tickets_per_run_respected(db_session: AsyncSession):
    user, _ = await _make_user_with_key(db_session)
    project = await _make_project(db_session, user)
    queue = await _make_queue(db_session, project, user, max_tickets=2)
    for _ in range(4):
        t = await _make_ticket(db_session, project)
        await _make_item(db_session, queue, t)
    await db_session.commit()

    result = await wq_engine.run_work_queue_step(
        db_session, queue=queue, wake_source="manual", wake_ref=None,
        max_tickets=None,
    )
    await db_session.commit()
    assert result.status == "ok"
    assert len(result.directives) == 2  # capped at max_tickets_per_run


# ── auto_adopt materialization cap ──


async def test_auto_adopt_materializes_capped(db_session: AsyncSession):
    user, _ = await _make_user_with_key(db_session)
    project = await _make_project(db_session, user)
    # 5 open tickets matching the selector; cap is 2 per wake.
    for _ in range(5):
        await _make_ticket(db_session, project, status="open")
    queue = await _make_queue(
        db_session, project, user, max_tickets=1, auto_adopt=True,
        max_adopt_per_wake=2, selector={"status": "open"},
    )
    await db_session.commit()

    await wq_engine.run_work_queue_step(
        db_session, queue=queue, wake_source="manual", wake_ref=None,
        max_tickets=None,
    )
    await db_session.commit()

    items = (
        await db_session.execute(
            select(WorkQueueItem).where(WorkQueueItem.work_queue_id == queue.id)
        )
    ).scalars().all()
    # Only max_adopt_per_wake materialized this wake.
    assert len(items) == 2


async def test_auto_adopt_off_means_no_adoption(db_session: AsyncSession):
    user, _ = await _make_user_with_key(db_session)
    project = await _make_project(db_session, user)
    for _ in range(3):
        await _make_ticket(db_session, project, status="open")
    queue = await _make_queue(
        db_session, project, user, auto_adopt=False,
        selector={"status": "open"},
    )
    await db_session.commit()

    result = await wq_engine.run_work_queue_step(
        db_session, queue=queue, wake_source="manual", wake_ref=None,
        max_tickets=None,
    )
    await db_session.commit()
    items = (
        await db_session.execute(
            select(WorkQueueItem).where(WorkQueueItem.work_queue_id == queue.id)
        )
    ).scalars().all()
    assert len(items) == 0  # nothing materialized
    assert result.status == "stopped"
    assert result.reason == "queue_empty"


# ── review_until_clean not available (WQ-P4) ──


async def test_review_until_clean_not_available(db_session: AsyncSession):
    user, _ = await _make_user_with_key(db_session)
    project = await _make_project(db_session, user)
    ticket = await _make_ticket(db_session, project, status="review")
    queue = await _make_queue(
        db_session, project, user, mode="review_until_clean"
    )
    await _make_item(db_session, queue, ticket)
    await db_session.commit()

    with pytest.raises(wq_engine.StepEngineError) as exc:
        await wq_engine.run_work_queue_step(
            db_session, queue=queue, wake_source="manual", wake_ref=None,
            max_tickets=None,
        )
    assert exc.value.code == "review_until_clean_not_available"
    assert exc.value.http_status == 422


# ── route-level: scope enforcement + not-available ──


def _wq_url(project_id: str) -> str:
    return "/api/v1/projects/" + project_id + "/work-queues"


def _err_code(body: dict) -> str:
    """Navigate the {error:{code,message,details}} envelope the app's
    exception handler wraps structured-dict HTTPException detail in. We raise
    {"error": <code>, "message": ...}; the handler files our "error" under
    details["error"]."""
    err = body.get("error")
    if isinstance(err, dict):
        details = err.get("details") or {}
        if isinstance(details, dict) and details.get("error"):
            return str(details["error"])
        return str(err.get("code", ""))
    return ""


async def test_route_missing_write_scope_403(
    client: AsyncClient, db_session: AsyncSession,
):
    org = await _make_org(db_session)
    minter, _ = await _make_user_with_key(db_session)
    project = await _make_project(db_session, minter)
    queue = await _make_queue(db_session, project, minter)
    ticket = await _make_ticket(db_session, project)
    await _make_item(db_session, queue, ticket)
    await db_session.commit()

    # service key with only read scope
    read_key = await _make_service_key(
        db_session, org.id, minter, ["work_queues:read"]
    )
    resp = await client.post(
        _wq_url(project.id) + f"/{queue.id}/step",
        headers=_hdrs(read_key),
        json={},
    )
    assert resp.status_code == 403


async def test_route_missing_downstream_tickets_write_403(
    client: AsyncClient, db_session: AsyncSession,
):
    org = await _make_org(db_session)
    minter, _ = await _make_user_with_key(db_session)
    project = await _make_project(db_session, minter)
    queue = await _make_queue(db_session, project, minter)
    ticket = await _make_ticket(db_session, project)
    await _make_item(db_session, queue, ticket)
    await db_session.commit()

    # work_queues:write but NO tickets:write
    key = await _make_service_key(
        db_session, org.id, minter, ["work_queues:write"]
    )
    resp = await client.post(
        _wq_url(project.id) + f"/{queue.id}/step",
        headers=_hdrs(key),
        json={},
    )
    assert resp.status_code == 403
    assert _err_code(resp.json()) == "insufficient_scope"


async def test_route_review_until_clean_422(
    client: AsyncClient, db_session: AsyncSession,
):
    user, raw = await _make_user_with_key(db_session)
    project = await _make_project(db_session, user)
    queue = await _make_queue(
        db_session, project, user, mode="review_until_clean"
    )
    ticket = await _make_ticket(db_session, project, status="review")
    await _make_item(db_session, queue, ticket)
    await db_session.commit()

    resp = await client.post(
        _wq_url(project.id) + f"/{queue.id}/step",
        headers=_hdrs(raw),
        json={},
    )
    assert resp.status_code == 422
    assert _err_code(resp.json()) == "review_until_clean_not_available"


async def test_route_step_then_complete_roundtrip(
    client: AsyncClient, db_session: AsyncSession,
):
    user, raw = await _make_user_with_key(db_session)
    project = await _make_project(db_session, user)
    queue = await _make_queue(db_session, project, user)
    ticket = await _make_ticket(db_session, project)
    await _make_item(db_session, queue, ticket)
    await db_session.commit()

    resp = await client.post(
        _wq_url(project.id) + f"/{queue.id}/step",
        headers=_hdrs(raw),
        json={"wake_source": "loop"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "ok"
    d = body["directives"][0]

    # post a comment via the API so the writeback is validated.
    cresp = await client.post(
        f"/api/v1/projects/{project.id}/tickets/{ticket.id}/comments",
        headers=_hdrs(raw),
        json={"content": "progress", "author_persona": "atlas",
              "lease_epoch": d["ticket_lease_epoch"]},
    )
    assert cresp.status_code == 201, cresp.text
    comment_id = cresp.json()["id"]

    comp = await client.post(
        _wq_url(project.id) + f"/{queue.id}/step/complete",
        headers=_hdrs(raw),
        json={
            "item_id": d["item_id"],
            "directive_id": d["directive_id"],
            "ticket_id": ticket.id,
            "outcome": "posted_progress",
            "comment_id": comment_id,
        },
    )
    assert comp.status_code == 200, comp.text
    assert comp.json()["status"] == "settled"
    assert comp.json()["item_status"] == "waiting"
