"""Deterministic project-group knowledge context (GitHub #101, phase 2).

A group's overview is built WITHOUT an LLM: for every member project the
compiler can read, it takes the project's compiled context summary plus its
highest-value active claims (at most ``CLAIMS_PER_PROJECT``, picked in SQL)
grouped by entry type, and writes a single markdown document onto
``ProjectGroup.context_document``. The document is capped at a word budget
(default 20,000 words); when it would overflow, the budget is split fairly
across projects and a truncation note records how many claims were left out.
The build is linear in its input and fully deterministic given the same
inputs — the scheduled sweep and a manual compile of an unchanged group
produce byte-identical output.

The same module hosts the group-wide reads (knowledge search and page list),
which share the same "readable member projects" boundary: a caller only ever
sees content from member projects they can already read on their own.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from sqlalchemy import func, select
from starlette.concurrency import run_in_threadpool

from sessionfs.server.auth.project_access import (
    accessible_project_ids_subquery,
)
from sessionfs.server.db.models import (
    KnowledgeEntry,
    KnowledgePage,
    Project,
    ProjectGroup,
    ProjectGroupAccess,
)
from sessionfs.server.services.compiler import SECTION_MAP

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

# Cap for the deterministic group overview. Configurable per-call so tests can
# exercise the truncation path without minting tens of thousands of words.
GROUP_CONTEXT_WORD_BUDGET = 20_000

# Words (as rendered, quote markers included) of a project's compiled context
# quoted as its summary.
PROJECT_SUMMARY_WORDS = 250

# Share of a project's word allotment its context summary may use when the
# document is being trimmed, so its claims still appear in a large group.
SUMMARY_SHARE_WHEN_TRIMMED = 0.4

# Active claims considered per member project: the highest-value N, selected
# in SQL so a project with thousands of claims never loads them all.
CLAIMS_PER_PROJECT = 50

# Active-claim filter shared by the compiler, search and (via the KB) pages.
_ACTIVE_CLAIMS = (
    KnowledgeEntry.dismissed == False,  # noqa: E712
    KnowledgeEntry.claim_class == "claim",
    KnowledgeEntry.superseded_by.is_(None),
    KnowledgeEntry.freshness_class.in_(("current", "aging")),
)

# The valid entry types, ordered for deterministic section output.
_ENTRY_TYPES = (
    "decision",
    "pattern",
    "discovery",
    "convention",
    "bug",
    "dependency",
)

_NO_CONTEXT = "_(no compiled context yet)_"
_SUMMARY_LABEL = "_Context summary:_"


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _words(line: str) -> int:
    return len(line.split())


def _one_line(text: str) -> str:
    """Collapse whitespace (including newlines) so a value can't break the
    markdown structure it is embedded in."""
    return " ".join((text or "").split())


@dataclass(frozen=True)
class ClaimInput:
    """The fields of one active claim the document builder needs."""

    id: int
    entry_type: str
    content: str
    confidence: float
    relevant_ts: float
    has_entity: int


@dataclass(frozen=True)
class ProjectInput:
    """One readable member project: name, compiled context, top claims."""

    id: str
    name: str
    context: str
    claims: tuple[ClaimInput, ...]


@dataclass
class GroupDocument:
    text: str
    projects: int
    claims_emitted: int
    claims_omitted: int
    truncated: bool


def _claim_priority_key(claim: ClaimInput) -> tuple:
    """Value order: confidence, then recency, then entity-bound, then id
    (mirrors ``compiler._priority_key``; sorted with ``reverse=True``)."""
    return (claim.confidence, claim.relevant_ts, claim.has_entity, claim.id)


def _quote_summary(context: str, cap: int) -> list[str]:
    """The leading lines of a project's compiled context as a blockquote,
    at most ``cap`` words as rendered (each ``>`` marker counts as a word).

    Quoting keeps the project's own headings from breaking the group
    document's structure. A cut line ends with an ellipsis."""
    text = (context or "").strip()
    if not text or cap <= 0:
        return []
    out: list[str] = []
    used = 0
    for raw in text.splitlines():
        raw = raw.rstrip()
        words = raw.split()
        cost = 1 + len(words)
        if used + cost <= cap:
            out.append(f"> {raw}" if words else ">")
            used += cost
            continue
        room = cap - used - 2  # the ">" marker and the ellipsis
        if room >= 1:
            out.append("> " + " ".join(words[:room]) + " …")
        elif cap - used >= 2:
            out.append("> …")
        break
    return out


def _lines_words(lines: list[str]) -> int:
    return sum(_words(line) for line in lines)


@dataclass
class _ProjectPlan:
    heading: str
    base: int  # project heading + summary label
    summary_full: int  # words of the uncapped (≤ PROJECT_SUMMARY_WORDS) summary
    claim_lines: list[tuple[ClaimInput, str, int]]  # (claim, line, words)
    type_headings: dict[str, tuple[str, int]]
    demand: int


def _plan_project(project: ProjectInput) -> _ProjectPlan:
    name = _one_line(project.name) or project.id
    heading = f"## {name}"
    base = _words(heading) + _words(_SUMMARY_LABEL)
    summary = _quote_summary(project.context, PROJECT_SUMMARY_WORDS)
    summary_full = _lines_words(summary) if summary else _words(_NO_CONTEXT)

    claims = sorted(project.claims, key=_claim_priority_key, reverse=True)
    claim_lines = []
    type_headings: dict[str, tuple[str, int]] = {}
    demand = base + summary_full
    for claim in claims:
        line = f"- [{name}/claim #{claim.id}] {_one_line(claim.content)}"
        cost = _words(line)
        claim_lines.append((claim, line, cost))
        if claim.entry_type not in type_headings:
            title = SECTION_MAP.get(claim.entry_type, f"## {claim.entry_type.title()}")
            type_line = f"### {title.lstrip('# ').strip()}"
            type_headings[claim.entry_type] = (type_line, _words(type_line))
            cost += type_headings[claim.entry_type][1]
        demand += cost
    return _ProjectPlan(heading, base, summary_full, claim_lines, type_headings, demand)


def _fair_allotments(demands: list[int], budget: int) -> list[int]:
    """Max-min fair split of ``budget`` words: small projects get everything
    they need, the rest share what is left equally, so one large project can
    never starve the projects after it."""
    alloc = [0] * len(demands)
    remaining = max(0, budget)
    left = len(demands)
    for i in sorted(range(len(demands)), key=lambda i: (demands[i], i)):
        share = remaining // left
        give = min(demands[i], share)
        alloc[i] = give
        remaining -= give
        left -= 1
    return alloc


def _render_project(
    project: ProjectInput, plan: _ProjectPlan, allotment: int
) -> tuple[list[str], int, int]:
    """(lines, claims emitted, claims omitted) for one project within its
    word allotment. The project heading and summary label always render.

    When the project gets less than it needs, the summary may use at most
    ``SUMMARY_SHARE_WHEN_TRIMMED`` of the allotment, and claims are taken in
    value order, skipping any single claim too big for what is left so the
    smaller ones after it can still fit."""
    lines = [plan.heading, "", _SUMMARY_LABEL]
    remaining = allotment - plan.base
    summary_cap = min(PROJECT_SUMMARY_WORDS, max(0, remaining))
    if allotment < plan.demand:
        summary_cap = min(summary_cap, int(allotment * SUMMARY_SHARE_WHEN_TRIMMED))
    summary = _quote_summary(project.context, summary_cap)
    if summary:
        lines.extend(summary)
        remaining -= _lines_words(summary)
    elif (project.context or "").strip():
        lines.append("> …")
        remaining -= 2
    else:
        lines.append(_NO_CONTEXT)
        remaining -= _words(_NO_CONTEXT)

    kept: dict[str, list[str]] = {}
    emitted = 0
    for claim, line, cost in plan.claim_lines:
        extra = 0 if claim.entry_type in kept else plan.type_headings[claim.entry_type][1]
        if cost + extra > remaining:
            continue
        kept.setdefault(claim.entry_type, []).append(line)
        remaining -= cost + extra
        emitted += 1

    lines.append("")
    for entry_type in _ENTRY_TYPES + tuple(sorted(set(kept) - set(_ENTRY_TYPES))):
        if entry_type not in kept:
            continue
        lines.append(plan.type_headings[entry_type][0])
        lines.extend(kept[entry_type])
        lines.append("")
    return lines, emitted, len(plan.claim_lines) - emitted


def _footer(claim_count: int, project_count: int, now: datetime) -> str:
    stamp = now.strftime("%Y-%m-%d %H:%M UTC")
    return f"Shaped by {claim_count} claims from {project_count} projects · last compile {stamp}"


def _truncation_note(max_words: int, omitted: int) -> str:
    return (
        f"_…(truncated: the group overview exceeded its {max_words}-word budget; "
        f"{omitted} lower-priority claims omitted)_"
    )


def build_group_document(
    group_name: str,
    projects: list[ProjectInput],
    *,
    now: datetime,
    max_words: int = GROUP_CONTEXT_WORD_BUDGET,
) -> GroupDocument:
    """Render the deterministic group overview in linear time.

    Every project's demand (words) is measured once; when the total exceeds
    the budget the words are split fairly across projects
    (``_fair_allotments``) and each project keeps its highest-value claims
    within its share. The header, every project heading, the footer and the
    truncation note always render, so the markdown structure survives any
    budget. The footer reports the claims actually emitted."""
    header = [f"# {_one_line(group_name)} — Group Knowledge Context", ""]
    plans = [_plan_project(p) for p in projects]
    footer_words = 2 + _words(_footer(0, 0, now))  # "---" + footer
    fixed = _lines_words(header) + footer_words
    total_demand = sum(plan.demand for plan in plans)

    truncated = fixed + total_demand > max_words
    if truncated:
        budget = max_words - fixed - _words(_truncation_note(max_words, 0))
        allotments = _fair_allotments([plan.demand for plan in plans], budget)
    else:
        allotments = [plan.demand for plan in plans]

    lines = list(header)
    emitted = omitted = 0
    for project, plan, allotment in zip(projects, plans, allotments):
        body, kept, dropped = _render_project(project, plan, allotment)
        lines.extend(body)
        emitted += kept
        omitted += dropped
    if truncated:
        lines.append(_truncation_note(max_words, omitted))
        lines.append("")
    lines.append("---")
    lines.append("")
    lines.append(_footer(emitted, len(projects), now))
    return GroupDocument(
        text="\n".join(lines) + "\n",
        projects=len(projects),
        claims_emitted=emitted,
        claims_omitted=omitted,
        truncated=truncated,
    )


async def _readable_member_projects(
    db: AsyncSession, group: ProjectGroup, user_id: str
) -> list[Project]:
    """Member projects of ``group`` the caller can read, ordered by name/id."""
    readable = await accessible_project_ids_subquery(db, user_id)
    return list(
        (
            await db.execute(
                select(Project)
                .join(ProjectGroupAccess, ProjectGroupAccess.project_id == Project.id)
                .where(
                    ProjectGroupAccess.group_id == group.id,
                    Project.org_id == group.org_id,
                    Project.id.in_(readable),
                )
                .order_by(Project.name, Project.id)
            )
        ).scalars().all()
    )


async def _top_claims(
    db: AsyncSession, project_ids: list[str], per_project: int
) -> dict[str, list[ClaimInput]]:
    """The ``per_project`` highest-value active claims of each project, picked
    in SQL with a window function (confidence, then
    ``coalesce(last_relevant_at, created_at)``, then id)."""
    if not project_ids:
        return {}
    rank = (
        func.row_number()
        .over(
            partition_by=KnowledgeEntry.project_id,
            # Same priority as ``_claim_priority_key``: confidence, then
            # relevance recency, then id.
            order_by=(
                KnowledgeEntry.confidence.desc(),
                func.coalesce(
                    KnowledgeEntry.last_relevant_at, KnowledgeEntry.created_at
                ).desc(),
                KnowledgeEntry.id.desc(),
            ),
        )
        .label("rank")
    )
    ranked = (
        select(
            KnowledgeEntry.id,
            KnowledgeEntry.project_id,
            KnowledgeEntry.entry_type,
            KnowledgeEntry.content,
            KnowledgeEntry.confidence,
            KnowledgeEntry.created_at,
            KnowledgeEntry.last_relevant_at,
            KnowledgeEntry.entity_ref,
            rank,
        )
        .where(KnowledgeEntry.project_id.in_(project_ids), *_ACTIVE_CLAIMS)
        .subquery()
    )
    rows = (
        await db.execute(select(ranked).where(ranked.c.rank <= per_project))
    ).all()
    out: dict[str, list[ClaimInput]] = defaultdict(list)
    for row in rows:
        rel = row.last_relevant_at or row.created_at
        out[row.project_id].append(
            ClaimInput(
                id=row.id,
                entry_type=row.entry_type,
                content=row.content or "",
                confidence=float(row.confidence or 0.0),
                relevant_ts=rel.timestamp() if rel is not None else 0.0,
                has_entity=1 if row.entity_ref else 0,
            )
        )
    return out


async def compile_group_context(
    db: AsyncSession,
    group: ProjectGroup,
    compiler_user_id: str,
    *,
    now: datetime | None = None,
    max_words: int = GROUP_CONTEXT_WORD_BUDGET,
    claims_per_project: int = CLAIMS_PER_PROJECT,
) -> dict:
    """Build the deterministic group overview and write it onto ``group``.

    Every member project is considered, but only those the compiler can read
    contribute content (and are recorded in ``context_sources``). Each
    project contributes at most ``claims_per_project`` claims. Returns
    ``{"projects", "claims", "claims_omitted", "truncated"}`` where
    ``claims`` is the number of claims actually written into the document.
    Does not commit.
    """
    now = now or _now_utc()
    projects = await _readable_member_projects(db, group, compiler_user_id)
    claims = await _top_claims(db, [p.id for p in projects], claims_per_project)
    inputs = [
        ProjectInput(
            id=p.id,
            name=p.name,
            context=p.context_document or "",
            claims=tuple(claims.get(p.id, ())),
        )
        for p in projects
    ]
    # Pure CPU work on plain values: keep it off the event loop.
    doc = await run_in_threadpool(
        build_group_document, group.name, inputs, now=now, max_words=max_words
    )

    group.context_document = doc.text
    group.context_compiled_at = now
    group.compiled_by_user_id = compiler_user_id
    group.context_sources = [p.id for p in projects]
    return {
        "projects": doc.projects,
        "claims": doc.claims_emitted,
        "claims_omitted": doc.claims_omitted,
        "truncated": doc.truncated,
    }


async def search_group_knowledge(
    db: AsyncSession,
    group: ProjectGroup,
    user_id: str,
    *,
    query: str | None = None,
    entry_type: str | None = None,
    limit: int = 100,
) -> list[dict]:
    """Active claims across the group's readable member projects.

    Each hit carries the source project id + name, the entry id, type,
    confidence, freshness class, the matching content and created_at."""
    projects = await _readable_member_projects(db, group, user_id)
    if not projects:
        return []
    by_id = {p.id: p.name for p in projects}

    stmt = (
        select(KnowledgeEntry)
        .where(KnowledgeEntry.project_id.in_(list(by_id)), *_ACTIVE_CLAIMS)
        .order_by(KnowledgeEntry.confidence.desc(), KnowledgeEntry.id.desc())
        .limit(max(1, min(limit, 100)))
    )
    if query is not None:
        q = query.strip()
        if q:
            # autoescape: a literal % or _ in the query matches itself.
            stmt = stmt.where(KnowledgeEntry.content.icontains(q, autoescape=True))
    if entry_type is not None:
        stmt = stmt.where(KnowledgeEntry.entry_type == entry_type)

    entries = list((await db.execute(stmt)).scalars().all())
    return [
        {
            "project_id": entry.project_id,
            "project_name": by_id[entry.project_id],
            "entry_id": entry.id,
            "entry_type": entry.entry_type,
            "confidence": entry.confidence,
            "freshness": entry.freshness_class,
            "created_at": entry.created_at,
            "content": entry.content,
        }
        for entry in entries
    ]


async def list_group_pages(
    db: AsyncSession, group: ProjectGroup, user_id: str, *, limit: int = 200
) -> list[dict]:
    """Wiki pages across the group's readable member projects, newest first
    (at most ``limit``, clamped to 1–500)."""
    projects = await _readable_member_projects(db, group, user_id)
    if not projects:
        return []
    by_id = {p.id: p.name for p in projects}

    pages = list(
        (
            await db.execute(
                select(KnowledgePage)
                .where(KnowledgePage.project_id.in_(list(by_id)))
                .order_by(KnowledgePage.updated_at.desc(), KnowledgePage.id)
                .limit(max(1, min(limit, 500)))
            )
        ).scalars().all()
    )
    return [
        {
            "slug": page.slug,
            "title": page.title,
            "project_id": page.project_id,
            "project_name": by_id[page.project_id],
            "updated_at": page.updated_at,
        }
        for page in pages
    ]
