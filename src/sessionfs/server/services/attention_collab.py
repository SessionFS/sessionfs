"""For You → "Working with others" (tk_dc118ffd6e804a9a, F2).

Per project the caller is active in (they hold in-flight tickets there), the
people and agent personas they work with lately, and which of their tickets
are waiting on someone else:

- **waiting for a reviewer**: the caller's ticket is in ``review``. The
  reviewer is the author of the latest trusted review round
  (``review_state``), else the person who represents the loop's reviewer
  persona; the round, last verdict and time in review come with it.
- **blocked by a dependency**: a ``depends_on`` ticket that is not done or
  cancelled. Its owner (assignee, the person representing its persona, else
  its creator), status and age come with it, plus one more hop when the
  blocker is itself waiting on something.

Every query is grouped (never one per ticket) and limited to projects the
caller can read whose plan has tickets. People are shown by display name or
a masked email; no user ids leave the server. A flow is addressed by an
opaque ``flow_key``; "Send a reminder" (``routes/attention``) rebuilds the
feed and resolves the recipient server-side from that key, so the client can
never pick who is reminded.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, or_, select

from sessionfs.server.db.models import (
    AgentPersona,
    AgentRun,
    OrgMember,
    ProjectGroup,
    ProjectGroupAccess,
    ProjectGroupMember,
    Ticket,
    TicketComment,
    TicketDependency,
    TicketEvent,
)
from sessionfs.server.roles import ADMIN_ROLES
from sessionfs.server.services import attention as att

IN_FLIGHT = ("open", "in_progress", "blocked", "review")
DONE = ("done", "cancelled")
COLLAB_WINDOW = timedelta(days=30)
MY_TICKET_CAP = 300
VERDICTS_PER_TICKET = 20
COLLAB_ROW_CAP = 200
PROJECT_CAP = 6
PEOPLE_CAP = 8
FLOWS_PER_PROJECT = 6
STALE_AFTER = timedelta(days=3)


@dataclass
class Flow:
    """One "your work waits on someone" row. ``recipient_user_id`` and
    ``source_ticket_id`` stay server-side (the remind route reads them)."""

    key: str
    project_id: str
    body: dict[str, Any]
    recipient_user_id: str | None
    source_ticket_id: str


async def _first_blockers(db, cols, waiter_ids: list[str], pids: list[str]) -> list[Any]:
    """The first unfinished dependency (oldest, then id) of each waiter: a
    window function keeps one row per waiter in SQL."""
    if not waiter_ids:
        return []
    rn = (
        func.row_number()
        .over(
            partition_by=TicketDependency.ticket_id,
            order_by=(Ticket.created_at, Ticket.id),
        )
        .label("rn")
    )
    inner = (
        select(*cols, rn)
        .join(Ticket, Ticket.id == TicketDependency.depends_on_id)
        .where(
            TicketDependency.ticket_id.in_(waiter_ids),
            Ticket.status.not_in(DONE),
            Ticket.project_id.in_(pids),
        )
        .subquery()
    )
    return list(
        (await db.execute(select(inner).where(inner.c.rn == 1).order_by(inner.c.waiter_id))).all()
    )


def flow_key_for(kind: str, *parts: str) -> str:
    digest = hashlib.sha256("\x00".join((kind, *parts)).encode()).hexdigest()
    return f"fyc_{digest[:40]}"


def _first(name: str) -> str:
    return (name or "").split(" ")[0] or name


def _loads(raw: str | None) -> dict[str, Any]:
    try:
        value = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


async def _flows_and_people(ctx: att._Ctx) -> tuple[list[Flow], dict[str, dict[str, Any]]]:
    db = ctx.db
    pids = await ctx.feature_project_ids("agent_tickets")
    if not pids or ctx.scope.is_service:
        return [], {}
    by_project, by_org = await ctx.personas()
    by_project = {p: n for p, n in by_project.items() if p in set(pids)}

    mine = list(
        (
            await db.execute(
                select(
                    Ticket.id,
                    Ticket.project_id,
                    Ticket.title,
                    Ticket.status,
                    Ticket.parent_ticket_id,
                    Ticket.created_by_user_id,
                    Ticket.updated_at,
                )
                .where(
                    Ticket.project_id.in_(pids),
                    Ticket.status.in_(IN_FLIGHT),
                    att._my_ticket_clause(by_project, ctx.user_id),
                )
                .order_by(Ticket.updated_at.desc(), Ticket.id)
                .limit(MY_TICKET_CAP)
            )
        ).all()
    )
    if not mine:
        return [], {}
    my_ids = [t.id for t in mine]
    active_pids = sorted({t.project_id for t in mine})

    blocker_cols = (
        TicketDependency.ticket_id.label("waiter_id"),
        Ticket.id,
        Ticket.project_id,
        Ticket.title,
        Ticket.status,
        Ticket.assigned_to,
        Ticket.assigned_user_id,
        Ticket.created_by_user_id,
        Ticket.created_at,
        Ticket.updated_at,
    )
    hop1 = await _first_blockers(db, blocker_cols, my_ids, pids)
    blockers = {b.id: b for b in hop1}
    hop2_rows = []
    if blockers:
        hop2_rows = await _first_blockers(db, blocker_cols, sorted(blockers), pids)
    hop2: dict[str, Any] = {}
    for b in hop2_rows:
        hop2.setdefault(b.waiter_id, b)

    review_ids = [t.id for t in mine if t.status == "review"]
    since_ids = sorted(set(my_ids) | set(blockers) | {b.id for b in hop2.values()})
    status_since = dict(
        (
            await db.execute(
                select(TicketEvent.ticket_id, func.max(TicketEvent.created_at))
                .where(TicketEvent.ticket_id.in_(since_ids), TicketEvent.field == "status")
                .group_by(TicketEvent.ticket_id)
            )
        ).all()
    )

    comments: dict[str, list[dict[str, Any]]] = {}
    if review_ids:
        # Only trusted verdict comments count toward rounds; the newest
        # VERDICTS_PER_TICKET per ticket, needed columns only.
        rn = (
            func.row_number()
            .over(
                partition_by=TicketComment.ticket_id,
                order_by=(TicketComment.created_at.desc(), TicketComment.id.desc()),
            )
            .label("rn")
        )
        inner = (
            select(
                TicketComment.id,
                TicketComment.ticket_id,
                TicketComment.author_persona,
                TicketComment.author_user_id,
                TicketComment.content,
                TicketComment.created_at,
                rn,
            )
            .where(TicketComment.ticket_id.in_(review_ids), TicketComment.verdict_trusted.is_(True))
            .subquery()
        )
        for c in (
            await db.execute(
                select(inner)
                .where(inner.c.rn <= VERDICTS_PER_TICKET)
                .order_by(inner.c.ticket_id, inner.c.created_at, inner.c.id)
            )
        ).all():
            comments.setdefault(c.ticket_id, []).append(
                {
                    "id": c.id,
                    "author_persona": c.author_persona,
                    "content": c.content,
                    "created_at": c.created_at,
                    "verdict_trusted": True,
                    "author_user_id": c.author_user_id,
                }
            )

    parent_ids = sorted({t.parent_ticket_id for t in mine if t.parent_ticket_id and t.status == "review"})
    loop_reviewer: dict[str, str] = {}
    if parent_ids:
        for pid_, cfg in (
            await db.execute(
                select(Ticket.id, Ticket.loop_config).where(
                    Ticket.id.in_(parent_ids), Ticket.loop_config.is_not(None)
                )
            )
        ).all():
            name = _loads(cfg).get("reviewer_persona")
            if isinstance(name, str) and name.strip():
                loop_reviewer[pid_] = name.strip()

    # Persona -> represented person, for blocker assignees and loop reviewers.
    persona_names = {b.assigned_to.lower() for b in blockers.values() if b.assigned_to}
    persona_names |= {b.assigned_to.lower() for b in hop2.values() if b.assigned_to}
    persona_names |= {n.lower() for n in loop_reviewer.values()}
    represented: dict[tuple[str, str], str] = {}
    if persona_names:
        org_ids = sorted({o for p in active_pids if (o := ctx.projects[p].org_id)})
        conds = [AgentPersona.project_id.in_(active_pids)]
        if org_ids:
            conds.append(AgentPersona.org_id.in_(org_ids))
        org_level: dict[tuple[str, str], str] = {}
        for p_id, o_id, name, uid in (
            await db.execute(
                select(
                    AgentPersona.project_id,
                    AgentPersona.org_id,
                    AgentPersona.name,
                    AgentPersona.represented_user_id,
                ).where(
                    or_(*conds),
                    AgentPersona.is_active.is_(True),
                    func.lower(AgentPersona.name).in_(sorted(persona_names)),
                )
            )
        ).all():
            if p_id:
                represented[(p_id, name.lower())] = uid or ""
            elif o_id and uid:
                org_level[(o_id, name.lower())] = uid
        for p in active_pids:
            org = ctx.projects[p].org_id
            for (o_id, lname), uid in org_level.items():
                if o_id == org:
                    represented.setdefault((p, lname), uid)

    def persona_user(project_id: str, name: str | None) -> str | None:
        if not name:
            return None
        return represented.get((project_id, name.lower())) or None

    # Recent collaborators: agent runs and comment authors on my tickets.
    since = ctx.now - COLLAB_WINDOW
    run_at = func.coalesce(AgentRun.occurred_at, AgentRun.created_at)
    runs = (
        await db.execute(
            select(AgentRun.project_id, AgentRun.persona_name, AgentRun.role, func.max(run_at))
            .where(
                AgentRun.ticket_id.in_(my_ids),
                AgentRun.project_id.in_(active_pids),
                run_at >= since,
            )
            .group_by(AgentRun.project_id, AgentRun.persona_name, AgentRun.role)
            .order_by(func.max(run_at).desc())
            .limit(COLLAB_ROW_CAP)
        )
    ).all()
    authors = (
        await db.execute(
            select(
                Ticket.project_id,
                TicketComment.author_user_id,
                TicketComment.author_persona,
                func.max(TicketComment.created_at),
            )
            .join(Ticket, Ticket.id == TicketComment.ticket_id)
            .where(
                TicketComment.ticket_id.in_(my_ids),
                TicketComment.created_at >= since,
                TicketComment.author_user_id != ctx.user_id,
            )
            .group_by(Ticket.project_id, TicketComment.author_user_id, TicketComment.author_persona)
            .order_by(func.max(TicketComment.created_at).desc())
            .limit(COLLAB_ROW_CAP)
        )
    ).all()

    # ── flows ──
    flows: list[Flow] = []
    recipients: set[str] = set()
    pending: list[tuple[str, dict[str, Any], str | None, str, str, str]] = []

    def since_of(ticket_id: str, fallback: datetime | None) -> str | None:
        return att._iso(status_since.get(ticket_id) or fallback)

    first_dep: dict[str, Any] = {}
    for b in hop1:
        first_dep.setdefault(b.waiter_id, b)
    for t in mine:
        b = first_dep.get(t.id)
        if b is not None:
            owner = b.assigned_user_id or persona_user(b.project_id, b.assigned_to) or b.created_by_user_id
            owner_role = "owner" if (b.assigned_user_id or persona_user(b.project_id, b.assigned_to)) else "created it"
            chain = [
                {"kind": "mine", "ref": t.id, "title": att._cap(t.title, 80), "status": t.status,
                 "who_user": ctx.user_id, "who_persona": None, "since": since_of(t.id, t.updated_at)},
                {"kind": "dep", "ref": b.id, "title": att._cap(b.title, 80), "status": b.status,
                 "who_user": owner, "who_persona": b.assigned_to, "since": since_of(b.id, b.updated_at)},
            ]
            nxt = hop2.get(b.id)
            if nxt is not None:
                n_owner = nxt.assigned_user_id or persona_user(nxt.project_id, nxt.assigned_to) or nxt.created_by_user_id
                chain.append(
                    {"kind": "dep2", "ref": nxt.id, "title": att._cap(nxt.title, 80), "status": nxt.status,
                     "who_user": n_owner, "who_persona": nxt.assigned_to,
                     "since": since_of(nxt.id, nxt.updated_at)}
                )
            body: dict[str, Any] = {
                "state": "dependency",
                "ticket": {"id": t.id, "title": att._cap(t.title), "status": t.status,
                           "link": att._project_link(t.project_id, tab="work", sub="tickets", ticket=t.id)},
                "blocking": {"id": b.id, "title": att._cap(b.title), "status": b.status,
                             "since": since_of(b.id, b.updated_at), "owner_role": owner_role,
                             "link": att._project_link(b.project_id, tab="work", sub="tickets", ticket=b.id)},
                "since": since_of(b.id, b.updated_at),
                "chain": chain,
            }
            key = flow_key_for("dep", t.id, b.id)
            pending.append((key, body, owner, b.id, t.project_id, "dep"))
            if owner:
                recipients.add(owner)
        if t.status == "review":
            state = att_review_state(comments.get(t.id, []))
            reviewer_user: str | None = None
            reviewer_persona: str | None = None
            if state is not None and state["reviewer_user_id"]:
                reviewer_user = state["reviewer_user_id"]
                reviewer_persona = state["reviewer_persona"]
            elif t.parent_ticket_id in loop_reviewer:
                reviewer_persona = loop_reviewer[t.parent_ticket_id]
                reviewer_user = persona_user(t.project_id, reviewer_persona)
            body = {
                "state": "review",
                "ticket": {"id": t.id, "title": att._cap(t.title), "status": t.status,
                           "link": att._project_link(t.project_id, tab="work", sub="tickets", ticket=t.id)},
                "round": state["next_round"] if state else 1,
                "last_verdict": state["last_verdict"] if state else None,
                "open_findings": state["open_findings"] if state else 0,
                "reviewer_persona": att._cap(reviewer_persona, 50) if reviewer_persona else None,
                "since": since_of(t.id, t.updated_at),
                "chain": [
                    {"kind": "mine", "ref": t.id, "title": att._cap(t.title, 80), "status": t.status,
                     "who_user": ctx.user_id, "who_persona": None, "since": since_of(t.id, t.updated_at)},
                    {"kind": "rev", "ref": f"Round {state['next_round'] if state else 1}",
                     "title": "Review", "status": "requested",
                     "who_user": reviewer_user, "who_persona": reviewer_persona,
                     "since": since_of(t.id, t.updated_at)},
                ],
            }
            key = flow_key_for("review", t.id)
            pending.append((key, body, reviewer_user, t.id, t.project_id, "review"))
            if reviewer_user:
                recipients.add(reviewer_user)

    people_ids = set(recipients)
    people_ids |= {a[1] for a in authors if a[1]}
    people_ids |= {t.created_by_user_id for t in mine if t.created_by_user_id}
    for _, body, *_rest in pending:
        for node in body["chain"]:
            if node.get("who_user"):
                people_ids.add(node["who_user"])
    people_ids.discard(ctx.user_id)
    people_ids.discard("")
    names = await att._user_names(ctx, sorted(people_ids))
    names[ctx.user_id] = "You"

    def who(user_id: str | None, persona: str | None) -> dict[str, Any]:
        person = names.get(user_id or "") if user_id else None
        label = person or (att._cap(persona, 50) if persona else "Nobody yet")
        if persona and person and user_id != ctx.user_id:
            label = f"{att._cap(persona, 40)} ({person})"
        return {"name": label, "agent": bool(persona) and not person, "you": user_id == ctx.user_id}

    for key, body, recipient, source_ticket, project_id, kind in pending:
        for node in body["chain"]:
            node["who"] = who(node.pop("who_user"), node.pop("who_persona"))
            since_dt = node.get("since")
            node["stale"] = bool(
                node["kind"] in ("dep", "dep2")
                and node["status"] == "open"
                and since_dt
                and ctx.now - (att._aware(datetime.fromisoformat(since_dt)) or ctx.now) >= STALE_AFTER
            )
        to_name = names.get(recipient or "") if recipient else None
        can_remind = bool(recipient) and recipient != ctx.user_id and to_name is not None
        body["to"] = {"name": to_name, "first": _first(to_name or "")} if to_name else None
        body["can_remind"] = can_remind
        first = _first(to_name or "there")
        my_ref = body["ticket"]["id"]
        if kind == "dep":
            blk = body["blocking"]
            body["label"] = f"Waiting on {blk['id']} to be done first"
            body["default_message"] = (
                f"Hi {first}, {my_ref} ({body['ticket']['title']}) is waiting on {blk['id']}, "
                f"{blk['title']}. Is it still on track? Happy to help if something is in the way. Thanks!"
            )
        else:
            body["label"] = "Waiting for a reviewer"
            body["default_message"] = (
                f"Hi {first}, {my_ref} ({body['ticket']['title']}) is ready for review round "
                f"{body['round']}. Could you take a look when you get a chance? Thanks!"
            )
        body["flow_key"] = key
        flows.append(Flow(key=key, project_id=project_id, body=body,
                          recipient_user_id=recipient if can_remind else None,
                          source_ticket_id=source_ticket))

    # ── collaborators per project ──
    people: dict[str, dict[str, dict[str, Any]]] = {p: {} for p in active_pids}

    def add(pid: str, key: str, name: str, role: str, agent: bool) -> None:
        bucket = people.setdefault(pid, {})
        if key not in bucket and len(bucket) < PEOPLE_CAP * 2:
            bucket[key] = {"name": att._cap(name, 60), "role": att._cap(role, 60), "agent": agent, "you": False}

    for f in flows:
        for node in f.body["chain"][1:]:
            w = node["who"]
            if not w["you"] and w["name"] != "Nobody yet":
                add(f.project_id, f"n:{w['name'].lower()}",
                    w["name"], "reviewer" if node["kind"] == "rev" else f"owns {node['ref']}", w["agent"])
    for pid, persona, role, _at in runs:
        if persona:
            add(pid, f"a:{persona.lower()}", persona,
                "agent, reviews" if role == "reviewer" else ("agent, implements" if role == "implementer" else "agent"), True)
    for pid, uid, persona, _at in authors:
        if persona:
            add(pid, f"a:{persona.lower()}", persona, "agent, commented", True)
        elif uid and uid in names:
            add(pid, f"n:{names[uid].lower()}", names[uid], "commented", False)
    for t in mine:
        if t.created_by_user_id and t.created_by_user_id != ctx.user_id and t.created_by_user_id in names:
            add(t.project_id, f"n:{names[t.created_by_user_id].lower()}", names[t.created_by_user_id],
                "created your ticket", False)
    return flows, people


def att_review_state(comments: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Round / verdict / reviewer from the trusted review rounds on one
    ticket (``review_state.compute_review_state``)."""
    from sessionfs.server.services.review_state import compute_review_state

    if not comments:
        return None
    state = compute_review_state(comments)
    if state is None or not state.rounds:
        return None
    last = state.rounds[-1]
    author = next((c for c in comments if c["id"] == last.comment_id), None)
    return {
        "next_round": last.round + 1,
        "last_verdict": last.verdict,
        "open_findings": len(state.open_findings),
        "reviewer_user_id": author["author_user_id"] if author else None,
        "reviewer_persona": author["author_persona"] if author else None,
    }


async def _visible_group_names(ctx: att._Ctx, pids: list[str]) -> dict[str, str]:
    """First live group name per project, only for groups the caller can see
    (an explicit grant, or an org admin)."""
    if not pids:
        return {}
    rows = (
        await ctx.db.execute(
            select(ProjectGroupAccess.project_id, ProjectGroup.id, ProjectGroup.org_id, ProjectGroup.name)
            .join(ProjectGroup, ProjectGroup.id == ProjectGroupAccess.group_id)
            .where(ProjectGroupAccess.project_id.in_(pids), ProjectGroup.deleted_at.is_(None))
            .order_by(ProjectGroupAccess.project_id, ProjectGroup.name)
        )
    ).all()
    if not rows:
        return {}
    member_of = set(
        (
            await ctx.db.execute(
                select(ProjectGroupMember.group_id).where(
                    ProjectGroupMember.user_id == ctx.user_id,
                    ProjectGroupMember.group_id.in_(sorted({r[1] for r in rows})),
                )
            )
        ).scalars().all()
    )
    admin_orgs = set(
        (
            await ctx.db.execute(
                select(OrgMember.org_id).where(
                    OrgMember.user_id == ctx.user_id,
                    OrgMember.org_id.in_(sorted({r[2] for r in rows})),
                    OrgMember.role.in_(ADMIN_ROLES),
                )
            )
        ).scalars().all()
    )
    out: dict[str, str] = {}
    for pid, gid, org_id, name in rows:
        if pid not in out and (gid in member_of or org_id in admin_orgs):
            out[pid] = att._cap(name, 60)
    return out


async def collaboration_flows(ctx: att._Ctx) -> list[Flow]:
    flows, _ = await _flows_and_people(ctx)
    return flows


async def provide_collaboration(ctx: att._Ctx) -> dict[str, Any]:
    """The "Working with others" block of ``GET /me/for-you``."""
    flows, people = await _flows_and_people(ctx)
    if not people and not flows:
        return {"projects": [], "waiting": 0}
    by_project: dict[str, list[Flow]] = {}
    for f in flows:
        by_project.setdefault(f.project_id, []).append(f)
    # Projects with waiting work first, then by collaborators, then name.
    order = sorted(
        people,
        key=lambda p: (-len(by_project.get(p, [])), -len(people[p]), ctx.projects[p].name.lower(), p),
    )
    order = [p for p in order if by_project.get(p) or people[p]][:PROJECT_CAP]
    groups = await _visible_group_names(ctx, order)
    projects: list[dict[str, Any]] = []
    for pid in order:
        members = list(people[pid].values())
        # People before agents, each in first-seen order.
        members = [m for m in members if not m["agent"]] + [m for m in members if m["agent"]]
        members = members[:PEOPLE_CAP]
        pflows = by_project.get(pid, [])[:FLOWS_PER_PROJECT]
        projects.append(
            {
                "project_id": pid,
                "project_name": att._cap(ctx.projects[pid].name, 80),
                "group_name": groups.get(pid),
                "people": members,
                "people_count": sum(1 for m in members if not m["agent"]),
                "agent_count": sum(1 for m in members if m["agent"]),
                "flows": [f.body for f in pflows],
            }
        )
    waiting = sum(len(p["flows"]) for p in projects)
    return {"projects": projects, "waiting": waiting}


__all__ = [
    "Flow",
    "collaboration_flows",
    "flow_key_for",
    "provide_collaboration",
]
