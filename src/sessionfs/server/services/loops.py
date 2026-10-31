"""Review Loop R3 — loop status rollup (Brief D1).

A loop is a parent issue ticket carrying a ``loop_config`` JSON column
(docs/design/review-loop-r3.md §4). Its children are the child tasks filed
under it; each child's live status is derived from three record kinds:

  - agent runs (``ticket_id``): latest branch/head, token and dollar totals;
  - trusted verdict comments (``ticket_comments``): review rounds, findings
    by harm, open-harmful count;
  - work-queue items: the implementer identity, so readiness can enforce the
    self-review prohibition.

Every kind is loaded with ONE query across all children (never a per-child
N+1), then reduced in Python. Dollar figures are serialized only when
``show_dollars`` is true (org owner/admin or personal-project owner; service
keys never see dollars — same rule as the spend rollups).
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server.db.models import AgentRun, Ticket, TicketComment
from sessionfs.server.services.loop_budget import usage_for_runs
from sessionfs.server.services.review_state import (
    ReviewFinding,
    compute_merge_readiness,
    load_implementer_identities,
    load_key_minters,
    compute_review_state,
    finding_is_harmful,
)

# Loop config keys, kept private so the JSON stays shape-stable.
_QUEUE_ID = "queue_id"
_REVIEWER_PERSONA = "reviewer_persona"
_TOKEN_BUDGET = "token_budget"
_COST_BUDGET_USD = "cost_budget_usd"
_MAX_REVIEW_ROUNDS = "max_review_rounds"
_CHECKLIST_VERSION = "checklist_version"

# Explicit harm buckets (findings tagged ``harm: <bucket>`` with bucket != none).
_HARM_BUCKETS: tuple[str, ...] = (
    "data-exposed",
    "money-spent",
    "wrong-actor",
    "crash",
    "other",
)


def _loads_config(raw: str | None) -> dict:
    if not raw:
        return {}
    try:
        import json

        loaded = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _rounds_by_harm(findings: list[ReviewFinding]) -> list[dict[str, int]]:
    """Per review round, the count of harmful findings raised in that round.

    Oldest first; drives the round rings. Counts ALL findings (open and
    closed) so a past round's ring reflects what that round actually caught.
    """
    by_round: dict[int, int] = {}
    for f in findings:
        if finding_is_harmful(f):
            by_round[f.round] = by_round.get(f.round, 0) + 1
    return [
        {"round": rnd, "harmful": count}
        for rnd, count in sorted(by_round.items())
    ]


def _open_harm_by_bucket(findings: list[ReviewFinding]) -> dict[str, int]:
    """Count OPEN harmful findings per explicit harm bucket (tag != none)."""
    counts: dict[str, int] = {}
    for f in findings:
        if f.closed_round is not None:
            continue
        if f.harm is None or f.harm == "none":
            continue
        counts[f.harm] = counts.get(f.harm, 0) + 1
    return counts


def _child_round(state: Any) -> int:
    return len(state.rounds) if state is not None else 0


def _build_child(
    *,
    child: Ticket,
    runs: list[AgentRun],
    comments: list[dict],
    implementer_identities: list[tuple[str | None, str | None]],
    key_minters: dict[str, frozenset[str] | None],
    budget: dict,
    show_dollars: bool,
) -> dict:
    """Reduce one child's pre-fetched records into the LoopChild shape."""
    usage = usage_for_runs(runs)
    tokens = usage.tokens
    cost_usd = float(usage.cost_usd) if show_dollars else None

    latest = runs[-1] if runs else None
    branch = latest.branch if latest is not None else None
    head_sha = latest.head_sha if latest is not None else None

    state = compute_review_state(comments)
    all_findings = (state.open_findings + state.closed_findings) if state else []
    open_findings = state.open_findings if state else []
    harmful_open = sum(1 for f in open_findings if finding_is_harmful(f))

    readiness = compute_merge_readiness(
        comments=comments,
        sha=head_sha or "",
        implementer_identities=implementer_identities,
        key_minters=key_minters,
    )

    # needs_human — budget/round caps (design §4). Cost cap is only evaluated
    # for dollar-visible callers so a non-admin never learns a dollar budget
    # exists; token and round caps are visible to everyone.
    needs_human: list[str] = []
    token_budget = budget.get("tokens")
    max_rounds = budget.get("max_rounds")
    if token_budget is not None and tokens >= int(token_budget):
        needs_human.append(f"token budget hit ({tokens:,} tokens spent)")
    if show_dollars and budget.get("cost_usd") is not None and cost_usd is not None:
        if cost_usd >= float(budget["cost_usd"]):
            needs_human.append("cost budget hit")
    if max_rounds is not None and _child_round(state) >= int(max_rounds):
        needs_human.append("round cap hit")

    out: dict[str, Any] = {
        "ticket_id": child.id,
        "title": child.title,
        "status": child.status,
        "branch": branch,
        "head_sha": head_sha,
        "round": _child_round(state),
        "harmful_open": harmful_open,
        "findings_by_harm": _open_harm_by_bucket(open_findings),
        "rounds": _rounds_by_harm(all_findings),
        "tokens": tokens,
        "budget": budget,
        "needs_human": "; ".join(needs_human) if needs_human else None,
        "ready": readiness["ready"],
        "verdict": readiness["verdict"],
        "not_ready_reasons": readiness["reasons"],
    }
    if show_dollars and cost_usd is not None:
        out["cost_usd"] = round(cost_usd, 6)
    return out


async def load_loops(
    db: AsyncSession,
    *,
    loop_tickets: list[Ticket],
    show_dollars: bool,
) -> list[dict]:
    """Return Loop dicts for the given parent issue tickets (no per-child N+1).

    ``loop_tickets`` are the loop parent issues (``loop_config`` set). Each
    returned dict is ``{"id", "title", "children": [LoopChild, ...]}`` matching
    ``dashboard/src/api/reviewLoop.ts``.
    """
    loop_ids = [t.id for t in loop_tickets]
    if not loop_ids:
        return []

    children = (
        await db.execute(
            select(Ticket)
            .where(Ticket.parent_ticket_id.in_(loop_ids))
            .order_by(Ticket.created_at, Ticket.id)
        )
    ).scalars().all()
    child_ids = [c.id for c in children]

    children_by_parent: dict[str, list[Ticket]] = {}
    for c in children:
        children_by_parent.setdefault(c.parent_ticket_id, []).append(c)

    runs_by_ticket: dict[str, list[AgentRun]] = {}
    comments_by_ticket: dict[str, list[dict]] = {}
    implementers_by_ticket: dict[str, list[tuple[str | None, str | None]]] = {}
    key_minters: dict[str, frozenset[str] | None] = {}

    if child_ids:
        runs = (
            await db.execute(
                select(AgentRun)
                .where(AgentRun.ticket_id.in_(child_ids))
                .order_by(
                    func.coalesce(AgentRun.occurred_at, AgentRun.created_at),
                    AgentRun.id,
                )
            )
        ).scalars().all()
        for r in runs:
            runs_by_ticket.setdefault(r.ticket_id, []).append(r)

        comment_rows = (
            await db.execute(
                select(TicketComment)
                .where(
                    TicketComment.ticket_id.in_(child_ids),
                    TicketComment.verdict_trusted.is_(True),
                )
                .order_by(TicketComment.created_at, TicketComment.id)
            )
        ).scalars().all()
        for c in comment_rows:
            comments_by_ticket.setdefault(c.ticket_id, []).append(
                {
                    "id": c.id,
                    "author_persona": c.author_persona,
                    "content": c.content,
                    "created_at": c.created_at,
                    "verdict_trusted": c.verdict_trusted,
                    "service_key_id": c.service_key_id,
                    "author_user_id": c.author_user_id,
                    "reviewed_commit": c.reviewed_commit,
                }
            )

        implementers_by_ticket.update(
            await load_implementer_identities(db, children)
        )
        key_minters.update(
            await load_key_minters(
                db,
                [c for cs in comments_by_ticket.values() for c in cs],
                [i for ids in implementers_by_ticket.values() for i in ids],
            )
        )

    loops: list[dict] = []
    for loop in loop_tickets:
        config = _loads_config(loop.loop_config)
        budget: dict[str, Any] = {}
        if config.get(_TOKEN_BUDGET) is not None:
            budget["tokens"] = int(config[_TOKEN_BUDGET])
        if show_dollars and config.get(_COST_BUDGET_USD) is not None:
            budget["cost_usd"] = float(config[_COST_BUDGET_USD])
        if config.get(_MAX_REVIEW_ROUNDS) is not None:
            budget["max_rounds"] = int(config[_MAX_REVIEW_ROUNDS])

        loop_children = children_by_parent.get(loop.id, [])
        children_out = [
            _build_child(
                child=c,
                runs=runs_by_ticket.get(c.id, []),
                comments=comments_by_ticket.get(c.id, []),
                implementer_identities=implementers_by_ticket.get(c.id, []),
                key_minters=key_minters,
                budget=budget,
                show_dollars=show_dollars,
            )
            for c in loop_children
        ]
        loops.append({"id": loop.id, "title": loop.title, "children": children_out})

    return loops
