"""Existing-ticket suggestions for meeting and chat action items.

Suggest, never auto-link: extraction attaches at most one *suggested* ticket
to an action item; only a person's explicit Link ties them together.

Two deterministic, lexical pieces (no server-side LLM key, no embeddings):

- ``ticket_candidates`` + ``shortlist``: before the extraction call, the
  newest ``SCAN_LIMIT`` tickets in the conversation's scope (by
  ``updated_at``) are ranked by BM25 against the transcript and the top
  ``SHORTLIST_SIZE`` go into the prompt. The model may pick one of those ids
  per item; any id outside the shortlist is dropped.
- ``best_text_match``: when the model picked nothing for an item, the item's
  own text is compared with every scanned ticket's title (IDF-weighted Dice
  over stemmed content words). A suggestion is made only above
  ``TEXT_MATCH_THRESHOLD``, with at least ``MIN_SHARED_TERMS`` shared terms
  and a clear margin over the runner-up, so an ambiguous or generic item
  suggests nothing.

Scope (``scope_ticket_query``): a project conversation → that project's
tickets; a group conversation → tickets of the group's member projects the
caller can read (``accessible_project_ids_subquery``). Cancelled tickets are
left out; done / closed tickets only when resolved in the last
``RECENT_DONE_DAYS`` days, so "this was already fixed" is visible. The same
scope query re-checks visibility on every read and on link, so a ticket the
caller can no longer read is never shown and never named.
"""

from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server.auth.dependencies import AuthContext
from sessionfs.server.db.models import (
    MeetingTranscript,
    Project,
    ProjectGroupAccess,
    Ticket,
)
from sessionfs.server.helper.docs import _STOP_WORDS, _stem, _tokenize

# Newest tickets (by updated_at) scanned per extraction.
SCAN_LIMIT = 500
# Tickets handed to the model.
SHORTLIST_SIZE = 30
# Done / closed tickets resolved within this many days stay candidates.
RECENT_DONE_DAYS = 90
# Deterministic fallback: IDF-weighted Dice, 0..1.
TEXT_MATCH_THRESHOLD = 0.5
MIN_SHARED_TERMS = 2
TEXT_MATCH_MARGIN = 0.05

# Characters of a ticket description that count towards the shortlist rank.
_DESCRIPTION_CHARS = 400
# Transcript characters considered for the shortlist query (the store caps a
# transcript at 512 KiB; this keeps the rank linear and bounded).
_TRANSCRIPT_CHARS = 512 * 1024
_TITLE_REPEAT = 2
_K1 = 1.2
_B = 0.75

_DONE_STATUSES = ("done", "closed")

# Words every action item and ticket title share; they carry no identity.
_GENERIC = frozenset(
    _stem(w)
    for w in (
        "action", "item", "ticket", "task", "issue", "need", "needs", "make",
        "sure", "please", "also", "just", "get", "let", "will", "going",
        "todo", "follow", "followup", "next", "week", "today", "tomorrow",
        "someone", "team",
    )
)


def terms(text: str) -> list[str]:
    """Stemmed content words of ``text`` (stop and generic words dropped)."""
    out: list[str] = []
    for word in _tokenize(text or ""):
        if word in _STOP_WORDS or len(word) < 2 or word.isdigit():
            continue
        stem = _stem(word)
        if stem in _GENERIC:
            continue
        out.append(stem)
    return out


@dataclass
class Candidate:
    id: str
    project_id: str
    title: str
    status: str
    title_terms: frozenset[str] = field(default_factory=frozenset)
    doc_terms: Counter = field(default_factory=Counter)
    length: int = 0


def suggestible_status_clause() -> object:
    """Status predicate: not cancelled; done/closed only if recently resolved."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=RECENT_DONE_DAYS)
    return and_(
        Ticket.status != "cancelled",
        or_(
            Ticket.status.not_in(_DONE_STATUSES),
            func.coalesce(Ticket.resolved_at, Ticket.updated_at) >= cutoff,
        ),
    )


async def scope_project_ids(
    db: AsyncSession, meeting: MeetingTranscript, auth: AuthContext
) -> list[str]:
    """Projects whose tickets an item of ``meeting`` may suggest or link.

    A project conversation: its project (the caller already passed the
    meeting's access check). A group conversation: the group's live member
    projects the caller can read, merge tombstones excluded. Service keys
    never reach a group conversation.
    """
    if meeting.project_id is not None:
        return [meeting.project_id]
    if meeting.group_id is None or auth.key_kind == "service":
        return []
    from sessionfs.server.auth.project_access import accessible_project_ids_subquery

    readable = await accessible_project_ids_subquery(db, auth.user.id)
    rows = (
        await db.execute(
            select(Project.id)
            .join(ProjectGroupAccess, ProjectGroupAccess.project_id == Project.id)
            .where(
                ProjectGroupAccess.group_id == meeting.group_id,
                Project.merged_into_project_id.is_(None),
                Project.id.in_(readable),
            )
        )
    ).scalars().all()
    return sorted(set(rows))


async def visible_tickets(
    db: AsyncSession,
    meeting: MeetingTranscript,
    auth: AuthContext,
    ticket_ids: set[str],
) -> dict[str, Ticket]:
    """The subset of ``ticket_ids`` the caller may see from this conversation
    (in its scope and readable). Anything else is simply absent, so an
    unreadable or moved ticket looks exactly like a missing one."""
    ids = {t for t in ticket_ids if t}
    if not ids:
        return {}
    project_ids = await scope_project_ids(db, meeting, auth)
    if not project_ids:
        return {}
    rows = (
        await db.execute(
            select(Ticket)
            .where(Ticket.id.in_(sorted(ids)), Ticket.project_id.in_(project_ids))
            .execution_options(populate_existing=True)
        )
    ).scalars().all()
    return {t.id: t for t in rows}


async def ticket_candidates(
    db: AsyncSession, meeting: MeetingTranscript, auth: AuthContext
) -> list[Candidate]:
    """The newest ``SCAN_LIMIT`` suggestible tickets in scope."""
    project_ids = await scope_project_ids(db, meeting, auth)
    if not project_ids:
        return []
    rows = (
        await db.execute(
            select(
                Ticket.id,
                Ticket.project_id,
                Ticket.title,
                Ticket.status,
                func.substr(Ticket.description, 1, _DESCRIPTION_CHARS),
            )
            .where(Ticket.project_id.in_(project_ids), suggestible_status_clause())
            .order_by(Ticket.updated_at.desc(), Ticket.id.desc())
            .limit(SCAN_LIMIT)
        )
    ).all()
    out: list[Candidate] = []
    for tid, pid, title, status, description in rows:
        title_terms = terms(title or "")
        doc = Counter(title_terms * _TITLE_REPEAT + terms(description or ""))
        out.append(
            Candidate(
                id=tid,
                project_id=pid,
                title=title or "",
                status=status or "",
                title_terms=frozenset(title_terms),
                doc_terms=doc,
                length=sum(doc.values()),
            )
        )
    return out


def shortlist(candidates: list[Candidate], text: str, size: int = SHORTLIST_SIZE) -> list[Candidate]:
    """Rank ``candidates`` by BM25 against ``text`` (each distinct transcript
    term is one query term) and keep the top ``size`` that share any term."""
    if not candidates:
        return []
    query = set(terms(text[:_TRANSCRIPT_CHARS]))
    if not query:
        return []
    n = len(candidates)
    df: Counter = Counter()
    for c in candidates:
        df.update(set(c.doc_terms))
    avg = (sum(c.length for c in candidates) / n) or 1.0
    scored: list[tuple[float, int, Candidate]] = []
    for order, c in enumerate(candidates):
        score = 0.0
        for term, tf in c.doc_terms.items():
            if term not in query:
                continue
            idf = math.log(1 + (n - df[term] + 0.5) / (df[term] + 0.5))
            score += idf * tf * (_K1 + 1) / (tf + _K1 * (1 - _B + _B * c.length / avg))
        if score > 0:
            # Ties keep the newer ticket first (candidates arrive newest first).
            scored.append((score, -order, c))
    scored.sort(key=lambda s: (s[0], s[1]), reverse=True)
    return [c for _, _, c in scored[:size]]


def _idf_table(candidates: list[Candidate]) -> tuple[Counter, int]:
    df: Counter = Counter()
    for c in candidates:
        df.update(c.title_terms)
    return df, len(candidates)


def _idf(term: str, df: Counter, n: int) -> float:
    return math.log(1 + (n + 1) / (df.get(term, 0) + 0.5))


def similarity(item_text: str, candidate: Candidate, df: Counter, n: int) -> tuple[float, int]:
    """IDF-weighted Dice between an item's text and a ticket title:
    ``(score 0..1, shared term count)``."""
    item_terms = set(terms(item_text))
    if not item_terms or not candidate.title_terms:
        return 0.0, 0
    shared = item_terms & candidate.title_terms
    if not shared:
        return 0.0, 0
    weight = sum(_idf(t, df, n) for t in shared)
    total = sum(_idf(t, df, n) for t in item_terms) + sum(
        _idf(t, df, n) for t in candidate.title_terms
    )
    return (2 * weight / total if total else 0.0), len(shared)


class Matcher:
    """Scores items against one scanned candidate set (IDF computed once)."""

    def __init__(self, candidates: list[Candidate]) -> None:
        self.candidates = candidates
        self.by_id = {c.id: c for c in candidates}
        self.df, self.n = _idf_table(candidates)

    def score(self, item_text: str, ticket_id: str) -> float:
        candidate = self.by_id.get(ticket_id)
        if candidate is None:
            return 0.0
        return round(similarity(item_text, candidate, self.df, self.n)[0], 4)

    def best_text_match(self, item_text: str) -> tuple[Candidate, float] | None:
        """The one clearly best title match above the threshold, else None."""
        ranked: list[tuple[float, int, Candidate]] = []
        for c in self.candidates:
            score, shared = similarity(item_text, c, self.df, self.n)
            if score > 0:
                ranked.append((score, shared, c))
        if not ranked:
            return None
        ranked.sort(key=lambda r: r[0], reverse=True)
        best_score, best_shared, best = ranked[0]
        if best_score < TEXT_MATCH_THRESHOLD or best_shared < MIN_SHARED_TERMS:
            return None
        if len(ranked) > 1 and best_score - ranked[1][0] < TEXT_MATCH_MARGIN:
            return None
        return best, round(best_score, 4)
