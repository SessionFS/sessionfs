"""Agent work-queue services.

tk_529a64620db846f5 (WQ-P1) — the correctness primitive for autonomous
agent work queues (design tk_c2ed6093acde4d55,
docs/design/agent-work-queues.md §5/§11 R1). WQ-P1 shipped ONLY the
atomic-claim helper.

tk_3de50bf7bb73418b (WQ-P3) — the generic STEP ENGINE + the load-bearing
safety envelope live here too, so routes stay thin: `run_work_queue_step`
(the stateless heartbeat — cadence gate → open-lease re-emit → fresh claim
→ bounded directive) and `complete_work_queue_step` (the single commit
point — idempotent settle, validate-writeback-landed, advance the ACKED
cursor, backoff/attempts/failed). `implement_until_done` + `triage` modes
ship; `review_until_clean` returns a clear not-available response (WQ-P4
needs the verdict_trusted stop oracle — already landed as
tk_d42170b4670f4448's verdict_trusted column, but the stop-oracle wiring +
trusted-provenance directive emission is WQ-P4's scope).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server.db.models import (
    Ticket,
    TicketComment,
    WorkQueue,
    WorkQueueItem,
    WorkQueueRun,
)

# ── Step-engine constants (design §9.1 safety envelope) ──

# Cadence floor / default (a queue may set a higher cadence; never lower).
_CADENCE_FLOOR_SECONDS = 120
_CADENCE_DEFAULT_SECONDS = 300

# Backoff curve on no-progress / reported failure: 2m → 5m → 15m → 60m (cap).
# Indexed by the item's attempt count AFTER the increment (1-based clamps to
# the last entry).
_BACKOFF_SECONDS: tuple[int, ...] = (120, 300, 900, 3600)

# How many comments the bounded comment_delta carries at most (token control,
# design §9 — small delta, NOT the whole thread). expand_hints point the agent
# at list_ticket_comments for the full thread when the delta is insufficient.
_COMMENT_DELTA_LIMIT = 20

# Item statuses that the engine treats as a settled-and-parked wait.
_WAITING_STATUS = "waiting"
_DONE_STATUS = "done"
_FAILED_STATUS = "failed"

# expand_hints — the on-demand context menu (NOT a payload). The agent pulls
# full context only when the bounded directive is insufficient.
_EXPAND_HINTS: list[str] = [
    "get_ticket",
    "get_context_section",
    "get_session_summary",
    "list_ticket_comments",
]


def _new_directive_id() -> str:
    return f"dir_{uuid.uuid4().hex[:16]}"


def _new_run_id() -> str:
    return f"wqr_{uuid.uuid4().hex[:16]}"


def _backoff_for_attempt(attempts: int) -> int:
    """Backoff seconds for the given (post-increment) attempt count.

    attempts==1 → 120s, ==2 → 300s, ==3 → 900s, >=4 → 3600s (cap). Always
    returns the cap for anything past the curve so a long-lived item never
    re-fires faster than every 60m.
    """
    idx = max(0, min(attempts, len(_BACKOFF_SECONDS)) - 1)
    return _BACKOFF_SECONDS[idx]


@dataclass
class StepResult:
    """Outcome of one run_work_queue_step wake."""

    status: str  # "ok" | "idle" | "stopped" | "not_available"
    work_queue_run_id: str | None = None
    reason: str | None = None
    directives: list[dict] = field(default_factory=list)
    next_eligible_at: datetime | None = None

    def to_dict(self) -> dict:
        out: dict[str, Any] = {
            "status": self.status,
            "work_queue_run_id": self.work_queue_run_id,
            "directives": self.directives,
        }
        if self.reason is not None:
            out["reason"] = self.reason
        if self.next_eligible_at is not None:
            out["next_eligible_at"] = self.next_eligible_at.isoformat()
        return out


@dataclass
class CompleteResult:
    """Outcome of one complete_work_queue_step settle."""

    status: str  # "settled" | "idempotent_replay" | "rejected"
    item_status: str | None = None
    item_terminal: bool = False
    reason: str | None = None
    attempts: int | None = None
    next_eligible_at: datetime | None = None

    def to_dict(self) -> dict:
        out: dict[str, Any] = {
            "status": self.status,
            "item_status": self.item_status,
            "item_terminal": self.item_terminal,
        }
        if self.reason is not None:
            out["reason"] = self.reason
        if self.attempts is not None:
            out["attempts"] = self.attempts
        if self.next_eligible_at is not None:
            out["next_eligible_at"] = self.next_eligible_at.isoformat()
        return out


class StepEngineError(Exception):
    """Raised for caller-correctable step-engine failures.

    The route maps `code` to an HTTP status (422 for not_available /
    bad directive references, 409 for lease conflicts).
    """

    def __init__(self, code: str, message: str, *, http_status: int = 422):
        super().__init__(message)
        self.code = code
        self.message = message
        self.http_status = http_status


async def claim_work_queue_item(
    db: AsyncSession,
    *,
    item_id: str,
    run_id: str,
) -> bool:
    """Atomically claim one work-queue item for a wake.

    Runs:

        UPDATE work_queue_items
           SET item_status='active',
               open_directive_run_id=:run_id,
               updated_at=now
         WHERE id=:item_id
           AND item_status IN ('pending', 'waiting')
           AND open_directive_id IS NULL
           AND open_directive_run_id IS NULL
           AND (next_eligible_at IS NULL OR next_eligible_at <= now)

    and returns ``True`` iff exactly one row was updated (``rowcount == 1``).

    An item with an OPEN directive lease (``open_directive_id`` /
    ``open_directive_run_id`` set) is NEVER fresh-claimed here, even if its
    backoff has expired: while a directive is open, the crash-replay contract
    (design §4.4/§4.5, R2) requires the step engine to RE-EMIT that same
    directive (preserving ``open_directive_run_id``), not mint a new claim that
    would overwrite the lease. Re-emit is a separate step-engine path (WQ-P2);
    this helper only takes FRESH claims of un-leased items. (Codex R1 on
    tk_529a64620db846f5.)

    The ``rowcount == 1`` status flip is THE correctness guard against double
    workers, and it works identically on PostgreSQL and SQLite: a second
    concurrent wake racing for the same item fails the ``item_status IN
    (...)`` predicate (the first wake already flipped it to ``active``) and
    gets ``rowcount == 0``. On SQLite, single-writer serialization makes this
    fully correct; on PostgreSQL, the row-level write lock on the UPDATE
    serializes the two transactions.

    NOTE: ``SELECT ... FOR UPDATE SKIP LOCKED`` is ONLY a PostgreSQL
    throughput optimization (it avoids lock waits when many wakes contend on
    a large eligible set) — it is NOT relied on for correctness. The atomic
    ``UPDATE ... WHERE`` rowcount==1 status flip is the correctness primitive.

    An item with ``next_eligible_at`` in the future (backoff) or already in a
    non-claimable status (``active`` / ``done`` / ``failed``) is not claimed.

    The caller is responsible for committing the transaction so a concurrent
    wake in a separate session observes the flipped status (mirrors the
    agent-run / ticket atomic-transition pattern in the routes layer).
    """
    now = datetime.now(timezone.utc)
    result = await db.execute(
        update(WorkQueueItem)
        .where(
            WorkQueueItem.id == item_id,
            WorkQueueItem.item_status.in_(("pending", "waiting")),
            # Never fresh-claim an item whose directive lease is still open —
            # that belongs to the step engine's re-emit path (Codex R1).
            WorkQueueItem.open_directive_id.is_(None),
            WorkQueueItem.open_directive_run_id.is_(None),
            (WorkQueueItem.next_eligible_at.is_(None))
            | (WorkQueueItem.next_eligible_at <= now),
        )
        .values(
            item_status="active",
            open_directive_run_id=run_id,
            updated_at=now,
        )
    )
    return result.rowcount == 1


# ─────────────────────────────────────────────────────────────────────────
# WQ-P3 (tk_3de50bf7bb73418b) — the STEP ENGINE.
# ─────────────────────────────────────────────────────────────────────────


def _comment_to_delta(c: TicketComment) -> dict:
    return {
        "id": c.id,
        "author_persona": c.author_persona,
        "content": c.content,
        "created_at": c.created_at.isoformat() if c.created_at else None,
        "verdict_trusted": bool(c.verdict_trusted),
    }


async def _comment_delta(
    db: AsyncSession,
    *,
    ticket_id: str,
    since: datetime | None,
    since_id: str | None,
) -> list[TicketComment]:
    """Bounded comment delta since the ACKED cursor.

    Mirrors routes/tickets.py:list_ticket_comments — explicit
    (created_at, id) > (since, since_id) tuple comparison so SQLite + PG both
    honor the tiebreaker without row-value support. Capped at
    _COMMENT_DELTA_LIMIT (token control, design §9 — small delta, NOT the
    whole thread).
    """
    query = (
        select(TicketComment)
        .where(TicketComment.ticket_id == ticket_id)
        .order_by(TicketComment.created_at, TicketComment.id)
        .limit(_COMMENT_DELTA_LIMIT)
    )
    if since is not None:
        if since.tzinfo is None:
            since = since.replace(tzinfo=timezone.utc)
        if since_id:
            query = query.where(
                or_(
                    TicketComment.created_at > since,
                    and_(
                        TicketComment.created_at == since,
                        TicketComment.id > since_id,
                    ),
                )
            )
        else:
            query = query.where(TicketComment.created_at > since)
    return list((await db.execute(query)).scalars().all())


async def _materialize_auto_adopt(
    db: AsyncSession,
    queue: WorkQueue,
    selector: dict,
    *,
    now: datetime,
) -> int:
    """For filter-selector queues with auto_adopt=true, materialize newly
    matching tickets as work_queue_items BEFORE any action (design §7).

    Capped at queue.max_adopt_per_wake per wake. Idempotent: an upsert-by-
    presence against uq_work_queue_item — a ticket already materialized is
    skipped. Every query is scoped to queue.project_id (design §11 R10) so a
    cross-project ticket can never enter the queue. Returns the count
    materialized this wake.
    """
    if not queue.auto_adopt:
        return 0

    stmt = select(Ticket.id).where(Ticket.project_id == queue.project_id)
    status = selector.get("status")
    if isinstance(status, str) and status:
        stmt = stmt.where(Ticket.status == status)
    kind = selector.get("kind")
    if isinstance(kind, str) and kind:
        stmt = stmt.where(Ticket.kind == kind)
    assigned_to = selector.get("assigned_to")
    if isinstance(assigned_to, str) and assigned_to:
        stmt = stmt.where(Ticket.assigned_to == assigned_to)
    priority = selector.get("priority")
    if isinstance(priority, str) and priority:
        stmt = stmt.where(Ticket.priority == priority)
    # Fetch a bounded candidate set (cap + existing-headroom). Deterministic
    # ordering so the cap selects the oldest unadopted tickets first.
    stmt = stmt.order_by(Ticket.created_at.asc()).limit(
        queue.max_adopt_per_wake + 200
    )
    candidate_ids = [
        tid for tid in (await db.execute(stmt)).scalars().all()
    ]
    if not candidate_ids:
        return 0

    existing = set(
        (
            await db.execute(
                select(WorkQueueItem.ticket_id).where(
                    WorkQueueItem.work_queue_id == queue.id,
                    WorkQueueItem.ticket_id.in_(candidate_ids),
                )
            )
        ).scalars().all()
    )
    materialized = 0
    for tid in candidate_ids:
        if materialized >= queue.max_adopt_per_wake:
            break
        if tid in existing:
            continue
        db.add(
            WorkQueueItem(
                id=f"wqi_{uuid.uuid4().hex[:16]}",
                work_queue_id=queue.id,
                ticket_id=tid,
                item_status="pending",
                attempts=0,
                created_at=now,
                updated_at=now,
            )
        )
        materialized += 1
    if materialized:
        await db.flush()
    return materialized


async def _build_directive(
    db: AsyncSession,
    queue: WorkQueue,
    item: WorkQueueItem,
    ticket: Ticket,
    directive_id: str,
    work_queue_run_id: str,
) -> dict:
    """Assemble the BOUNDED directive returned to the agent (design §4.4).

    Carries only: intent, ticket_id, ticket_lease_epoch, a small comment_delta
    (since the ACKED cursor), expand_hints, directive_id, work_queue_run_id.
    The comment_delta is the small delta, NOT the whole thread (token control).
    """
    delta_rows = await _comment_delta(
        db,
        ticket_id=item.ticket_id,
        since=item.last_acked_comment_at,
        since_id=item.last_acked_comment_id,
    )
    if queue.mode == "implement_until_done":
        # Pick/continue a ticket toward done: implement a fresh ticket, or fix
        # open review findings if any have come back.
        intent = "implement"
    elif queue.mode == "triage":
        intent = "triage"
    else:  # pragma: no cover - guarded earlier in run_work_queue_step
        intent = "review"
    return {
        "directive_id": directive_id,
        "work_queue_run_id": work_queue_run_id,
        "item_id": item.id,
        "intent": intent,
        "ticket_id": item.ticket_id,
        "ticket_lease_epoch": ticket.lease_epoch,
        "ticket": {
            "id": ticket.id,
            "title": ticket.title,
            "status": ticket.status,
            "kind": ticket.kind,
            "priority": ticket.priority,
            "assigned_to": ticket.assigned_to,
        },
        "comment_delta": [_comment_to_delta(c) for c in delta_rows],
        "persona_ref": queue.assigned_persona,
        "expand_hints": list(_EXPAND_HINTS),
        "writeback_contract": {
            "directive_id": directive_id,
            "post_via": "add_ticket_comment",
            "required_lease_epoch": ticket.lease_epoch,
            # The queue never supplies author_persona; trusted provenance is
            # server-derived (design §5.0 / tk_d42170b4670f4448 verdict_trusted).
            "author_persona_is_server_derived": True,
            "settle_via": (
                "complete_work_queue_step("
                f"directive_id={directive_id})"
            ),
        },
    }


async def run_work_queue_step(
    db: AsyncSession,
    *,
    queue: WorkQueue,
    wake_source: str,
    wake_ref: str | None,
    max_tickets: int | None,
    actor_type: str | None = None,
    service_key_id: str | None = None,
    service_key_name: str | None = None,
) -> StepResult:
    """The stateless heartbeat (design §4.4, §5, §6, §9.1).

    Order of operations (each gate short-circuits):
      a. CADENCE GATE — if now < last-wake + max(cadence_seconds, floor 120),
         return {status:"idle", reason:"cadence"} and do NO work.
      b. PAUSED/TERMINAL — a non-active queue does no work.
      c. review_until_clean → not_available (WQ-P4 stop oracle).
      d. OPEN-LEASE RE-EMIT — any item with open_directive_id set re-emits the
         SAME directive (same directive_id + work_queue_run_id); NO new claim,
         NO cursor advance. The crash-replay path.
      e. FRESH CLAIM — (auto_adopt materialization first, capped) claim up to
         max_tickets_per_run items via claim_work_queue_item; for each, mint a
         directive_id, INSERT a work_queue_runs row, set the lease, advance the
         SEEN cursor only, increment attempts (EMITTED directive).

    The ACKED cursor is NEVER advanced here — only complete_work_queue_step
    advances it. The whole safety envelope (§9.1) is enforced at runtime here +
    in complete_work_queue_step.

    Caller commits the transaction.
    """
    now = datetime.now(timezone.utc)

    # (b) Non-active queue: no work.
    if queue.status == "paused":
        return StepResult(status="stopped", reason="paused")
    if queue.status in ("completed", "cancelled"):
        return StepResult(status="stopped", reason=queue.status)

    # (c) review_until_clean is NOT buildable until WQ-P4 wires the trusted-
    # provenance stop oracle (the verdict_trusted column exists, but the
    # directive-emission + server re-derive at settle is WQ-P4 scope). Return a
    # clear not-available response rather than silently doing nothing.
    # TODO(WQ-P4 tk_*): wire the §5.0 trust filter + strict-VERIFIED-CLEAN stop
    # oracle and remove this guard.
    if queue.mode == "review_until_clean":
        raise StepEngineError(
            "review_until_clean_not_available",
            "review_until_clean mode is not yet available — the trusted-"
            "verdict stop oracle ships in WQ-P4. Use implement_until_done or "
            "triage queues for now.",
            http_status=422,
        )

    work_queue_run_id = _new_run_id()

    # (d) OPEN-LEASE RE-EMIT — checked BEFORE the cadence gate. A re-emit is a
    # crash-replay of an ALREADY-emitted directive, not new work, so the cadence
    # floor (which bounds how often FRESH work is dispatched) must not strand an
    # agent that retried within the window. No new claim, no cursor advance, no
    # attempts bump — it re-emits the SAME directive_id + originating run id.
    open_items = list(
        (
            await db.execute(
                select(WorkQueueItem).where(
                    WorkQueueItem.work_queue_id == queue.id,
                    WorkQueueItem.open_directive_id.is_not(None),
                )
            )
        ).scalars().all()
    )
    if open_items:
        directives: list[dict] = []
        for item in open_items:
            ticket = (
                await db.execute(
                    select(Ticket).where(
                        Ticket.id == item.ticket_id,
                        Ticket.project_id == queue.project_id,
                    )
                )
            ).scalar_one_or_none()
            if ticket is None:
                # Ticket hard-deleted under an open lease — settle as failed so
                # the loop never wedges on a missing ticket.
                item.item_status = _FAILED_STATUS
                item.open_directive_id = None
                item.open_directive_run_id = None
                item.updated_at = now
                continue
            directives.append(
                await _build_directive(
                    db,
                    queue,
                    item,
                    ticket,
                    # Re-emit the SAME directive id + the run that opened it.
                    directive_id=item.open_directive_id or _new_directive_id(),
                    work_queue_run_id=item.open_directive_run_id
                    or work_queue_run_id,
                )
            )
        # A pure re-emit does NOT mint a new work_queue_runs row (the original
        # one already records the directive). Return the FIRST open run id as
        # the handle so the agent can settle.
        handle = open_items[0].open_directive_run_id or work_queue_run_id
        return StepResult(
            status="ok",
            work_queue_run_id=handle,
            directives=directives,
        )

    # (a) CADENCE GATE — gates FRESH claims only (re-emit above already
    # returned). cadence_seconds is floored at 120 at runtime even if a legacy
    # row stored a lower value. If the most recent wake is younger than the
    # cadence interval, dispatch no NEW work.
    cadence = max(int(queue.cadence_seconds or _CADENCE_DEFAULT_SECONDS),
                  _CADENCE_FLOOR_SECONDS)
    last_wake = (
        await db.execute(
            select(func.max(WorkQueueRun.created_at)).where(
                WorkQueueRun.work_queue_id == queue.id
            )
        )
    ).scalar_one_or_none()
    if last_wake is not None:
        if last_wake.tzinfo is None:
            last_wake = last_wake.replace(tzinfo=timezone.utc)
        if now < last_wake + timedelta(seconds=cadence):
            return StepResult(
                status="idle",
                reason="cadence",
                next_eligible_at=last_wake + timedelta(seconds=cadence),
            )

    # (e) FRESH CLAIM. auto_adopt materialization first (capped), then claim up
    # to max_tickets_per_run eligible items.
    import json as _json

    try:
        selector = _json.loads(queue.selector or "{}")
        if not isinstance(selector, dict):
            selector = {}
    except (TypeError, ValueError):
        selector = {}
    await _materialize_auto_adopt(db, queue, selector, now=now)

    budget = int(queue.max_tickets_per_run or 1)
    if max_tickets is not None:
        budget = min(budget, max(1, int(max_tickets)))

    # Candidate eligible items (the atomic claim is the real guard — this
    # SELECT only narrows the set the claim races over).
    candidates = list(
        (
            await db.execute(
                select(WorkQueueItem)
                .where(
                    WorkQueueItem.work_queue_id == queue.id,
                    WorkQueueItem.item_status.in_(("pending", _WAITING_STATUS)),
                    WorkQueueItem.open_directive_id.is_(None),
                    or_(
                        WorkQueueItem.next_eligible_at.is_(None),
                        WorkQueueItem.next_eligible_at <= now,
                    ),
                )
                .order_by(WorkQueueItem.created_at.asc())
                .limit(budget * 4 + 8)
            )
        ).scalars().all()
    )

    directives = []
    claimed = 0
    for item in candidates:
        if claimed >= budget:
            break
        run_id_for_claim = _new_run_id()
        won = await claim_work_queue_item(
            db, item_id=item.id, run_id=run_id_for_claim
        )
        if not won:
            continue  # another wake won the race (rowcount 0)
        ticket = (
            await db.execute(
                select(Ticket).where(
                    Ticket.id == item.ticket_id,
                    Ticket.project_id == queue.project_id,
                )
            )
        ).scalar_one_or_none()
        if ticket is None:
            # Cross-project / deleted ticket: never act on it. Mark failed.
            await db.execute(
                update(WorkQueueItem)
                .where(WorkQueueItem.id == item.id)
                .values(
                    item_status=_FAILED_STATUS,
                    open_directive_run_id=None,
                    updated_at=now,
                )
            )
            continue

        directive_id = _new_directive_id()
        # SEEN cursor advances to the newest comment we are about to SHOW —
        # never the ACKED cursor (that moves only at settle).
        delta_rows = await _comment_delta(
            db,
            ticket_id=item.ticket_id,
            since=item.last_acked_comment_at,
            since_id=item.last_acked_comment_id,
        )
        newest = delta_rows[-1] if delta_rows else None
        await db.execute(
            update(WorkQueueItem)
            .where(WorkQueueItem.id == item.id)
            .values(
                open_directive_id=directive_id,
                open_directive_run_id=run_id_for_claim,
                last_seen_comment_at=(
                    newest.created_at if newest else item.last_seen_comment_at
                ),
                last_seen_comment_id=(
                    newest.id if newest else item.last_seen_comment_id
                ),
                # EMITTED directive — counts toward max_attempts (§9.1).
                attempts=WorkQueueItem.attempts + 1,
                updated_at=now,
            )
        )
        db.add(
            WorkQueueRun(
                id=run_id_for_claim,
                work_queue_id=queue.id,
                work_queue_item_id=item.id,
                directive_id=directive_id,
                outcome=None,  # settled by complete_work_queue_step
                created_at=now,
            )
        )
        # Refresh the in-memory item so _build_directive sees the new cursor.
        item.open_directive_id = directive_id
        item.open_directive_run_id = run_id_for_claim
        directives.append(
            await _build_directive(
                db, queue, item, ticket, directive_id, run_id_for_claim
            )
        )
        claimed += 1

    if not directives:
        # Nothing eligible. Record a no-op wake so cadence advances + the loop
        # is observable; report stop when the queue is genuinely empty.
        db.add(
            WorkQueueRun(
                id=work_queue_run_id,
                work_queue_id=queue.id,
                work_queue_item_id=None,
                directive_id=None,
                outcome="noop",
                created_at=now,
            )
        )
        total_open = (
            await db.execute(
                select(func.count(WorkQueueItem.id)).where(
                    WorkQueueItem.work_queue_id == queue.id,
                    WorkQueueItem.item_status.in_(
                        ("pending", "active", _WAITING_STATUS)
                    ),
                )
            )
        ).scalar_one()
        if int(total_open) == 0:
            return StepResult(
                status="stopped",
                work_queue_run_id=work_queue_run_id,
                reason="queue_empty",
            )
        return StepResult(
            status="idle",
            work_queue_run_id=work_queue_run_id,
            reason="no_eligible_items",
        )

    return StepResult(
        status="ok",
        work_queue_run_id=work_queue_run_id,
        directives=directives,
    )


async def complete_work_queue_step(
    db: AsyncSession,
    *,
    queue: WorkQueue,
    item_id: str,
    directive_id: str,
    ticket_id: str,
    outcome: str,
    comment_id: str | None,
    agent_run_id: str | None,
    failed: bool,
    ticket_lease_epoch: int | None = None,
    agent_summary: str | None = None,
) -> CompleteResult:
    """Settle one directive — the SINGLE commit point of the loop (design §4.5).

    Order:
      1. IDEMPOTENCY — if the directive is already settled (the
         work_queue_runs row's outcome is set), return the prior outcome and do
         NOT re-apply (no double cursor advance, no double attempts).
      2. LEASE MATCH — directive_id must equal the item's open_directive_id; a
         stale/forged directive is rejected (lease stays open, re-emits next
         wake).
      3. On reported FAILURE / no-progress: increment attempts (the EMITTED
         directive was already counted at emit, so here we only apply
         backoff/failed transition), set next_eligible_at via the 2m→60m
         backoff, flip to 'failed' when attempts >= max_attempts_per_item
         (human reset required). The lease is still settled so the next wake can
         re-claim after backoff.
      4. On SUCCESS: VALIDATE the writeback landed (the claimed comment_id
         exists on the ticket; lease-epoch fenced if supplied) BEFORE advancing.
         Then advance the ACKED cursor (THE ONLY place it advances), settle the
         lease, set item_status (waiting | done), link agent_run_id, stamp the
         work_queue_runs.outcome.

    Caller commits the transaction.
    """
    now = datetime.now(timezone.utc)

    item = (
        await db.execute(
            select(WorkQueueItem).where(
                WorkQueueItem.id == item_id,
                WorkQueueItem.work_queue_id == queue.id,
            )
        )
    ).scalar_one_or_none()
    if item is None:
        raise StepEngineError(
            "item_not_found",
            f"Work-queue item {item_id!r} not found in this queue.",
            http_status=404,
        )

    # (1) IDEMPOTENCY — a duplicate settle of the same directive returns the
    # prior outcome. The work_queue_runs row whose directive_id matches and
    # whose outcome is already set IS the settled record.
    settled_run = (
        await db.execute(
            select(WorkQueueRun).where(
                WorkQueueRun.work_queue_id == queue.id,
                WorkQueueRun.directive_id == directive_id,
                WorkQueueRun.outcome.is_not(None),
            )
        )
    ).scalar_one_or_none()
    if settled_run is not None:
        return CompleteResult(
            status="idempotent_replay",
            item_status=item.item_status,
            item_terminal=item.item_status in (_DONE_STATUS, _FAILED_STATUS),
            reason="directive already settled",
            attempts=item.attempts,
            next_eligible_at=item.next_eligible_at,
        )

    # (2) LEASE MATCH — the directive being settled must be the open one.
    if item.open_directive_id != directive_id:
        raise StepEngineError(
            "directive_mismatch",
            "directive_id does not match this item's open directive lease "
            "(stale or forged). The open directive re-emits on the next wake.",
            http_status=409,
        )

    # The run row that emitted this directive (to stamp its outcome).
    emit_run = (
        await db.execute(
            select(WorkQueueRun).where(
                WorkQueueRun.work_queue_id == queue.id,
                WorkQueueRun.directive_id == directive_id,
            )
        )
    ).scalar_one_or_none()

    # ── (3) FAILURE / no-progress branch ──
    if failed:
        # attempts was already incremented at emit (EMITTED directive). Apply
        # the backoff curve keyed on the post-emit attempt count, and park as
        # 'failed' when the budget is exhausted.
        max_attempts = int(queue.max_attempts_per_item or 3)
        exhausted = item.attempts >= max_attempts
        next_eligible = (
            None
            if exhausted
            else now + timedelta(seconds=_backoff_for_attempt(item.attempts))
        )
        new_status = _FAILED_STATUS if exhausted else _WAITING_STATUS
        item.item_status = new_status
        item.open_directive_id = None
        item.open_directive_run_id = None
        item.last_agent_run_id = agent_run_id or item.last_agent_run_id
        item.next_eligible_at = next_eligible
        item.updated_at = now
        if emit_run is not None:
            emit_run.outcome = "errored" if exhausted else "failed"
            if agent_run_id:
                emit_run.agent_run_id = agent_run_id
        return CompleteResult(
            status="settled",
            item_status=new_status,
            item_terminal=exhausted,
            reason="exhausted" if exhausted else "backoff",
            attempts=item.attempts,
            next_eligible_at=next_eligible,
        )

    # ── (4) SUCCESS branch — validate the writeback landed ──
    acked_comment: TicketComment | None = None
    if comment_id:
        cstmt = select(TicketComment).where(
            TicketComment.id == comment_id,
            TicketComment.ticket_id == ticket_id,
        )
        acked_comment = (await db.execute(cstmt)).scalar_one_or_none()
        if acked_comment is None:
            # The claimed writeback does not exist on this ticket → reject.
            # Lease stays OPEN so the directive re-emits next wake.
            raise StepEngineError(
                "writeback_not_found",
                "The claimed comment_id does not exist on this ticket — the "
                "writeback did not land. The directive re-emits next wake.",
                http_status=422,
            )

    # Lease fence (defense in depth — the comment write itself was already
    # lease-fenced at routes/tickets.py). When supplied, the ticket epoch must
    # still match.
    if ticket_lease_epoch is not None:
        cur_epoch = (
            await db.execute(
                select(Ticket.lease_epoch).where(
                    Ticket.id == ticket_id,
                    Ticket.project_id == queue.project_id,
                )
            )
        ).scalar_one_or_none()
        if cur_epoch is not None and cur_epoch != ticket_lease_epoch:
            raise StepEngineError(
                "stale_lease_epoch",
                "Ticket lease_epoch advanced since the directive was emitted. "
                "Re-run the step.",
                http_status=409,
            )

    # Advance the ACKED cursor — THE ONLY place it advances. Move it to the
    # newest comment the directive showed (the validated writeback, or the
    # already-known SEEN floor when no comment was claimed).
    if acked_comment is not None:
        item.last_acked_comment_at = acked_comment.created_at
        item.last_acked_comment_id = acked_comment.id
    elif item.last_seen_comment_id is not None:
        item.last_acked_comment_at = item.last_seen_comment_at
        item.last_acked_comment_id = item.last_seen_comment_id

    # Settle the lease + set the queue-view status.
    terminal = outcome in ("completed_ticket", "done", "resolved")
    new_status = _DONE_STATUS if terminal else _WAITING_STATUS
    item.item_status = new_status
    item.open_directive_id = None
    item.open_directive_run_id = None
    item.last_agent_run_id = agent_run_id or item.last_agent_run_id
    # A productive settle resets the no-progress backoff. The item becomes
    # eligible again immediately (waiting) or terminal (done).
    item.next_eligible_at = None
    if not terminal:
        # Reset attempts on real progress so a long, productive multi-round
        # loop is not parked as failed (attempts count CONSECUTIVE no-progress).
        item.attempts = 0
    item.updated_at = now

    if emit_run is not None:
        emit_run.outcome = outcome or ("completed_ticket" if terminal else "posted_progress")
        if agent_run_id:
            emit_run.agent_run_id = agent_run_id

    return CompleteResult(
        status="settled",
        item_status=new_status,
        item_terminal=terminal,
        attempts=item.attempts,
        next_eligible_at=item.next_eligible_at,
    )
