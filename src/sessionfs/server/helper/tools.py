"""Session helper tool registry and executor (piece 3).

The 9 tools from ``docs/design/session-helper.md``.  Every handler receives a
:class:`HelperContext` and calls the same access-checked queries the API routes
use — never ad-hoc queries that skip the ownership / project-access gates.
Access is re-checked on every call; a session or project the caller cannot read
is reported as "not found" (no existence oracle).  ``ctx.scope`` is a hard
filter: the model may narrow it further but can never widen it.

Output is compact plain text: session lines are ``[session:<id>]`` and excerpt
lines are ``[session:<id> msg:<index>]``.  Redaction, the per-turn retrieval
budget, and the retrieval-audit write are applied centrally in :func:`execute`
so no handler can forget them.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from sqlalchemy import func, select

from sessionfs.judge.agent_stream import ToolCall, ToolResult, ToolSpec
from sessionfs.retrieval_audit import sanitize_arguments
from sessionfs.server.auth.project_access import user_can_access_project
from sessionfs.server.db.models import KnowledgeEntry, Project, Session
from sessionfs.server.helper.context import HelperContext
from sessionfs.server.helper.excerpts import (
    DEFAULT_EXCERPT_TOKENS,
    MAX_EXCERPT_TOKENS,
    MAX_SCAN_MESSAGES,
    message_file_paths,
    normalize_path,
    rank_excerpts,
    redacted_snippet,
    relativize_path,
    touches_path,
    workspace_paths,
)
from sessionfs.server.helper.redaction import RedactionError, redact
from sessionfs.server.services.session_archive import extract_archive
from sessionfs.server.services.session_search import (
    search_sessions as search_sessions_service,
)
from sessionfs.server.services.summarizer import SessionSummary, summarize_session

logger = logging.getLogger("sessionfs.helper")

# Per-call result caps (the design's max counts).
MAX_SEARCH_RESULTS = 20
MAX_RECENT_RESULTS = 20
MAX_RELATED_RESULTS = 10
MAX_SUMMARIZE_SESSIONS = 5
MAX_PATH_RESULTS = 20
MAX_KB_RESULTS = 20

# Cost bounds for sessions_touching_path (finding 1): at most the 200 most
# recent sessions are considered and at most 64 MB of blob bytes are read.
MAX_PATH_CANDIDATES = 200
MAX_PATH_BLOB_BYTES = 64 * 1024 * 1024

# Summarization redacts + summarizes at most the 5000 most recent messages —
# the same cap as excerpt ranking (excerpts.MAX_SCAN_MESSAGES).
MAX_SUMMARY_MESSAGES = MAX_SCAN_MESSAGES


class ToolArgumentError(ValueError):
    """Raised by a handler for bad arguments; rendered as an ``is_error`` result."""


@dataclass(frozen=True)
class ToolOutput:
    """A handler's result: model-visible text plus audit refs."""

    text: str
    refs: dict[str, Any] = field(default_factory=dict)


Handler = Callable[[HelperContext, dict[str, Any]], Awaitable[ToolOutput]]


@dataclass(frozen=True)
class HelperTool:
    name: str
    description: str
    input_schema: dict[str, Any]
    handler: Handler


# ---------------------------------------------------------------------------
# Argument + access helpers
# ---------------------------------------------------------------------------


def _require_str(args: dict[str, Any], key: str) -> str:
    value = args.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ToolArgumentError(f"'{key}' is required")
    return value.strip()


def _optional_str(args: dict[str, Any], key: str) -> str | None:
    value = args.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ToolArgumentError(f"'{key}' must be a string")
    return value.strip() or None


def _capped_int(
    args: dict[str, Any], key: str, default: int, minimum: int, maximum: int
) -> int:
    value = args.get(key, default)
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int):
        raise ToolArgumentError(f"'{key}' must be an integer")
    return max(minimum, min(maximum, value))


def _display_name(ctx: HelperContext) -> str:
    return ctx.user.display_name or ctx.user.email or ctx.user.id


def _fmt_dt(dt: datetime | str | None) -> str:
    """Format a session timestamp as ``YYYY-MM-DD``.

    Raw ``text()`` search queries return ``updated_at`` as a string on SQLite
    (and can on other drivers), so accept both.
    """
    if dt is None:
        return ""
    if isinstance(dt, str):
        try:
            dt = datetime.fromisoformat(dt.replace("Z", "+00:00"))
        except ValueError:
            return dt[:10]
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.strftime("%Y-%m-%d")


def _refs(
    session_ids: list[str] | None = None,
    message_indexes: list[int] | None = None,
    kb_entry_ids: list[int] | None = None,
    project_ids: list[str] | None = None,
) -> dict[str, list[Any]]:
    out: dict[str, list[Any]] = {}
    if session_ids:
        out["session_ids"] = sorted(set(session_ids))
    if message_indexes:
        out["message_indexes"] = sorted(set(message_indexes))
    if kb_entry_ids:
        out["kb_entry_ids"] = sorted(set(kb_entry_ids))
    if project_ids:
        out["project_ids"] = sorted(set(project_ids))
    return out


def _scope_project(ctx: HelperContext, requested: str | None) -> str | None:
    """Resolve the effective project filter.  Scope wins; the model never widens."""
    if ctx.scope.project_id is not None:
        return ctx.scope.project_id
    return requested


async def _owned_session(ctx: HelperContext, session_id: str) -> Session | None:
    """A session the caller owns, not deleted, and inside ``ctx.scope``."""
    if ctx.scope.session_ids and session_id not in ctx.scope.session_ids:
        return None
    result = await ctx.db.execute(
        select(Session).where(
            Session.id == session_id,
            Session.user_id == ctx.user.id,
            Session.is_deleted == False,  # noqa: E712
        )
    )
    session = result.scalar_one_or_none()
    if session is None:
        return None
    if (
        ctx.scope.project_id is not None
        and session.project_id != ctx.scope.project_id
    ):
        return None
    return session


async def _accessible_project(ctx: HelperContext, project_id: str) -> Project | None:
    """A project the caller can read, inside ``ctx.scope``.  None = "not found"."""
    if ctx.scope.project_id is not None and project_id != ctx.scope.project_id:
        return None
    # When the scope is a set of sessions without a single project, KB tools
    # may only reach the projects those sessions belong to.
    if ctx.scope.session_ids and ctx.scope.project_id is None:
        allowed = await _scoped_session_project_ids(ctx)
        if project_id not in allowed:
            return None
    project = (
        await ctx.db.execute(select(Project).where(Project.id == project_id))
    ).scalar_one_or_none()
    if project is None:
        return None
    if not await user_can_access_project(ctx.db, ctx.user.id, project):
        return None
    return project


async def _scoped_session_project_ids(ctx: HelperContext) -> set[str]:
    """Project ids of the caller's own, non-deleted sessions in ``ctx.scope``."""
    result = await ctx.db.execute(
        select(Session.project_id).where(
            Session.id.in_(ctx.scope.session_ids),
            Session.user_id == ctx.user.id,
            Session.is_deleted == False,  # noqa: E712
        )
    )
    return {pid for (pid,) in result.all() if pid}


async def _load_archive(ctx: HelperContext, session: Session) -> dict:
    """Read and extract a session's archive, or an empty archive when unavailable."""
    if ctx.blob_store is None:
        return {"messages": [], "manifest": None, "workspace": None, "tools": None}
    data = await ctx.blob_store.get(session.blob_key)
    if not data:
        return {"messages": [], "manifest": None, "workspace": None, "tools": None}
    return await asyncio.to_thread(extract_archive, data)


def _redactor(ctx: HelperContext) -> Callable[[str], str]:
    """A single-argument redactor bound to this turn's DLP policy."""
    return lambda text: redact(text, ctx.dlp_policy)


def _redact_messages(
    messages: list[dict], redactor: Callable[[str], str]
) -> list[dict]:
    """Return a redacted copy of ``messages`` with every string value passed
    through ``redactor``.

    Summarization and any other downstream extraction must run on this copy so
    a later ``[:N]`` cut operates on already-redacted text.  (The summarizer's
    ``_extract_errors`` truncates error lines to 200 chars before helper
    redaction; without this, a secret near the end of a long error line would
    be cut in half and leaked.)
    """
    def _walk(value: Any) -> Any:
        if isinstance(value, str):
            return redactor(value)
        if isinstance(value, dict):
            return {k: _walk(v) for k, v in value.items()}
        if isinstance(value, list):
            return [_walk(v) for v in value]
        return value

    return [_walk(m) for m in messages]


def _summarize_redacted(
    messages: list[dict],
    manifest: dict | None,
    workspace: dict | None,
    redactor: Callable[[str], str],
) -> SessionSummary:
    """Redact then summarize ``messages`` (already capped to the most recent).

    Redaction and summary extraction are both CPU-bound, so callers run this
    via :func:`asyncio.to_thread` rather than on the event loop.
    """
    return summarize_session(
        _redact_messages(messages, redactor),
        manifest,
        workspace,
    )


def _relativize_summary_paths(
    summary: SessionSummary, workspace: dict | None
) -> SessionSummary:
    """Relativize the tool-call file paths a summary surfaces to the model.

    Captured ``file_path`` inputs are absolute (e.g. ``/Users/x/repo/src/auth.py``
    under root ``/Users/x/repo``), so without this the model sees
    machine-specific absolute paths. Mirrors :func:`_sessions_touching_path`.
    """
    root = workspace.get("root_path") if isinstance(workspace, dict) else None
    if not root:
        return summary
    summary.files_modified = [relativize_path(p, root) for p in summary.files_modified]
    summary.files_read = [relativize_path(p, root) for p in summary.files_read]
    return summary


def _indexed_text_mentions(messages_text: str | None, normalized_path: str) -> bool:
    """Whether the indexed text mentions ``normalized_path``.

    This is an ordering hint only — never an exclusion filter.  ``messages_text``
    indexes text blocks and tool commands, NOT tool_use file-path inputs or
    workspace files, so a session whose only path reference is a tool_use
    ``file_path`` (or a workspace file) must still have its blob fetched.  A
    matching session is simply examined first, so the cost bounds are spent on
    likely hits before unlikely ones.
    """
    if not messages_text:
        return False
    return normalized_path.lower() in messages_text.lower()


# ---------------------------------------------------------------------------
# Output formatting
# ---------------------------------------------------------------------------


def _format_search_hit(hit: Any, ctx: HelperContext, query: str = "") -> str:
    line = f"[session:{hit.session_id}] {hit.title or '(untitled)'}"
    if hit.alias:
        line += f" ({hit.alias})"
    line += f" [{hit.source_tool}]"
    if hit.project_id:
        line += f" [project:{hit.project_id}]"
    line += f" {_display_name(ctx)} {_fmt_dt(hit.updated_at)}"
    # Snippet is built from the REDACTED full text (not the SQL substr), so a
    # secret straddling the window boundary is redacted in place, never cut.
    snippet = redacted_snippet(hit.messages_text or "", query, _redactor(ctx))
    if snippet:
        line += f" — {snippet}"
    return line


def _format_session(session: Session, ctx: HelperContext) -> str:
    line = f"[session:{session.id}] {session.title or '(untitled)'}"
    if session.alias:
        line += f" ({session.alias})"
    line += f" [{session.source_tool}]"
    if session.project_id:
        line += f" [project:{session.project_id}]"
    line += f" {_display_name(ctx)} {_fmt_dt(session.updated_at)}"
    return line


def _format_summary(session: Session, summary: SessionSummary) -> str:
    parts = [f"[session:{session.id}] {summary.title or session.title or '(untitled)'}"]
    meta: list[str] = []
    if summary.tool:
        meta.append(f"tool: {summary.tool}")
    if summary.model:
        meta.append(f"model: {summary.model}")
    if summary.duration_minutes:
        meta.append(f"duration: {summary.duration_minutes} min")
    meta.append(f"messages: {summary.message_count}")
    if summary.tool_call_count:
        meta.append(f"tool calls: {summary.tool_call_count}")
    if meta:
        parts.append("  " + " | ".join(meta))
    if summary.files_modified:
        parts.append("  files modified: " + ", ".join(summary.files_modified[:20]))
    if summary.files_read:
        parts.append("  files read: " + ", ".join(summary.files_read[:20]))
    if summary.commands_executed:
        parts.append(f"  commands: {summary.commands_executed}")
    if summary.tests_run:
        parts.append(f"  tests: {summary.tests_passed}/{summary.tests_run} passed")
    if summary.errors_encountered:
        parts.append("  errors: " + "; ".join(summary.errors_encountered[:5]))
    if summary.skills_used:
        parts.append("  skills: " + ", ".join(summary.skills_used[:10]))
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Tool handlers
# ---------------------------------------------------------------------------


async def _search_sessions(ctx: HelperContext, args: dict[str, Any]) -> ToolOutput:
    query = _require_str(args, "query")
    limit = _capped_int(args, "limit", 20, 1, MAX_SEARCH_RESULTS)
    project_id = _scope_project(ctx, _optional_str(args, "project_id"))
    source_tool = _optional_str(args, "source_tool")

    hits, _total = await search_sessions_service(
        ctx.db,
        user_id=ctx.user.id,
        query=query,
        tool=source_tool,
        limit=limit,
        project_id=project_id,
        session_ids=ctx.scope.session_ids or None,
        include_text=True,
    )

    if not hits:
        return ToolOutput("No sessions matched.", refs=_refs())
    return ToolOutput(
        "\n".join(_format_search_hit(h, ctx, query) for h in hits),
        refs=_refs(session_ids=[h.session_id for h in hits]),
    )


async def _list_recent_sessions(ctx: HelperContext, args: dict[str, Any]) -> ToolOutput:
    limit = _capped_int(args, "limit", 20, 1, MAX_RECENT_RESULTS)
    project_id = _scope_project(ctx, _optional_str(args, "project_id"))

    stmt = select(Session).where(
        Session.user_id == ctx.user.id,
        Session.is_deleted == False,  # noqa: E712
    )
    if project_id is not None:
        stmt = stmt.where(Session.project_id == project_id)
    if ctx.scope.session_ids:
        stmt = stmt.where(Session.id.in_(ctx.scope.session_ids))
    stmt = stmt.order_by(Session.updated_at.desc()).limit(limit)
    sessions = (await ctx.db.execute(stmt)).scalars().all()

    if not sessions:
        return ToolOutput("No recent sessions.", refs=_refs())
    return ToolOutput(
        "\n".join(_format_session(s, ctx) for s in sessions),
        refs=_refs(session_ids=[s.id for s in sessions]),
    )


async def _find_related_sessions(ctx: HelperContext, args: dict[str, Any]) -> ToolOutput:
    file_path = _optional_str(args, "file_path")
    error_text = _optional_str(args, "error_text")
    if not file_path and not error_text:
        raise ToolArgumentError("provide 'file_path' or 'error_text'")
    limit = _capped_int(args, "limit", 10, 1, MAX_RELATED_RESULTS)
    project_id = _scope_project(ctx, _optional_str(args, "project_id"))

    # No server-side related-sessions index exists; map to full-text search
    # with the path/error text as the query.
    query = file_path or error_text or ""
    hits, _total = await search_sessions_service(
        ctx.db,
        user_id=ctx.user.id,
        query=query,
        limit=limit,
        project_id=project_id,
        session_ids=ctx.scope.session_ids or None,
        include_text=True,
    )

    if not hits:
        return ToolOutput("No related sessions found.", refs=_refs())
    return ToolOutput(
        "\n".join(_format_search_hit(h, ctx, query) for h in hits),
        refs=_refs(session_ids=[h.session_id for h in hits]),
    )


async def _get_session_summary(ctx: HelperContext, args: dict[str, Any]) -> ToolOutput:
    session_id = _require_str(args, "session_id")
    session = await _owned_session(ctx, session_id)
    if session is None:
        return ToolOutput("Session not found.", refs=_refs())

    archive = await _load_archive(ctx, session)
    messages = archive.get("messages") or []
    if not messages:
        # Blob unavailable: fall back to DB metadata only.
        summary = SessionSummary(
            session_id=session.id,
            title=session.title or "Untitled",
            tool=session.source_tool,
            model=session.model_id,
            message_count=session.message_count,
        )
    else:
        summary = await asyncio.to_thread(
            _summarize_redacted,
            messages[-MAX_SUMMARY_MESSAGES:],
            archive.get("manifest"),
            archive.get("workspace"),
            _redactor(ctx),
        )
        _relativize_summary_paths(summary, archive.get("workspace"))

    return ToolOutput(
        _format_summary(session, summary),
        refs=_refs(session_ids=[session.id]),
    )


async def _get_session_excerpts(ctx: HelperContext, args: dict[str, Any]) -> ToolOutput:
    session_id = _require_str(args, "session_id")
    query = _optional_str(args, "query") or ""
    max_tokens = _capped_int(
        args, "max_tokens", DEFAULT_EXCERPT_TOKENS, 1, MAX_EXCERPT_TOKENS
    )
    limit = _capped_int(args, "limit", 0, 0, 10_000)

    session = await _owned_session(ctx, session_id)
    if session is None:
        return ToolOutput("Session not found.", refs=_refs())

    archive = await _load_archive(ctx, session)
    messages = archive.get("messages") or []
    excerpts = await asyncio.to_thread(
        rank_excerpts,
        messages,
        query,
        max_tokens=max_tokens,
        limit=(limit or None),
        indexes=archive.get("message_indexes"),
        redactor=_redactor(ctx),
    )

    if not excerpts:
        return ToolOutput("No excerpts.", refs=_refs(session_ids=[session.id]))

    lines = [f"[session:{session.id} msg:{e.index}] {e.text}" for e in excerpts]
    return ToolOutput(
        "\n".join(lines),
        refs=_refs(
            session_ids=[session.id],
            message_indexes=[e.index for e in excerpts],
        ),
    )


def _escape_like(query: str) -> str:
    """Escape LIKE wildcards so a query of ``%`` or ``_`` matches literally.

    The ``escape="\\"`` argument on ``ilike`` tells the DB to treat a
    backslash as the escape character; here we backslash-escape every
    ``\\``, ``%`` and ``_`` in the user's query.
    """
    return query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


async def _search_project_knowledge(
    ctx: HelperContext, args: dict[str, Any]
) -> ToolOutput:
    project_id = _require_str(args, "project_id")
    query = _require_str(args, "query")
    limit = _capped_int(args, "limit", 20, 1, MAX_KB_RESULTS)

    project = await _accessible_project(ctx, project_id)
    if project is None:
        return ToolOutput("Project not found.", refs=_refs())

    stmt = (
        select(KnowledgeEntry)
        .where(
            KnowledgeEntry.project_id == project_id,
            KnowledgeEntry.dismissed == False,  # noqa: E712
            KnowledgeEntry.superseded_by.is_(None),
            KnowledgeEntry.claim_class != "note",
            KnowledgeEntry.content.ilike(f"%{_escape_like(query)}%", escape="\\"),
        )
        .order_by(KnowledgeEntry.confidence.desc(), KnowledgeEntry.created_at.desc())
        .limit(limit)
    )
    entries = (await ctx.db.execute(stmt)).scalars().all()

    if not entries:
        return ToolOutput("No knowledge entries matched.", refs=_refs())

    lines = [
        f"[kb:{e.id}] ({e.claim_class}) {e.content}" for e in entries
    ]
    return ToolOutput(
        "\n".join(lines),
        refs=_refs(project_ids=[project_id], kb_entry_ids=[e.id for e in entries]),
    )


async def _get_project_context(ctx: HelperContext, args: dict[str, Any]) -> ToolOutput:
    project_id = _require_str(args, "project_id")
    project = await _accessible_project(ctx, project_id)
    if project is None:
        return ToolOutput("Project not found.", refs=_refs())

    text = (project.context_document or "").strip()
    if not text:
        return ToolOutput(
            "No compiled context for this project.",
            refs=_refs(project_ids=[project_id]),
        )
    return ToolOutput(text, refs=_refs(project_ids=[project_id]))


async def _summarize_sessions(ctx: HelperContext, args: dict[str, Any]) -> ToolOutput:
    raw = args.get("session_ids")
    if not isinstance(raw, list) or not raw or not all(isinstance(s, str) for s in raw):
        raise ToolArgumentError("'session_ids' must be a non-empty list of ids")
    session_ids = [s for s in raw if s.strip()]
    if not session_ids:
        raise ToolArgumentError("'session_ids' must be a non-empty list of ids")
    if len(session_ids) > MAX_SUMMARIZE_SESSIONS:
        raise ToolArgumentError(f"at most {MAX_SUMMARIZE_SESSIONS} session ids")

    blocks: list[str] = []
    found: list[str] = []
    indexes: list[int] = []
    for session_id in session_ids:
        session = await _owned_session(ctx, session_id)
        if session is None:
            blocks.append(f"[session:{session_id}] (not found)")
            continue
        archive = await _load_archive(ctx, session)
        messages = archive.get("messages") or []
        if messages:
            summary = await asyncio.to_thread(
                _summarize_redacted,
                messages[-MAX_SUMMARY_MESSAGES:],
                archive.get("manifest"),
                archive.get("workspace"),
                _redactor(ctx),
            )
            _relativize_summary_paths(summary, archive.get("workspace"))
        else:
            summary = SessionSummary(
                session_id=session.id,
                title=session.title or "Untitled",
                tool=session.source_tool,
                model=session.model_id,
                message_count=session.message_count,
            )
        block = _format_summary(session, summary)
        excerpts = await asyncio.to_thread(
            rank_excerpts,
            messages,
            "",
            max_tokens=1200,
            limit=3,
            indexes=archive.get("message_indexes"),
            redactor=_redactor(ctx),
        )
        if excerpts:
            block += "\n  excerpts:\n" + "\n".join(
                f"  [session:{session.id} msg:{e.index}] {e.text}" for e in excerpts
            )
            indexes.extend(e.index for e in excerpts)
        blocks.append(block)
        found.append(session.id)

    return ToolOutput(
        "\n\n".join(blocks),
        refs=_refs(session_ids=found, message_indexes=indexes),
    )


async def _sessions_touching_path(
    ctx: HelperContext, args: dict[str, Any]
) -> ToolOutput:
    path = _require_str(args, "path")
    limit = _capped_int(args, "limit", 20, 1, MAX_PATH_RESULTS)
    project_id = _scope_project(ctx, _optional_str(args, "project_id"))
    normalized = normalize_path(path)

    stmt = select(Session).where(
        Session.user_id == ctx.user.id,
        Session.is_deleted == False,  # noqa: E712
    )
    if project_id is not None:
        stmt = stmt.where(Session.project_id == project_id)
    if ctx.scope.session_ids:
        stmt = stmt.where(Session.id.in_(ctx.scope.session_ids))

    count_stmt = select(func.count()).select_from(Session).where(
        Session.user_id == ctx.user.id,
        Session.is_deleted == False,  # noqa: E712
    )
    if project_id is not None:
        count_stmt = count_stmt.where(Session.project_id == project_id)
    if ctx.scope.session_ids:
        count_stmt = count_stmt.where(Session.id.in_(ctx.scope.session_ids))
    total = (await ctx.db.execute(count_stmt)).scalar_one() or 0

    # Cost bound: only the 200 most recent sessions are candidates.
    stmt = stmt.order_by(Session.updated_at.desc()).limit(MAX_PATH_CANDIDATES)
    sessions = (await ctx.db.execute(stmt)).scalars().all()

    # Order candidates so sessions whose indexed text mentions the path are
    # examined first, then most recent.  The text is an ordering hint only —
    # never a filter — because the index lacks tool_use file-path inputs and
    # workspace files (and is capped at 100 KB).  ``sort`` is stable, so the
    # SQL's recency ordering is preserved within each group.
    sessions.sort(
        key=lambda s: _indexed_text_mentions(s.messages_text, normalized),
        reverse=True,
    )

    matches: list[Session] = []
    bytes_read = 0
    checked = 0
    incomplete = total > len(sessions)
    for session in sessions:
        if ctx.blob_store is None:
            continue
        if bytes_read >= MAX_PATH_BLOB_BYTES:
            incomplete = True
            break
        checked += 1
        data = await ctx.blob_store.get(session.blob_key)
        if not data:
            continue
        bytes_read += len(data)
        archive = await asyncio.to_thread(extract_archive, data)
        workspace = archive.get("workspace")
        root = workspace.get("root_path") if isinstance(workspace, dict) else None
        paths: list[str] = []
        for msg in archive.get("messages") or []:
            for p in message_file_paths(msg):
                # Tool-call paths are captured absolute (e.g.
                # /Users/x/repo/src/auth.py under root /Users/x/repo). Keep the
                # raw form so an absolute query still matches, and add the
                # relativized form so a workspace-relative query does too.
                paths.append(p)
                rp = relativize_path(p, root)
                if rp != p:
                    paths.append(rp)
        for p in workspace_paths(workspace):
            paths.append(relativize_path(p, root))
        if touches_path(paths, normalized):
            matches.append(session)
        if len(matches) >= limit:
            break

    if not matches:
        text = "No sessions touched that path."
    else:
        text = "\n".join(_format_session(s, ctx) for s in matches)
    if incomplete:
        text += f"\n[checked {checked} of {total} sessions; results may be incomplete]"
    return ToolOutput(
        text,
        refs=_refs(session_ids=[s.id for s in matches]),
    )


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


TOOLS: dict[str, HelperTool] = {
    t.name: t
    for t in [
        HelperTool(
            name="search_sessions",
            description="Full-text search over the caller's own sessions.",
            input_schema={
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "limit": {"type": "integer", "minimum": 1, "maximum": MAX_SEARCH_RESULTS},
                    "project_id": {"type": "string"},
                    "source_tool": {"type": "string"},
                },
                "required": ["query"],
                "additionalProperties": False,
            },
            handler=_search_sessions,
        ),
        HelperTool(
            name="list_recent_sessions",
            description="Most recently updated sessions the caller owns.",
            input_schema={
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "minimum": 1, "maximum": MAX_RECENT_RESULTS},
                    "project_id": {"type": "string"},
                },
                "additionalProperties": False,
            },
            handler=_list_recent_sessions,
        ),
        HelperTool(
            name="find_related_sessions",
            description="Sessions related to a file path or error text.",
            input_schema={
                "type": "object",
                "properties": {
                    "file_path": {"type": "string"},
                    "error_text": {"type": "string"},
                    "limit": {"type": "integer", "minimum": 1, "maximum": MAX_RELATED_RESULTS},
                    "project_id": {"type": "string"},
                },
                "additionalProperties": False,
            },
            handler=_find_related_sessions,
        ),
        HelperTool(
            name="get_session_summary",
            description="Deterministic summary of one session (no narrative generation).",
            input_schema={
                "type": "object",
                "properties": {"session_id": {"type": "string"}},
                "required": ["session_id"],
                "additionalProperties": False,
            },
            handler=_get_session_summary,
        ),
        HelperTool(
            name="get_session_excerpts",
            description="Relevance-ranked transcript excerpts from one session.",
            input_schema={
                "type": "object",
                "properties": {
                    "session_id": {"type": "string"},
                    "query": {"type": "string"},
                    "max_tokens": {"type": "integer", "minimum": 1, "maximum": MAX_EXCERPT_TOKENS},
                    "limit": {"type": "integer", "minimum": 0},
                },
                "required": ["session_id"],
                "additionalProperties": False,
            },
            handler=_get_session_excerpts,
        ),
        HelperTool(
            name="search_project_knowledge",
            description="Search a project's knowledge base for matching claims.",
            input_schema={
                "type": "object",
                "properties": {
                    "project_id": {"type": "string"},
                    "query": {"type": "string"},
                    "limit": {"type": "integer", "minimum": 1, "maximum": MAX_KB_RESULTS},
                },
                "required": ["project_id", "query"],
                "additionalProperties": False,
            },
            handler=_search_project_knowledge,
        ),
        HelperTool(
            name="get_project_context",
            description="The compiled context document for one project.",
            input_schema={
                "type": "object",
                "properties": {"project_id": {"type": "string"}},
                "required": ["project_id"],
                "additionalProperties": False,
            },
            handler=_get_project_context,
        ),
        HelperTool(
            name="summarize_sessions",
            description="Deterministic summaries plus top excerpts for up to 5 sessions.",
            input_schema={
                "type": "object",
                "properties": {
                    "session_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "maxItems": MAX_SUMMARIZE_SESSIONS,
                        "minItems": 1,
                    },
                },
                "required": ["session_ids"],
                "additionalProperties": False,
            },
            handler=_summarize_sessions,
        ),
        HelperTool(
            name="sessions_touching_path",
            description="Sessions whose captured tool calls or workspace reference a path prefix.",
            input_schema={
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "limit": {"type": "integer", "minimum": 1, "maximum": MAX_PATH_RESULTS},
                    "project_id": {"type": "string"},
                },
                "required": ["path"],
                "additionalProperties": False,
            },
            handler=_sessions_touching_path,
        ),
    ]
}


def tool_specs() -> list[ToolSpec]:
    """JSON-Schema tool specs for the agent loop."""
    return [
        ToolSpec(name=t.name, description=t.description, input_schema=t.input_schema)
        for t in TOOLS.values()
    ]


async def _record(
    ctx: HelperContext,
    tool_name: str,
    safe_args: dict[str, Any],
    refs: dict[str, Any],
) -> None:
    """Write one retrieval-audit event; propagates failures so the caller can
    fail closed (an unrecorded retrieval must not send its data text)."""
    if ctx.audit is None:
        return
    await ctx.audit.record(tool_name, safe_args, refs)


async def _record_or_log(
    ctx: HelperContext,
    tool_name: str,
    safe_args: dict[str, Any],
    refs: dict[str, Any],
) -> None:
    """Best-effort audit write for error paths (nothing sensitive to protect)."""
    try:
        await _record(ctx, tool_name, safe_args, refs)
    except Exception:
        logger.exception("Retrieval audit write failed for %s", tool_name)


async def execute(
    ctx: HelperContext,
    call: ToolCall,
    *,
    refs_out: dict[str, dict[str, Any]] | None = None,
) -> ToolResult:
    """Run one tool call, redacting output and charging the retrieval budget.

    Returns a :class:`ToolResult` for every call — errors are surfaced as
    ``is_error=True`` results, never raised.  When ``refs_out`` is given, the
    call's audit refs (``session_ids`` / ``message_indexes`` / …) are recorded
    there keyed by ``call.id`` so the turn handler can emit structured
    ``tool_result`` events without re-deriving them from the result text.
    """
    tool = TOOLS.get(call.name)
    safe_args = sanitize_arguments(call.arguments or {})

    if tool is None:
        await _record_or_log(ctx, call.name, safe_args, {})
        return ToolResult(
            tool_call_id=call.id, content=f"Unknown tool: {call.name}", is_error=True
        )
    if call.invalid_arguments:
        await _record_or_log(ctx, call.name, safe_args, {})
        return ToolResult(
            tool_call_id=call.id,
            content=f"Malformed arguments for {call.name}",
            is_error=True,
        )

    # Finding 1: the budget is checked before any handler work (no blob reads,
    # no DB scans) once the per-turn retrieval budget is exhausted.
    if ctx.budget.remaining <= 0:
        await _record_or_log(ctx, call.name, safe_args, {})
        return ToolResult(
            tool_call_id=call.id,
            content="Retrieval budget exhausted.",
            is_error=True,
        )

    try:
        output = await tool.handler(ctx, call.arguments)
        if refs_out is not None:
            refs_out[call.id] = output.refs
        text = redact(output.text, ctx.dlp_policy)
    except ToolArgumentError as exc:
        await _record_or_log(ctx, call.name, safe_args, {})
        return ToolResult(tool_call_id=call.id, content=str(exc), is_error=True)
    except RedactionError:
        # DLP redaction failed (e.g. a bad org custom regex): fail closed and
        # send nothing rather than unredacted output.
        logger.exception("Redaction failed for %s", call.name)
        await _record_or_log(ctx, call.name, safe_args, {})
        return ToolResult(
            tool_call_id=call.id,
            content=f"{call.name} could not redact output",
            is_error=True,
        )
    except Exception:
        logger.exception("Tool %s failed", call.name)
        await _record_or_log(ctx, call.name, safe_args, {})
        return ToolResult(
            tool_call_id=call.id, content=f"{call.name} failed", is_error=True
        )

    text = ctx.budget.charge(text)
    try:
        await _record(ctx, call.name, safe_args, output.refs)
    except Exception:
        # Fail closed: if the retrieval can't be recorded, nothing is sent.
        logger.exception("Retrieval audit write failed for %s", call.name)
        return ToolResult(
            tool_call_id=call.id,
            content="could not record retrieval audit; nothing was sent",
            is_error=True,
        )
    return ToolResult(tool_call_id=call.id, content=text)
