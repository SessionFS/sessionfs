"""Ownership-scoped full-text session search.

Factored out of ``routes/sessions.py`` so the API route and the session
helper tools share one access-checked implementation.  Sessions are always
filtered to the caller's own rows (``Session.user_id == user_id``, not
deleted); there is no admin super-view.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import ColumnElement, func, literal, literal_column, null, or_, select
from sqlalchemy import text as sa_text
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server.db.models import Session

SOURCE_TOOL_ALIASES: dict[str, tuple[str, ...]] = {
    "gemini": ("gemini", "gemini-cli"),
    "gemini-cli": ("gemini", "gemini-cli"),
    "copilot": ("copilot", "copilot-cli"),
    "copilot-cli": ("copilot", "copilot-cli"),
}


def source_tool_filter_values(source_tool: str) -> tuple[str, ...]:
    """Return the ``source_tool`` values that should be treated as equivalent.

    The dashboard uses friendlier filter values like ``gemini`` and ``copilot``,
    while captured sessions may be stored as ``gemini-cli`` / ``copilot-cli``.
    Treat those as one logical family so the filter behaves how users expect.
    """
    return SOURCE_TOOL_ALIASES.get(source_tool.lower(), (source_tool,))


@dataclass(frozen=True)
class SessionSearchHit:
    """One search result row, already scoped to the caller's own sessions."""

    session_id: str
    title: str | None
    alias: str | None
    source_tool: str
    model_id: str | None
    message_count: int
    updated_at: datetime
    snippet: str
    project_id: str | None = None
    messages_text: str | None = None
    model_provider: str | None = None


async def search_sessions(
    db: AsyncSession,
    *,
    user_id: str,
    query: str,
    tool: str | None = None,
    days: int | None = None,
    limit: int = 20,
    offset: int = 0,
    project_id: str | None = None,
    session_ids: tuple[str, ...] | None = None,
    session_clause: ColumnElement[bool] | None = None,
    include_text: bool = False,
) -> tuple[list[SessionSearchHit], int]:
    """Full-text search across the caller's own sessions.

    Uses PostgreSQL ``ts_vector`` in production and a LIKE fallback on SQLite.
    ``project_id`` / ``session_ids`` / ``session_clause`` are optional hard
    filters (the helper's scope); when supplied they narrow the search, never
    widen it.  ``session_clause`` is any SQL predicate over ``Session`` (the
    helper passes ``project_session_clause`` so a project filter never has to
    be materialized into a list of ids).  Returns ``(hits, total)`` where
    ``total`` ignores the page window.

    ``include_text`` controls whether the full ``messages_text`` column is
    selected into each hit.  The public search route only needs the SQL-side
    snippet, so it leaves this off and avoids materializing the full text; the
    session helper turns it on because it builds its own redacted snippet from
    the full text.
    """
    dialect = db.bind.dialect.name if db.bind else "sqlite"

    conditions: list[ColumnElement[bool]] = [
        Session.user_id == user_id,
        Session.is_deleted == False,  # noqa: E712
    ]
    if tool:
        conditions.append(Session.source_tool.in_(source_tool_filter_values(tool)))
    if session_ids:
        conditions.append(Session.id.in_(session_ids))
    if session_clause is not None:
        conditions.append(session_clause)
    if project_id is not None:
        conditions.append(Session.project_id == project_id)
    if days is not None:
        # Kept as a string comparison bound exactly as before (an ISO cutoff,
        # cast to timestamptz on PostgreSQL).
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        if dialect == "postgresql":
            conditions.append(
                sa_text("sessions.updated_at >= CAST(:cutoff AS timestamptz)").bindparams(
                    cutoff=cutoff
                )
            )
        else:
            conditions.append(sa_text("sessions.updated_at >= :cutoff").bindparams(cutoff=cutoff))

    text_col = Session.messages_text if include_text else null()
    if dialect == "postgresql":
        search_vector = literal_column("sessions.search_vector")

        def tsquery() -> ColumnElement:
            return func.plainto_tsquery(literal_column("'english'"), query)

        conditions.append(search_vector.op("@@")(tsquery()))
        snippet = func.ts_headline(
            literal_column("'english'"),
            Session.messages_text,
            tsquery(),
            "MaxWords=30, MinWords=15, StartSel=<mark>, StopSel=</mark>",
        )
        order_by: list[ColumnElement] = [
            func.ts_rank(search_vector, tsquery()).desc(),
            Session.updated_at.desc(),
            Session.id.asc(),
        ]
    else:
        pattern = literal("%") + func.lower(literal(query)) + literal("%")
        conditions.append(
            or_(
                func.lower(Session.title).like(pattern),
                func.lower(Session.messages_text).like(pattern),
            )
        )
        snippet = func.substr(
            Session.messages_text,
            func.max(
                1,
                func.instr(func.lower(Session.messages_text), func.lower(literal(query))) - 40,
            ),
            100,
        )
        order_by = [Session.updated_at.desc(), Session.id.asc()]

    search_stmt = (
        select(
            Session.id,
            Session.title,
            Session.alias,
            Session.source_tool,
            Session.model_id,
            Session.model_provider,
            Session.message_count,
            Session.updated_at,
            Session.project_id,
            text_col.label("messages_text"),
            snippet.label("snippet"),
        )
        .where(*conditions)
        # Rank (PG) then recency, with the id as a final tiebreaker so pages
        # never repeat or skip rows that tie.
        .order_by(*order_by)
        .limit(limit)
        .offset(offset)
    )
    count_stmt = select(func.count()).select_from(Session).where(*conditions)

    rows = (await db.execute(search_stmt)).fetchall()
    total = (await db.execute(count_stmt)).scalar() or 0

    hits = [
        SessionSearchHit(
            session_id=row.id,
            title=row.title,
            alias=row.alias,
            source_tool=row.source_tool,
            model_id=row.model_id,
            message_count=row.message_count,
            updated_at=row.updated_at,
            snippet=row.snippet or "",
            project_id=row.project_id,
            messages_text=row.messages_text,
            model_provider=row.model_provider,
        )
        for row in rows
    ]
    return hits, total
