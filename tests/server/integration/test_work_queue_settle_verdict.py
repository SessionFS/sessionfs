"""tk_3539f7761e554ed5 — server-created reviewer verdict on the SETTLE path.

The contract gap: the post_review directive asked the reviewer to post the
verdict via `add_ticket_comment` WITHOUT author_persona (server-derived), but
`add_ticket_comment` is a generic comment tool with NO directive/queue context,
so a verdict posted through it lands `verdict_trusted=false` and
`complete_work_queue_step` (correctly) refuses to stop. The contract was
unfulfillable.

The SECURE fix (CEO-decided settle path): the reviewer's verdict flows through
`complete_work_queue_step(verdict_content=...)`. The settle call carries
directive_id → the server knows the queue, its reviewer persona, and the
AUTHENTICATED actor, so it CREATES the verdict comment itself with fully
server-derived provenance:
  - author_persona  ← queue.assigned_persona (NOT caller input)
  - verdict_trusted ← is_registered_trusted_reviewer(AUTHENTICATED actor against
                      trusted_reviewers) — NEVER a caller-supplied persona /
                      assume_persona bundle.

This matrix asserts the security invariant: only the settle path under a
REGISTERED identity mints a trusted verdict; a forged persona / unregistered
actor stays verdict_trusted=false and cannot stop the loop.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

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
)
from sessionfs.server.services import work_queues as wq_engine

R2_CLEAN = """\
Codex R2 review on tk_x: VERIFIED-CLEAN

Findings: none.
"""

R1_CHANGES = """\
Codex R1 review on tk_x: CHANGES REQUESTED

Findings:

 • HIGH — the thing is broken in src/foo.py:10
"""

R1_APPROVED = """\
Codex R1 review on tk_x: APPROVED

Findings: none.
"""


# ── builders ──


async def _make_org(db: AsyncSession) -> Organization:
    suffix = uuid.uuid4().hex[:8]
    org = Organization(
        id=f"org_{uuid.uuid4().hex[:12]}",
        name=f"org-{suffix}",
        slug=f"org-{suffix}",
        created_at=datetime.now(timezone.utc),
    )
    db.add(org)
    await db.flush()
    return org


async def _make_user_with_key(
    db: AsyncSession, tier: str = "team"
) -> User:
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
    await db.flush()
    return user


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
    db: AsyncSession, project: Project, owner: User
) -> WorkQueue:
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


async def _seed_implementer_comment(
    db: AsyncSession, ticket: Ticket, user: User
) -> TicketComment:
    """Seed an untrusted implementer comment so the next wake is the
    reviewer's turn (emits a post_review directive)."""
    c = TicketComment(
        id=f"tc_{uuid.uuid4().hex[:16]}",
        ticket_id=ticket.id,
        author_user_id=user.id,
        author_persona="atlas",
        content="Initial implementation pushed.",
        verdict_trusted=False,
        created_at=datetime.now(timezone.utc),
    )
    db.add(c)
    await db.flush()
    return c


async def _register_reviewer(
    db: AsyncSession,
    *,
    project: Project,
    org: Organization | None,
    user: User,
    persona: str = "codex-reviewer",
) -> TrustedReviewer:
    row = TrustedReviewer(
        id=f"tr_{uuid.uuid4().hex[:16]}",
        org_id=None,
        project_id=project.id,
        user_id=user.id,
        service_key_id=None,
        reviewer_persona=persona,
        is_active=True,
        created_by_user_id=user.id,
        created_at=datetime.now(timezone.utc),
    )
    db.add(row)
    await db.flush()
    return row


async def _refetch_item(
    db: AsyncSession, item_id: str
) -> WorkQueueItem:
    return (
        await db.execute(
            select(WorkQueueItem).where(WorkQueueItem.id == item_id)
        )
    ).scalar_one()


async def _emit_directive(
    db: AsyncSession, queue: WorkQueue
) -> dict:
    """Run one wake and return the single post_review directive."""
    result = await wq_engine.run_work_queue_step(
        db, queue=queue, wake_source="manual", wake_ref=None,
        max_tickets=None,
    )
    await db.flush()
    assert result.status == "ok", result.to_dict()
    assert len(result.directives) == 1
    d = result.directives[0]
    assert d["intent"] == "post_review"
    return d


async def _setup(db: AsyncSession, *, registered: bool):
    org = await _make_org(db)
    user = await _make_user_with_key(db)
    project = await _make_project(db, user, org)
    ticket = await _make_ticket(db, project)
    queue = await _make_queue(db, project, user)
    item = await _make_item(db, queue, ticket)
    await _seed_implementer_comment(db, ticket, user)
    if registered:
        await _register_reviewer(db, project=project, org=org, user=user)
    await db.commit()
    return org, user, project, ticket, queue, item


# ── (1) settle-path happy: registered reviewer + strict clean → done ──


async def test_settle_verdict_registered_reviewer_strict_clean_stops(
    db_session: AsyncSession,
):
    """A registered trusted_reviewer settles a post_review directive with a
    strict literal VERIFIED-CLEAN. The SERVER creates the verdict comment with
    author_persona from queue config + verdict_trusted=true (derived from the
    authenticated identity) → review_until_clean stops (item done)."""
    org, user, project, ticket, queue, item = await _setup(
        db_session, registered=True
    )
    d = await _emit_directive(db_session, queue)
    await db_session.commit()

    comp = await wq_engine.complete_work_queue_step(
        db_session, queue=queue, item_id=d["item_id"],
        directive_id=d["directive_id"], ticket_id=ticket.id,
        outcome="posted_review", comment_id=None,
        agent_run_id=None, failed=False,
        verdict_content=R2_CLEAN,
        actor_user_id=user.id, actor_org_id=org.id,
        actor_service_key_id=None, actor_type="user",
        service_key_name=None,
    )
    await db_session.commit()

    assert comp.status == "settled"
    assert comp.item_terminal is True
    assert comp.item_status == "done"

    # The server created exactly one verdict comment with the right provenance.
    verdicts = (
        await db_session.execute(
            select(TicketComment).where(
                TicketComment.ticket_id == ticket.id,
                TicketComment.author_persona == "codex-reviewer",
            )
        )
    ).scalars().all()
    assert len(verdicts) == 1
    assert verdicts[0].verdict_trusted is True
    assert verdicts[0].content == R2_CLEAN

    final = await _refetch_item(db_session, item.id)
    assert final.item_status == "done"


# ── (2) untrusted actor: same verdict by unregistered actor → not trusted ──


async def test_settle_verdict_unregistered_actor_not_trusted_no_stop(
    db_session: AsyncSession,
):
    """The SAME strict-clean verdict completed by a NON-registered actor →
    the server-created comment is verdict_trusted=false → the loop does NOT
    stop (item parks waiting)."""
    org, user, project, ticket, queue, item = await _setup(
        db_session, registered=False
    )
    d = await _emit_directive(db_session, queue)
    await db_session.commit()

    comp = await wq_engine.complete_work_queue_step(
        db_session, queue=queue, item_id=d["item_id"],
        directive_id=d["directive_id"], ticket_id=ticket.id,
        outcome="posted_review", comment_id=None,
        agent_run_id=None, failed=False,
        verdict_content=R2_CLEAN,
        actor_user_id=user.id, actor_org_id=org.id,
        actor_service_key_id=None, actor_type="user",
        service_key_name=None,
    )
    await db_session.commit()

    assert comp.status == "settled"
    assert comp.item_terminal is False
    assert comp.item_status == "waiting"

    verdicts = (
        await db_session.execute(
            select(TicketComment).where(
                TicketComment.ticket_id == ticket.id,
                TicketComment.author_persona == "codex-reviewer",
            )
        )
    ).scalars().all()
    assert len(verdicts) == 1
    assert verdicts[0].verdict_trusted is False  # unregistered → untrusted

    final = await _refetch_item(db_session, item.id)
    assert final.item_status != "done"


# ── (3) no-spoof: assume_persona-style caller persona never elevates trust ──


async def test_settle_verdict_author_persona_always_from_queue_not_caller(
    db_session: AsyncSession,
):
    """The settle path NEVER reads a caller-supplied persona for trust. The
    server stamps author_persona from queue.assigned_persona and derives
    verdict_trusted from the AUTHENTICATED identity only. An unregistered
    actor — even if it locally 'assumed' codex-reviewer — produces a
    verdict_trusted=false comment that cannot stop the loop. (The settle API
    has no author_persona parameter at all; the only trusted path is being a
    registered identity.)"""
    org, user, project, ticket, queue, item = await _setup(
        db_session, registered=False
    )
    d = await _emit_directive(db_session, queue)
    await db_session.commit()

    comp = await wq_engine.complete_work_queue_step(
        db_session, queue=queue, item_id=d["item_id"],
        directive_id=d["directive_id"], ticket_id=ticket.id,
        outcome="done", comment_id=None,  # caller asserts done — only a hint
        agent_run_id=None, failed=False,
        verdict_content=R2_CLEAN,
        actor_user_id=user.id, actor_org_id=org.id,
        actor_service_key_id=None, actor_type="user",
        service_key_name=None,
    )
    await db_session.commit()

    assert comp.item_terminal is False
    verdicts = (
        await db_session.execute(
            select(TicketComment).where(
                TicketComment.ticket_id == ticket.id,
                TicketComment.author_persona == "codex-reviewer",
            )
        )
    ).scalars().all()
    # author_persona is queue-derived; trust is false (unregistered identity).
    assert all(v.author_persona == "codex-reviewer" for v in verdicts)
    assert all(v.verdict_trusted is False for v in verdicts)


# ── (4) idempotent: re-send with same directive_id does not double-post ──


async def test_settle_verdict_idempotent_no_double_post(
    db_session: AsyncSession,
):
    """Re-sending complete with the SAME directive_id returns the prior
    outcome (idempotent_replay) and does NOT create a second verdict comment."""
    org, user, project, ticket, queue, item = await _setup(
        db_session, registered=True
    )
    d = await _emit_directive(db_session, queue)
    await db_session.commit()

    comp1 = await wq_engine.complete_work_queue_step(
        db_session, queue=queue, item_id=d["item_id"],
        directive_id=d["directive_id"], ticket_id=ticket.id,
        outcome="posted_review", comment_id=None,
        agent_run_id=None, failed=False,
        verdict_content=R2_CLEAN,
        actor_user_id=user.id, actor_org_id=org.id,
        actor_service_key_id=None, actor_type="user",
        service_key_name=None,
    )
    await db_session.commit()
    assert comp1.status == "settled"

    comp2 = await wq_engine.complete_work_queue_step(
        db_session, queue=queue, item_id=d["item_id"],
        directive_id=d["directive_id"], ticket_id=ticket.id,
        outcome="posted_review", comment_id=None,
        agent_run_id=None, failed=False,
        verdict_content=R2_CLEAN,
        actor_user_id=user.id, actor_org_id=org.id,
        actor_service_key_id=None, actor_type="user",
        service_key_name=None,
    )
    await db_session.commit()
    assert comp2.status == "idempotent_replay"

    verdicts = (
        await db_session.execute(
            select(TicketComment).where(
                TicketComment.ticket_id == ticket.id,
                TicketComment.author_persona == "codex-reviewer",
            )
        )
    ).scalars().all()
    assert len(verdicts) == 1  # NOT double-posted


# ── (5) alias-negative: trusted APPROVED via settle path does NOT auto-stop ──


async def test_settle_verdict_trusted_alias_does_not_auto_stop(
    db_session: AsyncSession,
):
    """A TRUSTED 'APPROVED' verdict delivered through the settle path is the
    server-created comment with verdict_trusted=true, but it is NOT the strict
    literal VERIFIED-CLEAN → the server re-derive does NOT close the item."""
    org, user, project, ticket, queue, item = await _setup(
        db_session, registered=True
    )
    d = await _emit_directive(db_session, queue)
    await db_session.commit()

    comp = await wq_engine.complete_work_queue_step(
        db_session, queue=queue, item_id=d["item_id"],
        directive_id=d["directive_id"], ticket_id=ticket.id,
        outcome="posted_review", comment_id=None,
        agent_run_id=None, failed=False,
        verdict_content=R1_APPROVED,
        actor_user_id=user.id, actor_org_id=org.id,
        actor_service_key_id=None, actor_type="user",
        service_key_name=None,
    )
    await db_session.commit()

    assert comp.item_terminal is False
    assert comp.item_status == "waiting"

    # The verdict IS trusted (registered reviewer) — it just isn't strict-clean.
    verdict = (
        await db_session.execute(
            select(TicketComment).where(
                TicketComment.ticket_id == ticket.id,
                TicketComment.author_persona == "codex-reviewer",
            )
        )
    ).scalars().one()
    assert verdict.verdict_trusted is True

    final = await _refetch_item(db_session, item.id)
    assert final.item_status != "done"
