"""Agent-run spend rollups (Review Loop R3, Brief A2).

One SQL ``GROUP BY`` per rollup over ``agent_runs`` — never a per-row
Python aggregation. Every rollup is filtered to the caller's readable
projects (the access subquery is supplied by the caller so the same
service serves both user keys and org-bound service keys), plus the
route's project/ticket scope and a ``created_at`` window (default: the
last 30 days).

Dollar rules (docs/design/agent-run-spend.md §9.1): runs with
``cost_basis`` ``reported`` or ``estimated`` contribute to ``cost_usd``;
runs with ``plan`` or ``unknown`` count tokens but no dollars, so the
totals carry ``tokens_on_plans`` alongside the dollar figure. Token
totals are always returned; ``cost_usd`` is only serialized when the
caller is allowed to see dollars.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import and_, Select, case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server.db.models import AgentRun

# group_by vocabulary accepted by the spend endpoints.
GROUP_BYS: tuple[str, ...] = ("persona", "model", "ticket", "role", "day")

# cost_basis buckets.
_DOLLAR_COST_BASES: tuple[str, ...] = ("reported", "estimated")
_PLAN_COST_BASES: tuple[str, ...] = ("plan", "unknown")

DEFAULT_RANGE_DAYS = 30
_COST_USD_SCALE = 6  # Numeric(12, 6)


def default_range() -> tuple[datetime, datetime]:
    """(from, to) for the default window — the last 30 days, half-open."""
    to = datetime.now(timezone.utc)
    return to - timedelta(days=DEFAULT_RANGE_DAYS), to


@dataclass
class SpendTotals:
    """Aggregated spend for one rollup row (or the overall total)."""

    runs: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    tokens_on_plans: int = 0
    cost_usd: Decimal | None = None  # reported + estimated dollars

    @classmethod
    def from_row(cls, row: Any) -> "SpendTotals":
        """Coerce a GROUP BY row into a SpendTotals. Token sums arrive as
        int on SQLite but Numeric on PostgreSQL — normalize to int; costs
        normalize to Decimal."""
        return cls(
            runs=_to_int(row.runs),
            input_tokens=_to_int(row.input_tokens),
            output_tokens=_to_int(row.output_tokens),
            cache_read_tokens=_to_int(row.cache_read_tokens),
            cache_write_tokens=_to_int(row.cache_write_tokens),
            tokens_on_plans=_to_int(row.tokens_on_plans),
            cost_usd=_to_decimal(row.cost_usd),
        )

    def to_dict(self, *, show_dollars: bool) -> dict[str, Any]:
        out: dict[str, Any] = {
            "runs": self.runs,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "cache_write_tokens": self.cache_write_tokens,
            "tokens_on_plans": self.tokens_on_plans,
        }
        if show_dollars:
            out["cost_usd"] = round(float(self.cost_usd or Decimal("0")), _COST_USD_SCALE)
        return out


def _to_int(value: Any) -> int:
    if value is None:
        return 0
    return int(value)


def _to_decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def _totals_columns() -> list[Any]:
    """SELECT columns shared by every rollup (the non-group aggregates)."""
    plan_tokens = (
        func.coalesce(AgentRun.input_tokens, 0)
        + func.coalesce(AgentRun.output_tokens, 0)
        + func.coalesce(AgentRun.cache_read_tokens, 0)
        + func.coalesce(AgentRun.cache_write_tokens, 0)
    )
    return [
        func.count(AgentRun.id).label("runs"),
        func.coalesce(func.sum(AgentRun.input_tokens), 0).label("input_tokens"),
        func.coalesce(func.sum(AgentRun.output_tokens), 0).label("output_tokens"),
        func.coalesce(func.sum(AgentRun.cache_read_tokens), 0).label("cache_read_tokens"),
        func.coalesce(func.sum(AgentRun.cache_write_tokens), 0).label("cache_write_tokens"),
        func.coalesce(
            func.sum(
                case(
                    (AgentRun.cost_basis.in_(_PLAN_COST_BASES), plan_tokens),
                    # Tokens with no cost and no basis are effectively unknown.
                    (
                        and_(AgentRun.cost_basis.is_(None), AgentRun.cost_usd.is_(None)),
                        plan_tokens,
                    ),
                    else_=0,
                )
            ),
            0,
        ).label("tokens_on_plans"),
        func.coalesce(
            func.sum(
                case(
                    (AgentRun.cost_basis.in_(_DOLLAR_COST_BASES), AgentRun.cost_usd),
                    else_=0,
                )
            ),
            0,
        ).label("cost_usd"),
    ]


_GROUP_COLUMNS: dict[str, Any] = {
    "persona": AgentRun.persona_name,
    "model": AgentRun.model,
    "ticket": AgentRun.ticket_id,
    "role": AgentRun.role,
    "day": func.date(func.coalesce(AgentRun.occurred_at, AgentRun.created_at)),
}


def rollup_statement(
    *,
    group_by: str | None = None,
    project_id: str | None = None,
    ticket_id: str | None = None,
    readable_projects: Select | None = None,
    from_dt: datetime | None = None,
    to_dt: datetime | None = None,
    work_queue_id: str | None = None,
) -> Select:
    """Build one spend rollup statement.

    ``group_by=None`` returns a single aggregate row (the total).
    ``readable_projects`` is the caller's ``accessible_project_ids_subquery``
    for user keys; service keys pass ``None`` and rely on the route's
    ``project_id`` scope + ``assert_service_key_can_access_project``.

    The ``created_at`` window is applied only when both ``from_dt`` and
    ``to_dt`` are supplied; pass both ``None`` for an unbounded rollup
    (the ticket view sums a ticket's whole lifetime).
    """
    if group_by is None:
        stmt = select(*_totals_columns())
    else:
        if group_by not in _GROUP_COLUMNS:
            raise ValueError(f"unknown group_by: {group_by}")
        stmt = select(
            _GROUP_COLUMNS[group_by].label("key"), *_totals_columns()
        ).group_by(_GROUP_COLUMNS[group_by])

    if from_dt is not None and to_dt is not None:
        stmt = stmt.where(
            func.coalesce(AgentRun.occurred_at, AgentRun.created_at) >= from_dt,
            func.coalesce(AgentRun.occurred_at, AgentRun.created_at) < to_dt,
        )
    if project_id is not None:
        stmt = stmt.where(AgentRun.project_id == project_id)
    if ticket_id is not None:
        stmt = stmt.where(AgentRun.ticket_id == ticket_id)
    if work_queue_id is not None:
        stmt = stmt.where(AgentRun.work_queue_id == work_queue_id)
    if readable_projects is not None:
        stmt = stmt.where(AgentRun.project_id.in_(readable_projects))
    return stmt


def _key(value: Any) -> Any:
    """Normalize a group key for JSON (day arrives as date/string)."""
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


async def rollup(
    db: AsyncSession,
    *,
    group_by: str | None,
    project_id: str | None,
    ticket_id: str | None,
    readable_projects: Select | None,
    from_dt: datetime | None,
    to_dt: datetime | None,
    show_dollars: bool,
    work_queue_id: str | None = None,
) -> dict[str, Any]:
    """Execute one rollup and return ``{"totals": {...}, "groups": [...]}``.

    ``group_by=None`` omits ``groups`` (single total)."""
    stmt = rollup_statement(
        group_by=group_by,
        project_id=project_id,
        ticket_id=ticket_id,
        readable_projects=readable_projects,
        from_dt=from_dt,
        to_dt=to_dt,
        work_queue_id=work_queue_id,
    )
    rows = (await db.execute(stmt)).all()
    if group_by is None:
        total = SpendTotals.from_row(rows[0]) if rows else SpendTotals()
        return {"totals": total.to_dict(show_dollars=show_dollars)}

    groups: list[dict[str, Any]] = []
    aggregate = SpendTotals()
    for row in rows:
        t = SpendTotals.from_row(row)
        aggregate = SpendTotals(
            runs=aggregate.runs + t.runs,
            input_tokens=aggregate.input_tokens + t.input_tokens,
            output_tokens=aggregate.output_tokens + t.output_tokens,
            cache_read_tokens=aggregate.cache_read_tokens + t.cache_read_tokens,
            cache_write_tokens=aggregate.cache_write_tokens + t.cache_write_tokens,
            tokens_on_plans=aggregate.tokens_on_plans + t.tokens_on_plans,
            cost_usd=_add_decimal(aggregate.cost_usd, t.cost_usd),
        )
        groups.append({"key": _key(row.key), **t.to_dict(show_dollars=show_dollars)})
    return {
        "totals": aggregate.to_dict(show_dollars=show_dollars),
        "groups": groups,
    }


def _add_decimal(a: Decimal | None, b: Decimal | None) -> Decimal | None:
    if a is None and b is None:
        return None
    return (a or Decimal("0")) + (b or Decimal("0"))
