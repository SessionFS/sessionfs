"""Agent work-queue services.

tk_529a64620db846f5 (WQ-P1) — the correctness primitive for autonomous
agent work queues (design tk_c2ed6093acde4d55,
docs/design/agent-work-queues.md §5/§11 R1). This phase ships ONLY the
atomic-claim helper; the step engine, hydration, mode algorithms, and routes
land in WQ-P2/P3.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server.db.models import WorkQueueItem


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
