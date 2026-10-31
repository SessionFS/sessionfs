"""Review-loop budgets and round caps (Review Loop R3, Brief D2).

Budgets live on the PARENT loop ticket's ``loop_config`` JSON
(``token_budget``, ``cost_budget_usd``, ``max_review_rounds``) — a column
built by a sibling track and NOT present on every branch yet. This module
reads it DEFENSIVELY: when the column is absent, the budget is treated as
"no limits" so nothing blocks work.

Usage is computed per child (worker) ticket from its own ``agent_runs``
rows (tokens always; dollars only from ``reported``/``estimated`` cost
bases, mirroring ``agent_spend``). Rounds are re-derived over TRUSTED
verdict comments with ``compute_review_state`` so a forged verdict can
never count toward the cap, and a round cap only blocks a loop that has
NOT yet reached a strict ``VERIFIED-CLEAN``.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Iterable

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server.db.models import (
    AgentRun,
    Ticket,
    TicketComment,
    WorkQueue,
    WorkQueueItem,
    WorkQueueRun,
)
from sessionfs.server.services.review_state import compute_review_state

# Dollar cost bases that count toward a dollar budget (mirrors agent_spend).
_DOLLAR_COST_BASES: tuple[str, ...] = ("reported", "estimated")


def _run_tokens(run: AgentRun) -> int:
    """Token total for one run (input + output + cache read/write)."""
    return (
        int(run.input_tokens or 0)
        + int(run.output_tokens or 0)
        + int(run.cache_read_tokens or 0)
        + int(run.cache_write_tokens or 0)
    )


@dataclass(frozen=True)
class TicketUsage:
    """Per-ticket usage rollup: tokens always; dollars only reported/estimated."""

    tokens: int = 0
    cost_usd: Decimal = Decimal("0")


def usage_for_runs(runs: Iterable[AgentRun]) -> TicketUsage:
    """Sum tokens and reported/estimated dollars across a ticket's runs.

    The single source of truth for per-ticket usage, shared by the loop status
    rollup (``services.loops``) and the budget engine (``budget_status``) so
    the two can never disagree on what counts toward a budget.
    """
    tokens = 0
    cost = Decimal("0")
    for run in runs:
        tokens += _run_tokens(run)
        if run.cost_basis in _DOLLAR_COST_BASES and run.cost_usd is not None:
            cost += Decimal(str(run.cost_usd))
    return TicketUsage(tokens=tokens, cost_usd=cost)


def _ticket_has_loop_config() -> bool:
    """Whether the ``tickets`` table carries a ``loop_config`` column.

    The column is built by a sibling Review Loop track; until it lands this
    branch treats budgets as absent (no limits) rather than raising.
    """
    return hasattr(Ticket, "loop_config")


def _loop_config_of(ticket: Ticket) -> dict[str, Any]:
    """Read the ticket's ``loop_config`` JSON, guarded and never raising."""
    if not _ticket_has_loop_config():
        return {}
    raw = getattr(ticket, "loop_config", None)
    if not raw:
        return {}
    if isinstance(raw, dict):
        return raw
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _as_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _as_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


@dataclass
class BudgetConfig:
    """Parsed budget limits; ``None`` means "no limit"."""

    token_budget: int | None = None
    cost_budget_usd: float | None = None
    max_review_rounds: int | None = None

    @classmethod
    def from_config(cls, config: dict[str, Any]) -> "BudgetConfig":
        return cls(
            token_budget=_as_int(config.get("token_budget")),
            cost_budget_usd=_as_float(config.get("cost_budget_usd")),
            max_review_rounds=_as_int(config.get("max_review_rounds")),
        )

    @property
    def any_limit(self) -> bool:
        return (
            self.token_budget is not None
            or self.cost_budget_usd is not None
            or self.max_review_rounds is not None
        )


@dataclass
class BudgetStatus:
    """Budget read for one worker ticket, plus the limit(s) it has hit."""

    config: BudgetConfig
    tokens_used: int = 0
    cost_usd_used: Decimal = Decimal("0")
    rounds_used: int = 0
    strict_clean: bool = False

    @property
    def token_exceeded(self) -> bool:
        return (
            self.config.token_budget is not None
            and self.tokens_used >= self.config.token_budget
        )

    @property
    def cost_exceeded(self) -> bool:
        return (
            self.config.cost_budget_usd is not None
            and float(self.cost_usd_used) >= self.config.cost_budget_usd
        )

    @property
    def rounds_exceeded(self) -> bool:
        # A round cap only blocks work that has NOT reached a strict clean
        # verdict; a clean loop is allowed to stop on its own.
        return (
            self.config.max_review_rounds is not None
            and not self.strict_clean
            and self.rounds_used >= self.config.max_review_rounds
        )

    @property
    def exceeded(self) -> bool:
        return self.token_exceeded or self.cost_exceeded or self.rounds_exceeded

    def limit_summary(self) -> str:
        """Human phrase naming the first limit hit (for the notification).

        The cost phrase intentionally carries no figure: a budget-hit
        notification must never leak the dollar amount to a non-admin watcher.
        """
        if self.token_exceeded:
            return f"token budget of {self.config.token_budget:,}"
        if self.cost_exceeded:
            return "a cost budget"
        if self.rounds_exceeded:
            return f"review-round cap of {self.config.max_review_rounds}"
        return "budget"


async def _usage_for_ticket(
    db: AsyncSession, ticket_id: str
) -> tuple[int, Decimal]:
    """Sum tokens + reported/estimated dollars across a ticket's runs."""
    runs = (
        (await db.execute(select(AgentRun).where(AgentRun.ticket_id == ticket_id)))
        .scalars()
        .all()
    )
    usage = usage_for_runs(runs)
    return usage.tokens, usage.cost_usd


async def _review_rounds_for_ticket(
    db: AsyncSession, ticket_id: str
) -> tuple[int, bool]:
    """(max trusted round, last-verdict-is-strict-clean) for a ticket."""
    comments = (
        (
            await db.execute(
                select(TicketComment)
                .where(TicketComment.ticket_id == ticket_id)
                .order_by(TicketComment.created_at, TicketComment.id)
            )
        )
        .scalars()
        .all()
    )
    rs = compute_review_state(
        [
            {
                "id": c.id,
                "author_persona": c.author_persona,
                "content": c.content,
                "created_at": c.created_at,
                "verdict_trusted": bool(c.verdict_trusted),
            }
            for c in comments
        ]
    )
    if rs is None:
        return 0, False
    max_round = max((r.round for r in rs.rounds), default=0)
    return max_round, rs.last_verdict_is_strict_verified_clean


async def budget_status(db: AsyncSession, ticket: Ticket) -> BudgetStatus:
    """Read the ticket's budget position.

    Limits come from the ticket's PARENT ``loop_config`` (guarded — absent
    column = no limits). Usage is this ticket's own tokens/dollars/rounds.
    """
    parent_id = getattr(ticket, "parent_ticket_id", None)
    config_dict: dict[str, Any] = {}
    if parent_id:
        parent = await db.get(Ticket, parent_id)
        if parent is not None:
            config_dict = _loop_config_of(parent)
    config = BudgetConfig.from_config(config_dict)

    tokens_used = 0
    cost_usd_used = Decimal("0")
    rounds_used = 0
    strict_clean = False
    # Only run the queries a configured limit actually needs; a limit-less
    # loop stays cheap (no per-ticket rollup on the hot claim path).
    if config.token_budget is not None or config.cost_budget_usd is not None:
        tokens_used, cost_usd_used = await _usage_for_ticket(db, ticket.id)
    if config.max_review_rounds is not None:
        rounds_used, strict_clean = await _review_rounds_for_ticket(db, ticket.id)
    return BudgetStatus(
        config=config,
        tokens_used=tokens_used,
        cost_usd_used=cost_usd_used,
        rounds_used=rounds_used,
        strict_clean=strict_clean,
    )


async def park_item_needs_human(
    db: AsyncSession,
    *,
    queue: WorkQueue,
    item: WorkQueueItem,
    ticket: Ticket,
    budget: BudgetStatus,
    actor_type: str | None,
    service_key_name: str | None,
) -> bool:
    """Park a loop item for a human and notify, after a budget/round-cap hit.

    Guarded (rowcount 1): only an item still pending/active/waiting is parked,
    so two concurrent finishes can't both write a run row and notify. Returns
    True when this call parked it.

    Shared by ``finish_worker_run`` and ``record_review_round``: a cap that
    only reveals itself once usage or a round lands must still move the item
    to ``needs_human``. Mirrors the parking block on the claim path in
    ``services/work_queues.py``; the notification is best-effort (the
    ``_notify_budget_hit`` import is deferred so ``loop_budget`` never imports
    ``work_queues`` at module load, which imports this module lazily too).
    """
    now = datetime.now(timezone.utc)
    parked = await db.execute(
        update(WorkQueueItem)
        .where(
            WorkQueueItem.id == item.id,
            WorkQueueItem.item_status.in_(("pending", "active", "waiting")),
        )
        .values(
            item_status="needs_human",
            open_directive_run_id=None,
            next_eligible_at=None,
            updated_at=now,
        )
    )
    if parked.rowcount != 1:
        return False
    db.add(
        WorkQueueRun(
            id=f"wqr_{uuid.uuid4().hex[:16]}",
            work_queue_id=queue.id,
            work_queue_item_id=item.id,
            directive_id=None,
            outcome="needs_human",
            created_at=now,
        )
    )
    from sessionfs.server.services.work_queues import _notify_budget_hit

    await _notify_budget_hit(
        db,
        queue,
        ticket,
        budget,
        actor_type=actor_type,
        service_key_name=service_key_name,
    )
    return True
