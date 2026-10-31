"""Review Loop R3 — the org-wide "Needs you" inbox (Brief C3).

One human feed across every project the caller can read: merge-ready child
branches, children that have hit a token/round cap, and proposed checklist
items waiting for acceptance. The read predicate is
``list_accessible_projects`` — the single source of truth for the projects
list — so a project the caller cannot read never leaks into the feed, and a
project the caller loses access to stops contributing at once.

The loop part reuses ``services.loops.load_loops`` verbatim (``show_dollars``
False), so "ready" and "needs_human" can never drift from the per-project
loop status surface, and the per-child records are loaded with grouped
queries — never a per-project or per-child N+1. Dollars are never read here.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server.auth.project_access import (
    editable_project_ids_subquery,
    list_accessible_projects,
)
from sessionfs.server.db.models import LoopChecklistItem, Ticket
from sessionfs.server.services.loops import load_loops
from sessionfs.server.tier_gate import resolve_project_plan_tiers
from sessionfs.server.tiers import Tier, get_features_for_tier

# Cap the feed's item array; ``counts`` always reflects the full totals.
_ITEM_LIMIT = 100

# needs_human reasons from services.loops._build_child -> a card headline.
_NEEDS_HUMAN_TITLES = (
    ("token budget hit", "Token budget hit"),
    ("round cap hit", "Round cap hit"),
)

_READY_TITLE = "Merge ready"
_READY_REASON = "Trusted VERIFIED-CLEAN bound to head"
_PROPOSED_REASON = "Proposed checklist item awaits acceptance"


def _needs_human_title(reason: str) -> str:
    for prefix, title in _NEEDS_HUMAN_TITLES:
        if reason.startswith(prefix):
            return title
    return reason


def _sort_at(dt: datetime | None) -> datetime:
    """Normalize a column timestamp for cross-kind ordering."""
    if dt is None:
        return datetime.min.replace(tzinfo=timezone.utc)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _empty_counts() -> dict[str, int]:
    return {"ready": 0, "needs_human": 0, "proposed_checklist": 0, "total": 0}


async def needs_you(db: AsyncSession, *, user_id: str) -> dict[str, Any]:
    """Return ``{"items": [...], "counts": {...}}`` for the caller's inbox."""
    projects = await list_accessible_projects(db, user_id)
    if not projects:
        return {"items": [], "counts": _empty_counts()}

    # Only projects whose plan includes agent_tickets (Team+) contribute;
    # one batched plan resolution, never a per-project query loop.
    tiers = await resolve_project_plan_tiers(list(projects), db)
    projects = [
        p
        for p in projects
        if "agent_tickets" in get_features_for_tier(tiers.get(p.id, Tier.FREE))
    ]
    if not projects:
        return {"items": [], "counts": _empty_counts()}

    project_ids = [p.id for p in projects]
    names = {p.id: p.name for p in projects}
    editable_ids = set(
        (
            await db.execute(
                await editable_project_ids_subquery(db, user_id)
            )
        )
        .scalars()
        .all()
    )

    # Loop parent issues across all readable projects, newest first.
    loops = (
        (
            await db.execute(
                select(Ticket)
                .where(
                    Ticket.project_id.in_(project_ids),
                    Ticket.loop_config.isnot(None),
                )
                .order_by(Ticket.created_at.desc(), Ticket.id.desc())
            )
        )
        .scalars()
        .all()
    )
    loop_project = {t.id: t.project_id for t in loops}
    loops_data = await load_loops(db, loop_tickets=loops, show_dollars=False)

    # Child updated_at for the newest-first sort, in ONE grouped query.
    child_ids = [c["ticket_id"] for loop in loops_data for c in loop["children"]]
    updated_by_child: dict[str, datetime] = {}
    if child_ids:
        rows = (
            await db.execute(
                select(Ticket.id, Ticket.updated_at).where(Ticket.id.in_(child_ids))
            )
        ).all()
        updated_by_child = {r[0]: r[1] for r in rows}

    items: list[tuple[datetime, dict[str, Any]]] = []

    for loop in loops_data:
        loop_id = loop["id"]
        project_id = loop_project[loop_id]
        project_name = names[project_id]
        can_act = project_id in editable_ids
        for child in loop["children"]:
            sort_at = _sort_at(updated_by_child.get(child["ticket_id"]))
            base = {
                "project_id": project_id,
                "project_name": project_name,
                "loop_id": loop_id,
                "ticket_id": child["ticket_id"],
                "ticket_title": child["title"],
                "can_act": can_act,
            }
            if child["ready"]:
                items.append(
                    (
                        sort_at,
                        {
                            **base,
                            "kind": "ready",
                            "title": _READY_TITLE,
                            "reason": _READY_REASON,
                        },
                    )
                )
            if child["needs_human"]:
                for reason in child["needs_human"].split("; "):
                    items.append(
                        (
                            sort_at,
                            {
                                **base,
                                "kind": "needs_human",
                                "title": _needs_human_title(reason),
                                "reason": reason,
                            },
                        )
                    )

    # Proposed checklist items across all readable projects, newest first.
    proposed = (
        (
            await db.execute(
                select(LoopChecklistItem)
                .where(
                    LoopChecklistItem.project_id.in_(project_ids),
                    LoopChecklistItem.status == "proposed",
                )
                .order_by(
                    LoopChecklistItem.created_at.desc(), LoopChecklistItem.id.desc()
                )
            )
        )
        .scalars()
        .all()
    )
    for item in proposed:
        project_id = item.project_id
        items.append(
            (
                _sort_at(item.created_at),
                {
                    "kind": "proposed_checklist",
                    "project_id": project_id,
                    "project_name": names[project_id],
                    "checklist_item_id": item.id,
                    "title": item.title,
                    "reason": _PROPOSED_REASON,
                    "can_act": project_id in editable_ids,
                },
            )
        )

    counts = _empty_counts()
    for _, item in items:
        kind = item["kind"]
        if kind == "ready":
            counts["ready"] += 1
        elif kind == "needs_human":
            counts["needs_human"] += 1
        elif kind == "proposed_checklist":
            counts["proposed_checklist"] += 1
    counts["total"] = sum(counts[k] for k in ("ready", "needs_human", "proposed_checklist"))

    # Newest first; stable tie-break so pagination/cap is deterministic.
    items.sort(key=lambda pair: (pair[0], pair[1].get("ticket_id") or "", pair[1].get("checklist_item_id") or ""), reverse=True)
    return {
        "items": [item for _, item in items[:_ITEM_LIMIT]],
        "counts": counts,
    }
