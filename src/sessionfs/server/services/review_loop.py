"""Review Loop rollups (Review Loop R3, Brief E2) — read-only service layer.

Three surfaces over agent-run findings (JSON), trusted verdict comments and
tickets, all pure reads over the existing 0.29.1 schema (no migration):

- ``loop_report``      — per review round: findings by harm, how many were
  known checklist classes vs. brand-new, how many were design-stage, and the
  tokens (and, for dollar-visible callers, dollars) the round cost.
- ``loop_insights``    — caught findings (harmful AND fixed), tokens/dollars
  per accepted change by month, and learning series per checklist class.
- ``ticket_story``     — an ordered event stream (brief → run → round →
  finding → fix → verdict → merge) for the ticket page.

Dollar figures mirror ``agent_spend``: only ``reported``/``estimated`` cost
bases contribute dollars, and the route decides whether to serialize them
(the service always computes them; the route drops the key for non-admins).
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import case, func, inspect, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server.db.models import (
    AgentRun,
    Deployment,
    PullRequest,
    Release,
    ReleasePr,
    Ticket,
    TicketComment,
    TicketPrLink,
)
from sessionfs.server.services.review_state import VERDICT_CLEAN, compute_review_state

# Same split as agent_spend: only reported/estimated cost bases have dollars.
_DOLLAR_COST_BASES: tuple[str, ...] = ("reported", "estimated")
_COST_USD_SCALE = 6  # Numeric(12, 6)
_CHECKLIST_TABLE = "loop_checklist_items"


def _as_utc(dt: datetime) -> datetime:
    """Normalise a naive/aware datetime to an aware UTC one (SQLite drops tz)."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    return _as_utc(dt).isoformat()


def _to_iso(val: Any) -> str | None:
    """Serialise a raw joined-at value to a consistent UTC ISO string.

    The checklist join date is read through raw ``text()`` so the driver hands
    back a ``datetime`` on PostgreSQL but a string on SQLite (its
    ``DateTime`` round-trips as ``YYYY-MM-DD HH:MM:SS.ffffff``). Normalise
    both to the same ISO form; a value we cannot parse is passed through as-is.
    """
    if val is None:
        return None
    if isinstance(val, datetime):
        return _iso(val)
    # ``fromisoformat`` on 3.10 rejects the space separator, so normalise it.
    norm = str(val).replace(" ", "T", 1)
    try:
        return _iso(datetime.fromisoformat(norm))
    except ValueError:
        return str(val)


def _decode_findings(value: str | None) -> list[dict[str, Any]]:
    try:
        findings = json.loads(value) if value else []
        if not isinstance(findings, list):
            findings = []
    except (ValueError, TypeError):
        findings = []
    return [f for f in findings if isinstance(f, dict)]


def _total_tokens(run: AgentRun) -> int:
    return (
        (run.input_tokens or 0)
        + (run.output_tokens or 0)
        + (run.cache_read_tokens or 0)
        + (run.cache_write_tokens or 0)
    )


def _dollar_cost(run: AgentRun) -> Decimal:
    if run.cost_basis in _DOLLAR_COST_BASES and run.cost_usd is not None:
        return run.cost_usd
    return Decimal("0")


def _round_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


# ── loop report ────────────────────────────────────────────────────

_STAGE_ORDER = {"design": 0, "code": 1}


def _new_round(round_no: int, stage: Any) -> dict[str, Any]:
    return {
        "round": round_no,
        "stage": stage,
        "findings_by_harm": {},
        "checklist_class_count": 0,
        "new_class_count": 0,
        "design_stage_count": 0,
        "tokens": 0,
        "cost_usd": Decimal("0"),
    }


def _accumulate(agg: dict[str, Any], finding: dict[str, Any]) -> None:
    harm = finding.get("harm")
    if harm is not None:
        key = str(harm)
        agg["findings_by_harm"][key] = agg["findings_by_harm"].get(key, 0) + 1
    cls = finding.get("class")
    if cls is not None:
        if cls == "new":
            agg["new_class_count"] += 1
        else:
            agg["checklist_class_count"] += 1
    if finding.get("stage") == "design":
        agg["design_stage_count"] += 1


async def loop_report(
    db: AsyncSession, *, project_id: str, ticket_id: str, show_dollars: bool
) -> dict[str, Any]:
    """Per-round rollup for one ticket.

    Each run with a ``round`` on at least one finding contributes its tokens
    (and dollars) to its *primary* round — the first (round, stage) key seen
    in its findings — which is the documented approximation for attributing
    a review run's cost to a single round.
    """
    runs = (
        (
            await db.execute(
                select(AgentRun)
                .where(AgentRun.project_id == project_id, AgentRun.ticket_id == ticket_id)
                .order_by(AgentRun.created_at, AgentRun.id)
            )
        )
        .scalars()
        .all()
    )

    rounds: dict[tuple[int, Any], dict[str, Any]] = {}
    for run in runs:
        tokens = _total_tokens(run)
        dollars = _dollar_cost(run)
        primary: tuple[int, Any] | None = None
        for finding in _decode_findings(run.findings):
            round_no = _round_int(finding.get("round"))
            if round_no is None:
                continue
            stage = finding.get("stage")
            key = (round_no, stage)
            if primary is None:
                primary = key
            _accumulate(rounds.setdefault(key, _new_round(round_no, stage)), finding)
        if primary is not None:
            rounds[primary]["tokens"] += tokens
            rounds[primary]["cost_usd"] += dollars

    ordered = sorted(
        rounds.values(),
        key=lambda r: (r["round"], _STAGE_ORDER.get(r["stage"], 2), r["stage"] or ""),
    )
    for agg in ordered:
        agg["tokens"] = int(agg["tokens"])
        if show_dollars:
            agg["cost_usd"] = round(float(agg["cost_usd"]), _COST_USD_SCALE)
        else:
            agg.pop("cost_usd", None)
    return {"rounds": ordered}


# ── loop insights ──────────────────────────────────────────────────


def _caught(run: Any, finding: dict[str, Any]) -> dict[str, Any]:
    return {
        "finding": str(finding.get("title") or finding.get("text") or ""),
        "ticket_id": run.ticket_id,
        "run_id": run.id,
        "harm": str(finding.get("harm")),
        "class": str(finding.get("class") or ""),
        "file": finding.get("file"),
        "line": finding.get("line"),
        "fixed_in_sha": finding.get("fixed_in_sha"),
    }


def _iso_monday(dt: datetime) -> str:
    d = _as_utc(dt).date()
    return (d - timedelta(days=d.weekday())).isoformat()


def _weekly_series(
    counts: dict[str, int], from_dt: datetime, to_dt: datetime
) -> list[dict[str, Any]]:
    """Zero-filled weekly buckets from the week of ``from_dt`` through ``to_dt``."""
    start = (_as_utc(from_dt).date() - timedelta(days=_as_utc(from_dt).date().weekday()))
    end = (_as_utc(to_dt).date() - timedelta(days=_as_utc(to_dt).date().weekday()))
    series: list[dict[str, Any]] = []
    cur = start
    while cur <= end:
        key = cur.isoformat()
        series.append({"week": key, "count": counts.get(key, 0)})
        cur += timedelta(days=7)
    return series


async def _load_checklist_items(
    db: AsyncSession, project_id: str
) -> dict[str, dict[str, Any]]:
    """Return ``{slug: {title, joined_at}}`` for ACTIVE checklist items.

    ``joined_at`` is the item's ``accepted_at`` (null for an active item that
    was never accepted). ``loop_checklist_items`` is added by a later
    review-loop migration; this endpoint is read-only and must not assume the
    column exists. Guard with the inspector: an older schema falls back to
    ``updated_at`` as the join date, and a schema without the table at all
    reports no join dates.
    """
    conn = await db.connection()
    has_table = await conn.run_sync(
        lambda sync_conn: inspect(sync_conn).has_table(_CHECKLIST_TABLE)
    )
    if not has_table:
        return {}
    columns = await conn.run_sync(
        lambda sync_conn: {
            c["name"] for c in inspect(sync_conn).get_columns(_CHECKLIST_TABLE)
        }
    )
    join_col = (
        "accepted_at"
        if "accepted_at" in columns
        else "updated_at"
        if "updated_at" in columns
        else None
    )
    cols = "slug, title" + (f", {join_col}" if join_col else "")
    rows = (
        await db.execute(
            text(
                f"SELECT {cols} FROM {_CHECKLIST_TABLE} "
                "WHERE project_id = :pid AND status = 'active'"
            ),
            {"pid": project_id},
        )
    ).all()
    out: dict[str, dict[str, Any]] = {}
    for row in rows:
        joined: str | None = None
        if join_col:
            joined = _to_iso(getattr(row, join_col, None))
        out[str(row.slug)] = {"title": str(row.title), "joined_at": joined}
    return out


async def _cost_per_accepted_change(
    db: AsyncSession,
    *,
    project_id: str,
    from_dt: datetime,
    to_dt: datetime,
    show_dollars: bool,
) -> list[dict[str, Any]]:
    """Tokens (and dollars) per accepted change, bucketed by resolve month.

    Numerator: tokens/dollars of every run on a ticket that reached ``done``
    that month (runs from any time — the window filters tickets, not runs).
    Denominator: the number of those tickets. A ``done`` ticket with no runs
    still counts in the denominator (LEFT JOIN), so the rate is honest.
    """
    token_expr = (
        func.coalesce(AgentRun.input_tokens, 0)
        + func.coalesce(AgentRun.output_tokens, 0)
        + func.coalesce(AgentRun.cache_read_tokens, 0)
        + func.coalesce(AgentRun.cache_write_tokens, 0)
    )
    stmt = (
        select(
            func.extract("year", Ticket.resolved_at).label("year"),
            func.extract("month", Ticket.resolved_at).label("month"),
            func.count(func.distinct(Ticket.id)).label("tickets"),
            func.coalesce(func.sum(token_expr), 0).label("tokens"),
            func.coalesce(
                func.sum(
                    case(
                        (AgentRun.cost_basis.in_(_DOLLAR_COST_BASES), AgentRun.cost_usd),
                        else_=0,
                    )
                ),
                0,
            ).label("cost_usd"),
        )
        .select_from(Ticket)
        .outerjoin(
            AgentRun,
            (AgentRun.ticket_id == Ticket.id) & (AgentRun.project_id == Ticket.project_id),
        )
        .where(
            Ticket.project_id == project_id,
            Ticket.status == "done",
            Ticket.resolved_at.is_not(None),
            Ticket.resolved_at >= from_dt,
            Ticket.resolved_at < to_dt,
        )
        .group_by("year", "month")
        .order_by("year", "month")
    )
    rows = (await db.execute(stmt)).all()

    points: list[dict[str, Any]] = []
    for row in rows:
        tickets = int(row.tickets or 0)
        tokens = int(row.tokens or 0)
        point: dict[str, Any] = {
            "period": f"{int(row.year)}-{int(row.month):02d}",
            "tokens": round(tokens / tickets, 2) if tickets else 0.0,
        }
        if show_dollars:
            cost = float(row.cost_usd or 0)
            point["cost_usd"] = round(cost / tickets, _COST_USD_SCALE) if tickets else 0.0
        points.append(point)
    return points


async def _scoreboard(
    db: AsyncSession,
    *,
    project_id: str,
    from_dt: datetime,
    to_dt: datetime,
    show_dollars: bool,
) -> list[dict[str, Any]]:
    """Model scoreboard: one row per (implementer, reviewer) model pair.

    Rows cover ``done`` tickets resolved in the window (the same "accepted
    change" denominator as ``_cost_per_accepted_change``). Per ticket the
    implementer/reviewer model is the most recent run with that role; a
    missing role/model groups under ``null``. ``avg_rounds_to_clean`` is the
    mean number of trusted rounds to the first ``VERIFIED-CLEAN`` (tickets
    that never reached clean are excluded from the mean but still count as
    accepted changes). Two fixed-size queries (runs + comments) — no N+1.
    """
    done_ids = list(
        (
            await db.execute(
                select(Ticket.id).where(
                    Ticket.project_id == project_id,
                    Ticket.status == "done",
                    Ticket.resolved_at.is_not(None),
                    Ticket.resolved_at >= from_dt,
                    Ticket.resolved_at < to_dt,
                )
            )
        )
        .scalars()
        .all()
    )
    if not done_ids:
        return []

    runs = list(
        (
            await db.execute(
                select(AgentRun)
                .where(
                    AgentRun.project_id == project_id,
                    AgentRun.ticket_id.in_(done_ids),
                )
                .order_by(AgentRun.created_at, AgentRun.id)
            )
        )
        .scalars()
        .all()
    )

    impl_by_ticket: dict[str, str | None] = {}
    rev_by_ticket: dict[str, str | None] = {}
    harmful_by_ticket: dict[str, int] = {}
    tokens_by_ticket: dict[str, int] = {}
    dollars_by_ticket: dict[str, Decimal] = {}
    for run in runs:
        ticket_id = run.ticket_id or ""
        role = (run.role or "").lower()
        if role in ("implementer", "ui-implementer") and run.model:
            impl_by_ticket[ticket_id] = run.model
        elif role in ("reviewer", "design-reviewer", "security-reviewer") and run.model:
            rev_by_ticket[ticket_id] = run.model
        tokens_by_ticket[ticket_id] = (
            tokens_by_ticket.get(ticket_id, 0) + _total_tokens(run)
        )
        dollars_by_ticket[ticket_id] = (
            dollars_by_ticket.get(ticket_id, Decimal("0")) + _dollar_cost(run)
        )
        for finding in _decode_findings(run.findings):
            if finding.get("harm") not in (None, "none"):
                harmful_by_ticket[ticket_id] = harmful_by_ticket.get(ticket_id, 0) + 1

    # Trusted rounds → rounds-to-first-clean, per ticket.
    comments = list(
        (
            await db.execute(
                select(TicketComment)
                .where(TicketComment.ticket_id.in_(done_ids))
                .order_by(TicketComment.created_at, TicketComment.id)
            )
        )
        .scalars()
        .all()
    )
    comments_by_ticket: dict[str, list[TicketComment]] = {}
    for c in comments:
        comments_by_ticket.setdefault(c.ticket_id, []).append(c)
    rounds_by_ticket: dict[str, int | None] = {}
    for ticket_id, crows in comments_by_ticket.items():
        state = compute_review_state(
            [
                {
                    "id": c.id,
                    "author_persona": c.author_persona,
                    "content": c.content,
                    "created_at": c.created_at,
                    "verdict_trusted": c.verdict_trusted,
                }
                for c in crows
            ]
        )
        clean_at: int | None = None
        if state is not None:
            for index, rnd in enumerate(state.rounds):
                if rnd.verdict == VERDICT_CLEAN:
                    clean_at = index + 1
                    break
        rounds_by_ticket[ticket_id] = clean_at

    groups: dict[tuple[str | None, str | None], dict[str, Any]] = {}
    for ticket_id in done_ids:
        key = (impl_by_ticket.get(ticket_id), rev_by_ticket.get(ticket_id))
        g = groups.setdefault(
            key,
            {
                "implementer_model": key[0],
                "reviewer_model": key[1],
                "accepted_changes": 0,
                "harmful_findings": 0,
                "clean_tickets": 0,
                "clean_rounds": 0,
                "tokens": 0,
                "cost_usd": Decimal("0"),
            },
        )
        g["accepted_changes"] += 1
        g["harmful_findings"] += harmful_by_ticket.get(ticket_id, 0)
        g["tokens"] += tokens_by_ticket.get(ticket_id, 0)
        g["cost_usd"] += dollars_by_ticket.get(ticket_id, Decimal("0"))
        if rounds_by_ticket.get(ticket_id) is not None:
            g["clean_tickets"] += 1
            g["clean_rounds"] += rounds_by_ticket[ticket_id]

    rows: list[dict[str, Any]] = []
    for key in sorted(groups, key=lambda k: (str(k[0]), str(k[1]))):
        g = groups[key]
        accepted = g["accepted_changes"]
        row: dict[str, Any] = {
            "implementer_model": g["implementer_model"],
            "reviewer_model": g["reviewer_model"],
            "accepted_changes": accepted,
            "harmful_findings_per_accepted_change": (
                round(g["harmful_findings"] / accepted, 2) if accepted else 0.0
            ),
            "avg_rounds_to_clean": (
                round(g["clean_rounds"] / g["clean_tickets"], 2)
                if g["clean_tickets"]
                else None
            ),
            "cost_per_accepted_change": {
                "tokens": round(g["tokens"] / accepted, 2) if accepted else 0.0,
            },
        }
        if show_dollars:
            row["cost_per_accepted_change"]["cost_usd"] = (
                round(float(g["cost_usd"]) / accepted, _COST_USD_SCALE)
                if accepted
                else 0.0
            )
        rows.append(row)
    return rows


async def loop_insights(
    db: AsyncSession,
    *,
    project_id: str,
    from_dt: datetime,
    to_dt: datetime,
    show_dollars: bool,
) -> dict[str, Any]:
    """Project-wide insights: caught findings, cost per change, learning."""
    # Only the columns the rollup reads — the full ORM entity drags in every
    # token/cost column for no reason.
    runs = (
        await db.execute(
            select(
                AgentRun.id,
                AgentRun.ticket_id,
                AgentRun.occurred_at,
                AgentRun.created_at,
                AgentRun.findings,
            )
            .where(
                AgentRun.project_id == project_id,
                func.coalesce(AgentRun.occurred_at, AgentRun.created_at) >= from_dt,
                func.coalesce(AgentRun.occurred_at, AgentRun.created_at) < to_dt,
            )
            .order_by(
                func.coalesce(AgentRun.occurred_at, AgentRun.created_at), AgentRun.id
            )
        )
    ).all()

    caught_all: list[dict[str, Any]] = []
    learning_weeks: dict[str, dict[str, int]] = {}
    for run in runs:
        at = run.occurred_at or run.created_at
        week = _iso_monday(at)
        for finding in _decode_findings(run.findings):
            harm = finding.get("harm")
            fixed = (
                finding.get("status") == "fixed"
                or finding.get("fixed_in_sha") is not None
            )
            if harm not in (None, "none") and fixed:
                caught_all.append(_caught(run, finding))
            cls = finding.get("class")
            if cls is not None and str(cls) != "new":
                bucket = learning_weeks.setdefault(str(cls), {})
                bucket[week] = bucket.get(week, 0) + 1

    # `caught_all` is ascending (oldest first), so the most recent 100 is the
    # tail; `caught_total` keeps the full count so the cap never hides scale.
    caught_total = len(caught_all)
    caught = caught_all[-100:]

    cost = await _cost_per_accepted_change(
        db, project_id=project_id, from_dt=from_dt, to_dt=to_dt, show_dollars=show_dollars
    )

    scoreboard = await _scoreboard(
        db,
        project_id=project_id,
        from_dt=from_dt,
        to_dt=to_dt,
        show_dollars=show_dollars,
    )

    checklist = await _load_checklist_items(db, project_id)
    learning: list[dict[str, Any]] = []
    for cls in sorted(learning_weeks):
        item = checklist.get(cls)
        learning.append(
            {
                "class": cls,
                "title": item["title"] if item else cls,
                "joined_at": item["joined_at"] if item else None,
                "series": _weekly_series(learning_weeks[cls], from_dt, to_dt),
            }
        )

    return {
        "caught": caught,
        "caught_total": caught_total,
        "cost_per_accepted_change": cost,
        "learning": learning,
        "scoreboard": scoreboard,
    }


# ── ticket story ───────────────────────────────────────────────────

_KIND_ORDER = {
    "brief": 0,
    "run": 1,
    "round": 2,
    "finding": 3,
    "fix": 4,
    "verdict": 5,
    "merge": 6,
}


async def ticket_story(
    db: AsyncSession, *, project_id: str, ticket: Ticket, show_dollars: bool
) -> dict[str, Any]:
    """Ordered event stream for a ticket's loop history."""
    events: list[dict[str, Any]] = [
        {"kind": "brief", "at": _iso(ticket.created_at), "title": ticket.title}
    ]

    runs = (
        (
            await db.execute(
                select(AgentRun)
                .where(AgentRun.project_id == project_id, AgentRun.ticket_id == ticket.id)
                .order_by(AgentRun.created_at, AgentRun.id)
            )
        )
        .scalars()
        .all()
    )
    for run in runs:
        at = _iso(run.occurred_at or run.created_at)
        event: dict[str, Any] = {
            "kind": "run",
            "at": at,
            "run_id": run.id,
            "role": run.role,
            "tokens": _total_tokens(run),
        }
        if run.result_summary:
            event["title"] = run.result_summary
        if show_dollars:
            event["cost_usd"] = round(float(_dollar_cost(run)), _COST_USD_SCALE)
        events.append(event)

        for finding in _decode_findings(run.findings):
            title = str(finding.get("title") or finding.get("text") or "")
            round_no = _round_int(finding.get("round"))
            events.append(
                {
                    "kind": "finding",
                    "at": at,
                    "run_id": run.id,
                    "round": round_no,
                    "harm": finding.get("harm"),
                    "class": finding.get("class"),
                    "file": finding.get("file"),
                    "line": finding.get("line"),
                    "title": title,
                }
            )
            if (
                finding.get("status") == "fixed"
                or finding.get("fixed_in_sha") is not None
            ):
                events.append(
                    {
                        "kind": "fix",
                        "at": at,
                        "run_id": run.id,
                        "round": round_no,
                        "harm": finding.get("harm"),
                        "class": finding.get("class"),
                        "sha": finding.get("fixed_in_sha"),
                        "title": title,
                    }
                )

    # Review rounds + trusted verdicts from the comment thread.
    comments = (
        (
            await db.execute(
                select(TicketComment)
                .where(TicketComment.ticket_id == ticket.id)
                .order_by(TicketComment.created_at, TicketComment.id)
                .limit(500)
            )
        )
        .scalars()
        .all()
    )
    meta_by_id = {
        c.id: {"author_persona": c.author_persona, "reviewed_commit": c.reviewed_commit}
        for c in comments
    }
    state = compute_review_state(
        [
            {
                "id": c.id,
                "author_persona": c.author_persona,
                "content": c.content,
                "created_at": c.created_at,
                "verdict_trusted": c.verdict_trusted,
            }
            for c in comments
        ]
    )
    if state is not None:
        for rnd in state.rounds:
            meta = meta_by_id.get(rnd.comment_id, {})
            events.append(
                {
                    "kind": "round",
                    "at": _iso(rnd.timestamp),
                    "round": rnd.round,
                    "title": rnd.verdict,
                    "reviewer": meta.get("author_persona"),
                }
            )
            if rnd.verdict == VERDICT_CLEAN:
                events.append(
                    {
                        "kind": "verdict",
                        "at": _iso(rnd.timestamp),
                        "reviewer": meta.get("author_persona"),
                        "sha": meta.get("reviewed_commit"),
                        "title": rnd.verdict,
                    }
                )

    if ticket.resolved_at is not None:
        events.append(
            {"kind": "merge", "at": _iso(ticket.resolved_at), "title": "Merged"}
        )

    events.sort(key=lambda e: (e["at"] or "", _KIND_ORDER.get(e["kind"], 99)))

    # Shipped context (track F1): PRs linked to this ticket, the releases that
    # carried them, and where those releases deployed. Fixed-size queries only.
    pr_ids = list(
        (
            await db.execute(
                select(TicketPrLink.pr_id).where(TicketPrLink.ticket_id == ticket.id)
            )
        )
        .scalars()
        .all()
    )
    prs: list[dict[str, Any]] = []
    releases_out: list[dict[str, Any]] = []
    deployments_out: list[dict[str, Any]] = []
    if pr_ids:
        pr_rows = list(
            (
                await db.execute(
                    select(PullRequest)
                    .where(
                        PullRequest.project_id == project_id,
                        PullRequest.id.in_(pr_ids),
                    )
                    .order_by(PullRequest.created_at, PullRequest.id)
                )
            )
            .scalars()
            .all()
        )
        prs = [
            {
                "id": p.id,
                "provider": p.provider,
                "repo": p.repo,
                "number": p.number,
                "title": p.title,
                "state": p.state,
                "branch": p.branch,
                "head_sha": p.head_sha,
                "merged_at": _iso(p.merged_at),
                "merge_commit_sha": p.merge_commit_sha,
                "external_url": p.external_url,
            }
            for p in pr_rows
        ]
        release_ids = list(
            (
                await db.execute(
                    select(ReleasePr.release_id).where(ReleasePr.pr_id.in_(pr_ids))
                )
            )
            .scalars()
            .all()
        )
        if release_ids:
            rel_rows = list(
                (
                    await db.execute(
                        select(Release)
                        .where(
                            Release.project_id == project_id,
                            Release.id.in_(release_ids),
                        )
                        .order_by(Release.released_at, Release.id)
                    )
                )
                .scalars()
                .all()
            )
            releases_out = [
                {"version": r.version, "released_at": _iso(r.released_at)}
                for r in rel_rows
            ]
            dep_rows = list(
                (
                    await db.execute(
                        select(Deployment)
                        .where(Deployment.release_id.in_([r.id for r in rel_rows]))
                        .order_by(Deployment.deployed_at, Deployment.id)
                    )
                )
                .scalars()
                .all()
            )
            deployments_out = [
                {
                    "environment": d.environment,
                    "deployed_at": _iso(d.deployed_at),
                    "source": d.source,
                }
                for d in dep_rows
            ]

    return {
        "ticket_id": ticket.id,
        "title": ticket.title,
        "events": events,
        "prs": prs,
        "releases": releases_out,
        "deployments": deployments_out,
    }
