"""Ownership-scoped full-text session search.

Factored out of ``routes/sessions.py`` so the API route and the session
helper tools share one access-checked implementation.  Sessions are always
filtered to the caller's own rows (``Session.user_id == user_id``, not
deleted); there is no admin super-view.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import text as sa_text
from sqlalchemy.ext.asyncio import AsyncSession

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
    include_text: bool = False,
) -> tuple[list[SessionSearchHit], int]:
    """Full-text search across the caller's own sessions.

    Uses PostgreSQL ``ts_vector`` in production and a LIKE fallback on SQLite.
    ``project_id`` / ``session_ids`` are optional hard filters (the helper's
    scope); when supplied they narrow the search, never widen it.  Returns
    ``(hits, total)`` where ``total`` ignores the page window.

    ``include_text`` controls whether the full ``messages_text`` column is
    selected into each hit.  The public search route only needs the SQL-side
    snippet, so it leaves this off and avoids materializing the full text; the
    session helper turns it on because it builds its own redacted snippet from
    the full text.
    """
    dialect = db.bind.dialect.name if db.bind else "sqlite"
    text_select = "messages_text" if include_text else "NULL"
    tool_values = source_tool_filter_values(tool) if tool else ()
    tool_params = {f"tool_{i}": value for i, value in enumerate(tool_values)}
    if len(tool_values) == 1:
        tool_clause = "AND source_tool = :tool_0"
    elif len(tool_values) > 1:
        placeholders = ", ".join(f":tool_{i}" for i in range(len(tool_values)))
        tool_clause = f"AND source_tool IN ({placeholders})"
    else:
        tool_clause = ""

    sid_params: dict[str, str] = {}
    sid_clause = ""
    if session_ids:
        placeholders = ", ".join(f":sid_{i}" for i in range(len(session_ids)))
        sid_params = {f"sid_{i}": value for i, value in enumerate(session_ids)}
        sid_clause = f"AND id IN ({placeholders})"

    project_clause = "AND project_id = :project_id" if project_id is not None else ""

    if dialect == "postgresql":
        cutoff_clause = "AND updated_at >= CAST(:cutoff AS timestamptz)" if days else ""

        search_sql = sa_text(f"""
            SELECT id, title, alias, source_tool, model_id, message_count, updated_at,
                   project_id, {text_select} AS messages_text,
                   ts_headline('english', messages_text, query,
                               'MaxWords=30, MinWords=15, StartSel=<mark>, StopSel=</mark>') as snippet
            FROM sessions, plainto_tsquery('english', :q) query
            WHERE user_id = :user_id
              AND search_vector @@ query
              AND is_deleted = false
              {project_clause}
              {sid_clause}
              {tool_clause}
              {cutoff_clause}
            ORDER BY ts_rank(search_vector, query) DESC
            LIMIT :limit OFFSET :offset
        """)
        count_sql = sa_text(f"""
            SELECT count(*)
            FROM sessions, plainto_tsquery('english', :q) query
            WHERE user_id = :user_id
              AND search_vector @@ query
              AND is_deleted = false
              {project_clause}
              {sid_clause}
              {tool_clause}
              {cutoff_clause}
        """)
    else:
        cutoff_clause = "AND updated_at >= :cutoff" if days else ""

        search_sql = sa_text(f"""
            SELECT id, title, alias, source_tool, model_id, message_count, updated_at,
                   project_id, {text_select} AS messages_text,
                   substr(messages_text, max(1, instr(lower(messages_text), lower(:q)) - 40), 100) as snippet
            FROM sessions
            WHERE user_id = :user_id
              AND (lower(title) LIKE '%' || lower(:q) || '%'
                   OR lower(messages_text) LIKE '%' || lower(:q) || '%')
              AND is_deleted = 0
              {project_clause}
              {sid_clause}
              {tool_clause}
              {cutoff_clause}
            ORDER BY updated_at DESC
            LIMIT :limit OFFSET :offset
        """)
        count_sql = sa_text(f"""
            SELECT count(*)
            FROM sessions
            WHERE user_id = :user_id
              AND (lower(title) LIKE '%' || lower(:q) || '%'
                   OR lower(messages_text) LIKE '%' || lower(:q) || '%')
              AND is_deleted = 0
              {project_clause}
              {sid_clause}
              {tool_clause}
              {cutoff_clause}
        """)

    cutoff = None
    if days is not None:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

    params: dict = {
        "q": query,
        "user_id": user_id,
        "limit": limit,
        "offset": offset,
    }
    count_params: dict = {"q": query, "user_id": user_id}
    params.update(tool_params)
    count_params.update(tool_params)
    params.update(sid_params)
    count_params.update(sid_params)
    if project_id is not None:
        params["project_id"] = project_id
        count_params["project_id"] = project_id
    if cutoff:
        params["cutoff"] = cutoff
        count_params["cutoff"] = cutoff

    result = await db.execute(search_sql, params)
    rows = result.fetchall()

    count_result = await db.execute(count_sql, count_params)
    total = count_result.scalar() or 0

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
        )
        for row in rows
    ]
    return hits, total
