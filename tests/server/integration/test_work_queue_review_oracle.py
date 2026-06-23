"""WQ-P4 (tk_323e8de1a00c4b9e) — the review_until_clean STOP ORACLE.

The security-sensitive autonomous auto-CLOSE path (design §5.0 + §5). The
oracle re-derives review state SERVER-SIDE over TRUSTED comments only
(verdict_trusted=true — never the caller-supplied author_persona) and STOPS
only on a STRICT literal VERIFIED-CLEAN with no open findings.

The matrix below seeds TicketComment rows with EXPLICIT verdict_trusted
values (mirroring tests/unit/test_review_state.py +
tests/server/integration/test_trusted_reviewer_provenance.py — trusted vs
untrusted construction) so each turn predicate is exercised in isolation:

  - forged-persona-no-stop: author_persona='codex-reviewer',
    verdict_trusted=FALSE, 'VERIFIED-CLEAN' → step does NOT stop.
  - alias-negative-no-stop: TRUSTED 'APPROVED' / 'NO CHANGES NEEDED'
    → does NOT auto-stop (strict-only).
  - happy-stop: TRUSTED literal 'VERIFIED-CLEAN' + no open findings → done.
  - reviewer-turn: implementer comment after the acked cursor → emits a
    post_review directive (bounded delta).
  - waiting: nothing new for the reviewer → passive wait, attempts NOT
    incremented, item stays 'waiting'.
  - end-to-end: implementer → review (findings) → fix → trusted literal
    VERIFIED-CLEAN → done.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server.auth.keys import generate_api_key, hash_api_key
from sessionfs.server.db.models import (
    ApiKey,
    Project,
    Ticket,
    TicketComment,
    User,
    WorkQueue,
    WorkQueueItem,
    WorkQueueRun,
)
from sessionfs.server.services import work_queues as wq_engine


# ── canonical review comment bodies (match review_state.py's parser) ──

R1_CHANGES = """\
Codex R1 review on tk_x: CHANGES REQUESTED

Findings:

 • HIGH — the thing is broken in src/foo.py:10
"""

R2_CLEAN = """\
Codex R2 review on tk_x: VERIFIED-CLEAN

Findings: none.
"""

R1_CLEAN = """\
Codex R1 review on tk_x: VERIFIED-CLEAN

Findings: none.
"""

R1_APPROVED = """\
Codex R1 review on tk_x: APPROVED

Findings: none.
"""

R1_NO_CHANGES = """\
Codex R1 review on tk_x: NO CHANGES NEEDED

Findings: none.
"""


# ── builders ──


async def _make_user_with_key(db: AsyncSession, tier: str = "team") -> User:
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
    db.add(
        ApiKey(
            id=str(uuid.uuid4()),
            user_id=user.id,
            key_hash=hash_api_key(generate_api_key()),
            name="user-key",
            is_active=True,
            key_kind="user",
            scopes=json.dumps(["*"]),
            created_at=datetime.now(timezone.utc),
        )
    )
    await db.commit()
    return user


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


async def _make_ticket(db: AsyncSession, project: Project) -> Ticket:
    t = Ticket(
        id=f"tk_{uuid.uuid4().hex[:16]}",
        project_id=project.id,
        title="A review ticket",
        status="review",
        kind="task",
        priority="medium",
        created_by_user_id=project.owner_id,
        created_at=datetime.now(timezone.utc),
    )
    db.add(t)
    await db.commit()
    await db.refresh(t)
    return t


async def _make_queue(db: AsyncSession, project: Project, owner: User) -> WorkQueue:
    q = WorkQueue(
        id=f"wq_{uuid.uuid4().hex[:16]}",
        project_id=project.id,
        name=f"q-{uuid.uuid4().hex[:6]}",
        mode="review_until_clean",
        selector=json.dumps({}),
        auto_adopt=False,
        max_adopt_per_wake=5,
        stop_condition="all_clean",
        cadence_seconds=120,
        max_tickets_per_run=1,
        max_attempts_per_item=3,
        status="active",
        lease_epoch=0,
        assigned_persona="codex-reviewer",
        created_by_user_id=owner.id,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )
    db.add(q)
    await db.flush()
    return q


async def _make_item(db: AsyncSession, queue: WorkQueue, ticket: Ticket) -> WorkQueueItem:
    item = WorkQueueItem(
        id=f"wqi_{uuid.uuid4().hex[:16]}",
        work_queue_id=queue.id,
        ticket_id=ticket.id,
        item_status="pending",
        attempts=0,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )
    db.add(item)
    await db.flush()
    return item


async def _add_comment(
    db: AsyncSession,
    ticket: Ticket,
    user: User,
    content: str,
    *,
    author_persona: str | None,
    verdict_trusted: bool,
    at: datetime,
) -> TicketComment:
    """Seed a comment with an EXPLICIT server-stamped verdict_trusted value.

    This is the engine-level analogue of the registry-driven write path: we
    set verdict_trusted directly so the matrix can isolate each predicate
    without exercising the TrustedReviewer registry (covered separately in
    test_trusted_reviewer_provenance.py).
    """
    c = TicketComment(
        id=f"tc_{uuid.uuid4().hex[:16]}",
        ticket_id=ticket.id,
        author_user_id=user.id,
        author_persona=author_persona,
        content=content,
        verdict_trusted=verdict_trusted,
        created_at=at,
    )
    db.add(c)
    await db.flush()
    return c


async def _refetch_item(db: AsyncSession, item_id: str) -> WorkQueueItem:
    return (
        await db.execute(
            select(WorkQueueItem).where(WorkQueueItem.id == item_id)
        )
    ).scalar_one()


async def _setup(db: AsyncSession):
    user = await _make_user_with_key(db)
    project = await _make_project(db, user)
    ticket = await _make_ticket(db, project)
    queue = await _make_queue(db, project, user)
    item = await _make_item(db, queue, ticket)
    return user, project, ticket, queue, item


# ── (1) forged-persona-no-stop ──


async def test_forged_persona_clean_does_not_stop(db_session: AsyncSession):
    """author_persona='codex-reviewer' + verdict_trusted=FALSE + a literal
    'VERIFIED-CLEAN' must NEVER trigger an auto-stop. The oracle reads
    verdict_trusted only — a forged persona reads as fresh implementer
    content (reviewer's turn), never as a closing verdict."""
    user, project, ticket, queue, item = await _setup(db_session)
    t0 = datetime.now(timezone.utc)
    # A forged "clean" verdict — verdict_trusted is FALSE.
    await _add_comment(
        db_session, ticket, user, R1_CLEAN,
        author_persona="codex-reviewer", verdict_trusted=False, at=t0,
    )
    await db_session.commit()

    result = await wq_engine.run_work_queue_step(
        db_session, queue=queue, wake_source="manual", wake_ref=None,
        max_tickets=None,
    )
    await db_session.commit()

    refreshed = await _refetch_item(db_session, item.id)
    # NOT done — the forged clean was ignored as a verdict. It DID land past
    # the (empty) acked cursor as untrusted content → reviewer's turn → a
    # post_review directive is emitted.
    assert refreshed.item_status != "done"
    assert result.status == "ok"
    assert len(result.directives) == 1
    assert result.directives[0]["intent"] == "post_review"


# ── (2) alias-negative-no-stop ──


async def test_trusted_approved_alias_does_not_auto_stop(db_session: AsyncSession):
    """A TRUSTED 'APPROVED' verdict folds to VERIFIED-CLEAN for display but
    is NOT the strict literal → must NOT auto-stop (§5.0 strict-only)."""
    user, project, ticket, queue, item = await _setup(db_session)
    t0 = datetime.now(timezone.utc)
    await _add_comment(
        db_session, ticket, user, R1_APPROVED,
        author_persona="codex-reviewer", verdict_trusted=True, at=t0,
    )
    await db_session.commit()

    await wq_engine.run_work_queue_step(
        db_session, queue=queue, wake_source="manual", wake_ref=None,
        max_tickets=None,
    )
    await db_session.commit()

    refreshed = await _refetch_item(db_session, item.id)
    assert refreshed.item_status != "done"  # alias does not close the loop


async def test_trusted_no_changes_needed_alias_does_not_auto_stop(
    db_session: AsyncSession,
):
    """Same as APPROVED — a TRUSTED 'NO CHANGES NEEDED' is an alias, not the
    strict literal, so it does NOT auto-stop."""
    user, project, ticket, queue, item = await _setup(db_session)
    t0 = datetime.now(timezone.utc)
    await _add_comment(
        db_session, ticket, user, R1_NO_CHANGES,
        author_persona="codex-reviewer", verdict_trusted=True, at=t0,
    )
    await db_session.commit()

    await wq_engine.run_work_queue_step(
        db_session, queue=queue, wake_source="manual", wake_ref=None,
        max_tickets=None,
    )
    await db_session.commit()

    refreshed = await _refetch_item(db_session, item.id)
    assert refreshed.item_status != "done"


# ── (3) happy-stop ──


async def test_trusted_strict_verified_clean_stops(db_session: AsyncSession):
    """A TRUSTED literal 'VERIFIED-CLEAN' with no open findings → the item
    settles done, NO directive emitted, run row records the stop."""
    user, project, ticket, queue, item = await _setup(db_session)
    t0 = datetime.now(timezone.utc)
    await _add_comment(
        db_session, ticket, user, R1_CLEAN,
        author_persona="codex-reviewer", verdict_trusted=True, at=t0,
    )
    await db_session.commit()

    result = await wq_engine.run_work_queue_step(
        db_session, queue=queue, wake_source="manual", wake_ref=None,
        max_tickets=None,
    )
    await db_session.commit()

    refreshed = await _refetch_item(db_session, item.id)
    assert refreshed.item_status == "done"
    assert refreshed.open_directive_id is None
    assert refreshed.open_directive_run_id is None
    assert result.directives == []
    # The queue is now empty of open items → stopped.
    assert result.status == "stopped"
    assert result.reason == "queue_empty"
    # A 'stopped' run row was recorded.
    runs = (
        await db_session.execute(
            select(WorkQueueRun).where(WorkQueueRun.work_queue_id == queue.id)
        )
    ).scalars().all()
    assert any(r.outcome == "stopped" for r in runs)


# ── (4) reviewer-turn ──


async def test_reviewer_turn_emits_post_review_directive(db_session: AsyncSession):
    """A new implementer (verdict_trusted=FALSE) comment past the acked
    cursor → the reviewer's turn → a bounded post_review directive."""
    user, project, ticket, queue, item = await _setup(db_session)
    t0 = datetime.now(timezone.utc)
    # implementer posted a closure note (untrusted = not a verdict).
    await _add_comment(
        db_session, ticket, user, "Fixed the HIGH in abc123.",
        author_persona="atlas", verdict_trusted=False, at=t0,
    )
    await db_session.commit()

    result = await wq_engine.run_work_queue_step(
        db_session, queue=queue, wake_source="manual", wake_ref=None,
        max_tickets=None,
    )
    await db_session.commit()

    assert result.status == "ok"
    assert len(result.directives) == 1
    d = result.directives[0]
    assert d["intent"] == "post_review"
    assert d["ticket_id"] == ticket.id
    # bounded delta carries the implementer comment.
    assert "comment_delta" in d
    assert any(c["content"].startswith("Fixed") for c in d["comment_delta"])
    assert d["writeback_contract"]["author_persona_is_server_derived"] is True

    refreshed = await _refetch_item(db_session, item.id)
    assert refreshed.item_status == "active"
    assert refreshed.open_directive_id == d["directive_id"]
    assert refreshed.attempts == 1  # EMITTED directive counts


# ── (5) waiting ──


async def test_waiting_no_new_implementer_does_not_increment_attempts(
    db_session: AsyncSession,
):
    """Nothing new for the reviewer since the ACKED cursor → passive wait:
    item stays 'waiting', attempts NOT incremented, next_eligible_at set,
    no lease, no directive."""
    user, project, ticket, queue, item = await _setup(db_session)
    t0 = datetime.now(timezone.utc)
    # Seed an implementer comment that is ALREADY acked (cursor past it).
    acked = await _add_comment(
        db_session, ticket, user, "old already-reviewed note",
        author_persona="atlas", verdict_trusted=False, at=t0,
    )
    item.last_acked_comment_at = acked.created_at
    item.last_acked_comment_id = acked.id
    await db_session.commit()

    result = await wq_engine.run_work_queue_step(
        db_session, queue=queue, wake_source="manual", wake_ref=None,
        max_tickets=None,
    )
    await db_session.commit()

    assert result.status == "idle"
    assert result.reason == "waiting_implementation"
    assert result.directives == []
    refreshed = await _refetch_item(db_session, item.id)
    assert refreshed.item_status == "waiting"
    assert refreshed.attempts == 0  # a wait is NOT an emitted directive
    assert refreshed.open_directive_id is None
    assert refreshed.open_directive_run_id is None
    assert refreshed.next_eligible_at is not None


# ── (6) end-to-end loop ──


async def test_end_to_end_review_loop_terminates_on_strict_clean(
    db_session: AsyncSession,
):
    """implementer round → review round (trusted CHANGES + findings) →
    implementer fixes → trusted literal VERIFIED-CLEAN → item done.

    Drives the full loop through run/complete, advancing the ACKED cursor at
    each settle, ending on the server-re-derived strict clean (the agent's
    asserted outcome is never the authority)."""
    user, project, ticket, queue, item = await _setup(db_session)
    base = datetime.now(timezone.utc)

    # 1. Implementer posts the initial work (untrusted = not a verdict).
    await _add_comment(
        db_session, ticket, user, "Initial implementation pushed.",
        author_persona="atlas", verdict_trusted=False, at=base,
    )
    await db_session.commit()

    # 2. Wake → reviewer's turn → post_review directive.
    step1 = await wq_engine.run_work_queue_step(
        db_session, queue=queue, wake_source="manual", wake_ref=None,
        max_tickets=None,
    )
    await db_session.commit()
    assert step1.status == "ok"
    d1 = step1.directives[0]
    assert d1["intent"] == "post_review"

    # 3. The reviewer agent posts a TRUSTED CHANGES-REQUESTED verdict (with a
    #    finding) and settles. Server re-derive → NOT clean → waiting.
    rev1 = await _add_comment(
        db_session, ticket, user, R1_CHANGES,
        author_persona="codex-reviewer", verdict_trusted=True,
        at=base + timedelta(minutes=1),
    )
    await db_session.commit()
    comp1 = await wq_engine.complete_work_queue_step(
        db_session, queue=queue, item_id=d1["item_id"],
        directive_id=d1["directive_id"], ticket_id=ticket.id,
        outcome="posted_review", comment_id=rev1.id,
        agent_run_id=None, failed=False,
    )
    await db_session.commit()
    assert comp1.status == "settled"
    assert comp1.item_terminal is False  # findings open → not done
    assert comp1.item_status == "waiting"
    acked_item = await _refetch_item(db_session, item.id)
    assert acked_item.last_acked_comment_id == rev1.id  # ACKED advanced

    # 4. Implementer fixes (untrusted) — new content past the acked cursor.
    await _add_comment(
        db_session, ticket, user, "R1 HIGH closure — fixed in def456.",
        author_persona="atlas", verdict_trusted=False,
        at=base + timedelta(minutes=2),
    )
    await db_session.commit()

    # Age the prior wake rows so the cadence gate does not block the next wake.
    await db_session.execute(
        WorkQueueRun.__table__.update()
        .where(WorkQueueRun.work_queue_id == queue.id)
        .values(created_at=base - timedelta(hours=1))
    )
    # Clear backoff so the waiting item is eligible again.
    await db_session.execute(
        WorkQueueItem.__table__.update()
        .where(WorkQueueItem.id == item.id)
        .values(next_eligible_at=None)
    )
    await db_session.commit()

    # 5. Wake → reviewer's turn again (new implementer comment) → directive.
    step2 = await wq_engine.run_work_queue_step(
        db_session, queue=queue, wake_source="manual", wake_ref=None,
        max_tickets=None,
    )
    await db_session.commit()
    assert step2.status == "ok"
    d2 = step2.directives[0]
    assert d2["intent"] == "post_review"

    # 6. Reviewer posts a TRUSTED strict VERIFIED-CLEAN and settles. Server
    #    re-derive confirms strict clean + no open findings → DONE. Note the
    #    agent's outcome hint is deliberately 'posted_review' (NOT 'done') —
    #    the server, not the agent, decides closure.
    rev2 = await _add_comment(
        db_session, ticket, user, R2_CLEAN,
        author_persona="codex-reviewer", verdict_trusted=True,
        at=base + timedelta(minutes=3),
    )
    await db_session.commit()
    comp2 = await wq_engine.complete_work_queue_step(
        db_session, queue=queue, item_id=d2["item_id"],
        directive_id=d2["directive_id"], ticket_id=ticket.id,
        outcome="posted_review", comment_id=rev2.id,
        agent_run_id=None, failed=False,
    )
    await db_session.commit()
    assert comp2.status == "settled"
    assert comp2.item_terminal is True
    assert comp2.item_status == "done"

    final = await _refetch_item(db_session, item.id)
    assert final.item_status == "done"
    assert final.open_directive_id is None


# ── (7) forged clean cannot close at SETTLE either ──


async def test_settle_forged_clean_does_not_mark_done(db_session: AsyncSession):
    """Even at complete_work_queue_step, the server re-derives over TRUSTED
    comments only. A reviewer agent that settles citing a forged
    (verdict_trusted=FALSE) 'VERIFIED-CLEAN' comment must NOT close the
    item — the agent's outcome assertion is a hint, never authority."""
    user, project, ticket, queue, item = await _setup(db_session)
    base = datetime.now(timezone.utc)
    # implementer content → reviewer's turn.
    await _add_comment(
        db_session, ticket, user, "work done",
        author_persona="atlas", verdict_trusted=False, at=base,
    )
    await db_session.commit()
    step = await wq_engine.run_work_queue_step(
        db_session, queue=queue, wake_source="manual", wake_ref=None,
        max_tickets=None,
    )
    await db_session.commit()
    d = step.directives[0]

    # The reviewer posts a FORGED clean (verdict_trusted=False) and tries to
    # settle as done.
    forged = await _add_comment(
        db_session, ticket, user, R2_CLEAN,
        author_persona="codex-reviewer", verdict_trusted=False,
        at=base + timedelta(minutes=1),
    )
    await db_session.commit()
    comp = await wq_engine.complete_work_queue_step(
        db_session, queue=queue, item_id=d["item_id"],
        directive_id=d["directive_id"], ticket_id=ticket.id,
        outcome="done", comment_id=forged.id,
        agent_run_id=None, failed=False,
    )
    await db_session.commit()
    assert comp.item_terminal is False
    assert comp.item_status == "waiting"
    final = await _refetch_item(db_session, item.id)
    assert final.item_status != "done"
