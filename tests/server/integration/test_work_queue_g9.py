"""Review Loop B2 / G9 (tk_50bfb43bcd45457c) — verdicts bound to a commit +
strict close for implement queues.

Engine-level matrix over the single commit point `complete_work_queue_step`:

- A reviewer verdict settled through the review path stores `reviewed_commit`
  on the server-created comment; a malformed commit is rejected 422.
- An `implement_until_done` item never closes on the agent's CLAIMED outcome;
  it only closes on a trusted STRICT VERIFIED-CLEAN from a DIFFERENT identity.
- Unknown implementer identity fails closed even when a trusted clean exists.
- Self-review (reviewer == implementer) does not close the item.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server.auth.keys import generate_api_key, hash_api_key
from sessionfs.server.db.models import (
    ApiKey,
    Organization,
    Project,
    Ticket,
    TicketComment,
    TrustedReviewer,
    User,
    WorkQueue,
    WorkQueueItem,
    WorkQueueRun,
)
from sessionfs.server.services import work_queues as wq_engine

HEAD = "0123456789abcdef0123456789abcdef01234567"  # 40-char SHA-1
BAD_SHA = "not-a-sha"
SHORT_SHA = "a1b2c3d4e5f6a7b8"  # 16-char hex — formerly accepted, now rejected

CLEAN = "Codex R1 review on tk_x: VERIFIED-CLEAN\n\nFindings: none."


# ── builders ──


async def _make_user(db: AsyncSession, name: str = "u") -> User:
    user = User(
        id=str(uuid.uuid4()),
        email=f"{name}-{uuid.uuid4().hex[:6]}@example.com",
        display_name=name.capitalize(),
        tier="team",
        email_verified=True,
        created_at=datetime.now(timezone.utc),
    )
    db.add(user)
    await db.flush()
    return user


async def _make_service_key(db: AsyncSession, minter: User) -> ApiKey:
    key = ApiKey(
        id=f"sk_{uuid.uuid4().hex[:24]}",
        user_id=minter.id,
        key_hash=hash_api_key(generate_api_key()),
        name=f"svc-{uuid.uuid4().hex[:6]}",
        key_kind="service",
        scopes='["tickets:read","tickets:write"]',
        created_at=datetime.now(timezone.utc),
    )
    db.add(key)
    await db.flush()
    return key


async def _make_org(db: AsyncSession) -> Organization:
    org = Organization(
        id=f"org_{uuid.uuid4().hex[:8]}",
        name=f"org-{uuid.uuid4().hex[:6]}",
        slug=f"org-{uuid.uuid4().hex[:6]}",
        created_at=datetime.now(timezone.utc),
    )
    db.add(org)
    await db.flush()
    return org


async def _make_project(
    db: AsyncSession, owner: User, org: Organization | None = None
) -> Project:
    project = Project(
        id=f"proj_{uuid.uuid4().hex[:12]}",
        name=f"wq-{uuid.uuid4().hex[:6]}",
        git_remote_normalized=f"github.com/acme/{uuid.uuid4().hex[:8]}",
        context_document="",
        owner_id=owner.id,
        org_id=org.id if org is not None else None,
        created_at=datetime.now(timezone.utc),
    )
    db.add(project)
    await db.flush()
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
    await db.flush()
    return t


async def _make_queue(
    db: AsyncSession, project: Project, owner: User, *, mode: str
) -> WorkQueue:
    q = WorkQueue(
        id=f"wq_{uuid.uuid4().hex[:16]}",
        project_id=project.id,
        name=f"q-{uuid.uuid4().hex[:6]}",
        mode=mode,
        selector=json.dumps({}),
        auto_adopt=False,
        max_adopt_per_wake=5,
        stop_condition="all_clean" if mode == "review_until_clean" else "queue_empty",
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


async def _make_item(
    db: AsyncSession, queue: WorkQueue, ticket: Ticket
) -> WorkQueueItem:
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


async def _seed_comment(
    db: AsyncSession,
    ticket: Ticket,
    user: User,
    *,
    content: str,
    verdict_trusted: bool = False,
    reviewed_commit: str | None = None,
    service_key_id: str | None = None,
    created_at: datetime | None = None,
) -> TicketComment:
    c = TicketComment(
        id=f"tc_{uuid.uuid4().hex[:16]}",
        ticket_id=ticket.id,
        author_user_id=user.id,
        author_persona="codex-reviewer" if verdict_trusted else "atlas",
        content=content,
        verdict_trusted=verdict_trusted,
        reviewed_commit=reviewed_commit,
        service_key_id=service_key_id,
        created_at=created_at or datetime.now(timezone.utc),
    )
    db.add(c)
    await db.flush()
    return c


async def _register_reviewer(
    db: AsyncSession, *, project: Project, org: Organization | None, user: User
) -> None:
    db.add(
        TrustedReviewer(
            id=f"tr_{uuid.uuid4().hex[:16]}",
            org_id=org.id if org is not None else None,
            project_id=project.id,
            user_id=user.id,
            service_key_id=None,
            reviewer_persona="codex-reviewer",
            is_active=True,
            created_by_user_id=user.id,
            created_at=datetime.now(timezone.utc),
        )
    )
    await db.flush()


async def _run_step(db: AsyncSession, queue: WorkQueue) -> dict:
    result = await wq_engine.run_work_queue_step(
        db, queue=queue, wake_source="manual", wake_ref=None, max_tickets=None
    )
    await db.flush()
    assert result.status == "ok", result.to_dict()
    assert len(result.directives) == 1
    return result.directives[0]


async def _refetch_item(db: AsyncSession, item_id: str) -> WorkQueueItem:
    return (
        await db.execute(select(WorkQueueItem).where(WorkQueueItem.id == item_id))
    ).scalar_one()


async def _backdate_runs(db: AsyncSession, queue: WorkQueue, seconds: int = 600) -> None:
    """Rewind a queue's run history so a subsequent wake clears the 120s cadence
    gate (multiple settles in one test run happen within the same wall-clock
    second)."""
    past = datetime.now(timezone.utc) - timedelta(seconds=seconds)
    for run in (
        await db.execute(
            select(WorkQueueRun).where(WorkQueueRun.work_queue_id == queue.id)
        )
    ).scalars():
        run.created_at = past
    await db.commit()


# ── verdict binding (review path) ──


async def test_review_verdict_stores_reviewed_commit(db_session: AsyncSession):
    """A review_until_clean settle with verdict_content + reviewed_commit stores
    the commit on the server-created trusted verdict row."""
    org = await _make_org(db_session)
    user = await _make_user(db_session, "reviewer")
    project = await _make_project(db_session, user, org)
    ticket = await _make_ticket(db_session, project)
    queue = await _make_queue(db_session, project, user, mode="review_until_clean")
    await _make_item(db_session, queue, ticket)
    # A prior implementer comment makes this the reviewer's turn.
    await _seed_comment(db_session, ticket, user, content="Implementer progress.")
    await _register_reviewer(db_session, project=project, org=org, user=user)
    await db_session.commit()

    d = await _run_step(db_session, queue)
    assert d["intent"] == "post_review"
    await db_session.commit()

    comp = await wq_engine.complete_work_queue_step(
        db_session, queue=queue, item_id=d["item_id"],
        directive_id=d["directive_id"], ticket_id=ticket.id,
        outcome="posted_review", comment_id=None,
        agent_run_id=None, failed=False,
        verdict_content=CLEAN,
        reviewed_commit=HEAD,
        actor_user_id=user.id, actor_org_id=org.id,
        actor_service_key_id=None, actor_type="user",
        service_key_name=None,
    )
    await db_session.commit()
    assert comp.item_terminal is True
    assert comp.item_status == "done"

    verdict = (
        await db_session.execute(
            select(TicketComment).where(
                TicketComment.ticket_id == ticket.id,
                TicketComment.author_persona == "codex-reviewer",
            )
        )
    ).scalars().one()
    assert verdict.verdict_trusted is True
    assert verdict.reviewed_commit == HEAD


@pytest.mark.parametrize("bad_sha", [BAD_SHA, SHORT_SHA])
async def test_review_verdict_rejects_malformed_reviewed_commit(
    db_session: AsyncSession, bad_sha: str
):
    """A non-full-commit-id reviewed_commit is rejected 422 BEFORE any verdict
    write — both a non-hex value and a short (formerly accepted) hex prefix."""
    org = await _make_org(db_session)
    user = await _make_user(db_session, "reviewer")
    project = await _make_project(db_session, user, org)
    ticket = await _make_ticket(db_session, project)
    queue = await _make_queue(db_session, project, user, mode="review_until_clean")
    await _make_item(db_session, queue, ticket)
    await _seed_comment(db_session, ticket, user, content="Implementer progress.")
    await _register_reviewer(db_session, project=project, org=org, user=user)
    await db_session.commit()

    d = await _run_step(db_session, queue)
    await db_session.commit()

    with pytest.raises(wq_engine.StepEngineError) as exc_info:
        await wq_engine.complete_work_queue_step(
            db_session, queue=queue, item_id=d["item_id"],
            directive_id=d["directive_id"], ticket_id=ticket.id,
            outcome="posted_review", comment_id=None,
            agent_run_id=None, failed=False,
            verdict_content=CLEAN,
            reviewed_commit=bad_sha,
            actor_user_id=user.id, actor_org_id=org.id,
            actor_service_key_id=None, actor_type="user",
            service_key_name=None,
        )
    assert exc_info.value.code == "reviewed_commit_invalid"
    assert exc_info.value.http_status == 422

    verdicts = (
        await db_session.execute(
            select(TicketComment).where(
                TicketComment.ticket_id == ticket.id,
                TicketComment.author_persona == "codex-reviewer",
            )
        )
    ).scalars().all()
    assert len(verdicts) == 0


# ── G9: implement_until_done strict close ──


async def test_implement_item_does_not_close_on_claimed_outcome(
    db_session: AsyncSession,
):
    """The implementer's own `outcome='completed_ticket'` is not a close: the
    item parks waiting for a trusted strict clean."""
    owner = await _make_user(db_session, "owner")
    project = await _make_project(db_session, owner)
    ticket = await _make_ticket(db_session, project)
    queue = await _make_queue(db_session, project, owner, mode="implement_until_done")
    item = await _make_item(db_session, queue, ticket)
    await db_session.commit()

    d = await _run_step(db_session, queue)
    assert d["intent"] == "implement"
    await db_session.commit()

    comment = await _seed_comment(
        db_session, ticket, owner, content="Implemented the change."
    )
    await db_session.commit()

    comp = await wq_engine.complete_work_queue_step(
        db_session, queue=queue, item_id=d["item_id"],
        directive_id=d["directive_id"], ticket_id=ticket.id,
        outcome="completed_ticket", comment_id=comment.id,
        agent_run_id=None, failed=False,
        actor_user_id=owner.id, actor_org_id=None,
        actor_service_key_id=None, actor_type="user",
        service_key_name=None,
    )
    await db_session.commit()
    assert comp.status == "settled"
    assert comp.item_terminal is False
    assert comp.item_status == "waiting"

    final = await _refetch_item(db_session, item.id)
    assert final.item_status == "waiting"


async def test_implement_unknown_implementer_fails_closed(
    db_session: AsyncSession,
):
    """A trusted strict clean exists, but the settle carries no implementer
    identity → fail closed (item stays waiting)."""
    owner = await _make_user(db_session, "owner")
    reviewer = await _make_user(db_session, "reviewer")
    project = await _make_project(db_session, owner)
    ticket = await _make_ticket(db_session, project)
    queue = await _make_queue(db_session, project, owner, mode="implement_until_done")
    item = await _make_item(db_session, queue, ticket)
    await db_session.commit()

    d = await _run_step(db_session, queue)
    await db_session.commit()

    comment = await _seed_comment(
        db_session, ticket, owner, content="Implemented the change."
    )
    # Trusted strict clean from a DIFFERENT identity.
    await _seed_comment(
        db_session, ticket, reviewer, content=CLEAN, verdict_trusted=True,
        created_at=datetime.now(timezone.utc) + timedelta(minutes=5),
    )
    await db_session.commit()

    # No actor identity on the settle → implementer identity unknown.
    comp = await wq_engine.complete_work_queue_step(
        db_session, queue=queue, item_id=d["item_id"],
        directive_id=d["directive_id"], ticket_id=ticket.id,
        outcome="posted_progress", comment_id=comment.id,
        agent_run_id=None, failed=False,
        actor_user_id=None, actor_org_id=None,
        actor_service_key_id=None, actor_type=None,
        service_key_name=None,
    )
    await db_session.commit()
    assert comp.status == "settled"
    assert comp.item_terminal is False
    assert comp.item_status == "waiting"

    final = await _refetch_item(db_session, item.id)
    assert final.item_status == "waiting"


async def test_implement_closes_on_trusted_clean_from_other_identity(
    db_session: AsyncSession,
):
    """Positive path: a trusted strict clean from a DIFFERENT identity that
    postdates the implementer's writeback closes the item."""
    owner = await _make_user(db_session, "owner")
    reviewer = await _make_user(db_session, "reviewer")
    project = await _make_project(db_session, owner)
    ticket = await _make_ticket(db_session, project)
    queue = await _make_queue(db_session, project, owner, mode="implement_until_done")
    item = await _make_item(db_session, queue, ticket)
    await db_session.commit()

    d = await _run_step(db_session, queue)
    await db_session.commit()

    # Implementer writeback (older), then the trusted clean (newer).
    impl_at = datetime.now(timezone.utc) - timedelta(minutes=10)
    comment = await _seed_comment(
        db_session, ticket, owner, content="Implemented the change.",
        created_at=impl_at,
    )
    await _seed_comment(
        db_session, ticket, reviewer, content=CLEAN, verdict_trusted=True,
        reviewed_commit=HEAD,
        created_at=datetime.now(timezone.utc),
    )
    await db_session.commit()

    comp = await wq_engine.complete_work_queue_step(
        db_session, queue=queue, item_id=d["item_id"],
        directive_id=d["directive_id"], ticket_id=ticket.id,
        outcome="posted_progress", comment_id=comment.id,
        agent_run_id=None, failed=False,
        actor_user_id=owner.id, actor_org_id=None,
        actor_service_key_id=None, actor_type="user",
        service_key_name=None,
    )
    await db_session.commit()
    assert comp.status == "settled"
    assert comp.item_terminal is True
    assert comp.item_status == "done"

    final = await _refetch_item(db_session, item.id)
    assert final.item_status == "done"
    assert final.implementer_user_id == owner.id
    assert final.closed_by_user_id == reviewer.id
    assert final.auto_close_review_kind == "human"


async def test_implement_out_of_order_rounds_do_not_close(db_session: AsyncSession):
    """A trusted R9 VERIFIED-CLEAN posts, then a LATER R2 CHANGES_REQUESTED
    with a HIGH finding. Chronology decides the latest verdict, so the item
    must NOT close on the stale (earlier) clean round."""
    owner = await _make_user(db_session, "owner")
    reviewer = await _make_user(db_session, "reviewer")
    project = await _make_project(db_session, owner)
    ticket = await _make_ticket(db_session, project)
    queue = await _make_queue(db_session, project, owner, mode="implement_until_done")
    item = await _make_item(db_session, queue, ticket)
    await db_session.commit()

    d = await _run_step(db_session, queue)
    await db_session.commit()

    base = datetime.now(timezone.utc) - timedelta(minutes=20)
    comment = await _seed_comment(
        db_session, ticket, owner, content="Implemented the change.",
        created_at=base,
    )
    await _seed_comment(
        db_session, ticket, reviewer,
        content="Codex R9 review on tk_x: VERIFIED-CLEAN\n\nFindings: none.",
        verdict_trusted=True, reviewed_commit=HEAD,
        created_at=base + timedelta(minutes=5),
    )
    await _seed_comment(
        db_session, ticket, reviewer,
        content=(
            "Codex R2 review on tk_x: CHANGES REQUESTED\n\n"
            "Findings:\n\n • HIGH — residual crash risk in src/foo.py:10\n"
        ),
        verdict_trusted=True,
        created_at=base + timedelta(minutes=10),
    )
    await db_session.commit()

    comp = await wq_engine.complete_work_queue_step(
        db_session, queue=queue, item_id=d["item_id"],
        directive_id=d["directive_id"], ticket_id=ticket.id,
        outcome="posted_progress", comment_id=comment.id,
        agent_run_id=None, failed=False,
        actor_user_id=owner.id, actor_org_id=None,
        actor_service_key_id=None, actor_type="user",
        service_key_name=None,
    )
    await db_session.commit()
    assert comp.status == "settled"
    assert comp.item_terminal is False
    assert comp.item_status == "waiting"

    final = await _refetch_item(db_session, item.id)
    assert final.item_status == "waiting"


async def test_later_settle_does_not_overwrite_human_implementer(
    db_session: AsyncSession,
):
    """Human A implements, then human B settles later — the implementer
    identity must stay A so A's own later trusted clean is self-review and
    does NOT close the item."""
    owner = await _make_user(db_session, "owner")
    other = await _make_user(db_session, "other")
    project = await _make_project(db_session, owner)
    ticket = await _make_ticket(db_session, project)
    queue = await _make_queue(db_session, project, owner, mode="implement_until_done")
    item = await _make_item(db_session, queue, ticket)
    await db_session.commit()

    # ── A implements (first settle) ──
    d1 = await _run_step(db_session, queue)
    await db_session.commit()
    a_comment = await _seed_comment(
        db_session, ticket, owner, content="Implemented the change.",
        created_at=datetime.now(timezone.utc) - timedelta(minutes=30),
    )
    await db_session.commit()
    comp1 = await wq_engine.complete_work_queue_step(
        db_session, queue=queue, item_id=d1["item_id"],
        directive_id=d1["directive_id"], ticket_id=ticket.id,
        outcome="posted_progress", comment_id=a_comment.id,
        agent_run_id=None, failed=False,
        actor_user_id=owner.id, actor_org_id=None,
        actor_service_key_id=None, actor_type="user",
        service_key_name=None,
    )
    await db_session.commit()
    assert comp1.item_terminal is False

    # ── B settles later (must NOT replace A) ──
    await _backdate_runs(db_session, queue)
    d2 = await _run_step(db_session, queue)
    await db_session.commit()
    b_comment = await _seed_comment(
        db_session, ticket, other, content="Touched the ticket.",
        created_at=datetime.now(timezone.utc) - timedelta(minutes=20),
    )
    await db_session.commit()
    comp2 = await wq_engine.complete_work_queue_step(
        db_session, queue=queue, item_id=d2["item_id"],
        directive_id=d2["directive_id"], ticket_id=ticket.id,
        outcome="posted_progress", comment_id=b_comment.id,
        agent_run_id=None, failed=False,
        actor_user_id=other.id, actor_org_id=None,
        actor_service_key_id=None, actor_type="user",
        service_key_name=None,
    )
    await db_session.commit()
    assert comp2.item_terminal is False

    # ── A posts a trusted clean and settles again — self-review, no close ──
    clean = await _seed_comment(
        db_session, ticket, owner, content=CLEAN, verdict_trusted=True,
        reviewed_commit=HEAD,
        created_at=datetime.now(timezone.utc),
    )
    await db_session.commit()

    await _backdate_runs(db_session, queue)
    d3 = await _run_step(db_session, queue)
    await db_session.commit()
    comp3 = await wq_engine.complete_work_queue_step(
        db_session, queue=queue, item_id=d3["item_id"],
        directive_id=d3["directive_id"], ticket_id=ticket.id,
        outcome="posted_progress", comment_id=clean.id,
        agent_run_id=None, failed=False,
        actor_user_id=owner.id, actor_org_id=None,
        actor_service_key_id=None, actor_type="user",
        service_key_name=None,
    )
    await db_session.commit()
    assert comp3.item_terminal is False
    assert comp3.item_status == "waiting"

    final = await _refetch_item(db_session, item.id)
    assert final.implementer_user_id == owner.id
    assert final.item_status == "waiting"


async def test_implement_self_review_does_not_close(db_session: AsyncSession):
    """Reviewer identity == implementer identity → self-review, no close."""
    owner = await _make_user(db_session, "owner")
    project = await _make_project(db_session, owner)
    ticket = await _make_ticket(db_session, project)
    queue = await _make_queue(db_session, project, owner, mode="implement_until_done")
    item = await _make_item(db_session, queue, ticket)
    await db_session.commit()

    d = await _run_step(db_session, queue)
    await db_session.commit()

    impl_at = datetime.now(timezone.utc) - timedelta(minutes=10)
    comment = await _seed_comment(
        db_session, ticket, owner, content="Implemented the change.",
        created_at=impl_at,
    )
    # Same identity posts the trusted clean.
    await _seed_comment(
        db_session, ticket, owner, content=CLEAN, verdict_trusted=True,
        reviewed_commit=HEAD,
        created_at=datetime.now(timezone.utc),
    )
    await db_session.commit()

    comp = await wq_engine.complete_work_queue_step(
        db_session, queue=queue, item_id=d["item_id"],
        directive_id=d["directive_id"], ticket_id=ticket.id,
        outcome="posted_progress", comment_id=comment.id,
        agent_run_id=None, failed=False,
        actor_user_id=owner.id, actor_org_id=None,
        actor_service_key_id=None, actor_type="user",
        service_key_name=None,
    )
    await db_session.commit()
    assert comp.item_terminal is False
    assert comp.item_status == "waiting"

    final = await _refetch_item(db_session, item.id)
    assert final.item_status == "waiting"


async def test_implement_no_close_when_reviewer_and_implementer_keys_share_minter(
    db_session: AsyncSession,
):
    """Two DIFFERENT service keys minted by the same user are the same
    identity (a key IS its minter): a trusted clean from one over work the
    other did must NOT auto-close."""
    owner = await _make_user(db_session, "owner")
    minter = await _make_user(db_session, "minter")
    project = await _make_project(db_session, owner)
    ticket = await _make_ticket(db_session, project)
    queue = await _make_queue(db_session, project, owner, mode="implement_until_done")
    item = await _make_item(db_session, queue, ticket)
    impl_svc = await _make_service_key(db_session, minter)
    reviewer_svc = await _make_service_key(db_session, minter)
    await db_session.commit()

    d = await _run_step(db_session, queue)
    await db_session.commit()

    impl_at = datetime.now(timezone.utc) - timedelta(minutes=10)
    comment = await _seed_comment(
        db_session, ticket, minter, content="Implemented the change.",
        service_key_id=impl_svc.id, created_at=impl_at,
    )
    await _seed_comment(
        db_session, ticket, minter, content=CLEAN, verdict_trusted=True,
        reviewed_commit=HEAD, service_key_id=reviewer_svc.id,
        created_at=datetime.now(timezone.utc),
    )
    await db_session.commit()

    comp = await wq_engine.complete_work_queue_step(
        db_session, queue=queue, item_id=d["item_id"],
        directive_id=d["directive_id"], ticket_id=ticket.id,
        outcome="posted_progress", comment_id=comment.id,
        agent_run_id=None, failed=False,
        actor_user_id=minter.id, actor_org_id=None,
        actor_service_key_id=impl_svc.id, actor_type="service_key",
        service_key_name="impl-svc",
    )
    await db_session.commit()
    assert comp.item_terminal is False
    assert comp.item_status == "waiting"

    final = await _refetch_item(db_session, item.id)
    assert final.implementer_service_key_id == impl_svc.id
    assert final.item_status == "waiting"
    assert final.closed_by_service_key_id is None


async def test_implement_closes_when_reviewer_and_implementer_keys_have_distinct_minters(
    db_session: AsyncSession,
):
    """Two service keys minted by DIFFERENT users are independent identities,
    so a trusted clean from one closes work the other did."""
    owner = await _make_user(db_session, "owner")
    reviewer = await _make_user(db_session, "reviewer")
    implementer = await _make_user(db_session, "implementer")
    project = await _make_project(db_session, owner)
    ticket = await _make_ticket(db_session, project)
    queue = await _make_queue(db_session, project, owner, mode="implement_until_done")
    item = await _make_item(db_session, queue, ticket)
    impl_svc = await _make_service_key(db_session, implementer)
    reviewer_svc = await _make_service_key(db_session, reviewer)
    await db_session.commit()

    d = await _run_step(db_session, queue)
    await db_session.commit()

    impl_at = datetime.now(timezone.utc) - timedelta(minutes=10)
    comment = await _seed_comment(
        db_session, ticket, implementer, content="Implemented the change.",
        service_key_id=impl_svc.id, created_at=impl_at,
    )
    await _seed_comment(
        db_session, ticket, reviewer, content=CLEAN, verdict_trusted=True,
        reviewed_commit=HEAD, service_key_id=reviewer_svc.id,
        created_at=datetime.now(timezone.utc),
    )
    await db_session.commit()

    comp = await wq_engine.complete_work_queue_step(
        db_session, queue=queue, item_id=d["item_id"],
        directive_id=d["directive_id"], ticket_id=ticket.id,
        outcome="posted_progress", comment_id=comment.id,
        agent_run_id=None, failed=False,
        actor_user_id=implementer.id, actor_org_id=None,
        actor_service_key_id=impl_svc.id, actor_type="service_key",
        service_key_name="impl-svc",
    )
    await db_session.commit()
    assert comp.item_terminal is True
    assert comp.item_status == "done"

    final = await _refetch_item(db_session, item.id)
    assert final.item_status == "done"
    assert final.closed_by_service_key_id == reviewer_svc.id
    assert final.auto_close_review_kind == "resident_trusted"


async def test_implement_no_close_when_implementer_key_minter_unresolvable(
    db_session: AsyncSession,
):
    """An implementer service-key id with no ApiKey row can't have its minter
    resolved — fail closed rather than treat an anonymous key as independent."""
    owner = await _make_user(db_session, "owner")
    reviewer = await _make_user(db_session, "reviewer")
    project = await _make_project(db_session, owner)
    ticket = await _make_ticket(db_session, project)
    queue = await _make_queue(db_session, project, owner, mode="implement_until_done")
    item = await _make_item(db_session, queue, ticket)
    reviewer_svc = await _make_service_key(db_session, reviewer)
    await db_session.commit()

    d = await _run_step(db_session, queue)
    await db_session.commit()

    impl_at = datetime.now(timezone.utc) - timedelta(minutes=10)
    comment = await _seed_comment(
        db_session, ticket, owner, content="Implemented the change.",
        service_key_id="sk_missing", created_at=impl_at,
    )
    await _seed_comment(
        db_session, ticket, reviewer, content=CLEAN, verdict_trusted=True,
        reviewed_commit=HEAD, service_key_id=reviewer_svc.id,
        created_at=datetime.now(timezone.utc),
    )
    await db_session.commit()

    comp = await wq_engine.complete_work_queue_step(
        db_session, queue=queue, item_id=d["item_id"],
        directive_id=d["directive_id"], ticket_id=ticket.id,
        outcome="posted_progress", comment_id=comment.id,
        agent_run_id=None, failed=False,
        actor_user_id=owner.id, actor_org_id=None,
        actor_service_key_id="sk_missing", actor_type="service_key",
        service_key_name="missing",
    )
    await db_session.commit()
    assert comp.item_terminal is False
    assert comp.item_status == "waiting"

    final = await _refetch_item(db_session, item.id)
    assert final.implementer_service_key_id == "sk_missing"
    assert final.item_status == "waiting"
    assert final.closed_by_service_key_id is None


async def test_implement_no_close_when_admin_rotated_the_implementer_key(
    db_session: AsyncSession,
):
    """An admin who rotated a member's key is part of that key's identity, so
    their own trusted clean over that key's work must not auto-close."""
    owner = await _make_user(db_session, "owner")
    member = await _make_user(db_session, "member")
    admin = await _make_user(db_session, "admin")
    project = await _make_project(db_session, owner)
    ticket = await _make_ticket(db_session, project)
    queue = await _make_queue(db_session, project, owner, mode="implement_until_done")
    item = await _make_item(db_session, queue, ticket)
    old_svc = await _make_service_key(db_session, member)
    impl_svc = await _make_service_key(db_session, member)
    impl_svc.created_by_user_id = admin.id
    impl_svc.rotated_from_key_id = old_svc.id
    reviewer_svc = await _make_service_key(db_session, admin)
    await db_session.commit()

    d = await _run_step(db_session, queue)
    await db_session.commit()

    impl_at = datetime.now(timezone.utc) - timedelta(minutes=10)
    comment = await _seed_comment(
        db_session, ticket, member, content="Implemented the change.",
        service_key_id=impl_svc.id, created_at=impl_at,
    )
    await _seed_comment(
        db_session, ticket, admin, content=CLEAN, verdict_trusted=True,
        reviewed_commit=HEAD, service_key_id=reviewer_svc.id,
        created_at=datetime.now(timezone.utc),
    )
    await db_session.commit()

    comp = await wq_engine.complete_work_queue_step(
        db_session, queue=queue, item_id=d["item_id"],
        directive_id=d["directive_id"], ticket_id=ticket.id,
        outcome="posted_progress", comment_id=comment.id,
        agent_run_id=None, failed=False,
        actor_user_id=member.id, actor_org_id=None,
        actor_service_key_id=impl_svc.id, actor_type="service_key",
        service_key_name="impl-svc",
    )
    await db_session.commit()
    assert comp.item_terminal is False
    final = await _refetch_item(db_session, item.id)
    assert final.item_status == "waiting"


async def test_implement_no_close_when_human_reviews_work_done_with_their_own_key(
    db_session: AsyncSession,
):
    """A human who minted the implementer key is that key's identity."""
    owner = await _make_user(db_session, "owner")
    minter = await _make_user(db_session, "minter")
    project = await _make_project(db_session, owner)
    ticket = await _make_ticket(db_session, project)
    queue = await _make_queue(db_session, project, owner, mode="implement_until_done")
    item = await _make_item(db_session, queue, ticket)
    impl_svc = await _make_service_key(db_session, minter)
    await db_session.commit()

    d = await _run_step(db_session, queue)
    await db_session.commit()

    comment = await _seed_comment(
        db_session, ticket, minter, content="Implemented the change.",
        service_key_id=impl_svc.id, created_at=datetime.now(timezone.utc) - timedelta(minutes=10),
    )
    await _seed_comment(
        db_session, ticket, minter, content=CLEAN, verdict_trusted=True,
        reviewed_commit=HEAD, created_at=datetime.now(timezone.utc),
    )
    await db_session.commit()

    comp = await wq_engine.complete_work_queue_step(
        db_session, queue=queue, item_id=d["item_id"],
        directive_id=d["directive_id"], ticket_id=ticket.id,
        outcome="posted_progress", comment_id=comment.id,
        agent_run_id=None, failed=False,
        actor_user_id=minter.id, actor_org_id=None,
        actor_service_key_id=impl_svc.id, actor_type="service_key",
        service_key_name="impl-svc",
    )
    await db_session.commit()
    assert comp.item_terminal is False
    final = await _refetch_item(db_session, item.id)
    assert final.item_status == "waiting"


async def test_implement_no_close_when_key_reviews_its_minters_human_work(
    db_session: AsyncSession,
):
    """A reviewer key whose minter implemented as a human is the same identity."""
    owner = await _make_user(db_session, "owner")
    minter = await _make_user(db_session, "minter")
    project = await _make_project(db_session, owner)
    ticket = await _make_ticket(db_session, project)
    queue = await _make_queue(db_session, project, owner, mode="implement_until_done")
    item = await _make_item(db_session, queue, ticket)
    reviewer_svc = await _make_service_key(db_session, minter)
    await db_session.commit()

    d = await _run_step(db_session, queue)
    await db_session.commit()

    comment = await _seed_comment(
        db_session, ticket, minter, content="Implemented the change.",
        service_key_id=None, created_at=datetime.now(timezone.utc) - timedelta(minutes=10),
    )
    await _seed_comment(
        db_session, ticket, owner, content=CLEAN, verdict_trusted=True,
        reviewed_commit=HEAD, service_key_id=reviewer_svc.id,
        created_at=datetime.now(timezone.utc),
    )
    await db_session.commit()

    comp = await wq_engine.complete_work_queue_step(
        db_session, queue=queue, item_id=d["item_id"],
        directive_id=d["directive_id"], ticket_id=ticket.id,
        outcome="posted_progress", comment_id=comment.id,
        agent_run_id=None, failed=False,
        actor_user_id=minter.id, actor_org_id=None,
        actor_service_key_id=None, actor_type="user",
        service_key_name="impl-svc",
    )
    await db_session.commit()
    assert comp.item_terminal is False
    final = await _refetch_item(db_session, item.id)
    assert final.item_status == "waiting"
