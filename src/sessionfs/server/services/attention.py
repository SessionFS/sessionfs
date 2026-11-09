"""For You: one ranked list of what needs a person (tk_dc118ffd6e804a9a).

A read-time aggregator. Nothing is copied: every request asks each source's
provider for the caller's items, using that source's own read predicate
recomputed on this request (``list_accessible_projects``, group visibility,
reminder privacy, approver eligibility). Losing access removes the card on
the next poll. There is no cross-request cache.

Item model (shared by REST, MCP, the CLI and the helper)::

    {item_key, kind, band, involvement, occurred_at, due_at, title, why,
     link, project_id, actions[], source{type, id}, detail?}

- ``item_key`` is opaque (``fy_`` + a sha256 of kind and source id).
- ``band`` is ``now`` | ``your_turn`` | ``lately``.
- Ranking is deterministic: band, then involvement (owner/assignee/approver
  before editor before watcher before member), then urgency (overdue, then
  due soonest, then harm, then priority), then recency, then the key.

Providers each issue a fixed number of grouped queries (never one per item)
and run under a time limit inside a SAVEPOINT, so a slow or failing source
yields ``sections[name].error = "couldn't load"`` while the feed still
answers.

Hides (``attention_hides``) apply only while the stored ``version_key``
matches the item's live version and ``hidden_until`` (when set) is in the
future; when the source changes, the item comes back. Hiding a key that is
not in the caller's live feed is a silent no-op, so the endpoint is never an
existence oracle.

Service keys get their minter's feed limited to the key's org and project
allowlist (``AttentionScope``). Group-scoped meetings reach a service key
only when it has no project allowlist and the group is in the key's org.
Dollars (proof card) only ever reach org owners/admins (or the owner of a
personal project) on a user key.
"""

from __future__ import annotations

import asyncio
import hashlib
import importlib
import json
import logging
import re
import time
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, Awaitable, Callable
from urllib.parse import quote, urlencode
from zoneinfo import ZoneInfo

from sqlalchemy import and_, case, delete, func, inspect, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server.auth.project_access import (
    list_accessible_projects,
    project_session_clause,
    user_can_access_project,
)
from sessionfs.server.db.timeouts import bound_heavy_reads
from sessionfs.server.db.models import (
    AgentPersona,
    AgentRun,
    AttentionHide,
    KnowledgeEntry,
    MeetingActionItem,
    MeetingParticipant,
    MeetingTranscript,
    OrgMember,
    Project,
    ProjectGroup,
    ProjectGroupAccess,
    Reminder,
    Session,
    Ticket,
    TicketComment,
    Handoff,
    User,
)
from sessionfs.server.roles import ADMIN_ROLES
from sessionfs.server.tier_gate import resolve_project_plan_tiers
from sessionfs.server.tiers import Tier, get_features_for_tier

logger = logging.getLogger("sessionfs.attention")

BANDS: tuple[str, ...] = ("now", "your_turn", "upcoming", "lately")
_BAND_RANK = {b: i for i, b in enumerate(BANDS)}

# Display sections of the For You page, in page order. Every item carries
# one; the band still drives ranking (and the MCP/CLI/helper views).
GROUPS: tuple[str, ...] = (
    "radar",  # On your radar: harmful, overdue, blocking
    "waiting",  # Waiting on you
    "asking",  # Your team is asking
    "meetings",  # From your meetings and chats
    "coming",  # Coming up
    "second",  # Worth a second look
    "lately",  # What changed this week
)
ACTIONABLE_GROUPS: frozenset[str] = frozenset({"radar", "waiting", "asking"})
_DEFAULT_GROUP = {"now": "radar", "your_turn": "waiting", "upcoming": "coming", "lately": "lately"}
_INVOLVEMENT_RANK = {
    "owner": 0,
    "assignee": 0,
    "approver": 0,
    "editor": 1,
    "watcher": 2,
    "member": 3,
}
_PRIORITY_RANK = {"critical": 0, "high": 1, "medium": 2, "low": 3}

ITEM_LIMIT = 100
# Safety bound on rows any one provider reads; the counts are exact below it.
PROVIDER_ROW_CAP = 1000
# Per-provider time limit (seconds). Module-level so tests can shrink it.
PROVIDER_TIMEOUT_S = 2.0
SECTION_ERROR = "couldn't load"

ACTIONABLE_TICKET_STATUSES: tuple[str, ...] = ("open", "in_progress", "blocked")
KNOWLEDGE_WINDOW = timedelta(days=7)
# "Coming up": due after today and within this many days.
UPCOMING_WINDOW = timedelta(days=7)
# "Worth a second look": no activity for this long.
QUIET_AFTER = timedelta(days=7)
QUESTION_WINDOW = timedelta(days=7)
HANDOFF_QUIET_AFTER = timedelta(days=2)
QUOTE_MAX = 280
# Meeting suggestion quotes read transcript text; at most this many distinct
# transcripts are loaded per request (the rest still link to their lines).
QUOTE_TRANSCRIPTS_MAX = 12
QUOTE_BYTES_MAX = 1_000_000
CHYRON_MAX = 12
PROOF_WINDOW = timedelta(days=7)
WORKED_IN_WINDOW = timedelta(days=30)
HIDE_RETENTION = timedelta(days=90)
HIDE_MAX_FUTURE = timedelta(days=365)
SWEEP_BATCH = 1000
SWEEP_MAX_BATCHES = 50

_DOLLAR_COST_BASES: tuple[str, ...] = ("reported", "estimated")
_MIN_DT = datetime.min.replace(tzinfo=timezone.utc)
_MAX_DT = datetime.max.replace(tzinfo=timezone.utc)


# ── Scope + item model ──


@dataclass(frozen=True)
class AttentionScope:
    """Whose feed, and (for a service key) the key's boundary."""

    user_id: str
    is_service: bool = False
    org_id: str | None = None
    project_ids: frozenset[str] | None = None
    scopes: frozenset[str] = frozenset()

    @classmethod
    def from_auth(cls, auth: Any) -> "AttentionScope":
        if getattr(auth, "key_kind", "user") != "service":
            return cls(user_id=auth.user.id)
        allow = auth.project_ids
        return cls(
            user_id=auth.user.id,
            is_service=True,
            org_id=auth.org_id,
            project_ids=frozenset(allow) if allow is not None else None,
            scopes=frozenset(auth.scopes or ()),
        )

    def allows_project(self, project: Project | None) -> bool:
        if project is None:
            return False
        if not self.is_service:
            return True
        if self.org_id is None or project.org_id != self.org_id:
            return False
        return self.project_ids is None or project.id in self.project_ids

    def allows_group(self, group_org_id: str | None) -> bool:
        if not self.is_service:
            return True
        return (
            self.project_ids is None
            and self.org_id is not None
            and group_org_id == self.org_id
        )


@dataclass
class Item:
    kind: str
    source_type: str
    source_id: str
    band: str
    involvement: str
    title: str
    why: str
    link: str
    version_key: str
    occurred_at: datetime | None = None
    due_at: datetime | None = None
    overdue: bool = False
    harm: bool = False
    priority: str | None = None
    project_id: str | None = None
    actions: list[str] = field(default_factory=lambda: ["snooze", "hide"])
    detail: dict[str, Any] | None = None
    section: str = ""
    # Display section (``GROUPS``); empty = the band's default.
    group: str = ""
    # Server-built card context: quote, facts, people, progress, related…
    context: dict[str, Any] | None = None
    # "Why it ranks here": one row per ranking factor (filled after sorting).
    rank: list[dict[str, Any]] | None = None
    # Sort-only tie-break (a provider's own order); never serialised, so the
    # response body (and its ETag) stays stable between polls.
    tiebreak: int = 0

    @property
    def display_group(self) -> str:
        return self.group or _DEFAULT_GROUP.get(self.band, "waiting")

    @property
    def item_key(self) -> str:
        return item_key_for(self.kind, self.source_id)

    def sort_key(self) -> tuple:
        occurred = _aware(self.occurred_at) or _MIN_DT
        return (
            _BAND_RANK.get(self.band, 9),
            _INVOLVEMENT_RANK.get(self.involvement, 9),
            0 if self.overdue else 1,
            _aware(self.due_at) or _MAX_DT,
            0 if self.harm else 1,
            _PRIORITY_RANK.get(self.priority or "", 9),
            -occurred.timestamp(),
            self.tiebreak,
            self.item_key,
        )

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "item_key": self.item_key,
            "kind": self.kind,
            "band": self.band,
            "involvement": self.involvement,
            "occurred_at": _iso(self.occurred_at),
            "due_at": _iso(self.due_at),
            "title": self.title,
            "why": self.why,
            "link": self.link,
            "project_id": self.project_id,
            "actions": list(self.actions),
            "source": {"type": self.source_type, "id": self.source_id},
            "group": self.display_group,
            "context": self.context or {},
            "rank": self.rank or [],
        }
        if self.detail is not None:
            out["detail"] = self.detail
        return out


# Item kind -> (key code, owning provider). The code is in the opaque key so
# hide and context can run only the provider that owns an item.
KIND_CODES: dict[str, tuple[str, str]] = {
    "ticket": ("tk", "tickets"),
    "reminder": ("rm", "reminders"),
    "loop_ready": ("lr", "loop_needs_you"),
    "loop_needs_human": ("lh", "loop_needs_you"),
    "loop_checklist": ("lc", "loop_needs_you"),
    "loop_harmful_findings": ("hf", "harmful_findings"),
    "meeting_action": ("ma", "conversation_actions"),
    "meeting_suggestion": ("ms", "conversation_actions"),
    "chat_action": ("ca", "conversation_actions"),
    "chat_suggestion": ("cs", "conversation_actions"),
    "ticket_question": ("tq", "questions"),
    "handoff": ("ho", "handoffs"),
    "knowledge_changes": ("kc", "knowledge_changes"),
    "proof": ("pf", "proof"),
}
_CODE_PROVIDER = {code: prov for code, prov in KIND_CODES.values()}


def item_key_for(kind: str, source_id: str) -> str:
    digest = hashlib.sha256(f"{kind}\x00{source_id}".encode()).hexdigest()
    code = KIND_CODES.get(kind, ("ap" if kind.startswith("approval") else "xx", ""))[0]
    return f"fy_{code}_{digest[:36]}"


def provider_for_key(item_key: str) -> str | None:
    """The provider that owns ``item_key`` (None when unknown: run all)."""
    parts = item_key.split("_", 2)
    if len(parts) != 3 or parts[0] != "fy":
        return None
    if parts[1] == "ap":
        return "approvals"
    return _CODE_PROVIDER.get(parts[1])


def _aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def _iso(dt: datetime | None) -> str | None:
    dt = _aware(dt)
    return dt.isoformat() if dt is not None else None


def _date_at(d: date) -> datetime:
    return datetime(d.year, d.month, d.day, tzinfo=timezone.utc)


def _cap(text: str | None, limit: int = 200) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _due_phrase(due: date | None, today: date) -> tuple[str | None, bool]:
    """('overdue' | 'due today' | 'due tomorrow' | 'due YYYY-MM-DD', overdue)."""
    if due is None:
        return None, False
    if due < today:
        return "overdue", True
    if due == today:
        return "due today", False
    if due == today + timedelta(days=1):
        return "due tomorrow", False
    return f"due {due.isoformat()}", False


def _project_link(project_id: str, **params: str) -> str:
    return f"/projects/{quote(project_id, safe='')}?{urlencode(params)}"


# ── Per-request context (never shared across requests) ──


@dataclass
class _Ctx:
    db: AsyncSession
    scope: AttentionScope
    now: datetime
    projects: dict[str, Project]
    # Per-request memo shared by every provider's copy of the context.
    memo: dict[str, Any] = field(default_factory=dict)

    @property
    def user_id(self) -> str:
        return self.scope.user_id

    @property
    def today(self) -> date:
        return self.now.date()

    async def feature_project_ids(self, feature: str) -> list[str]:
        tiers = self.memo.get("tiers")
        if tiers is None:
            tiers = self.memo["tiers"] = await resolve_project_plan_tiers(
                list(self.projects.values()), self.db
            )
        return sorted(
            pid
            for pid in self.projects
            if feature in get_features_for_tier(tiers.get(pid, Tier.FREE))
        )

    async def personas(self) -> tuple[dict[str, set[str]], dict[str, set[str]]]:
        """(per project id, per org id) lower-cased names of the active org
        personas that represent the caller, in orgs the caller belongs to.

        The project map drops names an ACTIVE project persona overrides (the
        same precedence as ``list_effective_personas``). Two grouped queries.
        """
        cached = self.memo.get("personas")
        if cached is not None:
            return cached
        by_org: dict[str, set[str]] = {}
        rows = (
            await self.db.execute(
                select(AgentPersona.org_id, AgentPersona.name)
                .join(
                    OrgMember,
                    and_(
                        OrgMember.org_id == AgentPersona.org_id,
                        OrgMember.user_id == self.user_id,
                    ),
                )
                .where(
                    AgentPersona.represented_user_id == self.user_id,
                    AgentPersona.is_active.is_(True),
                    AgentPersona.org_id.is_not(None),
                )
            )
        ).all()
        for org_id, name in rows:
            by_org.setdefault(org_id, set()).add(name.lower())
        by_project: dict[str, set[str]] = {}
        for pid, project in self.projects.items():
            names = by_org.get(project.org_id or "")
            if names:
                by_project[pid] = set(names)
        if by_project:
            all_names = sorted({n for names in by_project.values() for n in names})
            shadowed = (
                await self.db.execute(
                    select(AgentPersona.project_id, func.lower(AgentPersona.name)).where(
                        AgentPersona.project_id.in_(sorted(by_project)),
                        AgentPersona.is_active.is_(True),
                        func.lower(AgentPersona.name).in_(all_names),
                    )
                )
            ).all()
            for pid, lname in shadowed:
                by_project.get(pid, set()).discard(lname)
            by_project = {p: n for p, n in by_project.items() if n}
        self.memo["personas"] = (by_project, by_org)
        return by_project, by_org


def _persona_clause(column_project, column_name, by_project: dict[str, set[str]]):
    """OR over (project in P AND lower(name) in N), grouped by name set."""
    groups: dict[frozenset[str], list[str]] = {}
    for pid, names in by_project.items():
        groups.setdefault(frozenset(names), []).append(pid)
    return [
        and_(column_project.in_(sorted(pids)), func.lower(column_name).in_(sorted(names)))
        for names, pids in groups.items()
    ]


def _my_ticket_clause(by_project: dict[str, set[str]], user_id: str):
    return or_(
        Ticket.assigned_user_id == user_id,
        *_persona_clause(Ticket.project_id, Ticket.assigned_to, by_project),
    )


# ── Providers ──


def _json_list(raw: str | None) -> list:
    try:
        value = json.loads(raw or "[]")
    except (TypeError, ValueError):
        return []
    return value if isinstance(value, list) else []


def _people(*rows: tuple[str | None, str, bool, bool]) -> list[dict[str, Any]]:
    """[(name, role, agent, you)] -> people dicts, skipping empty names and
    repeats (first role wins)."""
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for name, role, agent, you in rows:
        if not name:
            continue
        key = name.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append({"name": _cap(name, 60), "role": _cap(role, 60), "agent": agent, "you": you})
    return out


async def provide_tickets(ctx: _Ctx) -> tuple[list[Item], int]:
    """Tickets assigned to the caller, or to a persona they represent, that
    are open / in progress / blocked, in readable projects whose plan has
    tickets.

    Display section: due after today and within a week = Coming up; no
    activity for a week (and not due soon) = Worth a second look; otherwise
    Waiting on you."""
    pids = await ctx.feature_project_ids("agent_tickets")
    if not pids:
        return [], 0
    by_project, _ = await ctx.personas()
    by_project = {p: n for p, n in by_project.items() if p in set(pids)}
    rows = (
        await ctx.db.execute(
            select(
                Ticket.id,
                Ticket.project_id,
                Ticket.title,
                Ticket.description,
                Ticket.status,
                Ticket.priority,
                Ticket.due_date,
                Ticket.assigned_to,
                Ticket.assigned_user_id,
                Ticket.created_by_persona,
                Ticket.acceptance_criteria,
                Ticket.created_at,
                Ticket.updated_at,
            )
            .where(
                Ticket.project_id.in_(pids),
                Ticket.status.in_(ACTIONABLE_TICKET_STATUSES),
                _my_ticket_clause(by_project, ctx.user_id),
            )
            .order_by(Ticket.updated_at.desc(), Ticket.id)
            .limit(PROVIDER_ROW_CAP)
        )
    ).all()
    items: list[Item] = []
    for r in rows:
        direct = r.assigned_user_id == ctx.user_id
        who = (
            "Assigned to you"
            if direct
            else f"Assigned to {_cap(r.assigned_to, 50)} (you)"
        )
        due_text, overdue = _due_phrase(r.due_date, ctx.today)
        parts = [who]
        if due_text:
            parts.append(due_text)
        if r.priority in ("critical", "high"):
            parts.append(f"priority {r.priority}")
        if r.status == "blocked":
            parts.append("blocked")
        elif r.status == "in_progress":
            parts.append("in progress")
        updated = _aware(r.updated_at)
        quiet_days = (
            (ctx.now - updated).days if updated is not None and ctx.now - updated >= QUIET_AFTER else None
        )
        band, group = "your_turn", "waiting"
        if overdue:
            group = "radar"
        elif r.due_date is not None and ctx.today < r.due_date <= ctx.today + UPCOMING_WINDOW:
            band, group = "upcoming", "coming"
        elif quiet_days is not None and not overdue:
            group = "second"
            parts.append(f"no activity for {quiet_days} days")
        criteria = _json_list(r.acceptance_criteria)
        facts = [["Status", r.status.replace("_", " ")], ["Priority", r.priority]]
        if r.due_date:
            facts.append(["Due", r.due_date.isoformat()])
        if r.created_at:
            facts.append(["Created", _iso(r.created_at) or ""])
        context: dict[str, Any] = {
            "source_label": "Ticket",
            "ref": r.id,
            "project_name": _cap(ctx.projects[r.project_id].name, 80),
            "facts": facts,
            "people": _people(
                ("You", "assignee" if direct else f"represent {r.assigned_to}", False, True),
                (r.assigned_to if not direct else None, "persona, assignee", True, False),
                (r.created_by_persona, "persona, created it", True, False),
            ),
            "ticket": {"id": r.id, "project_id": r.project_id, "status": r.status},
        }
        description = _cap(r.description, QUOTE_MAX) if r.description else ""
        if description:
            context["quote"] = {"by": "Description", "text": description}
        if criteria:
            context["progress"] = [
                {"label": "Acceptance criteria", "value": None, "max": len(criteria),
                 "text": f"{len(criteria)} criteria"}
            ]
        if quiet_days is not None and group == "second":
            context["quiet_days"] = quiet_days
        items.append(
            Item(
                kind="ticket",
                source_type="ticket",
                source_id=r.id,
                band=band,
                involvement="assignee",
                title=_cap(r.title),
                why=" · ".join(parts),
                link=_project_link(r.project_id, tab="work", sub="tickets", ticket=r.id),
                version_key=f"{r.status}|{_iso(r.updated_at)}",
                occurred_at=r.updated_at,
                due_at=_date_at(r.due_date) if r.due_date else None,
                overdue=overdue,
                priority=r.priority,
                project_id=r.project_id,
                actions=["context", "snooze", "hide"],
                group=group,
                context=context,
            )
        )
    return items, len(items)


_LOOP_KIND = {
    "ready": "loop_ready",
    "needs_human": "loop_needs_human",
    "proposed_checklist": "loop_checklist",
}


async def provide_loop_needs_you(ctx: _Ctx) -> tuple[list[Item], int]:
    """The Review Loop "Needs you" feed (``services.loop_needs_you``,
    unchanged), limited to the scope's projects."""
    from sessionfs.server.services.loop_needs_you import needs_you

    result = await needs_you(ctx.db, user_id=ctx.user_id)
    items: list[Item] = []
    raw = result["items"]
    for idx, it in enumerate(raw):
        pid = it["project_id"]
        if pid not in ctx.projects:
            continue
        kind = _LOOP_KIND.get(it["kind"])
        if kind is None:
            continue
        if kind == "loop_checklist":
            source_type, source_id = "checklist_item", it["checklist_item_id"]
            link = _project_link(pid, tab="work", sub="loop")
            title = _cap(it["title"])
        else:
            source_type, source_id = "ticket", it["ticket_id"]
            link = _project_link(pid, tab="work", sub="loop", story=it["ticket_id"])
            title = f"{it['title']}: {_cap(it.get('ticket_title'), 150)}"
        actions = ["open", "snooze", "hide"]
        loop_ctx: dict[str, Any] | None = None
        if kind == "loop_needs_human":
            # One child can need a human for several reasons; one card each.
            source_id = f"{source_id}#{it['reason']}"
            reason = str(it["reason"])
            limit = (
                "tokens" if reason.startswith("token budget hit")
                else "rounds" if reason.startswith("round cap hit")
                else None
            )
            if limit is not None and it.get("can_act") and it.get("loop_id"):
                # "Raise limit": PATCH /projects/{p}/loops/{loop} (editors).
                actions = ["raise", "open", "snooze", "hide"]
                loop_ctx = {
                    "id": it["loop_id"],
                    "project_id": pid,
                    "ticket_id": it.get("ticket_id"),
                    "limit": limit,
                }
        items.append(
            Item(
                kind=kind,
                source_type=source_type,
                source_id=source_id,
                band="your_turn",
                involvement="editor" if it.get("can_act") else "watcher",
                title=title,
                why=f"{it['reason']} · {_cap(it.get('project_name'), 80)}",
                link=link,
                version_key=f"{it['kind']}|{it['reason']}",
                # needs_you is newest first; keep that order (sort only).
                tiebreak=idx,
                project_id=pid,
                actions=actions,
                context={
                    **({"loop": loop_ctx} if loop_ctx else {}),
                    "source_label": "Review loop",
                    "ref": it.get("ticket_id") or it.get("checklist_item_id"),
                    "project_name": _cap(it.get("project_name") or ctx.projects[pid].name, 80),
                    "facts": [
                        ["Reason", _cap(it["reason"], 120)],
                        ["Project", _cap(it.get("project_name") or ctx.projects[pid].name, 80)],
                    ],
                    "people": _people(("You", "can act" if it.get("can_act") else "watching", False, True)),
                },
            )
        )
    # needs_you caps its items at 100 but counts every one. A service key's
    # scope can only narrow, so only add the uncounted tail for user keys.
    total = len(items)
    if not ctx.scope.is_service:
        total += max(0, int(result["counts"]["total"]) - len(raw))
    return items, total


async def provide_harmful_findings(ctx: _Ctx) -> tuple[list[Item], int]:
    """Review Loop children assigned to the caller (or a persona they
    represent) with harmful open findings — the "Now" band. Loop state comes
    from ``services.loops.load_loops`` (grouped, no per-child N+1)."""
    from sessionfs.server.services.loops import load_loops

    pids = await ctx.feature_project_ids("review_loop")
    if not pids:
        return [], 0
    by_project, _ = await ctx.personas()
    by_project = {p: n for p, n in by_project.items() if p in set(pids)}
    mine = (
        await ctx.db.execute(
            select(Ticket.id, Ticket.parent_ticket_id)
            .where(
                Ticket.project_id.in_(pids),
                Ticket.parent_ticket_id.is_not(None),
                Ticket.status.not_in(("done", "cancelled")),
                _my_ticket_clause(by_project, ctx.user_id),
            )
            .limit(PROVIDER_ROW_CAP)
        )
    ).all()
    if not mine:
        return [], 0
    my_children = {r.id for r in mine}
    parents = list(
        (
            await ctx.db.execute(
                select(Ticket).where(
                    Ticket.id.in_(sorted({r.parent_ticket_id for r in mine})),
                    Ticket.loop_config.is_not(None),
                )
            )
        ).scalars()
    )
    if not parents:
        return [], 0
    project_of = {t.id: t.project_id for t in parents}
    items: list[Item] = []
    for loop in await load_loops(ctx.db, loop_tickets=parents, show_dollars=False):
        pid = project_of[loop["id"]]
        for child in loop["children"]:
            n = int(child.get("harmful_open") or 0)
            if child["ticket_id"] not in my_children or n <= 0:
                continue
            branch = child.get("branch")
            why = f"{n} harmful open finding{'s' if n != 1 else ''}"
            if branch:
                why += f" on {_cap(branch, 80)}"
            items.append(
                Item(
                    kind="loop_harmful_findings",
                    source_type="ticket",
                    source_id=child["ticket_id"],
                    band="now",
                    involvement="assignee",
                    title=_cap(child.get("title")),
                    why=why + f" · {_cap(ctx.projects[pid].name, 80)}",
                    link=_project_link(pid, tab="work", sub="loop", story=child["ticket_id"]),
                    version_key=f"{n}|{child.get('head_sha') or ''}|{child.get('round')}",
                    harm=True,
                    project_id=pid,
                    actions=["open", "snooze", "hide"],
                    context={
                        "source_label": "Review loop",
                        "ref": child["ticket_id"],
                        "project_name": _cap(ctx.projects[pid].name, 80),
                        "harm": "harmful",
                        "facts": [
                            f
                            for f in (
                                ["Branch", _cap(branch, 80)] if branch else None,
                                ["Head commit", str(child.get("head_sha"))[:12]]
                                if child.get("head_sha")
                                else None,
                                ["Round", str(child.get("round"))] if child.get("round") else None,
                                ["Harmful open", str(n)],
                            )
                            if f
                        ],
                        "people": _people(("You", "assignee", False, True)),
                    },
                )
            )
    return items, len(items)


_CHAT_STAMP = re.compile(r"^\[[0-9: -]{1,24}\]\s?")


def _speaker_and_text(lines: list[str], chat: bool = False) -> tuple[str | None, str]:
    """The speaker of the first cited line (``Name: text``) and the cited
    text joined into one quote. A chat thread stores one message per line
    as ``[YYYY-MM-DD HH:MM] Name: text``; the time stamp is dropped."""
    speaker: str | None = None
    parts: list[str] = []
    for raw in lines:
        line = raw.strip()
        if chat:
            line = _CHAT_STAMP.sub("", line, count=1).strip()
        if not line:
            continue
        head, sep, rest = line.partition(":")
        if sep and 0 < len(head.strip()) <= 40 and not head.strip().startswith("http"):
            if speaker is None:
                speaker = head.strip()
        parts.append(line)
    return speaker, _cap(" ".join(parts), QUOTE_MAX)


async def _readable_suggestions(ctx: Any, rows: list) -> dict[str, Ticket]:
    """``{item id: ticket}`` for pending items whose (not dismissed)
    suggested ticket the caller can read in the conversation's scope: the
    meeting's own project, or a readable member project of its group."""
    wanted = {
        r.id: r.suggested_ticket_id
        for r in rows
        if r.suggested_ticket_id and r.suggestion_dismissed_at is None
    }
    if not wanted:
        return {}
    readable = set(ctx.projects)
    tickets = {
        t.id: t
        for t in (
            await ctx.db.execute(
                select(Ticket).where(
                    Ticket.id.in_(sorted(set(wanted.values()))),
                    Ticket.project_id.in_(sorted(readable)),
                )
            )
        ).scalars().all()
    }
    group_ids = sorted({r.group_id for r in rows if r.group_id and r.id in wanted})
    group_projects: set[tuple[str, str]] = set()
    if group_ids:
        group_projects = set(
            (
                await ctx.db.execute(
                    select(ProjectGroupAccess.group_id, ProjectGroupAccess.project_id).where(
                        ProjectGroupAccess.group_id.in_(group_ids)
                    )
                )
            ).all()
        )
    out: dict[str, Ticket] = {}
    for r in rows:
        ticket = tickets.get(wanted.get(r.id, ""))
        if ticket is None:
            continue
        if r.project_id is not None:
            if ticket.project_id != r.project_id:
                continue
        elif (r.group_id, ticket.project_id) not in group_projects:
            continue
        out[r.id] = ticket
    return out


async def provide_conversation_actions(ctx: _Ctx) -> tuple[list[Item], int]:
    """Pending meeting and chat-thread action items that may be the caller's: resolved to the caller (or
    an org persona that represents them in the meeting's scope), or with no
    owner at all in a meeting the caller took part in.

    Each card carries the cited lines, the speaker, the suggested owner and
    due date, a confidence wording and the destination (the meeting's
    project or group). Acknowledge is the existing promote flow (preview
    then confirm), "Not an action" the existing dismiss, "Remind me later"
    the meeting-item reminder route (which stores a ``chat_item`` reminder
    for a chat thread's item).

    Chat threads (``MeetingTranscript.kind == 'chat'``) store one message
    per line, so message N is line N: their ranges render as ``M<a>-<b>``
    and their cards say "chat" where a meeting's say "meeting".
    """
    from sqlalchemy.orm import aliased

    from sessionfs.server.helper.tools import meeting_scope_ids

    scope = SimpleNamespace(project_id=None, group_id=None, session_ids=None)
    project_ids, group_ids = await meeting_scope_ids(ctx.db, ctx.user_id, scope)
    project_ids = {p for p in project_ids if p in ctx.projects}
    groups: dict[str, tuple[str, str]] = {}
    if group_ids:
        groups = {
            gid: (org_id, name)
            for gid, org_id, name in (
                await ctx.db.execute(
                    select(ProjectGroup.id, ProjectGroup.org_id, ProjectGroup.name).where(
                        ProjectGroup.id.in_(sorted(group_ids))
                    )
                )
            ).all()
        }
        group_ids = {g for g in group_ids if g in groups and ctx.scope.allows_group(groups[g][0])}
    if not project_ids and not group_ids:
        return [], 0

    owner_p = aliased(MeetingParticipant)
    by_project, by_org = await ctx.personas()
    persona = owner_p.persona_name
    owner_conds = [owner_p.user_id == ctx.user_id]
    owner_conds += _persona_clause(
        MeetingTranscript.project_id,
        persona,
        {p: n for p, n in by_project.items() if p in project_ids},
    )
    per_org_groups: dict[str, list[str]] = {}
    for gid in group_ids:
        per_org_groups.setdefault(groups[gid][0], []).append(gid)
    for org_id, gids in per_org_groups.items():
        names = by_org.get(org_id)
        if names:
            owner_conds.append(
                and_(
                    MeetingTranscript.group_id.in_(sorted(gids)),
                    func.lower(persona).in_(sorted(names)),
                )
            )
    attended = (
        select(MeetingParticipant.id)
        .where(
            MeetingParticipant.transcript_id == MeetingTranscript.id,
            MeetingParticipant.user_id == ctx.user_id,
        )
        .exists()
    )
    scope_conds = []
    if project_ids:
        scope_conds.append(MeetingTranscript.project_id.in_(sorted(project_ids)))
    if group_ids:
        scope_conds.append(MeetingTranscript.group_id.in_(sorted(group_ids)))

    cols = [
        MeetingActionItem.id,
        MeetingActionItem.description,
        MeetingActionItem.due_date,
        MeetingActionItem.line_start,
        MeetingActionItem.line_end,
        MeetingActionItem.status,
        MeetingActionItem.extracted_at,
        MeetingActionItem.owner_participant_id,
        MeetingTranscript.id.label("meeting_id"),
        MeetingTranscript.title.label("meeting_title"),
        MeetingTranscript.project_id,
        MeetingTranscript.group_id,
        MeetingTranscript.meeting_at,
        owner_p.user_id.label("owner_user_id"),
        owner_p.persona_name,
        owner_p.name_raw.label("owner_name"),
        MeetingTranscript.kind.label("conversation_kind"),
        MeetingTranscript.channel_name,
        MeetingActionItem.suggested_ticket_id,
        MeetingActionItem.suggestion_dismissed_at,
    ]
    rows = (
        await ctx.db.execute(
            select(*cols)
            .join(MeetingTranscript, MeetingTranscript.id == MeetingActionItem.transcript_id)
            .outerjoin(owner_p, owner_p.id == MeetingActionItem.owner_participant_id)
            .where(
                MeetingTranscript.deleted_at.is_(None),
                or_(*scope_conds),
                MeetingActionItem.kind == "action",
                MeetingActionItem.status == "pending",
                or_(
                    and_(MeetingActionItem.owner_participant_id.is_not(None), or_(*owner_conds)),
                    and_(MeetingActionItem.owner_participant_id.is_(None), attended),
                ),
            )
            .order_by(MeetingActionItem.extracted_at.desc(), MeetingActionItem.id)
            .limit(PROVIDER_ROW_CAP)
        )
    ).all()
    if not rows:
        return [], 0

    # Cited lines for the newest few transcripts: sizes first, then the text
    # of at most QUOTE_TRANSCRIPTS_MAX transcripts within QUOTE_BYTES_MAX.
    quote_ids: list[str] = []
    for r in rows:
        if r.meeting_id not in quote_ids:
            quote_ids.append(r.meeting_id)
        if len(quote_ids) >= QUOTE_TRANSCRIPTS_MAX:
            break
    sizes = dict(
        (
            await ctx.db.execute(
                select(MeetingTranscript.id, func.length(MeetingTranscript.content)).where(
                    MeetingTranscript.id.in_(quote_ids)
                )
            )
        ).all()
    )
    budget, load = QUOTE_BYTES_MAX, []
    for mid in quote_ids:
        size = int(sizes.get(mid) or 0)
        if size <= budget:
            load.append(mid)
            budget -= size
    contents = dict(
        (
            await ctx.db.execute(
                select(MeetingTranscript.id, MeetingTranscript.content).where(
                    MeetingTranscript.id.in_(load)
                )
            )
        ).all()
    ) if load else {}
    split_cache: dict[str, list[str]] = {}
    suggestions = await _readable_suggestions(ctx, rows)

    items: list[Item] = []
    for r in rows:
        is_chat = r.conversation_kind == "chat"
        label = "chat" if is_chat else "meeting"
        prefix = "M" if is_chat else "L"
        due_text, overdue = _due_phrase(r.due_date, ctx.today)
        owned = r.owner_participant_id is not None
        mine_direct = owned and r.owner_user_id == ctx.user_id
        if mine_direct:
            owner_label, involvement = "You", "owner"
        elif owned:
            owner_label, involvement = f"{_cap(r.persona_name, 50)} (you)", "owner"
        else:
            owner_label, involvement = "Not named", "member"
        meeting_title = _cap(r.meeting_title, 80)
        parts = [f"From {label} “{meeting_title}”"]
        if owned:
            parts.append("you were named" if mine_direct else f"owner {_cap(r.persona_name, 50)} (you)")
        else:
            parts.append("nobody was named · you took part")
        if due_text:
            parts.append(due_text)
        suggested = suggestions.get(r.id)
        if suggested is not None:
            parts.append(f"looks like {suggested.id} “{_cap(suggested.title, 60)}”")
        else:
            parts.append("not a ticket yet")
        lines = (
            f"{r.line_start}-{r.line_end}" if r.line_end != r.line_start else str(r.line_start)
        )
        cited = f"{prefix}{lines}"
        speaker: str | None = None
        quote_text = ""
        if r.meeting_id in contents:
            all_lines = split_cache.get(r.meeting_id)
            if all_lines is None:
                all_lines = split_cache[r.meeting_id] = (contents[r.meeting_id] or "").split("\n")
            lo = max(1, int(r.line_start))
            hi = min(len(all_lines), int(r.line_end), lo + 7)
            speaker, quote_text = _speaker_and_text(all_lines[lo - 1 : hi], chat=is_chat)
        if mine_direct and r.due_date is not None:
            level, wording = 3, "Sounds like an action for you"
        elif owned:
            level, wording = 2, "Sounds like an action for you"
        else:
            level, wording = 1, "Might be an action for you"
        if r.project_id:
            dest = {
                "kind": "project",
                "id": r.project_id,
                "name": _cap(ctx.projects[r.project_id].name, 80)
                if r.project_id in ctx.projects
                else r.project_id,
            }
        else:
            gname = groups.get(r.group_id or "", ("", ""))[1]
            dest = {"kind": "group", "id": r.group_id, "name": _cap(gname, 80)}
        context: dict[str, Any] = {
            "source_label": "Chat" if is_chat else "Meeting",
            "ref": f"{r.meeting_id} · {cited}",
            "project_name": dest["name"],
            "quote": (
                {
                    "by": f"{meeting_title} · {cited}" + (f" · {_cap(speaker, 40)}" if speaker else ""),
                    "text": quote_text,
                }
                if quote_text
                else None
            ),
            "speaker": _cap(speaker, 40) if speaker else None,
            "suggest": {
                "level": level,
                "wording": wording,
                "owner": owner_label,
                "due": r.due_date.isoformat() if r.due_date else None,
                "destination": dest,
                "title": _cap(r.description, 200),
            },
            "meeting": {
                "id": r.meeting_id,
                "item_id": r.id,
                "title": meeting_title,
                "lines": lines,
                "range": cited,
                "kind": label,
                "channel": _cap(r.channel_name, 80) if is_chat and r.channel_name else None,
                "project_id": r.project_id,
                "group_id": r.group_id,
                "meeting_at": _iso(r.meeting_at),
            },
            "facts": [
                f
                for f in (
                    ["Meeting" if not is_chat else "Chat thread", meeting_title],
                    ["Channel", _cap(r.channel_name, 80)] if is_chat and r.channel_name else None,
                    ["Cited messages" if is_chat else "Cited lines", cited],
                    ["Said by", _cap(speaker, 40)] if speaker else None,
                    ["Goes to", f"{dest['name']} ({dest['kind']})"],
                    ["Looks like", f"{suggested.id} · {_cap(suggested.title, 80)}"]
                    if suggested is not None
                    else None,
                )
                if f
            ],
            "people": _people(
                (_cap(speaker, 40) if speaker else None, "said it", False, False),
                ("You", "suggested owner" if owned else "took part", False, True),
            ),
        }
        items.append(
            Item(
                kind=f"{label}_action" if owned else f"{label}_suggestion",
                source_type=f"{label}_item",
                source_id=r.id,
                band="your_turn",
                involvement=involvement,
                title=_cap(r.description),
                why=" · ".join(parts),
                link=f"/meetings/{quote(r.meeting_id, safe='')}?lines={lines}",
                version_key=f"{r.status}|{_iso(r.extracted_at)}|{r.due_date}",
                occurred_at=r.extracted_at,
                due_at=_date_at(r.due_date) if r.due_date else None,
                overdue=overdue,
                project_id=r.project_id,
                actions=["promote", "remind", "dismiss"],
                group="meetings",
                context=context,
            )
        )
    return items, len(items)


def _reminder_today_end(r: Reminder, now: datetime) -> datetime:
    """End of the reminder's local "today" (its IANA timezone, else UTC)."""
    tz: Any = timezone.utc
    if r.timezone:
        try:
            tz = ZoneInfo(r.timezone)
        except Exception:  # noqa: BLE001 — unknown/invalid zone: fall back to UTC
            tz = timezone.utc
    local = now.astimezone(tz)
    end = datetime(local.year, local.month, local.day, tzinfo=tz) + timedelta(days=1)
    return end.astimezone(timezone.utc)


async def provide_reminders(ctx: _Ctx) -> tuple[list[Item], int]:
    """Open reminders the caller owns, re-checked through
    ``reminder_visibility``: overdue or due today (in the reminder's own
    timezone) are Now / On your radar; due within the next week are Coming
    up."""
    from sessionfs.server.services import reminders as rsvc

    rows = list(
        (
            await ctx.db.execute(
                select(Reminder)
                .where(
                    Reminder.owner_user_id == ctx.user_id,
                    Reminder.status == "open",
                    # Widest possible "today" (UTC+14) plus the upcoming week.
                    Reminder.due_at < ctx.now + UPCOMING_WINDOW + timedelta(hours=38),
                )
                .order_by(Reminder.due_at.asc(), Reminder.id.asc())
                .limit(PROVIDER_ROW_CAP)
            )
        ).scalars()
    )
    if not rows:
        return [], 0
    cache = rsvc.AccessCache()
    vis = await rsvc.reminder_visibility(ctx.db, ctx.user_id, rows, cache)
    creators = sorted({r.created_by_user_id for r in rows if r.created_by_user_id and r.created_by_user_id != ctx.user_id})
    creator_names = await _user_names(ctx, creators)
    items: list[Item] = []
    for r in rows:
        v = vis[r.id]
        if not v.visible:
            continue
        source_project = v.source.project_id if v.source is not None else None
        if ctx.scope.is_service and not (
            source_project
            and rsvc.service_key_allows_project(ctx.scope, cache.projects.get(source_project))
        ):
            continue
        due = _aware(r.due_at)
        today_end = _reminder_today_end(r, ctx.now)
        if due is None:
            continue
        upcoming = due >= today_end
        if upcoming and due >= today_end + UPCOMING_WINDOW:
            continue
        overdue = due <= ctx.now
        if upcoming:
            why = "Reminder · coming up"
        else:
            why = "Reminder · overdue" if overdue else "Reminder · due today"
        # A service key sees the source's title and kind only with that
        # source's own read scope.
        source = v.source
        if source is not None and ctx.scope.is_service:
            need = SOURCE_SCOPES.get(source.kind)
            if need is None or need not in ctx.scope.scopes:
                source = None
        if source is not None and source.title:
            why += f" · {_cap(source.title, 80)}"
        from_other = r.created_by_user_id != ctx.user_id
        if from_other:
            why += f" · from {creator_names.get(r.created_by_user_id, 'a teammate')}"
        facts = [["Due", _iso(r.due_at) or ""]]
        if r.timezone:
            facts.append(["Time zone", _cap(r.timezone, 64)])
        if r.snooze_count:
            facts.append(["Snoozed", f"{r.snooze_count} time{'s' if r.snooze_count != 1 else ''}"])
        if source is not None:
            facts.append(["Source", f"{source.kind.replace('_', ' ')} {_cap(source.title, 80) or ''}".strip()])
        context: dict[str, Any] = {
            "source_label": "Reminder",
            "ref": r.id,
            "project_name": _cap(sp.name, 80) if (sp := cache.projects.get(source_project or "")) else None,
            "facts": facts,
            "people": _people(
                ("You", "own it", False, True),
                (creator_names.get(r.created_by_user_id) if from_other else None, "set it for you", False, False),
            ),
            "reminder": {"id": r.id, "timezone": r.timezone},
        }
        items.append(
            Item(
                kind="reminder",
                source_type="reminder",
                source_id=r.id,
                band="upcoming" if upcoming else "now",
                involvement="owner",
                title=_cap(r.title),
                why=why,
                link=f"/reminders?{urlencode({'reminder': r.id})}",
                version_key=f"{_iso(r.due_at)}|{_iso(r.updated_at)}",
                occurred_at=r.due_at,
                due_at=r.due_at,
                overdue=overdue,
                project_id=source_project,
                actions=["done", "snooze", "hide"],
                # A teammate's nudge reads as "your team is asking".
                group="coming" if upcoming else ("asking" if from_other else "radar"),
                context=context,
            )
        )
    return items, len(items)


async def _user_names(ctx: _Ctx, user_ids: list[str]) -> dict[str, str]:
    """Display names for ``user_ids`` (display name, else a masked email),
    one query, memoised per request."""
    from sessionfs.server.email import mask_address

    memo: dict[str, str] = ctx.memo.setdefault("user_names", {})
    missing = [u for u in user_ids if u and u not in memo]
    if missing:
        for uid, name, email in (
            await ctx.db.execute(
                select(User.id, User.display_name, User.email).where(User.id.in_(missing))
            )
        ).all():
            memo[uid] = _cap(name, 60) if name and name.strip() else mask_address(email)
        for uid in missing:
            memo.setdefault(uid, "a former user")
    return {u: memo[u] for u in user_ids if u in memo}


_MENTION_STRIP = str.maketrans({c: " " for c in ".,;:!?()[]{}<>\"'`"})


def _mentions(content: str, handles: set[str]) -> bool:
    words = {w.lower() for w in content.translate(_MENTION_STRIP).split() if w.startswith("@")}
    return any(f"@{h}" in words for h in handles)


async def provide_questions(ctx: _Ctx) -> tuple[list[Item], int]:
    """Your team is asking: the newest comment from another person on a
    ticket you are assigned to (or created) in the last week that mentions
    you (``@name``) or asks a question, and that you have not answered since
    (no later comment from you). One card per ticket. Review-verdict and
    Jira-mirrored comments never count."""
    pids = await ctx.feature_project_ids("agent_tickets")
    if not pids:
        return [], 0
    by_project, _ = await ctx.personas()
    by_project = {p: n for p, n in by_project.items() if p in set(pids)}
    since = ctx.now - QUESTION_WINDOW
    mine = or_(_my_ticket_clause(by_project, ctx.user_id), Ticket.created_by_user_id == ctx.user_id)
    rows = (
        await ctx.db.execute(
            select(
                TicketComment.id,
                TicketComment.ticket_id,
                TicketComment.author_user_id,
                TicketComment.content,
                TicketComment.created_at,
                Ticket.project_id,
                Ticket.title,
                Ticket.assigned_user_id,
            )
            .join(Ticket, Ticket.id == TicketComment.ticket_id)
            .where(
                Ticket.project_id.in_(pids),
                Ticket.status.not_in(("done", "cancelled")),
                mine,
                TicketComment.created_at >= since,
                TicketComment.author_user_id != ctx.user_id,
                TicketComment.author_persona.is_(None),
                TicketComment.actor_type == "user",
                TicketComment.source == "sessionfs",
                TicketComment.verdict_trusted.is_(False),
            )
            .order_by(TicketComment.created_at.desc(), TicketComment.id.desc())
            .limit(PROVIDER_ROW_CAP)
        )
    ).all()
    if not rows:
        return [], 0
    ticket_ids = sorted({r.ticket_id for r in rows})
    answered = dict(
        (
            await ctx.db.execute(
                select(TicketComment.ticket_id, func.max(TicketComment.created_at))
                .where(
                    TicketComment.ticket_id.in_(ticket_ids),
                    TicketComment.author_user_id == ctx.user_id,
                    TicketComment.created_at >= since,
                )
                .group_by(TicketComment.ticket_id)
            )
        ).all()
    )
    me_row = (
        await ctx.db.execute(select(User.display_name, User.email).where(User.id == ctx.user_id))
    ).first()
    handles: set[str] = set()
    if me_row is not None:
        if me_row.display_name:
            first = me_row.display_name.split()[0].lower() if me_row.display_name.split() else ""
            if first:
                handles.add(first)
            handles.add(me_row.display_name.replace(" ", "").lower())
        if me_row.email:
            handles.add(me_row.email.split("@", 1)[0].lower())
    names = await _user_names(ctx, sorted({r.author_user_id for r in rows}))
    seen: set[str] = set()
    items: list[Item] = []
    for r in rows:
        if r.ticket_id in seen:
            continue
        mentioned = _mentions(r.content or "", handles)
        asks = "?" in (r.content or "")
        if not (mentioned or asks):
            continue
        last_mine = _aware(answered.get(r.ticket_id))
        if last_mine is not None and last_mine >= (_aware(r.created_at) or _MIN_DT):
            continue
        seen.add(r.ticket_id)
        who = names.get(r.author_user_id, "A teammate")
        title = f"{who} {'mentioned you' if mentioned else 'asked a question'} on “{_cap(r.title, 90)}”"
        why = ("Mentioned you" if mentioned else "A question on your ticket") + " · not answered"
        items.append(
            Item(
                kind="ticket_question",
                source_type="ticket_comment",
                source_id=r.id,
                band="your_turn",
                involvement="assignee" if r.assigned_user_id == ctx.user_id else "editor",
                title=title,
                why=why,
                link=_project_link(r.project_id, tab="work", sub="tickets", ticket=r.ticket_id),
                version_key=f"{r.id}|{_iso(r.created_at)}",
                occurred_at=r.created_at,
                project_id=r.project_id,
                actions=["reply", "answered", "snooze", "hide"],
                group="asking",
                context={
                    "source_label": "Ticket comment",
                    "ref": r.ticket_id,
                    "project_name": _cap(ctx.projects[r.project_id].name, 80),
                    "quote": {"by": f"{who} · comment", "text": _cap(r.content, QUOTE_MAX)},
                    "facts": [["Ticket", _cap(r.title, 120)], ["Asked", _iso(r.created_at) or ""]],
                    "people": _people((who, "asked", False, False)),
                    "reply": {
                        "project_id": r.project_id,
                        "ticket_id": r.ticket_id,
                        "to": who,
                    },
                },
            )
        )
    return items, len(items)


async def provide_handoffs(ctx: _Ctx) -> tuple[list[Item], int]:
    """Pending, unexpired handoffs sent to the caller (by user id or by their
    email). Waiting on you while fresh; Worth a second look once they have
    waited two days. Service keys never see handoffs here."""
    if ctx.scope.is_service:
        return [], 0
    me = (
        await ctx.db.execute(select(User.email, User.email_verified).where(User.id == ctx.user_id))
    ).first()
    conds = [Handoff.recipient_user_id == ctx.user_id]
    # An email-addressed handoff counts as yours only once you have proven
    # you own that address.
    if me is not None and me.email and me.email_verified:
        conds.append(Handoff.recipient_email_normalized == me.email.strip().lower())
    rows = (
        await ctx.db.execute(
            select(Handoff)
            .where(Handoff.status == "pending", Handoff.expires_at > ctx.now, or_(*conds))
            .order_by(Handoff.created_at.desc(), Handoff.id)
            .limit(PROVIDER_ROW_CAP)
        )
    ).scalars().all()
    if not rows:
        return [], 0
    names = await _user_names(ctx, sorted({h.sender_id for h in rows if h.sender_id}))
    items: list[Item] = []
    for h in rows:
        created = _aware(h.created_at) or ctx.now
        waited = ctx.now - created
        quiet = waited >= HANDOFF_QUIET_AFTER
        sender = names.get(h.sender_id, "A teammate")
        title = f"{sender} handed you {_cap(h.snapshot_title, 120) or 'a session'}"
        parts = ["Handed to you"]
        if quiet:
            parts.append(f"not claimed in {waited.days} days")
        parts.append(f"expires {(_aware(h.expires_at) or ctx.now).date().isoformat()}")
        context: dict[str, Any] = {
            "source_label": "Handoff",
            "ref": h.id,
            "facts": [
                f
                for f in (
                    ["Tool", _cap(h.snapshot_tool, 40)] if h.snapshot_tool else None,
                    ["Messages", str(h.snapshot_message_count)] if h.snapshot_message_count else None,
                    ["Ticket", h.ticket_id] if h.ticket_id else None,
                    ["Expires", _iso(h.expires_at) or ""],
                )
                if f
            ],
            "people": _people((sender, "sent it", False, False)),
        }
        if h.message:
            context["quote"] = {"by": f"{sender} · handoff note", "text": _cap(h.message, QUOTE_MAX)}
        if quiet:
            context["quiet_days"] = waited.days
        items.append(
            Item(
                kind="handoff",
                source_type="handoff",
                source_id=h.id,
                band="your_turn",
                involvement="assignee",
                title=title,
                why=" · ".join(parts),
                link=f"/handoffs/{quote(h.id, safe='')}",
                version_key=f"{h.status}|{_iso(h.expires_at)}",
                occurred_at=h.created_at,
                due_at=h.expires_at,
                # "Claim" is POST /handoffs/{id}/claim (user keys; handoffs
                # never reach a service key's feed).
                actions=["claim", "open", "snooze", "hide"],
                group="second" if quiet else "waiting",
                context=context,
            )
        )
    return items, len(items)


async def _has_table(db: AsyncSession, name: str) -> bool:
    conn = await db.connection()
    return bool(await conn.run_sync(lambda c: inspect(c).has_table(name)))


def _parse_iso(value: Any) -> datetime | None:
    """An ISO-8601 string (or datetime) as an aware datetime; None when
    missing or unparseable. ``fromisoformat`` on Python 3.10 refuses "Z"."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return _aware(value)
    try:
        return _aware(datetime.fromisoformat(str(value).replace("Z", "+00:00")))
    except ValueError:
        return None


async def provide_approvals(ctx: _Ctx) -> tuple[list[Item], int] | None:
    """Approval requests the caller can decide (0.29.6).

    ``services.approvals.attention_items`` owns approver eligibility (an
    active org member who fits a requirement, can read the target's project
    and is not its implementer or author) and narrows a service key to its
    org and project allowlist. It returns dicts with ``source``
    (``{"type": "approval_request", "id": ...}``) and ISO-string
    ``occurred_at``/``due_at``. Returns None (no section) when approvals are
    not installed.
    """
    if not await _has_table(ctx.db, "approval_requests"):
        return None
    try:
        mod = importlib.import_module("sessionfs.server.services.approvals")
    except ImportError:
        return None
    fn: Callable[..., Awaitable[list[dict[str, Any]]]] | None = getattr(
        mod, "attention_items", None
    )
    if fn is None:
        return None
    # Decisions need a person's user key (the decide route refuses service
    # keys), so a service key never gets approval cards it could not clear.
    if ctx.scope.is_service:
        return [], 0
    raw = await fn(
        ctx.db,
        user_id=ctx.user_id,
        scope=ctx.scope if ctx.scope.is_service else None,
        allowed_project_ids=sorted(ctx.projects),
    )
    titles: dict[str, str] = {}
    ids = [
        str((d.get("source") or {}).get("id") or "")
        for d in raw
        if isinstance(d.get("source"), dict)
    ]
    # Target titles (ticket title / start of the entry) follow the same scope
    # rule as GET /me/approvals: a service key needs the matching read scope.
    show_tickets = not ctx.scope.is_service or "tickets:read" in ctx.scope.scopes
    show_kb = not ctx.scope.is_service or "knowledge:read" in ctx.scope.scopes
    target_titles = getattr(mod, "target_titles", None)
    if ids and target_titles is not None and (show_tickets or show_kb):
        from sessionfs.server.db.models import ApprovalRequest

        reqs = [
            r
            for r in (
                await ctx.db.execute(select(ApprovalRequest).where(ApprovalRequest.id.in_(ids)))
            ).scalars().all()
            if (r.target_kind == "merge" and show_tickets)
            or (r.target_kind == "kb_promote" and show_kb)
        ]
        if reqs:
            titles = await target_titles(ctx.db, reqs)
    items: list[Item] = []
    for d in raw:
        src = d.get("source")
        if not isinstance(src, dict) or not src.get("id"):
            continue
        rid = str(src["id"])
        pid = d.get("project_id")
        if pid is None or pid not in ctx.projects:
            continue
        raw_detail = d.get("approval")
        detail: dict[str, Any] = raw_detail if isinstance(raw_detail, dict) else {}
        got = int(detail.get("approved_count") or 0)
        need = int(detail.get("required_count") or 0)
        target_kind = detail.get("target_kind")
        target_id = str(detail.get("target_id") or "")
        sha = str(detail.get("target_fingerprint") or "")
        policy_name = _cap(detail.get("policy_name") or "Approval", 120)
        blocking = bool(detail.get("blocking", True))
        target_title = _cap(titles.get(rid), 160) if titles.get(rid) else None
        if target_kind == "merge":
            target_label = f"commit {sha[:7]}" if sha else "this merge"
            target_fact = ["Ticket", target_id]
        else:
            target_label = "this knowledge entry"
            target_fact = ["Knowledge entry", target_id]
        context: dict[str, Any] = {
            "source_label": "Approval",
            "ref": rid,
            "project_name": _cap(ctx.projects[pid].name, 80),
            "progress": [
                {"label": "Approvals", "value": got, "max": need, "text": f"{got} of {need}"}
            ],
            "facts": [
                f
                for f in (
                    ["Policy", policy_name + (f" v{detail['policy_version']}" if detail.get("policy_version") else "")],
                    target_fact if target_id else None,
                    ["Commit", sha[:12]] if target_kind == "merge" and sha else None,
                    ["Target", target_title] if target_title else None,
                    ["Approvals", f"{got} of {need}"],
                    ["Effect", "Blocks the merge" if blocking and target_kind == "merge"
                     else "Holds the entry as a note" if blocking else "Advisory"],
                    ["Expires", str(d.get("due_at") or "")] if d.get("due_at") else None,
                )
                if f
            ],
            "approval": {
                "request_id": rid,
                "policy_name": policy_name,
                "target_kind": target_kind,
                "target_label": target_label,
                "target_title": target_title,
                "approved_count": got,
                "required_count": need,
                "blocking": blocking,
            },
            "people": _people(("You", "approver", False, True)),
        }
        items.append(
            Item(
                kind="approval",
                source_type="approval_request",
                source_id=rid,
                band="now",
                involvement="approver",
                title=_cap(d.get("title")),
                why=_cap(d.get("why"), 300),
                link=str(d.get("link") or f"/approvals?request={quote(rid, safe='')}"),
                # Changes when someone else approves (the count moves) or the
                # request is re-raised, so a hidden card comes back.
                version_key=f"pending|{got}/{need}|{d.get('due_at') or ''}"[:200],
                occurred_at=_parse_iso(d.get("occurred_at")),
                due_at=_parse_iso(d.get("due_at")),
                project_id=pid,
                actions=["approve", "reject", "open", "snooze", "hide"],
                group="radar" if blocking else "waiting",
                context=context,
            )
        )
    return items, len(items)


async def _worked_in_project_ids(ctx: _Ctx) -> list[str]:
    """Readable projects the caller owns or captured a session in lately."""
    ids = {pid for pid, p in ctx.projects.items() if p.owner_id == ctx.user_id}
    rows = (
        await ctx.db.execute(
            select(Session.project_id)
            .where(
                Session.user_id == ctx.user_id,
                Session.is_deleted.is_(False),
                Session.project_id.in_(sorted(ctx.projects)),
                Session.updated_at >= ctx.now - WORKED_IN_WINDOW,
            )
            .distinct()
        )
    ).scalars().all()
    ids.update(p for p in rows if p)
    return sorted(ids)


async def provide_knowledge_changes(ctx: _Ctx) -> tuple[list[Item], int]:
    """Active knowledge claims others added in the last 7 days to projects
    the caller works in, one card per project."""
    pids = await _worked_in_project_ids(ctx)
    if not pids:
        return [], 0
    since = ctx.now - KNOWLEDGE_WINDOW
    rows = (
        await ctx.db.execute(
            select(
                KnowledgeEntry.project_id,
                func.count(KnowledgeEntry.id).label("n"),
                func.sum(case((KnowledgeEntry.entry_type == "decision", 1), else_=0)).label(
                    "decisions"
                ),
                func.max(KnowledgeEntry.created_at).label("latest"),
            )
            .where(
                KnowledgeEntry.project_id.in_(pids),
                KnowledgeEntry.created_at >= since,
                KnowledgeEntry.dismissed.is_(False),
                KnowledgeEntry.superseded_by.is_(None),
                KnowledgeEntry.claim_class == "claim",
                KnowledgeEntry.user_id != ctx.user_id,
            )
            .group_by(KnowledgeEntry.project_id)
        )
    ).all()
    entries: dict[str, list[dict[str, Any]]] = {}
    if rows:
        recent = (
            await ctx.db.execute(
                select(
                    KnowledgeEntry.project_id,
                    KnowledgeEntry.entry_type,
                    KnowledgeEntry.content,
                    KnowledgeEntry.created_at,
                    KnowledgeEntry.persona_name,
                )
                .where(
                    KnowledgeEntry.project_id.in_(sorted({r.project_id for r in rows})),
                    KnowledgeEntry.created_at >= since,
                    KnowledgeEntry.dismissed.is_(False),
                    KnowledgeEntry.superseded_by.is_(None),
                    KnowledgeEntry.claim_class == "claim",
                    KnowledgeEntry.user_id != ctx.user_id,
                )
                .order_by(KnowledgeEntry.created_at.desc(), KnowledgeEntry.id.desc())
                .limit(PROVIDER_ROW_CAP)
            )
        ).all()
        for e in recent:
            bucket = entries.setdefault(e.project_id, [])
            if len(bucket) < 4:
                bucket.append(
                    {
                        "type": e.entry_type,
                        "text": _cap(e.content, 200),
                        "by": _cap(e.persona_name, 40) if e.persona_name else None,
                        "at": _iso(e.created_at),
                    }
                )
    items: list[Item] = []
    for r in rows:
        n, decisions = int(r.n or 0), int(r.decisions or 0)
        name = _cap(ctx.projects[r.project_id].name, 80)
        if decisions:
            other = n - decisions
            title = f"{decisions} decision{'s' if decisions != 1 else ''}"
            if other:
                title += f" and {other} other change{'s' if other != 1 else ''}"
            title += f" in {name}"
        else:
            title = f"{n} knowledge change{'s' if n != 1 else ''} in {name}"
        items.append(
            Item(
                kind="knowledge_changes",
                source_type="project",
                source_id=r.project_id,
                band="lately",
                involvement="member",
                title=title,
                why="Knowledge changed in a project you work in · last 7 days",
                link=_project_link(r.project_id, tab="knowledge", sub="history"),
                version_key=f"{n}|{_iso(r.latest)}",
                occurred_at=r.latest,
                project_id=r.project_id,
                actions=["read", "open", "hide"],
                context={
                    "source_label": "Knowledge base",
                    "ref": f"{n} change{'s' if n != 1 else ''}",
                    "project_name": name,
                    "entries": entries.get(r.project_id, []),
                    "facts": [["Changes this week", str(n)], ["Decisions", str(decisions)]],
                    "people": _people(
                        *(
                            (e["by"], "persona", True, False)
                            for e in entries.get(r.project_id, [])
                            if e.get("by")
                        )
                    ),
                },
            )
        )
    return items, len(items)


def _fmt_tokens(n: float) -> str:
    if n >= 100_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}k"
    return str(int(n))


async def _dollar_project_ids(ctx: _Ctx, pids: list[str]) -> set[str]:
    """Projects whose dollars the caller may see (``user_can_view_spend_dollars``
    as a set): personal projects they own, org projects of orgs they own or
    administer. Never for service keys."""
    if ctx.scope.is_service or not pids:
        return set()
    org_ids = sorted({o for p in pids if (o := ctx.projects[p].org_id)})
    admin_orgs: set[str] = set()
    if org_ids:
        admin_orgs = set(
            (
                await ctx.db.execute(
                    select(OrgMember.org_id).where(
                        OrgMember.user_id == ctx.user_id,
                        OrgMember.org_id.in_(org_ids),
                        OrgMember.role.in_(ADMIN_ROLES),
                    )
                )
            ).scalars().all()
        )
    out = set()
    for pid in pids:
        p = ctx.projects[pid]
        if (p.org_id is None and p.owner_id == ctx.user_id) or (p.org_id in admin_orgs):
            out.add(pid)
    return out


async def provide_proof(ctx: _Ctx) -> tuple[list[Item], int]:
    """The weekly proof card: harmful findings the loop caught (harmful AND
    fixed — ``review_loop.is_caught``, the insights rule) on the caller's
    tickets this week, and tokens per accepted change. Dollars only when the
    caller may see them on EVERY contributing project."""
    from sessionfs.server.services.review_loop import decode_findings, is_caught

    pids = await ctx.feature_project_ids("review_loop")
    if not pids:
        return [], 0
    by_project, _ = await ctx.personas()
    by_project = {p: n for p, n in by_project.items() if p in set(pids)}
    since = ctx.now - PROOF_WINDOW
    mine = _my_ticket_clause(by_project, ctx.user_id)
    run_at = func.coalesce(AgentRun.occurred_at, AgentRun.created_at)
    runs = (
        await ctx.db.execute(
            select(AgentRun.project_id, AgentRun.findings, run_at.label("run_at"))
            .join(
                Ticket,
                and_(Ticket.id == AgentRun.ticket_id, Ticket.project_id == AgentRun.project_id),
            )
            .where(AgentRun.project_id.in_(pids), run_at >= since, mine)
            .limit(PROVIDER_ROW_CAP)
        )
    ).all()
    caught = 0
    caught_projects: set[str] = set()
    per_day: dict[str, dict[str, int]] = {}
    for pid, findings, at in runs:
        n = sum(1 for f in decode_findings(findings) if is_caught(f))
        day = (_aware(at) or ctx.now).date().isoformat()
        bucket = per_day.setdefault(day, {"rounds": 0, "caught": 0})
        bucket["rounds"] += 1
        if n:
            caught += n
            caught_projects.add(pid)
            bucket["caught"] += 1

    token_expr = (
        func.coalesce(AgentRun.input_tokens, 0)
        + func.coalesce(AgentRun.output_tokens, 0)
        + func.coalesce(AgentRun.cache_read_tokens, 0)
        + func.coalesce(AgentRun.cache_write_tokens, 0)
    )
    cost_rows = (
        await ctx.db.execute(
            select(
                Ticket.project_id,
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
                and_(AgentRun.ticket_id == Ticket.id, AgentRun.project_id == Ticket.project_id),
            )
            .where(
                Ticket.project_id.in_(pids),
                Ticket.status == "done",
                Ticket.resolved_at.is_not(None),
                Ticket.resolved_at >= since,
                mine,
            )
            .group_by(Ticket.project_id)
        )
    ).all()
    accepted = sum(int(r.tickets or 0) for r in cost_rows)
    tokens = sum(int(r.tokens or 0) for r in cost_rows)
    if not caught and not accepted:
        return [], 0
    contributing = sorted(caught_projects | {r.project_id for r in cost_rows})
    dollar_ok = await _dollar_project_ids(ctx, contributing)
    show_dollars = bool(contributing) and set(contributing) <= dollar_ok

    title = (
        f"The loop caught {caught} harmful bug{'s' if caught != 1 else ''} "
        "on your work this week"
        if caught
        else f"{accepted} change{'s' if accepted != 1 else ''} accepted this week"
    )
    detail: dict[str, Any] = {
        "caught": caught,
        "accepted_changes": accepted,
        "rounds": len(runs),
        "days": [{"date": d, **per_day[d]} for d in sorted(per_day)],
    }
    why = "Weekly proof"
    if accepted:
        per = tokens / accepted
        detail["tokens_per_accepted_change"] = round(per, 2)
        why += f" · cost per accepted change {_fmt_tokens(per)} tokens"
        if show_dollars:
            usd = sum(float(r.cost_usd or 0) for r in cost_rows) / accepted
            detail["cost_usd_per_accepted_change"] = round(usd, 6)
            why += f" (${usd:.2f})"
    week = ctx.now.isocalendar()
    item_week = f"{week[0]}-W{week[1]:02d}"
    week_start = datetime(ctx.now.year, ctx.now.month, ctx.now.day, tzinfo=timezone.utc) - timedelta(
        days=ctx.now.weekday()
    )
    item = Item(
        kind="proof",
        source_type="weekly_proof",
        source_id=f"{week[0]}-W{week[1]:02d}",
        band="lately",
        involvement="member",
        title=title,
        why=why,
        link="/review-loops",
        version_key=f"{caught}|{accepted}",
        occurred_at=week_start,
        detail=detail,
        actions=["open", "hide"],
        context={
            "source_label": "Weekly proof",
            "ref": item_week,
            "proof": detail,
            "dollars_visible": show_dollars,
            "facts": [
                ["Review rounds", str(len(runs))],
                ["Accepted changes", str(accepted)],
                ["Harmful caught", str(caught)],
            ],
        },
    )
    return [item], 1


Provider = Callable[[_Ctx], Awaitable["tuple[list[Item], int] | None"]]

# Order is the section order in the response.
PROVIDERS: list[tuple[str, Provider]] = [
    ("reminders", provide_reminders),
    ("approvals", provide_approvals),
    ("tickets", provide_tickets),
    ("loop_needs_you", provide_loop_needs_you),
    ("harmful_findings", provide_harmful_findings),
    ("conversation_actions", provide_conversation_actions),
    ("questions", provide_questions),
    ("handoffs", provide_handoffs),
    ("knowledge_changes", provide_knowledge_changes),
    ("proof", provide_proof),
]


# The read scope a service key needs to see a reminder's source metadata.
SOURCE_SCOPES: dict[str, str] = {
    "ticket": "tickets:read",
    "meeting": "meetings:read",
    "meeting_item": "meetings:read",
    "chat_item": "meetings:read",
    "knowledge_entry": "knowledge:read",
    "session": "sessions:read",
}

# A service key reads a section only with the scope its content needs
# (the same rules ``item_context`` applies to knowledge and sessions).
PROVIDER_SCOPES: dict[str, str] = {
    "reminders": "reminders:read",
    "tickets": "tickets:read",
    "loop_needs_you": "tickets:read",
    "harmful_findings": "tickets:read",
    "questions": "tickets:read",
    "proof": "agent_runs:read",
    "conversation_actions": "meetings:read",
    "knowledge_changes": "knowledge:read",
    "approvals": "approvals:read",
}


def _scope_allows(scope: AttentionScope, name: str) -> bool:
    need = PROVIDER_SCOPES.get(name)
    return not scope.is_service or need is None or need in scope.scopes


# ── Aggregation ──


@dataclass
class Feed:
    items: list[Item]
    hidden_keys: set[str]
    sections: dict[str, dict[str, Any]]
    # Section name -> items the provider counted but did not list.
    unlisted: dict[str, int]
    # "Working with others" (``attention_collab``), when requested.
    collaboration: dict[str, Any] | None = None


async def _readable_projects(db: AsyncSession, scope: AttentionScope) -> dict[str, Project]:
    projects = await list_accessible_projects(db, scope.user_id)
    return {p.id: p for p in projects if scope.allows_project(p)}


async def _run_provider(ctx: _Ctx, name: str, fn: Provider) -> tuple[Any, str | None]:
    """Run one provider on its OWN session under the time limit.

    A separate session means a provider cancelled mid-query (or one that
    fails) can never poison the request's session: its connection is
    released and discarded, and the feed carries on."""
    started = time.monotonic()
    session = AsyncSession(bind=ctx.db.bind, expire_on_commit=False)
    try:
        # The database cancels the provider's statements too (PostgreSQL), so
        # a timed-out section never leaves a query running on the server.
        await bound_heavy_reads(session, int(PROVIDER_TIMEOUT_S * 1000) + 500)
        # asyncio.wait_for, not asyncio.timeout: the server runs on 3.10.
        result = await asyncio.wait_for(fn(replace(ctx, db=session)), PROVIDER_TIMEOUT_S)
        return result, None
    except Exception:  # noqa: BLE001 — a broken source must not break the feed
        logger.warning(
            "attention provider %s failed after %.0f ms",
            name,
            (time.monotonic() - started) * 1000,
            exc_info=True,
        )
        return None, SECTION_ERROR
    finally:
        try:
            await session.close()
        except Exception:  # noqa: BLE001
            logger.warning("attention provider %s session close failed", name, exc_info=True)


async def _prepared_ctx(db: AsyncSession, scope: AttentionScope, now: datetime) -> _Ctx:
    """A context with the shared per-request lookups (plan tiers, personas)
    done once up front, so concurrent providers reuse them."""
    ctx = _Ctx(db=db, scope=scope, now=now, projects=await _readable_projects(db, scope))
    await ctx.feature_project_ids("agent_tickets")
    await ctx.personas()
    return ctx


async def _run_all(ctx: _Ctx, jobs: list[tuple[str, Any]]) -> dict[str, tuple[Any, str | None]]:
    """Run providers concurrently, each on its own session and time limit.

    SQLite (tests, single-user installs) has one connection, so there they
    run one after another; the per-provider limit still applies."""
    if ctx.db.get_bind().dialect.name == "sqlite":
        return {name: await _run_provider(ctx, name, fn) for name, fn in jobs}
    outs = await asyncio.gather(*(_run_provider(ctx, name, fn) for name, fn in jobs))
    return {name: out for (name, _), out in zip(jobs, outs)}


async def build_feed(
    db: AsyncSession,
    scope: AttentionScope,
    *,
    now: datetime | None = None,
    collaboration: bool = False,
) -> Feed:
    """Run every provider and apply the caller's hides."""
    now = now or datetime.now(timezone.utc)
    ctx = await _prepared_ctx(db, scope, now)
    items: list[Item] = []
    sections: dict[str, dict[str, Any]] = {}
    unlisted: dict[str, int] = {}
    from sessionfs.server.services import attention_collab

    jobs: list[tuple[str, Any]] = [(n, f) for n, f in PROVIDERS if _scope_allows(scope, n)]
    if collaboration:
        jobs.append(("collaboration", attention_collab.provide_collaboration))
    results = await _run_all(ctx, jobs)
    collab: dict[str, Any] | None = None
    for name, (result, error) in results.items():
        if name == "collaboration":
            collab = (
                {"projects": [], "waiting": 0, "error": error}
                if error is not None
                else {**(result or {"projects": [], "waiting": 0}), "error": None}
            )
            continue
        if error is None and result is None:
            continue  # source not installed (approvals before 0.29.6)
        if error is not None:
            sections[name] = {"count": 0, "error": error}
            continue
        got, total = result
        for it in got:
            it.section = name
        items.extend(got)
        sections[name] = {"count": 0, "error": None}
        unlisted[name] = max(0, total - len(got))

    hidden: set[str] = set()
    if items:
        keys = sorted({it.item_key for it in items})
        rows = (
            await db.execute(
                select(AttentionHide.item_key, AttentionHide.version_key, AttentionHide.hidden_until)
                .where(AttentionHide.user_id == scope.user_id, AttentionHide.item_key.in_(keys))
            )
        ).all()
        current = {it.item_key: it.version_key for it in items}
        for key, version, until in rows:
            until = _aware(until)
            if current.get(key) == version and (until is None or until > now):
                hidden.add(key)
    visible = [it for it in items if it.item_key not in hidden]
    for name, sec in sections.items():
        if sec["error"] is None:
            sec["count"] = sum(1 for it in visible if it.section == name) + unlisted.get(name, 0)
    visible.sort(key=Item.sort_key)
    explain_ranking(visible)
    relate_items(visible)
    return Feed(
        items=visible,
        hidden_keys=hidden,
        sections=sections,
        unlisted=unlisted,
        collaboration=collab,
    )


_BAND_TEXT = {
    "now": "On your radar: due now, blocking, or harmful",
    "your_turn": "Your turn: it waits on you",
    "upcoming": "Coming up: due in the next few days",
    "lately": "What changed: informational",
}
_INVOLVEMENT_TEXT = {
    "owner": "You own it",
    "assignee": "Assigned to you or a persona you represent",
    "approver": "You are a named approver",
    "editor": "You can act on it",
    "watcher": "You are watching it",
    "member": "You work in this project",
}


def _rank_parts(it: Item) -> tuple[tuple, tuple, tuple, tuple]:
    k = it.sort_key()
    return (k[0:1], k[1:2], k[2:6], k[6:7])


def explain_ranking(items: list[Item]) -> None:
    """Fill ``Item.rank``: the four ranking factors in order, each with a
    plain sentence, and ``decides`` on the first factor that separates the
    item from its neighbour above (or below, for the first item). Fixed
    rules, no learned weighting."""
    parts = [_rank_parts(it) for it in items]
    for i, it in enumerate(items):
        decides = -1
        other = i - 1 if i > 0 else (i + 1 if i + 1 < len(items) else None)
        if other is not None:
            for f in range(4):
                if parts[i][f] != parts[other][f]:
                    decides = f
                    break
        if it.overdue:
            urgency = "Overdue"
        elif it.due_at is not None:
            urgency = "Has a due date"
        elif it.harm:
            urgency = "A harmful finding is open"
        elif it.priority in ("critical", "high"):
            urgency = f"Priority {it.priority}"
        elif it.priority:
            urgency = f"Priority {it.priority}, no due date"
        else:
            urgency = "No deadline"
        rows: list[dict[str, Any]] = [
            {"factor": "band", "text": _BAND_TEXT.get(it.band, it.band)},
            {"factor": "involvement", "text": _INVOLVEMENT_TEXT.get(it.involvement, it.involvement)},
            {"factor": "urgency", "text": urgency, "due_at": _iso(it.due_at)},
            {"factor": "recency", "text": "Most recent activity", "at": _iso(it.occurred_at)},
        ]
        if 0 <= decides < 4:
            rows[decides]["decides"] = True
        it.rank = rows


def relate_items(items: list[Item], limit: int = 3) -> None:
    """Link each card to up to ``limit`` other cards in the same project (or,
    for cards without one, the same source ticket), so "More context" can
    jump between them. Built from the visible feed only."""
    by_project: dict[str, list[Item]] = {}
    for it in items:
        if it.project_id:
            by_project.setdefault(it.project_id, []).append(it)
    for it in items:
        ctx = it.context if it.context is not None else {}
        peers = [o for o in by_project.get(it.project_id or "", []) if o is not it][:limit]
        ctx["related"] = [
            {"item_key": o.item_key, "title": o.title, "ref": o.source_id, "group": o.display_group}
            for o in peers
        ]
        it.context = ctx


_CHYRON_CATEGORY = {"asking": "mention", "meetings": "meeting", "second": "forgotten", "coming": "reminder"}


def chyron(items: list[Item], limit: int = CHYRON_MAX) -> list[dict[str, Any]]:
    """The "Right now" ticker: the top ``limit`` actionable-or-soon items in
    rank order with a category tag (urgent, approval, mention, meeting,
    reminder, ticket, forgotten). "What changed" items never scroll by."""
    out: list[dict[str, Any]] = []
    for it in items:
        group = it.display_group
        if group == "lately":
            continue
        if it.harm or it.overdue:
            cat = "urgent"
        elif it.kind.startswith("approval") or it.involvement == "approver":
            cat = "approval"
        elif it.kind == "reminder" and group != "asking":
            cat = "reminder"
        else:
            cat = _CHYRON_CATEGORY.get(group, "ticket")
        ref = (it.context or {}).get("ref") or it.source_id
        out.append(
            {"item_key": it.item_key, "category": cat, "text": _cap(it.title, 120), "ref": _cap(str(ref), 60)}
        )
        if len(out) >= limit:
            break
    return out


async def raw_items(
    db: AsyncSession, scope: AttentionScope, *, only: str | None = None
) -> list[Item]:
    """Every live item for the caller, hides NOT applied (hide + context).
    ``only`` runs just the provider that owns a key."""
    now = datetime.now(timezone.utc)
    ctx = await _prepared_ctx(db, scope, now)
    jobs = [(n, f) for n, f in PROVIDERS if (only is None or n == only) and _scope_allows(scope, n)]
    out: list[Item] = []
    for _name, (result, error) in (await _run_all(ctx, jobs)).items():
        if error is None and result is not None:
            out.extend(result[0])
    return out


# /count reads a short-lived per-process cache filled by every feed build,
# so the sidebar badge never builds a second feed right after the page did.
COUNT_CACHE_TTL = 30.0
_count_cache: dict[tuple, tuple[float, dict[str, int]]] = {}


def _count_cache_key(scope: AttentionScope) -> tuple:
    return (scope.user_id, scope.is_service, scope.org_id, scope.project_ids, scope.scopes)


def remember_counts(scope: AttentionScope, counts: dict[str, int]) -> None:
    if len(_count_cache) > 10_000:
        _count_cache.clear()
    _count_cache[_count_cache_key(scope)] = (time.monotonic(), dict(counts))


def forget_counts(scope: AttentionScope) -> None:
    _count_cache.pop(_count_cache_key(scope), None)


async def feed_counts(db: AsyncSession, scope: AttentionScope) -> dict[str, int]:
    hit = _count_cache.get(_count_cache_key(scope))
    if hit is not None and time.monotonic() - hit[0] < COUNT_CACHE_TTL:
        return dict(hit[1])
    counts = feed_response(await build_feed(db, scope), limit=1)["counts"]
    remember_counts(scope, counts)
    return counts


def feed_response(feed: Feed, *, band: str | None = None, limit: int = ITEM_LIMIT) -> dict[str, Any]:
    """The REST/MCP body: up to ``limit`` items (optionally one band) with
    full per-band counts and per-section counts/errors."""
    counts = {b: 0 for b in BANDS}
    for it in feed.items:
        counts[it.band] = counts.get(it.band, 0) + 1
    # Items a provider counted but could not list (the loop feed's own cap)
    # are all "your_turn".
    counts["your_turn"] += sum(feed.unlisted.values())
    counts["total"] = sum(counts[b] for b in BANDS)
    groups = {g: 0 for g in GROUPS}
    for it in feed.items:
        groups[it.display_group] = groups.get(it.display_group, 0) + 1
    # The loop feed's uncounted tail is all "Waiting on you".
    groups["waiting"] += sum(feed.unlisted.values())
    counts["actionable"] = sum(groups[g] for g in ACTIONABLE_GROUPS)
    items = [it for it in feed.items if band is None or it.band == band]
    limit = max(1, min(limit, ITEM_LIMIT))
    body: dict[str, Any] = {
        "items": [it.to_dict() for it in items[:limit]],
        "counts": counts,
        "groups": groups,
        "sections": feed.sections,
        "truncated": len(items) > limit,
        "chyron": chyron(feed.items),
    }
    if feed.collaboration is not None:
        body["collaboration"] = feed.collaboration
    return body


def etag_for(body: dict[str, Any]) -> str:
    raw = json.dumps(body, sort_keys=True, separators=(",", ":"), default=str)
    return '"' + hashlib.sha256(raw.encode()).hexdigest()[:32] + '"'


# ── Hides ──


async def hide_item(
    db: AsyncSession,
    scope: AttentionScope,
    item_key: str,
    *,
    until: datetime | None = None,
) -> bool:
    """Hide ``item_key`` at its current version (idempotent upsert).

    A key that is not in the caller's live feed is a silent no-op (returns
    False, and the route answers the same 204 either way)."""
    if not item_key.startswith("fy_") or len(item_key) > 200:
        return False
    owner = provider_for_key(item_key)
    match = next(
        (it for it in await raw_items(db, scope, only=owner) if it.item_key == item_key), None
    )
    if match is None:
        return False
    values = {
        "user_id": scope.user_id,
        "item_key": item_key,
        "version_key": match.version_key[:200],
        "hidden_until": until,
        "created_at": datetime.now(timezone.utc),
    }
    # One statement: concurrent hides of the same key can't collide.
    if db.get_bind().dialect.name == "postgresql":
        from sqlalchemy.dialects.postgresql import insert as dialect_insert
    else:
        from sqlalchemy.dialects.sqlite import insert as dialect_insert  # type: ignore[assignment]
    stmt = dialect_insert(AttentionHide).values(**values)
    stmt = stmt.on_conflict_do_update(
        index_elements=[AttentionHide.user_id, AttentionHide.item_key],
        set_={k: stmt.excluded[k] for k in ("version_key", "hidden_until", "created_at")},
    )
    await db.execute(stmt)
    await db.commit()
    forget_counts(scope)
    return True


async def unhide_item(db: AsyncSession, scope: AttentionScope, item_key: str) -> None:
    """Undo a hide (or a snooze done as a hide). Unknown keys are a no-op."""
    if not item_key.startswith("fy_") or len(item_key) > 200:
        return
    await db.execute(
        delete(AttentionHide).where(
            AttentionHide.user_id == scope.user_id, AttentionHide.item_key == item_key
        )
    )
    await db.commit()
    forget_counts(scope)


async def sweep_attention_hides(
    db: AsyncSession, *, batch_size: int = SWEEP_BATCH, now: datetime | None = None
) -> dict[str, int]:
    """Delete hides older than 90 days in bounded batches (each commits)."""
    cutoff = (now or datetime.now(timezone.utc)) - HIDE_RETENTION
    deleted = 0
    batches = 0
    for _ in range(SWEEP_MAX_BATCHES):
        keys = (
            await db.execute(
                select(AttentionHide.user_id, AttentionHide.item_key)
                .where(AttentionHide.created_at < cutoff)
                .order_by(AttentionHide.created_at, AttentionHide.user_id, AttentionHide.item_key)
                .limit(batch_size)
            )
        ).all()
        if not keys:
            break
        result = await db.execute(
            delete(AttentionHide).where(
                or_(
                    *[
                        and_(AttentionHide.user_id == u, AttentionHide.item_key == k)
                        for u, k in keys
                    ]
                ),
                AttentionHide.created_at < cutoff,
            )
        )
        await db.commit()
        deleted += int(getattr(result, "rowcount", 0) or 0)
        batches += 1
        if len(keys) < batch_size:
            break
    return {"deleted": deleted, "batches": batches}


# ── "Before you start" context ──


_STOPWORDS = frozenset(
    "the and for with from that this into when then than have has not are was "
    "were will should could would about after before over under add fix make "
    "update remove ticket task issue".split()
)


def _keywords(title: str, limit: int = 5) -> list[str]:
    seen: list[str] = []
    for word in "".join(c if c.isalnum() else " " for c in title.lower()).split():
        if len(word) >= 4 and word not in _STOPWORDS and word not in seen:
            seen.append(word)
        if len(seen) >= limit:
            break
    return seen


def _like(word: str) -> str:
    return "%" + word.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


async def item_context(
    db: AsyncSession, scope: AttentionScope, item_key: str
) -> dict[str, Any] | None:
    """Related knowledge + the caller's past sessions for an assigned ticket
    card. None (one 404) for any key that is not a ticket card in the
    caller's live feed, or whose ticket the caller can no longer read."""
    if not item_key.startswith("fy_") or len(item_key) > 200:
        return None
    if provider_for_key(item_key) != "tickets":
        return None
    match = next(
        (
            it
            for it in await raw_items(db, scope, only="tickets")
            if it.item_key == item_key and it.kind == "ticket"
        ),
        None,
    )
    if match is None:
        return None
    ticket = (
        await db.execute(select(Ticket).where(Ticket.id == match.source_id))
    ).scalar_one_or_none()
    if ticket is None:
        return None
    project = (
        await db.execute(select(Project).where(Project.id == ticket.project_id))
    ).scalar_one_or_none()
    if (
        project is None
        or not scope.allows_project(project)
        or not await user_can_access_project(db, scope.user_id, project)
    ):
        return None
    tiers = await resolve_project_plan_tiers([project], db)
    if "agent_tickets" not in get_features_for_tier(tiers.get(project.id, Tier.FREE)):
        return None

    words = _keywords(ticket.title or "")
    knowledge: list[dict[str, Any]] = []
    if words and (not scope.is_service or "knowledge:read" in scope.scopes):
        rows = (
            await db.execute(
                select(KnowledgeEntry)
                .where(
                    KnowledgeEntry.project_id == project.id,
                    KnowledgeEntry.dismissed.is_(False),
                    KnowledgeEntry.superseded_by.is_(None),
                    KnowledgeEntry.claim_class == "claim",
                    or_(*[KnowledgeEntry.content.ilike(_like(w), escape="\\") for w in words]),
                )
                .order_by(KnowledgeEntry.created_at.desc(), KnowledgeEntry.id.desc())
                .limit(5)
            )
        ).scalars().all()
        knowledge = [
            {
                "id": k.id,
                "entry_type": k.entry_type,
                "content": _cap(k.content, 400),
                "created_at": _iso(k.created_at),
            }
            for k in rows
        ]
    sessions: list[dict[str, Any]] = []
    if not scope.is_service or "sessions:read" in scope.scopes:
        related = [Session.ticket_id == ticket.id]
        related += [Session.title.ilike(_like(w), escape="\\") for w in words]
        rows2 = (
            await db.execute(
                select(Session.id, Session.title, Session.source_tool, Session.updated_at)
                .where(
                    Session.user_id == scope.user_id,
                    Session.is_deleted.is_(False),
                    project_session_clause(project, legacy_only=True),
                    or_(*related),
                )
                .order_by(Session.updated_at.desc(), Session.id.desc())
                .limit(5)
            )
        ).all()
        sessions = [
            {
                "id": s.id,
                "title": _cap(s.title, 200),
                "source_tool": s.source_tool,
                "updated_at": _iso(s.updated_at),
                "link": f"/sessions/{quote(s.id, safe='')}",
            }
            for s in rows2
        ]
    return {
        "item_key": item_key,
        "ticket": {
            "id": ticket.id,
            "project_id": project.id,
            "title": _cap(ticket.title),
            "status": ticket.status,
        },
        "knowledge": knowledge,
        "sessions": sessions,
    }
