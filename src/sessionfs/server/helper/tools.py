"""Session helper tool registry and executor (piece 3 + piece 6).

The 9 read tools from ``docs/design/session-helper.md`` plus the write-action
tools (``list_projects``, ``get_project_settings``, ``set_project_auto_narrative``,
``trash_sessions``).  Every handler receives a :class:`HelperContext` and calls
the same access-checked queries the API routes use — never ad-hoc queries that
skip the ownership / project-access gates.  Access is re-checked on every call;
a session or project the caller cannot read is reported as "not found" (no
existence oracle).  ``ctx.scope`` is a hard filter: the model may narrow it
further but can never widen it.

The write-action tools can only PROPOSE: they queue a pending action on
``ctx.pending_actions`` and return "Proposed action ha_… — waiting for the
user to confirm"; nothing changes until the caller confirms in the dashboard.

Output is compact plain text: session lines are ``[session:<id>]`` and excerpt
lines are ``[session:<id> msg:<index>]``.  Redaction, the per-turn retrieval
budget, and the retrieval-audit write are applied centrally in :func:`execute`
so no handler can forget them.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import threading
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Awaitable, Callable

from fastapi import HTTPException
from sqlalchemy import ColumnElement, and_, case, func, or_, select

from sessionfs.judge.agent_stream import ToolCall, ToolResult, ToolSpec
from sessionfs.retrieval_audit import sanitize_arguments
from sessionfs.server.auth.project_access import (
    accessible_project_ids_subquery,
    list_accessible_projects,
    project_session_clause,
    user_can_access_project,
    user_can_edit_project,
    user_has_read_standing,
)
from sessionfs.server.db.models import (
    AgentPersona,
    AgentRun,
    KnowledgeEntry,
    KnowledgePage,
    OrgMember,
    Organization,
    OrgTrackerConnection,
    Project,
    ProjectGroup,
    ProjectGroupAccess,
    Session,
    Ticket,
    TicketComment,
    TicketDependency,
    TicketEvent,
    TicketTrackerLink,
    TrackerProjectMapping,
    User,
    WikiPageRevision,
)
from sessionfs.server.services.personas import (
    list_effective_personas,
    visible_persona_org_id,
)
from sessionfs.server.services.helper_ticket_actions import (
    MAX_COMMENT_CHARS,
    MAX_CRITERIA,
    MAX_CRITERION_CHARS,
    MAX_DESCRIPTION_CHARS,
    MAX_TITLE_CHARS,
    HelperDraftError,
    check_comment_body,
    check_reassignment_allowed,
    check_ticket_draft,
    jira_project_key_override_error,
    jira_template_override_error,
    jira_template_override_version,
    resolve_assignee,
)
from sessionfs.server.helper.context import HelperContext
from sessionfs.server.helper.docs import MAX_QUERY_CHARS, MAX_QUERY_TERMS, get_docs_index
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
from sessionfs.server.integrations.trackers.base import TrackerError
from sessionfs.server.services import jira_sync
from sessionfs.server.services.ticket_events import decode_event_value
from sessionfs.server.services.jira_links import JiraLinkError, normalize_link_issue_key
from sessionfs.server.services.session_archive import extract_archive
from sessionfs.server.services.session_models import (
    escape_like,
    model_bucket,
    model_family_filter,
    provider_bucket,
    provider_family_filter,
)
from sessionfs.server.services.session_search import (
    search_sessions as search_sessions_service,
    source_tool_filter_values,
)
from sessionfs.server.services.summarizer import SessionSummary, summarize_session
from sessionfs.server.services.project_settings import check_auto_narrative_gate
from sessionfs.server.tier_gate import (
    UserContext,
    build_user_context,
    check_project_feature,
    resolve_project_plan_tiers,
)

logger = logging.getLogger("sessionfs.helper")

# Per-call result caps (the design's max counts).
MAX_SEARCH_RESULTS = 20
# A metadata listing (no text query) may page through up to 100 sessions at a
# time — the trash proposal's cap — so "all sessions that used X" fits a page.
MAX_RECENT_RESULTS = 100
MAX_MODEL_FILTER_VALUES = 20
MAX_MODEL_FILTER_CHARS = 100
MAX_FILTER_DAYS = 3650
MAX_MODEL_BUCKETS = 100
MAX_OFFSET = 100_000
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

# Write-action proposals (piece 6).  The helper can only PROPOSE; nothing
# changes until the caller confirms in the dashboard.
MAX_ACTION_TARGETS = 100
ACTION_TTL_MINUTES = 15
MAX_LIST_PROJECTS = 200

# Official-docs search (piece: session helper can search the SessionFS docs).
MAX_DOC_SEARCH_RESULTS = 8

# Read-tool caps for tickets / wiki / agent runs / personas (GitHub #104 part 1).
MAX_LIST_TICKETS = 50
MAX_TICKET_COMMENTS = 10
MAX_TICKET_COMMENT_CHARS = 1_500
MAX_LIST_WIKI_PAGES = 200
MAX_WIKI_BODY_CHARS = 20_000
MAX_LIST_AGENT_RUNS = 50
MAX_LIST_PERSONAS = 100
MAX_AGENT_RUN_FINDINGS = 20
MAX_TICKET_DESCRIPTION_CHARS = 6_000
MAX_TICKET_CRITERIA = 30
MAX_TICKET_CRITERION_CHARS = 400
# Dependency links shown on one ticket (the rest are counted, not listed).
MAX_TICKET_DEPENDENCIES = 50
MAX_RUN_SUMMARY_CHARS = 2_000
MAX_RUN_SUMMARY_LINE_CHARS = 200
MAX_FINDING_MESSAGE_CHARS = 500
# Global cap on one tool result's text, applied in ``execute`` after
# redaction: twice the wiki body cap, so no single call can eat the whole
# per-turn retrieval budget (~96k characters).  The per-part ticket caps
# above are sized so a whole get_ticket result fits within it: description
# (6k) + 30 capped criteria (~12.1k) + 10 capped comments (~15.8k with their
# headers) + 50 dependency refs (~1.5k) + header lines (title ≤ 200, project
# name, status, dates; well under 1k) ≈ 36.4k worst case.
MAX_TOOL_RESULT_CHARS = 2 * MAX_WIKI_BODY_CHARS
TRUNCATED_NOTE = "…[truncated]"
_VALID_TICKET_KINDS = {"issue", "task"}
_VALID_PRIORITIES = {"critical", "high", "medium", "low"}

# Proposal-source citation markers.  These mirror the turn handler's citation
# regexes in ``routes/helper.py`` (a circular import keeps them local): a
# proposal's ``sources`` list must contain only these forms or the literal
# ``user``, and every non-user source must have been surfaced by a tool result
# this conversation (tracked in ``ctx.seen_*``).
_SOURCE_SESSION_RE = re.compile(r"\[session:(?P<id>[A-Za-z0-9_-]+)(?:\s+msg:(?P<idx>\d+))?\]")
_SOURCE_DOC_RE = re.compile(r"\[doc:(?P<slug>[a-z0-9][a-z0-9/_-]*)(?:#(?P<anchor>[a-z0-9_-]+))?\]")
_SOURCE_TICKET_RE = re.compile(r"\[ticket:(?P<id>tk_[0-9a-f]{16})\]")
_SOURCE_WIKI_RE = re.compile(r"\[wiki:(?P<project_id>proj_[0-9a-f]{16})/(?P<slug>[^\]\s]+)\]")

# Plan features the routes gate these read tools on (``check_project_feature``).
# The wiki read routes have no plan gate, so the wiki tools have none either.
FEATURE_TICKETS = "agent_tickets"
FEATURE_AGENT_RUNS = "agent_runs"
FEATURE_PERSONAS = "agent_personas"
FEATURE_JIRA = "jira_integration"

# A get_jira_issue read is bounded to this wall-clock (the Jira client itself
# times out per request at 20 s), so one tool call can't stall a whole turn.
JIRA_ISSUE_TIMEOUT_SECONDS = 30.0


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


def _optional_bool(args: dict[str, Any], key: str) -> bool:
    value = args.get(key, False)
    if value is None:
        return False
    if not isinstance(value, bool):
        raise ToolArgumentError(f"'{key}' must be a boolean")
    return value


def _capped_int(
    args: dict[str, Any], key: str, default: int, minimum: int, maximum: int
) -> int:
    value = args.get(key, default)
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int):
        raise ToolArgumentError(f"'{key}' must be an integer")
    return max(minimum, min(maximum, value))


def _optional_int(args: dict[str, Any], key: str) -> int | None:
    value = args.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ToolArgumentError(f"'{key}' must be an integer")
    return value


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


def _age(dt: datetime | str | None, now: datetime | None = None) -> int | None:
    """Whole days between ``dt`` and now (positive = in the past)."""
    if dt is None:
        return None
    if isinstance(dt, str):
        try:
            dt = datetime.fromisoformat(dt.replace("Z", "+00:00"))
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    ref = now or datetime.now(timezone.utc)
    return (ref - dt).days


def _fmt_dt_with_age(dt: datetime | str | None) -> str:
    """``YYYY-MM-DD (Nd ago)`` — the per-result source date the model cites.

    Results always carry a date so the model can say "as of <date>" and the
    dashboard can render a relative-age chip.  Same-day results read "today".
    """
    if dt is None:
        return ""
    date_str = _fmt_dt(dt)
    age = _age(dt)
    if age is None:
        return date_str
    if age < 1:
        return f"{date_str} (today)"
    return f"{date_str} ({age}d ago)"


def _cap(text: str, limit: int) -> str:
    """Cut ``text`` to ``limit`` characters with a visible truncation note."""
    if len(text) <= limit:
        return text
    return text[:limit] + TRUNCATED_NOTE


def _loads_json_list(raw: str | None) -> list:
    """Decode a JSON-list Text column (acceptance_criteria, findings, …)."""
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return []
    return data if isinstance(data, list) else []


def _refs(
    session_ids: list[str] | None = None,
    message_indexes: list[int] | None = None,
    kb_entry_ids: list[int] | None = None,
    project_ids: list[str] | None = None,
    doc_slugs: list[str] | None = None,
    ticket_ids: list[str] | None = None,
    jira_issue_keys: list[str] | None = None,
    wiki_refs: list[tuple[str, str]] | None = None,
    message_refs: list[tuple[str, int]] | None = None,
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
    if doc_slugs:
        out["doc_slugs"] = sorted(set(doc_slugs))
    if ticket_ids:
        out["ticket_ids"] = sorted(set(ticket_ids))
    if jira_issue_keys:
        out["jira_issue_keys"] = sorted(set(jira_issue_keys))
    if wiki_refs:
        out["wiki_refs"] = sorted({(pid, slug) for pid, slug in wiki_refs})
    if message_refs:
        # (session_id, message index) pairs: an index only means something
        # within its own session.
        out["message_refs"] = sorted({(sid, idx) for sid, idx in message_refs})
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
    stmt = select(Session).where(
        Session.id == session_id,
        Session.user_id == ctx.user.id,
        Session.is_deleted == False,  # noqa: E712
    )
    if ctx.scope.project_id is not None:
        project = (
            await ctx.db.execute(
                select(Project).where(Project.id == ctx.scope.project_id)
            )
        ).scalar_one_or_none()
        if project is None:
            return None
        # Multi-repo aware: a legacy session with a NULL project_id still
        # belongs to the scope project when its remote matches the project's
        # primary or linked remotes (v0.10.22 P2 §3.3 B1).  ``legacy_only``:
        # a session attributed to ANOTHER project never belongs to this one,
        # even when it sits on one of this project's remotes.
        stmt = stmt.where(project_session_clause(project, legacy_only=True))
    elif ctx.scope.group_id is not None:
        clause = await _group_session_clause(ctx)
        if clause is None:
            return None
        stmt = stmt.where(clause)
    return (await ctx.db.execute(stmt)).scalar_one_or_none()


async def _accessible_project(ctx: HelperContext, project_id: str) -> Project | None:
    """A project the caller can read, inside ``ctx.scope``.  None = "not found"."""
    if ctx.scope.project_id is not None and project_id != ctx.scope.project_id:
        return None
    # When the scope is a group, a project is reachable only if it is still a
    # readable member of that group (recomputed per call).
    if ctx.scope.group_id is not None:
        allowed = await group_effective_project_ids(ctx.db, ctx.user.id, ctx.scope.group_id)
        if project_id not in allowed:
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


async def _session_project_ids(
    db: Any, user_id: str, session_ids: tuple[str, ...]
) -> set[str]:
    """Project ids of the caller's own, non-deleted sessions in ``session_ids``."""
    result = await db.execute(
        select(Session.project_id).where(
            Session.id.in_(session_ids),
            Session.user_id == user_id,
            Session.is_deleted == False,  # noqa: E712
        )
    )
    return {pid for (pid,) in result.all() if pid}


async def _scoped_session_project_ids(ctx: HelperContext) -> set[str]:
    """Project ids of the caller's own, non-deleted sessions in ``ctx.scope``."""
    return await _session_project_ids(ctx.db, ctx.user.id, ctx.scope.session_ids)


async def group_effective_project_ids(db: Any, user_id: str, group_id: str) -> set[str]:
    """The projects a group-scoped turn may read: the group's member projects
    intersected with the projects the caller can already read.

    Recomputed from the tables on every call — never cached — so a membership
    change between turns is reflected immediately (GitHub #113).
    """
    readable = await accessible_project_ids_subquery(db, user_id)
    rows = await db.execute(
        select(ProjectGroupAccess.project_id)
        .join(ProjectGroup, ProjectGroup.id == ProjectGroupAccess.group_id)
        .where(
            ProjectGroupAccess.group_id == group_id,
            ProjectGroup.deleted_at.is_(None),
            ProjectGroupAccess.project_id.in_(readable),
        )
    )
    return {pid for (pid,) in rows.all()}


async def scope_project_ids(db: Any, user_id: str, scope: Any) -> set[str]:
    """Every project id a turn with ``scope`` may read — the id-set form of
    :func:`_accessible_projects` (same access predicate, same scope rules).

    The turn handler uses it to validate ``[ticket:…]`` / ``[wiki:…]``
    citations: a cited ticket or page must live in one of these projects.
    """
    if scope.project_id is not None:
        project = (
            await db.execute(select(Project).where(Project.id == scope.project_id))
        ).scalar_one_or_none()
        if project is None or not await user_can_access_project(db, user_id, project):
            return set()
        return {project.id}
    if scope.group_id is not None:
        return await group_effective_project_ids(db, user_id, scope.group_id)
    ids = set(
        (await db.execute(await accessible_project_ids_subquery(db, user_id))).scalars().all()
    )
    if scope.session_ids:
        ids &= await _session_project_ids(db, user_id, scope.session_ids)
    return ids


async def _accessible_projects(ctx: HelperContext) -> list[Project]:
    """The projects a tool may reach this turn.  Scope wins; the model never
    widens it (same rules as ``_list_projects`` / ``_accessible_project``)."""
    if ctx.scope.project_id is not None:
        project = await _accessible_project(ctx, ctx.scope.project_id)
        # A scope project merged since the thread started is a tombstone:
        # its tickets, personas and sessions now live on the target, so it
        # lists nothing (GitHub #111) — same as the projects route.
        if project is None or project.merged_into_project_id is not None:
            return []
        return [project]
    if ctx.scope.group_id is not None:
        allowed = await group_effective_project_ids(ctx.db, ctx.user.id, ctx.scope.group_id)
        if not allowed:
            return []
        return [
            p for p in await list_accessible_projects(ctx.db, ctx.user.id)
            if p.id in allowed
        ]
    if ctx.scope.session_ids:
        allowed = await _scoped_session_project_ids(ctx)
        return [
            p for p in await list_accessible_projects(ctx.db, ctx.user.id)
            if p.id in allowed
        ]
    return await list_accessible_projects(ctx.db, ctx.user.id)


async def _feature_allowed(ctx: HelperContext, feature: str, project: Project) -> bool:
    """Whether the project's plan includes ``feature`` — the same
    ``check_project_feature`` gate the matching read route applies.

    Call only for a project the caller can already read.  Cached per turn.
    """
    key = (feature, project.id)
    cached = ctx.feature_cache.get(key)
    if cached is not None:
        return cached
    if ctx.user_context is None:
        ctx.user_context = await build_user_context(ctx.user, ctx.db)
    try:
        await check_project_feature(ctx.user_context, feature, project, ctx.db)
        allowed = True
    except HTTPException as exc:
        if exc.status_code != 403:
            raise
        allowed = False
    ctx.feature_cache[key] = allowed
    return allowed


@dataclass(frozen=True)
class _SessionFilter:
    """A project/scope restriction on the caller's own sessions.

    ``clause`` is a SQL predicate over ``Session`` (None = no restriction
    beyond the caller's own, non-deleted sessions).  ``empty`` means the
    requested project is not reachable, so nothing may match.
    """

    clause: ColumnElement[bool] | None = None
    empty: bool = False


async def _check_scope_project(ctx: HelperContext, requested: str | None) -> None:
    """Refuse a project the conversation's scope doesn't cover.

    The session tools used to swap a different requested project for the
    scope project silently, so "trash project B's sessions" in a project-A
    conversation proposed A's sessions.  Say so instead.  A group-scoped
    conversation applies the same rule against the group's effective project
    set (recomputed per call).
    """
    scope_project = ctx.scope.project_id
    if scope_project is not None and requested is not None and requested != scope_project:
        raise ToolArgumentError(
            f"This conversation is limited to project {scope_project}; start an "
            f"unscoped conversation to work with project {requested}."
        )
    if ctx.scope.group_id is not None and requested is not None:
        allowed = await group_effective_project_ids(ctx.db, ctx.user.id, ctx.scope.group_id)
        if requested not in allowed:
            raise ToolArgumentError(
                f"This conversation is limited to project group {ctx.scope.group_id}; "
                f"project {requested} is not a readable member of it."
            )


async def group_session_clause(
    db: Any, user_id: str, group_id: str
) -> ColumnElement[bool] | None:
    """Sessions belonging to any readable member project of the scope's group.

    Reuses ``project_session_clause`` (``legacy_only``) per project — the same
    attribution a project-scoped turn uses — so a group-scoped turn never reads
    a session outside its effective project set.  None = the group has no
    effective projects, so nothing may match.
    """
    allowed = await group_effective_project_ids(db, user_id, group_id)
    if not allowed:
        return None
    projects = list(
        (await db.execute(select(Project).where(Project.id.in_(allowed)))).scalars().all()
    )
    return or_(*[project_session_clause(p, legacy_only=True) for p in projects])


async def _group_session_clause(ctx: HelperContext) -> ColumnElement[bool] | None:
    """``group_session_clause`` bound to the turn's context."""
    return await group_session_clause(ctx.db, ctx.user.id, ctx.scope.group_id)


async def _project_session_filter(
    ctx: HelperContext, project_id: str | None
) -> _SessionFilter:
    """Resolve a project filter to a SQL predicate (multi-repo aware).

    The predicate is :func:`project_session_clause` in ``legacy_only`` mode
    (sessions attributed to the project, plus unattributed legacy sessions
    on its remotes), ANDed with ``ctx.scope.session_ids`` when set.  It is a
    clause, never a materialized id list, so a project with thousands of
    sessions costs one subquery rather than thousands of bind parameters.
    """
    await _check_scope_project(ctx, project_id)
    effective = _scope_project(ctx, project_id)
    scope_clause = (
        Session.id.in_(ctx.scope.session_ids) if ctx.scope.session_ids else None
    )
    if effective is None:
        # A group-scoped turn with no requested project still narrows to the
        # group's effective project set (recomputed per call).
        if ctx.scope.group_id is not None:
            clause = await _group_session_clause(ctx)
            if clause is None:
                return _SessionFilter(empty=True)
            if scope_clause is not None:
                clause = and_(clause, scope_clause)
            return _SessionFilter(clause=clause)
        return _SessionFilter(clause=scope_clause)

    project = await _accessible_project(ctx, effective)
    if project is None:
        return _SessionFilter(empty=True)

    clause = project_session_clause(project, legacy_only=True)
    if scope_clause is not None:
        clause = and_(clause, scope_clause)
    return _SessionFilter(clause=clause)


def _string_list_arg(
    args: dict[str, Any], key: str, *, max_items: int, max_chars: int
) -> tuple[str, ...]:
    """A string-or-list-of-strings argument, stripped, deduped (case-insensitive)."""
    raw = args.get(key)
    if raw is None:
        return ()
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list) or not all(isinstance(v, str) for v in raw):
        raise ToolArgumentError(f"'{key}' must be a string or a list of strings")
    out: list[str] = []
    seen: set[str] = set()
    for value in raw:
        value = value.strip()
        if not value or value.lower() in seen:
            continue
        if len(value) > max_chars:
            raise ToolArgumentError(f"each '{key}' value must be at most {max_chars} characters")
        seen.add(value.lower())
        out.append(value)
    if len(out) > max_items:
        raise ToolArgumentError(f"at most {max_items} '{key}' values")
    return tuple(out)


def _date_arg(args: dict[str, Any], key: str, *, end_of_day: bool) -> datetime | None:
    """A ``YYYY-MM-DD`` (or ISO datetime) argument as an aware UTC datetime.

    A bare date used as an upper bound means the END of that day, so
    ``until: 2026-10-01`` includes sessions active on Oct 1.
    """
    raw = _optional_str(args, key)
    if raw is None:
        return None
    try:
        if len(raw) == 10:
            day = datetime.strptime(raw, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            return day + timedelta(days=1) if end_of_day else day
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        raise ToolArgumentError(f"'{key}' must be a date like 2026-10-01") from None
    return _as_utc(dt)


@dataclass(frozen=True)
class _SessionMatch:
    """Metadata filters on the caller's sessions (model, provider, tool, dates).

    ``models`` / ``providers`` are case-insensitive family matches
    (``deepseek`` matches ``deepseek-v4-pro``); ``unknown`` selects the
    unknown bucket.  Dates bound ``updated_at`` (last activity): ``since`` is
    inclusive, ``until`` exclusive.
    """

    models: tuple[str, ...] = ()
    providers: tuple[str, ...] = ()
    source_tool: str | None = None
    since: datetime | None = None
    until: datetime | None = None

    def any(self) -> bool:
        return bool(
            self.models or self.providers or self.source_tool or self.since or self.until
        )

    def clauses(self) -> list[ColumnElement[bool]]:
        out: list[ColumnElement[bool]] = []
        if self.models:
            out.append(model_family_filter(list(self.models)))
        if self.providers:
            out.append(provider_family_filter(list(self.providers)))
        if self.source_tool:
            out.append(Session.source_tool.in_(source_tool_filter_values(self.source_tool)))
        if self.since is not None:
            out.append(Session.updated_at >= self.since)
        if self.until is not None:
            out.append(Session.updated_at < self.until)
        return out

    def describe(self) -> str:
        parts: list[str] = []
        if self.models:
            parts.append("model ~ " + " or ".join(_clean_meta(m) for m in self.models))
        if self.providers:
            parts.append(
                "provider ~ " + " or ".join(_clean_meta(p) for p in self.providers)
            )
        if self.source_tool:
            parts.append(f"tool = {_clean_meta(self.source_tool)}")
        if self.since is not None:
            parts.append(f"active since {_fmt_dt(self.since)}")
        if self.until is not None:
            parts.append(f"active before {_fmt_dt(self.until)}")
        # Echoed into the paging note that leads every listing; keep it short
        # so the note never eats into the listing's character budget.
        return _clip(", ".join(parts), MAX_MATCH_DESCRIPTION_CHARS)

    def as_params(self) -> dict[str, Any]:
        """JSON-safe form, stored on a proposal for the audit trail."""
        out: dict[str, Any] = {}
        if self.models:
            out["model"] = list(self.models)
        if self.providers:
            out["provider"] = list(self.providers)
        if self.source_tool:
            out["source_tool"] = self.source_tool
        if self.since is not None:
            out["since"] = self.since.isoformat()
        if self.until is not None:
            out["until"] = self.until.isoformat()
        return out


def _session_match(args: dict[str, Any]) -> _SessionMatch:
    models = _string_list_arg(
        args, "model", max_items=MAX_MODEL_FILTER_VALUES, max_chars=MAX_MODEL_FILTER_CHARS
    )
    providers = _string_list_arg(
        args, "provider", max_items=MAX_MODEL_FILTER_VALUES, max_chars=MAX_MODEL_FILTER_CHARS
    )
    source_tool = _optional_str(args, "source_tool")
    since = _date_arg(args, "since", end_of_day=False)
    until = _date_arg(args, "until", end_of_day=True)
    if args.get("days") is not None:
        days = _capped_int(args, "days", 30, 1, MAX_FILTER_DAYS)
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        since = max(since, cutoff) if since is not None else cutoff
    if since is not None and until is not None and since >= until:
        raise ToolArgumentError("'since' must be before 'until'")
    return _SessionMatch(
        models=models,
        providers=providers,
        source_tool=source_tool,
        since=since,
        until=until,
    )


async def _session_where(
    ctx: HelperContext, project_id: str | None, match: _SessionMatch
) -> list[ColumnElement[bool]] | None:
    """Every predicate for "the caller's sessions in scope matching ``match``".

    Ownership, trash state, the conversation scope (and requested project,
    which can only narrow it) and the metadata filters.  None = the requested
    project is not reachable, so nothing may match.
    """
    session_filter = await _project_session_filter(ctx, project_id)
    if session_filter.empty:
        return None
    where: list[ColumnElement[bool]] = [
        Session.user_id == ctx.user.id,
        Session.is_deleted == False,  # noqa: E712
    ]
    if session_filter.clause is not None:
        where.append(session_filter.clause)
    where.extend(match.clauses())
    return where


def _page_note(shown: int, offset: int, total: int, match: _SessionMatch, noun: str) -> str:
    """``Showing 21–40 of 57 sessions (model ~ deepseek). Next page: offset=40.``"""
    filters = match.describe()
    suffix = f" ({filters})" if filters else ""
    if shown == 0:
        return f"Showing 0 of {total} {noun}{suffix}."
    note = f"Showing {offset + 1}–{offset + shown} of {total} {noun}{suffix}."
    if offset + shown < total:
        note += f" Next page: offset={offset + shown}."
    return note


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


_META_UNSAFE_RE = re.compile(r"[\x00-\x1f\x7f\[\]]+")


def _clean_meta(value: str | None, limit: int = MAX_MODEL_FILTER_CHARS) -> str:
    """Client-supplied metadata (model id, provider) made safe for one line.

    The values come from the capture manifest, so control characters and
    brackets (which could forge a ``[session:…]`` marker) become spaces.
    """
    return _META_UNSAFE_RE.sub(" ", value or "").strip()[:limit]


def _model_name(model_id: str | None, provider: str | None) -> str:
    """``deepseek-v4-pro (deepseek)`` — or ``unknown`` for the unknown bucket.

    The provider is added only when the id doesn't already name it.
    """
    bucket = model_bucket(model_id)
    if bucket is None:
        return "unknown"
    name = _clean_meta(bucket)
    prov = provider_bucket(provider)
    if prov is not None and prov.lower() not in bucket.lower():
        name += f" ({_clean_meta(prov, 50)})"
    return name


def _model_label(model_id: str | None, provider: str | None) -> str:
    """``model: deepseek-v4-pro`` — the per-session model the helper sees."""
    return f"model: {_model_name(model_id, provider)}"


MAX_LISTING_TITLE_CHARS = 120
MAX_LISTING_ALIAS_CHARS = 60
# Room a listing's lines may take: the global result cap minus space for the
# paging note, so a page is never cut mid-line by ``execute``.
MAX_MATCH_DESCRIPTION_CHARS = 300
_LISTING_CHAR_BUDGET = MAX_TOOL_RESULT_CHARS - 1_000


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _title_part(title: str | None, alias: str | None) -> str:
    out = _clip(title or "(untitled)", MAX_LISTING_TITLE_CHARS)
    if alias:
        out += f" ({_clip(alias, MAX_LISTING_ALIAS_CHARS)})"
    return out


def _fit_lines(lines: list[str], budget: int | None = None) -> int:
    """How many leading ``lines`` fit in ``budget`` characters (at least one)."""
    if budget is None:
        budget = _LISTING_CHAR_BUDGET
    used = 0
    for i, line in enumerate(lines):
        used += len(line) + 1
        if used > budget and i > 0:
            return i
    return len(lines)


def _format_search_hit(hit: Any, ctx: HelperContext, query: str = "") -> str:
    line = f"[session:{hit.session_id}] {_title_part(hit.title, hit.alias)}"
    line += f" [{hit.source_tool}] {_model_label(hit.model_id, hit.model_provider)}"
    if hit.project_id:
        line += f" [project:{hit.project_id}]"
    line += f" {_display_name(ctx)} {_fmt_dt_with_age(hit.updated_at)}"
    # Snippet is built from the REDACTED full text (not the SQL substr), so a
    # secret straddling the window boundary is redacted in place, never cut.
    snippet = redacted_snippet(hit.messages_text or "", query, _redactor(ctx))
    if snippet:
        line += f" — {snippet}"
    return line


def _format_session(session: Session, ctx: HelperContext) -> str:
    line = f"[session:{session.id}] {_title_part(session.title, session.alias)}"
    line += f" [{session.source_tool}] {_model_label(session.model_id, session.model_provider)}"
    if session.project_id:
        line += f" [project:{session.project_id}]"
    line += f" {_display_name(ctx)} {_fmt_dt_with_age(session.updated_at)}"
    return line


def _format_summary(session: Session, summary: SessionSummary) -> str:
    parts = [f"[session:{session.id}] {summary.title or session.title or '(untitled)'}"]
    meta: list[str] = []
    if summary.tool:
        meta.append(f"tool: {summary.tool}")
    # The stored model id first (the one the model filters match), then the
    # archive's manifest value.
    meta.append(_model_label(session.model_id or summary.model, session.model_provider))
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
    query = _optional_str(args, "query")
    match = _session_match(args)
    if query is None:
        if not match.any():
            raise ToolArgumentError(
                "'query' is required unless model, provider, source_tool, days, "
                "since or until is given"
            )
        # "All sessions that used X": a metadata listing, no text match.
        return await _list_sessions(ctx, args, match, empty_text="No sessions matched.")

    raw_limit = args.get("limit")
    if isinstance(raw_limit, int) and not isinstance(raw_limit, bool) and raw_limit > MAX_SEARCH_RESULTS:
        raise ToolArgumentError(
            f"'limit' is at most {MAX_SEARCH_RESULTS} with a text query (up to "
            f"{MAX_RECENT_RESULTS} without one); page with 'offset'"
        )
    limit = _capped_int(args, "limit", 20, 1, MAX_SEARCH_RESULTS)
    offset = _capped_int(args, "offset", 0, 0, MAX_OFFSET)
    project_id = _optional_str(args, "project_id")

    where = await _session_where(ctx, project_id, match)
    if where is None:
        return ToolOutput("No sessions matched.", refs=_refs())

    # ``where`` already carries ownership + trash; the service re-applies those
    # and adds the full-text match.
    hits, total = await search_sessions_service(
        ctx.db,
        user_id=ctx.user.id,
        query=query,
        limit=limit,
        offset=offset,
        project_id=None,
        session_clause=and_(*where),
        include_text=True,
    )

    if not hits:
        text = "No sessions matched."
        if total:
            text += " " + _page_note(0, offset, total, match, "matching sessions")
        return ToolOutput(text, refs=_refs())
    lines = [_format_search_hit(h, ctx, query) for h in hits]
    shown = _fit_lines(lines)
    lines, hits = lines[:shown], hits[:shown]
    # The paging note leads, so a cut later on can never drop it.
    if offset or total > len(hits) or match.any():
        lines.insert(0, _page_note(len(hits), offset, total, match, "matching sessions"))
    return ToolOutput(
        "\n".join(lines),
        refs=_refs(session_ids=[h.session_id for h in hits]),
    )


async def _list_sessions(
    ctx: HelperContext,
    args: dict[str, Any],
    match: _SessionMatch,
    *,
    empty_text: str,
) -> ToolOutput:
    """Newest-first metadata listing of the caller's in-scope sessions, paged."""
    limit = _capped_int(args, "limit", 20, 1, MAX_RECENT_RESULTS)
    offset = _capped_int(args, "offset", 0, 0, MAX_OFFSET)
    project_id = _optional_str(args, "project_id")

    where = await _session_where(ctx, project_id, match)
    if where is None:
        return ToolOutput(empty_text, refs=_refs())

    total = (
        await ctx.db.execute(select(func.count()).select_from(Session).where(*where))
    ).scalar_one() or 0
    stmt = (
        select(Session)
        .where(*where)
        .order_by(Session.updated_at.desc(), Session.id.asc())
        .offset(offset)
        .limit(limit)
    )
    sessions = (await ctx.db.execute(stmt)).scalars().all()

    if not sessions:
        text = empty_text
        if total:
            text += " " + _page_note(0, offset, total, match, "sessions")
        return ToolOutput(text, refs=_refs())
    lines = [_format_session(s, ctx) for s in sessions]
    shown = _fit_lines(lines)
    lines, sessions = lines[:shown], sessions[:shown]
    # The paging note leads, so a cut later on can never drop it; ``shown``
    # (and the refs) count only the lines actually returned.
    lines.insert(0, _page_note(len(sessions), offset, total, match, "sessions"))
    return ToolOutput(
        "\n".join(lines),
        refs=_refs(session_ids=[s.id for s in sessions]),
    )


async def _list_recent_sessions(ctx: HelperContext, args: dict[str, Any]) -> ToolOutput:
    return await _list_sessions(
        ctx, args, _session_match(args), empty_text="No recent sessions."
    )


@dataclass
class _ModelBucket:
    """One row of ``list_session_models``: a (model, provider) bucket."""

    model: str | None
    provider: str | None
    count: int = 0
    tools: dict[str, int] = field(default_factory=dict)
    first: datetime | None = None
    last: datetime | None = None


def _agg_dt(value: Any) -> datetime | None:
    """An aggregate (min/max) timestamp as aware UTC; SQLite may return text."""
    if value is None:
        return None
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    return _as_utc(value)


async def _list_session_models(ctx: HelperContext, args: dict[str, Any]) -> ToolOutput:
    """Models used across the caller's in-scope sessions, with counts.

    Grouped by (model id, provider) with the same unknown bucket as
    ``GET /sessions/models``: NULL, "", "unknown" and "<synthetic>" model ids
    are one "unknown" row whatever their provider.
    """
    project_id = _optional_str(args, "project_id")
    match = _session_match(args)
    if match.models or match.providers:
        raise ToolArgumentError(
            "list_session_models lists every model; filter by model with "
            "list_recent_sessions or search_sessions"
        )
    where = await _session_where(ctx, project_id, match)
    if where is None:
        return ToolOutput("No sessions found.", refs=_refs())

    rows = (
        await ctx.db.execute(
            select(
                Session.model_id,
                Session.model_provider,
                Session.source_tool,
                func.count(Session.id),
                func.min(Session.created_at),
                func.max(Session.updated_at),
            )
            .where(*where)
            .group_by(Session.model_id, Session.model_provider, Session.source_tool)
        )
    ).all()

    buckets: dict[tuple[str | None, str | None], _ModelBucket] = {}
    for model_id, provider, tool, count, first, last in rows:
        model = model_bucket(model_id)
        # One unknown bucket, whatever provider the rows carry.
        prov = provider_bucket(provider) if model is not None else None
        key = (model, prov.lower() if prov else None)
        bucket = buckets.setdefault(key, _ModelBucket(model=model, provider=prov))
        bucket.count += count
        bucket.tools[tool] = bucket.tools.get(tool, 0) + count
        first_dt, last_dt = _agg_dt(first), _agg_dt(last)
        if first_dt is not None and (bucket.first is None or first_dt < bucket.first):
            bucket.first = first_dt
        if last_dt is not None and (bucket.last is None or last_dt > bucket.last):
            bucket.last = last_dt

    if not buckets:
        filters = match.describe()
        return ToolOutput(
            "No sessions found" + (f" ({filters})." if filters else "."), refs=_refs()
        )

    ordered = sorted(
        buckets.values(),
        key=lambda b: (b.model is None, -b.count, (b.model or "").lower(), b.provider or ""),
    )
    total_sessions = sum(b.count for b in ordered)
    filters = match.describe()
    lines = [
        f"{len(ordered)} model bucket(s) across {total_sessions} session(s)"
        + (f" ({filters})" if filters else "")
        + ", most used first. Model ids are reported by the capturing tool."
    ]
    for b in ordered[:MAX_MODEL_BUCKETS]:
        if b.model is None:
            name = "unknown (no model recorded)"
        else:
            name = _clean_meta(b.model)
            if b.provider:
                name += f" (provider: {_clean_meta(b.provider, 50)})"
        tools = ", ".join(
            f"{_clean_meta(t, 50)} {n}"
            for t, n in sorted(b.tools.items(), key=lambda kv: (-kv[1], kv[0]))
        )
        lines.append(
            f"- {name} — {b.count} session(s); tools: {tools}; "
            f"first {_fmt_dt(b.first)}, last {_fmt_dt_with_age(b.last)}"
        )
    if len(ordered) > MAX_MODEL_BUCKETS:
        lines.append(f"[showing {MAX_MODEL_BUCKETS} of {len(ordered)} model buckets]")
    lines.append(
        "To list the sessions, pass model (or provider) to list_recent_sessions "
        "or search_sessions; \"unknown\" selects the unknown bucket."
    )
    effective = _scope_project(ctx, project_id)
    return ToolOutput(
        "\n".join(lines),
        refs=_refs(project_ids=[effective] if effective else None),
    )


async def _find_related_sessions(ctx: HelperContext, args: dict[str, Any]) -> ToolOutput:
    file_path = _optional_str(args, "file_path")
    error_text = _optional_str(args, "error_text")
    if not file_path and not error_text:
        raise ToolArgumentError("provide 'file_path' or 'error_text'")
    limit = _capped_int(args, "limit", 10, 1, MAX_RELATED_RESULTS)
    project_id = _optional_str(args, "project_id")

    session_filter = await _project_session_filter(ctx, project_id)
    if session_filter.empty:
        return ToolOutput("No related sessions found.", refs=_refs())

    # No server-side related-sessions index exists; map to full-text search
    # with the path/error text as the query.
    query = file_path or error_text or ""
    hits, _total = await search_sessions_service(
        ctx.db,
        user_id=ctx.user.id,
        query=query,
        limit=limit,
        project_id=None,
        session_clause=session_filter.clause,
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
            message_refs=[(session.id, e.index) for e in excerpts],
        ),
    )


# LIKE-wildcard escaping lives with the session model filters (which use it
# too); pair it with ``escape="\\"`` on ``ilike``.
_escape_like = escape_like


async def _search_project_knowledge(
    ctx: HelperContext, args: dict[str, Any]
) -> ToolOutput:
    project_id = _require_str(args, "project_id")
    query = _require_str(args, "query")
    limit = _capped_int(args, "limit", 20, 1, MAX_KB_RESULTS)

    project = await _accessible_project(ctx, project_id)
    if project is None:
        return ToolOutput("Project not found.", refs=_refs())

    # Freshness first: current > aging > stale, then confidence, then newest.
    # Superseded/dismissed rows are excluded above, so the only freshness
    # classes left are current/aging/stale (any unknown value sorts last).
    freshness_order = case(
        (KnowledgeEntry.freshness_class == "current", 0),
        (KnowledgeEntry.freshness_class == "aging", 1),
        (KnowledgeEntry.freshness_class == "stale", 2),
        else_=3,
    )
    stmt = (
        select(KnowledgeEntry)
        .where(
            KnowledgeEntry.project_id == project_id,
            KnowledgeEntry.dismissed == False,  # noqa: E712
            KnowledgeEntry.superseded_by.is_(None),
            KnowledgeEntry.claim_class != "note",
            KnowledgeEntry.content.ilike(f"%{_escape_like(query)}%", escape="\\"),
        )
        .order_by(
            freshness_order,
            KnowledgeEntry.confidence.desc(),
            KnowledgeEntry.created_at.desc(),
        )
        .limit(limit)
    )
    entries = (await ctx.db.execute(stmt)).scalars().all()

    if not entries:
        return ToolOutput("No knowledge entries matched.", refs=_refs())

    lines = [
        f"[kb:{e.id}] ({e.freshness_class}, {e.claim_class}, "
        f"as of {_fmt_dt_with_age(e.compiled_at or e.created_at)}) {e.content}"
        for e in entries
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
    pairs: list[tuple[str, int]] = []
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
            pairs.extend((session.id, e.index) for e in excerpts)
        blocks.append(block)
        found.append(session.id)

    return ToolOutput(
        "\n\n".join(blocks),
        refs=_refs(session_ids=found, message_indexes=indexes, message_refs=pairs),
    )


async def _sessions_touching_path(
    ctx: HelperContext, args: dict[str, Any]
) -> ToolOutput:
    path = _require_str(args, "path")
    limit = _capped_int(args, "limit", 20, 1, MAX_PATH_RESULTS)
    project_id = _optional_str(args, "project_id")
    normalized = normalize_path(path)
    match = _session_match(args)

    where = await _session_where(ctx, project_id, match)
    if where is None:
        return ToolOutput("No sessions touched that path.", refs=_refs())

    stmt = select(Session).where(*where)
    count_stmt = select(func.count()).select_from(Session).where(*where)
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
# Read tools: projects + settings (piece 6)
# ---------------------------------------------------------------------------


def _new_action_id() -> str:
    return "ha_" + uuid.uuid4().hex[:24]


def _as_utc(dt: datetime) -> datetime:
    """Aware UTC (SQLite returns naive datetimes for timezone columns)."""
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


_proposal_clock_lock = threading.Lock()
_last_proposal_time: datetime | None = None


def _proposal_time() -> datetime:
    """Proposal timestamp, strictly increasing within this process.

    The pending cap expires the OLDEST proposals first by ``created_at``; two
    proposals from one turn must never tie (a tie would leave the order to
    the random id), so a clock that hasn't advanced is nudged by 1 µs.
    """
    global _last_proposal_time
    with _proposal_clock_lock:
        now = datetime.now(timezone.utc)
        if _last_proposal_time is not None and now <= _last_proposal_time:
            now = _last_proposal_time + timedelta(microseconds=1)
        _last_proposal_time = now
        return now


async def _list_projects(ctx: HelperContext, args: dict[str, Any]) -> ToolOutput:
    """Every project the caller can reach, same predicate as the projects route.

    A project-scoped conversation returns just its scope project (the model
    can never widen the scope); otherwise the full accessible list, capped at
    ``MAX_LIST_PROJECTS`` with a truncation note.
    """
    projects = await _accessible_projects(ctx)

    truncated = len(projects) > MAX_LIST_PROJECTS
    shown = projects[:MAX_LIST_PROJECTS]

    plan_tiers = await resolve_project_plan_tiers(shown, ctx.db) if shown else {}
    org_ids = {p.org_id for p in shown if p.org_id}
    org_names: dict[str, str] = {}
    if org_ids:
        orgs = (
            await ctx.db.execute(select(Organization).where(Organization.id.in_(org_ids)))
        ).scalars().all()
        org_names = {o.id: o.name for o in orgs}

    lines: list[str] = []
    for p in shown:
        tier = plan_tiers.get(p.id)
        scope = org_names.get(p.org_id, "personal") if p.org_id else "personal"
        lines.append(
            f"[project:{p.id}] {p.name} "
            f"(scope: {scope}, tier: {tier.value if tier else 'unknown'}, "
            f"auto_narrative: {'on' if p.auto_narrative else 'off'})"
        )
    if not lines:
        return ToolOutput("No projects accessible.", refs=_refs())
    text = "\n".join(lines)
    if truncated:
        text += f"\n[showing {MAX_LIST_PROJECTS} of {len(projects)} projects]"
    return ToolOutput(text, refs=_refs(project_ids=[p.id for p in shown]))


async def _get_project_settings(ctx: HelperContext, args: dict[str, Any]) -> ToolOutput:
    project_id = _require_str(args, "project_id")
    project = await _accessible_project(ctx, project_id)
    if project is None:
        return ToolOutput("Project not found.", refs=_refs())
    lines = [
        f"[project:{project.id}] {project.name}",
        f"auto_narrative: {'on' if project.auto_narrative else 'off'}",
        f"compile_schedule: {project.compile_schedule or 'off'}",
        f"kb_retention_days: {project.kb_retention_days}",
        f"kb_max_context_words: {project.kb_max_context_words}",
        f"kb_section_page_limit: {project.kb_section_page_limit}",
    ]
    if project.compile_after_claims is not None:
        lines.append(f"compile_after_claims: {project.compile_after_claims}")
    return ToolOutput("\n".join(lines), refs=_refs(project_ids=[project_id]))


# ---------------------------------------------------------------------------
# Write-action tools: propose only (piece 6)
# ---------------------------------------------------------------------------


def _session_name(session: Session) -> str:
    return session.title or session.alias or "(untitled)"


def _format_action_targets(targets: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    for t in targets:
        if t["change"] == "will_change":
            if "before" in t and "after" in t:
                lines.append(
                    f"- [{t['id']}] {t['name']}: {t['before']} -> {t['after']} (will change)"
                )
            else:
                # Create/comment kinds have no meaningful before/after state.
                lines.append(f"- [{t['id']}] {t['name']} (will change)")
        elif t["change"] == "unchanged":
            lines.append(f"- [{t['id']}] {t['name']}: already {t['after']} (unchanged)")
        else:
            lines.append(f"- [{t['id']}] {t['name']}: skipped ({t['reason']})")
    return "\n".join(lines)


def _redact_targets(ctx: HelperContext, targets: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Run every display name through the turn's DLP policy.

    Names are user-authored (session titles, project names) and land in the
    stored preview and the ``action_proposed`` event, so they get the same
    redaction as tool output.  Raises :class:`RedactionError` (the caller's
    ``execute`` fails the call closed and the proposal is dropped).
    """
    out: list[dict[str, Any]] = []
    for target in targets:
        t = dict(target)
        if isinstance(t.get("name"), str):
            t["name"] = redact(t["name"], ctx.dlp_policy)
        out.append(t)
    return out


def _propose_action(
    ctx: HelperContext,
    *,
    kind: str,
    params: dict[str, Any],
    targets: list[dict[str, Any]],
    summary: str,
    refs: dict[str, Any] | None = None,
) -> ToolOutput:
    """Queue a pending write-action proposal; never mutates anything.

    The proposal is only queued here.  The turn handler persists it (and only
    then emits ``action_proposed``) once ``execute`` returns a successful
    result, so a call that fails redaction or the audit write never leaves a
    row or a card behind.  ``refs`` is the audit fan-out (project/ticket ids
    the handler actually read); callers pass it for kinds whose params don't
    use the historical ``project_ids``/``session_ids`` keys.
    """
    action_id = _new_action_id()
    created_at = _proposal_time()
    expires_at = created_at + timedelta(minutes=ACTION_TTL_MINUTES)
    targets = _redact_targets(ctx, targets)
    ctx.pending_actions.append(
        {
            "id": action_id,
            "kind": kind,
            "params": params,
            "preview": {"summary": summary, "targets": targets},
            "created_at": created_at,
            "expires_at": expires_at,
        }
    )
    text = (
        f"Proposed action {action_id} — waiting for the user to confirm. "
        "Do not claim it is done.\n"
        "Targets:\n"
        f"{_format_action_targets(targets)}"
    )
    return ToolOutput(
        text,
        refs=refs
        if refs is not None
        else _refs(
            project_ids=params.get("project_ids"),
            session_ids=params.get("session_ids"),
        ),
    )


def _project_ids_arg(args: dict[str, Any]) -> list[str]:
    raw = args.get("project_ids")
    if not isinstance(raw, list) or not raw or not all(isinstance(s, str) for s in raw):
        raise ToolArgumentError("'project_ids' must be a non-empty list of project ids")
    ids = sorted({s for s in raw if s.strip()})
    if not ids:
        raise ToolArgumentError("'project_ids' must be a non-empty list of project ids")
    if len(ids) > MAX_ACTION_TARGETS:
        raise ToolArgumentError(f"at most {MAX_ACTION_TARGETS} projects per proposal")
    return ids


def _session_ids_arg(args: dict[str, Any]) -> list[str]:
    raw = args.get("session_ids")
    if not isinstance(raw, list) or not raw or not all(isinstance(s, str) for s in raw):
        raise ToolArgumentError("'session_ids' must be a non-empty list of session ids")
    ids = sorted({s for s in raw if s.strip()})
    if not ids:
        raise ToolArgumentError("'session_ids' must be a non-empty list of session ids")
    if len(ids) > MAX_ACTION_TARGETS:
        raise ToolArgumentError(f"at most {MAX_ACTION_TARGETS} sessions per proposal")
    return ids


async def _set_project_auto_narrative(
    ctx: HelperContext, args: dict[str, Any]
) -> ToolOutput:
    project_ids = _project_ids_arg(args)
    enabled = args.get("enabled")
    if not isinstance(enabled, bool):
        raise ToolArgumentError("'enabled' must be a boolean")

    targets: list[dict[str, Any]] = []
    will_change: list[str] = []
    user_ctx: UserContext | None = None
    for pid in project_ids:
        # Same edit rule as PUT /projects/{id}/settings: project access (inside
        # this conversation's scope), editor standing (owner or org member —
        # a captured-session reader can view but not change the project),
        # then the project_context plan gate.
        project = await _accessible_project(ctx, pid)
        if project is None:
            targets.append(
                {"id": pid, "name": pid, "change": "skipped", "reason": "not_accessible"}
            )
            continue
        if not await user_can_edit_project(ctx.db, ctx.user.id, project):
            targets.append(
                {"id": pid, "name": project.name, "change": "skipped", "reason": "not_editor"}
            )
            continue
        before = bool(project.auto_narrative)
        if before != enabled:
            if user_ctx is None:
                user_ctx = await build_user_context(ctx.user, ctx.db)
            try:
                await check_auto_narrative_gate(project, ctx=user_ctx, db=ctx.db)
            except HTTPException as exc:
                if exc.status_code != 403:
                    raise
                targets.append(
                    {
                        "id": pid,
                        "name": project.name,
                        "change": "skipped",
                        "reason": "upgrade_required",
                    }
                )
                continue
        if before == enabled:
            targets.append(
                {
                    "id": pid,
                    "name": project.name,
                    "change": "unchanged",
                    "before": before,
                    "after": enabled,
                }
            )
        else:
            targets.append(
                {
                    "id": pid,
                    "name": project.name,
                    "change": "will_change",
                    "before": before,
                    "after": enabled,
                }
            )
            will_change.append(pid)

    if not will_change:
        # Audit only the projects the caller could actually read here.
        return ToolOutput(
            "Nothing to change.\n" + _format_action_targets(targets),
            refs=_refs(
                project_ids=[
                    t["id"] for t in targets if t.get("reason") != "not_accessible"
                ]
            ),
        )

    summary = f"Set auto-narrative {'on' if enabled else 'off'} for {len(will_change)} project(s)"
    return _propose_action(
        ctx,
        kind="set_auto_narrative",
        params={"project_ids": will_change, "enabled": enabled},
        targets=targets,
        summary=summary,
    )


async def _resolve_trash_filter(
    ctx: HelperContext, match: _SessionMatch, project_id: str | None
) -> tuple[list[str], int]:
    """Resolve trash filters to concrete session ids, server-side.

    Returns ``(ids, total)``: at most ``MAX_ACTION_TARGETS`` ids of the
    caller's own, active, in-scope sessions matching the filters, newest
    activity first (id breaks ties) so the same filters always propose the
    same batch, plus the total match count.  A ``project_id`` outside the
    conversation's scope is refused (:func:`_check_scope_project`).
    """
    where = await _session_where(ctx, project_id, match)
    if where is None:
        return [], 0
    total = (
        await ctx.db.execute(select(func.count()).select_from(Session).where(*where))
    ).scalar_one() or 0
    ids = list(
        (
            await ctx.db.execute(
                select(Session.id)
                .where(*where)
                .order_by(Session.updated_at.desc(), Session.id.asc())
                .limit(MAX_ACTION_TARGETS)
            )
        ).scalars().all()
    )
    return ids, total


async def _trash_sessions(ctx: HelperContext, args: dict[str, Any]) -> ToolOutput:
    has_ids = args.get("session_ids") is not None
    # Decided AFTER parsing: blank values (" ", [""], an empty date) parse to
    # nothing and must not turn into "trash my 100 newest sessions".
    match = _session_match(args)
    project_id = _optional_str(args, "project_id")
    has_filter = match.any() or project_id is not None
    if has_ids and has_filter:
        raise ToolArgumentError("pass either 'session_ids' or filters, not both")
    if not has_ids and not has_filter:
        raise ToolArgumentError(
            "pass 'session_ids', or at least one filter (model, provider, "
            "project_id, source_tool, days, since, until)"
        )
    reason = _optional_str(args, "reason")

    filter_note = ""
    match_params: dict[str, Any] | None = None
    if has_ids:
        session_ids = _session_ids_arg(args)
    else:
        session_ids, total = await _resolve_trash_filter(ctx, match, project_id)
        # A requested project equals the scope project here (anything else was
        # refused); a scoped conversation resolves against its project.
        project_id = _scope_project(ctx, project_id)
        described = match.describe()
        if project_id:
            described = ", ".join(p for p in (f"project = {project_id}", described) if p)
        if not session_ids:
            return ToolOutput(
                f"No sessions match ({described}); nothing to propose.", refs=_refs()
            )
        match_params = match.as_params()
        if project_id:
            match_params["project_id"] = project_id
        match_params["matched"] = total
        if total > len(session_ids):
            filter_note = (
                f"{total} sessions match ({described}); this proposal covers the "
                f"first {len(session_ids)} (most recently active first). After the "
                "user confirms, call trash_sessions again with the same filters to "
                "propose the next batch.\n"
            )
        else:
            filter_note = f"{total} session(s) match ({described}).\n"

    targets: list[dict[str, Any]] = []
    will_change: list[str] = []
    for sid in session_ids:
        session = (
            await ctx.db.execute(select(Session).where(Session.id == sid))
        ).scalar_one_or_none()
        if session is None or session.user_id != ctx.user.id:
            targets.append(
                {"id": sid, "name": sid, "change": "skipped", "reason": "not_owned"}
            )
            continue
        if session.is_deleted:
            targets.append(
                {"id": sid, "name": _session_name(session), "change": "skipped", "reason": "already_trashed"}
            )
            continue
        # Same scope rule as every read tool (``_owned_session``: scope
        # session ids, the scope project's sessions incl. unattributed legacy
        # ones on its remotes, or a scope group's member-project sessions).
        # An out-of-scope session is named by id only — nothing from outside
        # the scope reaches the model.
        if (
            ctx.scope.session_ids
            or ctx.scope.project_id is not None
            or ctx.scope.group_id is not None
        ) and (await _owned_session(ctx, sid)) is None:
            targets.append(
                {"id": sid, "name": sid, "change": "skipped", "reason": "not_in_scope"}
            )
            continue
        targets.append(
            {
                "id": sid,
                "name": _session_name(session),
                "change": "will_change",
                "before": "active",
                "after": "trashed",
                "tool": session.source_tool,
                "model": _model_name(session.model_id, session.model_provider),
                "created_at": (
                    _as_utc(session.created_at).isoformat() if session.created_at else None
                ),
            }
        )
        will_change.append(sid)

    if not will_change:
        # Audit only the caller's own, in-scope sessions.
        return ToolOutput(
            filter_note + "Nothing to change.\n" + _format_action_targets(targets),
            refs=_refs(
                session_ids=[
                    t["id"]
                    for t in targets
                    if t.get("reason") not in ("not_owned", "not_in_scope")
                ]
            ),
        )

    summary = f"Move {len(will_change)} session(s) to Trash"
    params: dict[str, Any] = {"session_ids": will_change}
    if match_params is not None:
        # The filters the ids were resolved from — audit context only; confirm
        # acts on ``session_ids`` exactly.
        params["match"] = match_params
        if match_params["matched"] > len(will_change):
            summary += f" (first {len(will_change)} of {match_params['matched']} matching)"
    if reason:
        # Model-written free text: stored and audited, so redacted first.
        params["reason"] = redact(reason, ctx.dlp_policy)[:500]
    out = _propose_action(
        ctx,
        kind="trash_sessions",
        params=params,
        targets=targets,
        summary=summary,
    )
    return ToolOutput(filter_note + out.text, refs=out.refs) if filter_note else out


# ---------------------------------------------------------------------------
# Ticket write-action tools (GitHub #104 part 2): propose only
# ---------------------------------------------------------------------------


# Fields a proposal's ``sources`` object may map to citations.
_SOURCE_FIELDS = (
    "title",
    "description",
    "acceptance_criteria",
    "priority",
    "kind",
    "assignee_persona",
    "body",
)


def _not_seen(src: str) -> ToolArgumentError:
    return ToolArgumentError(f"source {src!r} was not returned by a tool this conversation")


def _validate_source_list(
    ctx: HelperContext, field: str, raw: list[Any]
) -> list[str]:
    """Validate one field's citations; every marker must have been seen.

    "Seen" means a tool returned that exact row as a structured ref
    (``ctx.seen_*``) — never that the marker text appeared somewhere in a
    tool result's content, which a ticket, wiki page or session could carry.
    A ``[session:<id> msg:<n>]`` source needs that exact (session, message)
    pair; a message index surfaced for another session doesn't count.
    """
    if not all(isinstance(r, str) and r.strip() for r in raw):
        raise ToolArgumentError(
            f"every source for {field!r} must be a non-empty string"
        )
    cleaned: list[str] = []
    for item in raw:
        src = item.strip()
        if src == "user":
            cleaned.append(src)
            continue
        m = _SOURCE_TICKET_RE.fullmatch(src)
        if m:
            if m.group("id") not in ctx.seen_ticket_ids:
                raise _not_seen(src)
            cleaned.append(src)
            continue
        m = _SOURCE_WIKI_RE.fullmatch(src)
        if m:
            if (m.group("project_id"), m.group("slug")) not in ctx.seen_wiki_refs:
                raise _not_seen(src)
            cleaned.append(src)
            continue
        m = _SOURCE_SESSION_RE.fullmatch(src)
        if m:
            sid = m.group("id")
            if m.group("idx") is not None:
                if (sid, int(m.group("idx"))) not in ctx.seen_message_refs:
                    raise _not_seen(src)
            elif sid not in ctx.seen_session_ids:
                raise _not_seen(src)
            cleaned.append(src)
            continue
        m = _SOURCE_DOC_RE.fullmatch(src)
        if m:
            if m.group("slug") not in ctx.seen_doc_slugs:
                raise _not_seen(src)
            cleaned.append(src)
            continue
        raise ToolArgumentError(f"unrecognized source {src!r}")
    return cleaned


def _validate_sources(
    ctx: HelperContext,
    sources: Any,
    *,
    required_fields: tuple[str, ...],
) -> dict[str, list[str]]:
    """Reject a proposal that leaves a content field unsourced or cites
    something the conversation's tool results never surfaced.

    ``sources`` maps each field to the citations that back it ("the
    conversation citations each field came from").  Every required field must
    carry at least one citation; every citation must be one of the markers
    (``[session:…]``, ``[ticket:…]``, ``[wiki:…]``, ``[doc:…]``) or ``user``,
    and a marker's id/ref must be in the conversation's seen sets: the
    structured refs ``execute`` records as results are delivered, seeded by
    the turn handler from prior turns' stored tool refs and confirmed
    helper-action results.
    """
    if not isinstance(sources, dict):
        raise ToolArgumentError(
            "'sources' must be an object mapping each field to a list of citations"
        )
    cleaned: dict[str, list[str]] = {}
    for fname in required_fields:
        raw = sources.get(fname)
        if not isinstance(raw, list) or not raw:
            raise ToolArgumentError(f"field {fname!r} needs at least one source")
        cleaned[fname] = _validate_source_list(ctx, fname, raw)
    for fname, raw in sources.items():
        if fname in cleaned:
            continue
        if fname not in _SOURCE_FIELDS:
            raise ToolArgumentError(f"unknown source field {fname!r}")
        if not isinstance(raw, list) or not raw:
            raise ToolArgumentError(f"field {fname!r} needs at least one source")
        cleaned[fname] = _validate_source_list(ctx, fname, raw)
    return cleaned


async def _propose_ticket(ctx: HelperContext, args: dict[str, Any]) -> ToolOutput:
    project_id = _require_str(args, "project_id")
    title = args.get("title")
    if not isinstance(title, str):
        raise ToolArgumentError("'title' is required")
    assignee = _optional_str(args, "assignee_persona")
    publish_to_jira = _optional_bool(args, "publish_to_jira")
    due_date = _optional_str(args, "due_date")
    estimated_effort = _optional_str(args, "estimated_effort")
    # Same shape rules as the tickets route (``TicketCreate``) plus the
    # helper's write limits; an over-length draft is refused, not truncated.
    try:
        draft = check_ticket_draft(
            title=title,
            description=args.get("description") or "",
            acceptance_criteria=args.get("acceptance_criteria") or [],
            priority=args.get("priority") or "medium",
            kind=args.get("kind") or "task",
            assigned_to=assignee,
            due_date=due_date,
            estimated_effort=estimated_effort,
        )
    except HelperDraftError as exc:
        raise ToolArgumentError(str(exc)) from None
    sources = _validate_sources(
        ctx,
        args.get("sources"),
        required_fields=("title", "description", "acceptance_criteria"),
    )

    project = await _accessible_project(ctx, project_id)
    if project is None:
        return ToolOutput("Project not found.", refs=_refs())
    if not await _feature_allowed(ctx, FEATURE_TICKETS, project):
        return ToolOutput(
            "Tickets are not available on this project's plan.", refs=_refs()
        )

    # Publishing to Jira is an optional extension of the create: only when the
    # caller asks for it do we gate on the same preconditions the confirm will
    # re-check.  Any refusal here returns no proposal row.
    jira_project_key: str | None = None
    jira_project_key_override: str | None = None
    jira_template: str | None = None
    jira_template_version: int | None = None
    jira_template_builtin: bool | None = None
    jira_sprint_id: int | None = None
    if publish_to_jira:
        if not await user_can_edit_project(ctx.db, ctx.user.id, project):
            return ToolOutput(
                "Cannot publish to Jira: publishing requires editor access to "
                "the project.",
                refs=_refs(project_ids=[project_id]),
            )
        if not await _feature_allowed(ctx, FEATURE_JIRA, project):
            return ToolOutput(
                "Cannot publish to Jira: the organization's plan does not "
                "include Jira integration.",
                refs=_refs(project_ids=[project_id]),
            )
        if project.org_id is None:
            return ToolOutput(
                "Cannot publish to Jira: this project is not org-scoped.",
                refs=_refs(project_ids=[project_id]),
            )
        conn = (
            await ctx.db.execute(
                select(OrgTrackerConnection).where(
                    OrgTrackerConnection.org_id == project.org_id
                )
            )
        ).scalar_one_or_none()
        if conn is None or not conn.enabled:
            return ToolOutput(
                "Cannot publish to Jira: this organization has not connected "
                "Jira.",
                refs=_refs(project_ids=[project_id]),
            )
        mapping = (
            await ctx.db.execute(
                select(TrackerProjectMapping).where(
                    TrackerProjectMapping.project_id == project.id,
                    TrackerProjectMapping.org_id == project.org_id,
                )
            )
        ).scalar_one_or_none()
        if mapping is None:
            return ToolOutput(
                "Cannot publish to Jira: this project has no Jira mapping. Ask "
                "an admin to add a mapping.",
                refs=_refs(project_ids=[project_id]),
            )
        jira_project_key = mapping.jira_project_key

        # Optional publish overrides (GitHub #113 C).  A project-key override
        # must name a Jira project some SessionFS project in this org maps to,
        # AND the caller must be able to edit at least one such project.  An
        # invalid override refuses with no proposal row.
        override_key = _optional_str(args, "jira_project_key")
        if override_key is not None and override_key.upper() != jira_project_key.upper():
            err = await jira_project_key_override_error(
                ctx.db,
                org_id=project.org_id,
                jira_project_key=override_key,
                user_id=ctx.user.id,
            )
            if err is not None:
                return ToolOutput(
                    f"Cannot publish to Jira: {err}",
                    refs=_refs(project_ids=[project_id]),
                )
            jira_project_key_override = override_key.strip().upper()
            jira_project_key = jira_project_key_override

        template_slug = _optional_str(args, "jira_template")
        if template_slug is not None:
            err = await jira_template_override_error(
                ctx.db, org_id=project.org_id, slug=template_slug
            )
            if err is not None:
                return ToolOutput(
                    f"Cannot publish to Jira: {err}",
                    refs=_refs(project_ids=[project_id]),
                )
            jira_template = template_slug
            jira_template_version = await jira_template_override_version(
                ctx.db, org_id=project.org_id, slug=template_slug
            )
            # Record whether the slug resolved to a built-in (no org custom row,
            # hence no version) so confirm can keep publishing the built-in even
            # if a custom template later shadows it — and, conversely, a pinned
            # custom template never falls back to a same-slug built-in (L-c).
            jira_template_builtin = jira_template_version is None

        jira_sprint_id = _optional_int(args, "jira_sprint_id")
        if jira_sprint_id is not None and jira_sprint_id <= 0:
            raise ToolArgumentError("'jira_sprint_id' must be a positive integer")
        if jira_sprint_id is not None and not conn.sprint_field_id:
            return ToolOutput(
                "Cannot publish to Jira: this connection has no sprint field "
                "discovered; ask an org admin to save the connection or set a "
                "sprint field id.",
                refs=_refs(project_ids=[project_id]),
            )

    # A drafted assignee resolves exactly like propose_assignment: a persona
    # name, or a human org member to the persona representing them, or a
    # teammate email/id to a direct-to-person assignment (GitHub #96).
    assigned_to: str | None = None
    assigned_user_id: str | None = None
    assignee_name: str | None = None
    if assignee:
        try:
            resolved = await resolve_assignee(
                ctx.db, project=project, user_id=ctx.user.id, name=assignee
            )
        except HelperDraftError as exc:
            raise ToolArgumentError(str(exc)) from None
        if resolved.kind == "persona":
            assigned_to = resolved.persona_name
        else:
            assigned_user_id = resolved.user_id
            assignee_name = resolved.display_name

    # The model draft is stored redacted — it is user-authored free text drawn
    # from the caller's own data.
    params: dict[str, Any] = {
        "project_id": project_id,
        "title": redact(draft.title, ctx.dlp_policy),
        "description": redact(draft.description, ctx.dlp_policy),
        "acceptance_criteria": [redact(c, ctx.dlp_policy) for c in draft.acceptance_criteria],
        "priority": draft.priority,
        "kind": draft.kind,
        "assigned_to": assigned_to,
        "assigned_user_id": assigned_user_id,
        "assignee_name": assignee_name,
        "due_date": draft.due_date.isoformat() if draft.due_date else None,
        "estimated_effort": draft.estimated_effort,
        "sources": sources,
    }
    if publish_to_jira:
        params["publish_to_jira"] = True
        params["jira_project_key"] = jira_project_key
        if jira_project_key_override is not None:
            params["jira_project_key_override"] = jira_project_key_override
        if jira_template is not None:
            params["jira_template"] = jira_template
            params["jira_template_version"] = jira_template_version
            params["jira_template_builtin"] = jira_template_builtin
        if jira_sprint_id is not None:
            params["jira_sprint_id"] = jira_sprint_id
    summary = redact(
        f"Create {_article(draft.kind)} {draft.kind} ticket in {project.name}",
        ctx.dlp_policy,
    )
    if publish_to_jira:
        parts = [f"and publish it to Jira {jira_project_key}"]
        if jira_template is not None:
            parts.append(f"with template {jira_template}")
        if jira_sprint_id is not None:
            parts.append(f"into sprint {jira_sprint_id}")
        summary = redact(
            f"Create {_article(draft.kind)} {draft.kind} ticket in {project.name} "
            + " ".join(parts),
            ctx.dlp_policy,
        )
    return _propose_action(
        ctx,
        kind="create_ticket",
        params=params,
        targets=[{"id": project_id, "name": project.name, "change": "will_change"}],
        summary=summary,
        refs=_refs(project_ids=[project_id]),
    )


def _article(word: str) -> str:
    return "an" if word[:1].lower() in "aeiou" else "a"


async def _propose_assignment(ctx: HelperContext, args: dict[str, Any]) -> ToolOutput:
    ticket_id = _require_str(args, "ticket_id")
    persona_name = _require_str(args, "persona_name")

    ticket = (
        await ctx.db.execute(select(Ticket).where(Ticket.id == ticket_id))
    ).scalar_one_or_none()
    if ticket is None:
        return ToolOutput("Ticket not found.", refs=_refs())
    project = await _accessible_project(ctx, ticket.project_id)
    if project is None or not await _feature_allowed(ctx, FEATURE_TICKETS, project):
        return ToolOutput("Ticket not found.", refs=_refs())

    # Same rules as the assignment route (ticket creator or project admin; a
    # non-editor can't reassign once the ticket is past suggested/open or a
    # work queue holds it).  Checked again at confirm time.
    refusal = await check_reassignment_allowed(
        ctx.db, ticket=ticket, project=project, user_id=ctx.user.id
    )
    if refusal is not None:
        return ToolOutput(
            f"You cannot assign this ticket: {refusal}",
            refs=_refs(project_ids=[ticket.project_id], ticket_ids=[ticket.id]),
        )

    try:
        resolved = await resolve_assignee(
            ctx.db, project=project, user_id=ctx.user.id, name=persona_name
        )
    except HelperDraftError as exc:
        raise ToolArgumentError(str(exc)) from None

    # A readable "before" label for either a persona or a person assignment.
    before = ticket.assigned_to
    if ticket.assigned_user_id:
        before_user = await ctx.db.get(User, ticket.assigned_user_id)
        if before_user is not None:
            before = before_user.display_name or before_user.email

    if resolved.kind == "persona":
        after = resolved.persona_name or ""
        unchanged = (
            ticket.assigned_user_id is None and ticket.assigned_to == resolved.persona_name
        )
        params: dict[str, Any] = {
            "ticket_id": ticket.id,
            "project_id": ticket.project_id,
            "assigned_to": resolved.persona_name,
        }
    else:
        after = resolved.display_name or resolved.user_id or ""
        unchanged = ticket.assigned_user_id == resolved.user_id
        params = {
            "ticket_id": ticket.id,
            "project_id": ticket.project_id,
            "assigned_user_id": resolved.user_id,
            "assignee_name": resolved.display_name,
        }

    if unchanged:
        return ToolOutput(
            "Nothing to change: the ticket is already assigned to "
            f"{after}.\n"
            + _format_action_targets(
                [{"id": ticket.id, "name": ticket.title, "change": "unchanged", "before": before, "after": after}]
            ),
            refs=_refs(project_ids=[ticket.project_id], ticket_ids=[ticket.id]),
        )

    summary = redact(f"Assign {ticket.title} to {after}", ctx.dlp_policy)
    return _propose_action(
        ctx,
        kind="assign_ticket",
        # ``project_id`` pins the ticket's project: confirm refuses if the
        # ticket has moved since.
        params=params,
        targets=[
            {
                "id": ticket.id,
                "name": ticket.title,
                "change": "will_change",
                "before": before,
                "after": after,
            }
        ],
        summary=summary,
        refs=_refs(project_ids=[ticket.project_id], ticket_ids=[ticket.id]),
    )


async def _propose_comment(ctx: HelperContext, args: dict[str, Any]) -> ToolOutput:
    ticket_id = _require_str(args, "ticket_id")
    # Same shape rules as the comment route (strip, required, 10,000 chars)
    # and never a review verdict; an over-length draft is refused.
    try:
        body = check_comment_body(args.get("body"))
    except HelperDraftError as exc:
        raise ToolArgumentError(str(exc)) from None
    sources = _validate_sources(
        ctx, args.get("sources"), required_fields=("body",)
    )
    send_to_jira = _optional_bool(args, "send_to_jira")

    ticket = (
        await ctx.db.execute(select(Ticket).where(Ticket.id == ticket_id))
    ).scalar_one_or_none()
    if ticket is None:
        return ToolOutput("Ticket not found.", refs=_refs())
    project = await _accessible_project(ctx, ticket.project_id)
    if project is None or not await _feature_allowed(ctx, FEATURE_TICKETS, project):
        return ToolOutput("Ticket not found.", refs=_refs())

    params: dict[str, Any] = {
        "ticket_id": ticket.id,
        # Pins the ticket's project: confirm refuses if the ticket has moved.
        "project_id": ticket.project_id,
        "body": redact(body, ctx.dlp_policy),
        "sources": sources,
    }
    refs = _refs(project_ids=[ticket.project_id], ticket_ids=[ticket.id])
    issue_key: str | None = None
    if send_to_jira:
        # Refuse (no proposal row) when the Jira path can't be honoured, so the
        # helper can propose without send_to_jira instead of silently dropping
        # the flag.  Confirm re-checks every one of these before writing.
        target = await jira_sync._sync_target(ctx.db, ticket.id)
        if target is None:
            return ToolOutput(
                f"Cannot post to Jira: ticket {ticket.title} is not linked to a "
                "Jira issue. Propose without send_to_jira, or link the ticket first.",
                refs=refs,
            )
        link, _conn = target
        if not await user_can_edit_project(ctx.db, ctx.user.id, project):
            return ToolOutput(
                "Cannot post to Jira: posting to Jira requires editor access to "
                "the project. Propose without send_to_jira.",
                refs=refs,
            )
        if not await _feature_allowed(ctx, FEATURE_JIRA, project):
            return ToolOutput(
                "Cannot post to Jira: the organization's plan does not include "
                "Jira integration. Propose without send_to_jira.",
                refs=refs,
            )
        if not await jira_sync.comment_forwarding_enabled(ctx.db, ticket.id):
            return ToolOutput(
                "Cannot post to Jira: the organization's Jira comment sync is not "
                "set to outbound/both. Propose without send_to_jira, or ask an "
                "org admin to enable outbound comment sync.",
                refs=refs,
            )
        issue_key = link.issue_key
        params["send_to_jira"] = True
        params["issue_key"] = issue_key

    if send_to_jira:
        summary = redact(
            f"Comment on {ticket.title} (will also be posted to Jira issue {issue_key})",
            ctx.dlp_policy,
        )
    else:
        summary = redact(f"Comment on {ticket.title}", ctx.dlp_policy)
    return _propose_action(
        ctx,
        kind="comment_ticket",
        params=params,
        targets=[{"id": ticket.id, "name": ticket.title, "change": "will_change"}],
        summary=summary,
        refs=refs,
    )


async def _propose_jira_link(ctx: HelperContext, args: dict[str, Any]) -> ToolOutput:
    ticket_id = _require_str(args, "ticket_id")
    raw_key = _require_str(args, "issue_key")
    try:
        issue_key = normalize_link_issue_key(raw_key)
    except JiraLinkError as exc:
        return ToolOutput(f"Cannot link to Jira: {exc.message}", refs=_refs())

    ticket = (
        await ctx.db.execute(select(Ticket).where(Ticket.id == ticket_id))
    ).scalar_one_or_none()
    if ticket is None:
        return ToolOutput("Ticket not found.", refs=_refs())
    project = await _accessible_project(ctx, ticket.project_id)
    if project is None or not await _feature_allowed(ctx, FEATURE_TICKETS, project):
        return ToolOutput("Ticket not found.", refs=_refs())

    refs = _refs(project_ids=[ticket.project_id], ticket_ids=[ticket.id])

    # Refuse (no proposal row) when any precondition can't be honoured, so the
    # helper never proposes a link the confirm would refuse.  Confirm re-checks
    # every one of these before writing.
    if not await user_can_edit_project(ctx.db, ctx.user.id, project):
        return ToolOutput(
            "Cannot link to Jira: linking requires editor access to the project.",
            refs=refs,
        )
    if not await _feature_allowed(ctx, FEATURE_JIRA, project):
        return ToolOutput(
            "Cannot link to Jira: the organization's plan does not include "
            "Jira integration.",
            refs=refs,
        )
    if project.org_id is None:
        return ToolOutput(
            "Cannot link to Jira: this project is not org-scoped.",
            refs=refs,
        )
    conn = (
        await ctx.db.execute(
            select(OrgTrackerConnection).where(
                OrgTrackerConnection.org_id == project.org_id
            )
        )
    ).scalar_one_or_none()
    if conn is None or not conn.enabled:
        return ToolOutput(
            "Cannot link to Jira: this organization has not connected Jira.",
            refs=refs,
        )
    mapping = (
        await ctx.db.execute(
            select(TrackerProjectMapping).where(
                TrackerProjectMapping.project_id == project.id,
                TrackerProjectMapping.org_id == project.org_id,
            )
        )
    ).scalar_one_or_none()
    if mapping is None:
        return ToolOutput(
            "Cannot link to Jira: this project has no Jira mapping. Ask an "
            "admin to add a mapping.",
            refs=refs,
        )

    existing = (
        await ctx.db.execute(
            select(TicketTrackerLink.ticket_id).where(
                TicketTrackerLink.ticket_id == ticket.id
            )
        )
    ).first()
    if existing is not None:
        return ToolOutput(
            f"Cannot link to Jira: ticket {ticket.title} already has a Jira link.",
            refs=refs,
        )

    summary = redact(f"Link {ticket.title} to Jira {issue_key}", ctx.dlp_policy)
    return _propose_action(
        ctx,
        kind="link_jira",
        params={
            "ticket_id": ticket.id,
            # Pins the ticket's project: confirm refuses if the ticket has moved.
            "project_id": ticket.project_id,
            "issue_key": issue_key,
        },
        targets=[{"id": ticket.id, "name": ticket.title, "change": "will_change"}],
        summary=summary,
        refs=refs,
    )


async def _propose_jira_overwrite(ctx: HelperContext, args: dict[str, Any]) -> ToolOutput:
    ticket_id = _require_str(args, "ticket_id")
    direction = _require_str(args, "direction")
    if direction not in ("to_jira", "from_jira"):
        raise ToolArgumentError("'direction' must be 'to_jira' or 'from_jira'")

    ticket = (
        await ctx.db.execute(select(Ticket).where(Ticket.id == ticket_id))
    ).scalar_one_or_none()
    if ticket is None:
        return ToolOutput("Ticket not found.", refs=_refs())
    project = await _accessible_project(ctx, ticket.project_id)
    if project is None or not await _feature_allowed(ctx, FEATURE_TICKETS, project):
        return ToolOutput("Ticket not found.", refs=_refs())

    refs = _refs(project_ids=[ticket.project_id], ticket_ids=[ticket.id])

    # Same preconditions as propose_jira_link, except the ticket must already
    # carry a linked issue (whose key the proposal reuses) and no remote read
    # is made.  Confirm re-checks every one of these before writing.
    if not await user_can_edit_project(ctx.db, ctx.user.id, project):
        return ToolOutput(
            "Cannot overwrite Jira: overwriting requires editor access to the project.",
            refs=refs,
        )
    if direction == "from_jira" and ticket.created_by_user_id != ctx.user.id:
        from sessionfs.server.auth.project_access import user_is_project_admin

        if not await user_is_project_admin(ctx.db, ctx.user.id, project):
            return ToolOutput(
                "Cannot update the ticket from Jira: only the ticket's creator or "
                "a project admin may replace its title and description.",
                refs=refs,
            )
    if not await _feature_allowed(ctx, FEATURE_JIRA, project):
        return ToolOutput(
            "Cannot overwrite Jira: the organization's plan does not include "
            "Jira integration.",
            refs=refs,
        )
    if project.org_id is None:
        return ToolOutput(
            "Cannot overwrite Jira: this project is not org-scoped.",
            refs=refs,
        )
    conn = (
        await ctx.db.execute(
            select(OrgTrackerConnection).where(
                OrgTrackerConnection.org_id == project.org_id
            )
        )
    ).scalar_one_or_none()
    if conn is None or not conn.enabled:
        return ToolOutput(
            "Cannot overwrite Jira: this organization has not connected Jira.",
            refs=refs,
        )
    mapping = (
        await ctx.db.execute(
            select(TrackerProjectMapping).where(
                TrackerProjectMapping.project_id == project.id,
                TrackerProjectMapping.org_id == project.org_id,
            )
        )
    ).scalar_one_or_none()
    if mapping is None:
        return ToolOutput(
            "Cannot overwrite Jira: this project has no Jira mapping. Ask an "
            "admin to add a mapping.",
            refs=refs,
        )

    link = (
        await ctx.db.execute(
            select(TicketTrackerLink).where(TicketTrackerLink.ticket_id == ticket.id)
        )
    ).scalar_one_or_none()
    if link is None or link.status != "linked" or not link.issue_key:
        return ToolOutput(
            f"Cannot overwrite Jira: ticket {ticket.title} has no linked Jira issue.",
            refs=refs,
        )

    issue_key = link.issue_key
    if direction == "to_jira":
        summary = redact(f"Replace Jira {issue_key} with {ticket.title}", ctx.dlp_policy)
    else:
        summary = redact(f"Replace {ticket.title} with Jira {issue_key}", ctx.dlp_policy)
    return _propose_action(
        ctx,
        kind="jira_overwrite",
        params={
            "ticket_id": ticket.id,
            # Pins the ticket's project: confirm refuses if the ticket has moved.
            "project_id": ticket.project_id,
            "issue_key": issue_key,
            "direction": direction,
        },
        targets=[{"id": ticket.id, "name": ticket.title, "change": "will_change"}],
        summary=summary,
        refs=refs,
    )


async def _search_sessionfs_docs(
    ctx: HelperContext, args: dict[str, Any]
) -> ToolOutput:
    """Answer "how do I …?" from the official SessionFS docs (read-only)."""
    # Bound the work a query can cause: the first 500 characters and the
    # first 32 distinct words (docs.search caps its stemmed terms again).
    words = _require_str(args, "query")[:MAX_QUERY_CHARS].split()
    query = " ".join(list(dict.fromkeys(words))[:MAX_QUERY_TERMS])
    limit = _capped_int(
        args, "limit", MAX_DOC_SEARCH_RESULTS, 1, MAX_DOC_SEARCH_RESULTS
    )
    results = get_docs_index().search(query, limit)
    if not results:
        return ToolOutput("No SessionFS docs matched that query.", refs=_refs())

    lines: list[str] = []
    for r in results:
        marker = f"[doc:{r['slug']}"
        if r["anchor"]:
            marker += f"#{r['anchor']}"
        marker += "]"
        heading = (
            f"{r['page_title']} › {r['heading']}" if r["heading"] else r["page_title"]
        )
        lines.append(f"{marker} {heading}\n{r['url']}\n{r['excerpt']}")
    return ToolOutput(
        "\n\n".join(lines),
        refs=_refs(doc_slugs=[r["slug"] for r in results]),
    )


async def _get_sessionfs_doc(
    ctx: HelperContext, args: dict[str, Any]
) -> ToolOutput:
    """Return one docs page (or a single section when ``anchor`` is given)."""
    slug = _require_str(args, "slug")
    anchor = _optional_str(args, "anchor")
    doc = get_docs_index().get(slug, anchor)
    if doc is None:
        suggestions = get_docs_index().close_slugs(slug, 10)
        text = f"Doc '{slug}' not found."
        if suggestions:
            text += "\nDid you mean one of these?\n" + "\n".join(
                f"- {s}" for s in suggestions
            )
        return ToolOutput(text, refs=_refs())

    marker = f"[doc:{doc['slug']}"
    if doc["anchor"]:
        marker += f"#{doc['anchor']}"
    marker += "]"
    heading = (
        f"{doc['page_title']} › {doc['heading']}" if doc["heading"] else doc["page_title"]
    )
    return ToolOutput(
        f"{marker} {heading}\n{doc['url']}\n{doc['text']}",
        refs=_refs(doc_slugs=[doc["slug"]]),
    )


# ---------------------------------------------------------------------------
# Read tools: tickets, wiki pages, agent runs, personas (GitHub #104 part 1)
# ---------------------------------------------------------------------------


def _assignee_label(
    t: Ticket, users: dict[str, User], *, show_email: bool = False
) -> str:
    """Readable assignee: the direct person assignment when set, else persona.

    ``users`` is a batch-loaded id → User map so lists don't hit the user
    table once per ticket (GitHub #96). The email fallback is masked unless
    the caller has read standing on the ticket's project — a captured-session
    reader (possibly outside the org) must not learn teammates' addresses.
    The display name is user-controlled, so brackets/control characters are
    neutralised before it reaches the model.
    """
    if t.assigned_user_id:
        user = users.get(t.assigned_user_id)
        if user is not None:
            if user.display_name:
                return _clean_meta(user.display_name) or t.assigned_user_id
            from sessionfs.server.email import mask_address

            return _clean_meta(user.email if show_email else mask_address(user.email))
        return t.assigned_user_id
    return t.assigned_to or "unassigned"


def _user_reference_label(
    user: User | None,
    user_id: str | None,
    *,
    show_email: bool,
    persona: str | None = None,
) -> str:
    """Readable creator/resolver label for ``get_ticket`` (GitHub #113).

    Follows the assignee email rule: full address only when the caller has
    read standing on the project (``show_email``); a captured-session reader
    sees the masked address. Display names are user-controlled, so brackets
    and control characters are neutralised before they reach the model. A
    deleted user keeps their id; a missing id reads "a former user".
    """
    from sessionfs.server.email import mask_address

    if user is not None and user.display_name:
        label = _clean_meta(user.display_name) or _clean_meta(user_id or "")
    elif user is not None:
        label = _clean_meta(user.email if show_email else mask_address(user.email))
    else:
        label = _clean_meta(user_id) if user_id else "a former user"
    if user is not None and user.email and user.display_name:
        label += f" ({user.email if show_email else mask_address(user.email)})"
    if persona:
        label += f" (persona: {_clean_meta(persona)})"
    return label


def _event_value_text(value: object) -> str:
    """A short, neutralised rendering of a decoded before/after value."""
    if value is None:
        return "—"
    if isinstance(value, dict):
        if "changed" in value and "length" in value:
            return f"changed ({value['length']} chars)"
        if "persona" in value:
            return _clean_meta(str(value["persona"]))
        if "user_id" in value:
            return _clean_meta(str(value["user_id"]))
        if "issue_key" in value:
            return _clean_meta(str(value["issue_key"]))
        return ", ".join(_event_value_text(v) for v in value.values())
    return _clean_meta(str(value), 60)


def _event_actor_name(
    event: TicketEvent, users: dict[str, User], *, show_email: bool = False
) -> str:
    """The actor for one timeline line, neutralised like other metadata.

    A user actor without a display name falls back to their address, masked
    unless the caller has read standing (``show_email``) — same rule as the
    assignee/creator/resolver labels, so a captured-session reader never
    learns a teammate's address from the timeline.
    """
    if event.actor_type == "jira":
        return "Jira"
    if event.actor_type == "system":
        return "system"
    if event.actor_type == "service":
        return _clean_meta(event.actor_label) or "a service key"
    if event.actor_persona:
        return _clean_meta(event.actor_persona)
    uid = event.actor_user_id
    user = users.get(uid) if uid else None
    if user is not None and user.display_name:
        return _clean_meta(user.display_name)
    if user is not None:
        if user.email:
            from sessionfs.server.email import mask_address

            return _clean_meta(user.email if show_email else mask_address(user.email))
        return _clean_meta(uid or "")
    return _clean_meta(uid) if uid else "a user"


def _event_subject(event: TicketEvent, users: dict[str, User]) -> str:
    """Short description of what one timeline event did (actor excluded)."""
    after = decode_event_value(event.after_value)
    before = decode_event_value(event.before_value)
    if event.action == "created":
        return "created the ticket"
    if event.action == "status_changed":
        return f"changed status {_event_value_text(before)} → {_event_value_text(after)}"
    if event.action == "assigned":
        if isinstance(after, dict) and "persona" in after:
            return f"assigned to {_clean_meta(str(after['persona']))}"
        if isinstance(after, dict) and "user_id" in after:
            return "assigned to " + _user_reference_label(
                users.get(str(after["user_id"])), str(after["user_id"]), show_email=False
            )
        return "assigned"
    if event.action == "unassigned":
        return "unassigned"
    if event.action == "due_date_set":
        return f"set due date to {_event_value_text(after)}" if after else "cleared due date"
    if event.action == "estimate_set":
        return f"set estimate to {_event_value_text(after)}" if after else "cleared estimate"
    if event.action == "field_edited":
        return f"edited {_clean_meta(event.field) or 'a field'}"
    if event.action == "jira_linked":
        return "linked to Jira"
    if event.action == "jira_published":
        return "published to Jira"
    if event.action == "jira_unlinked":
        return "unlinked from Jira"
    if event.action == "jira_pulled":
        return "pulled from Jira"
    if event.action == "approved":
        return "approved"
    if event.action == "dismissed":
        return "dismissed"
    return _clean_meta(event.action)


def _format_ticket_line(t: Ticket, project_name: str, assignee: str | None = None) -> str:
    if assignee is None:
        assignee = t.assigned_to or "unassigned"
    return (
        f"[ticket:{t.id}] {t.title} "
        f"(kind: {t.kind}, status: {t.status}, priority: {t.priority or '—'}, "
        f"assignee: {assignee}, project: {project_name}) "
        f"{_fmt_dt_with_age(t.updated_at)}"
    )


async def _my_persona_names(
    ctx: HelperContext, projects: list[Project]
) -> dict[str, set[str]]:
    """Per project id: lower-cased names of the active org personas that
    represent the caller there.

    ``assigned_to_me`` means "assigned to a persona whose represented_user_id
    is the caller, or created by the caller".  Only org personas carry a
    ``represented_user_id`` (project personas have it NULL by constraint), an
    org persona is only visible from a project whose org the caller is a
    member of, and an ACTIVE project persona with the same name
    (case-insensitive) overrides it — exactly how the personas route resolves
    the effective set (``list_effective_personas``).
    """
    names: dict[str, set[str]] = {}
    for p in projects:
        if not p.org_id:
            continue
        visible_org = await visible_persona_org_id(ctx.db, p, user_id=ctx.user.id)
        if visible_org is None:
            continue
        effective = await list_effective_personas(ctx.db, p, org_id=visible_org)
        mine = {
            e.persona.name.lower()
            for e in effective
            if e.persona.org_id is not None
            and e.persona.represented_user_id == ctx.user.id
        }
        if mine:
            names[p.id] = mine
    return names


async def _readable_ticket_ids(
    ctx: HelperContext, ticket_ids: set[str]
) -> set[str]:
    """The subset of ``ticket_ids`` that resolve to tickets in accessible,
    in-scope projects whose plan includes tickets — the only ids a tool may
    surface as ``[ticket:…]`` (and so the only ones the model may cite).

    Each distinct project is checked with the same ``_accessible_project`` +
    plan gate as ``get_ticket``."""
    if not ticket_ids:
        return set()
    rows = (
        await ctx.db.execute(
            select(Ticket.id, Ticket.project_id).where(Ticket.id.in_(sorted(ticket_ids)))
        )
    ).all()
    allowed: dict[str, bool] = {}
    for _tid, pid in rows:
        if pid in allowed:
            continue
        project = await _accessible_project(ctx, pid)
        allowed[pid] = project is not None and await _feature_allowed(
            ctx, FEATURE_TICKETS, project
        )
    return {tid for tid, pid in rows if allowed.get(pid)}


async def _list_tickets(ctx: HelperContext, args: dict[str, Any]) -> ToolOutput:
    project_id = _optional_str(args, "project_id")
    status = _optional_str(args, "status")
    kind = _optional_str(args, "kind")
    assignee = _optional_str(args, "assignee")
    assigned_to_me = args.get("assigned_to_me") is True
    overdue = args.get("overdue") is True
    query = _optional_str(args, "query")
    limit = _capped_int(args, "limit", 50, 1, MAX_LIST_TICKETS)

    if kind is not None and kind not in _VALID_TICKET_KINDS:
        raise ToolArgumentError(f"kind must be one of: {sorted(_VALID_TICKET_KINDS)}")

    projects = await _accessible_projects(ctx)
    if project_id is not None:
        projects = [p for p in projects if p.id == project_id]
        if projects and not await _feature_allowed(ctx, FEATURE_TICKETS, projects[0]):
            return ToolOutput(
                "Tickets are not available on this project's plan.", refs=_refs()
            )
    else:
        projects = [p for p in projects if await _feature_allowed(ctx, FEATURE_TICKETS, p)]
    if not projects:
        return ToolOutput("No tickets accessible.", refs=_refs())
    project_ids = [p.id for p in projects]
    project_names = {p.id: p.name for p in projects}

    stmt = select(Ticket).where(Ticket.project_id.in_(project_ids))
    if status is not None:
        stmt = stmt.where(Ticket.status == status)
    if kind is not None:
        stmt = stmt.where(Ticket.kind == kind)
    if assignee is not None:
        # Case-insensitive to match the list_tickets route (tk_884b2321fdb74170).
        stmt = stmt.where(func.lower(Ticket.assigned_to) == assignee.lower())
    if assigned_to_me:
        conditions: list[ColumnElement[bool]] = [
            Ticket.created_by_user_id == ctx.user.id,
            Ticket.assigned_user_id == ctx.user.id,
        ]
        for pid, names in (await _my_persona_names(ctx, projects)).items():
            conditions.append(
                and_(
                    Ticket.project_id == pid,
                    func.lower(Ticket.assigned_to).in_(sorted(names)),
                )
            )
        stmt = stmt.where(or_(*conditions))
    if query is not None:
        stmt = stmt.where(
            or_(
                Ticket.title.ilike(f"%{_escape_like(query)}%", escape="\\"),
                Ticket.description.ilike(f"%{_escape_like(query)}%", escape="\\"),
            )
        )
    if overdue:
        # GitHub #113 — past-due, non-terminal tickets only.
        stmt = stmt.where(
            Ticket.due_date.is_not(None),
            Ticket.due_date < date.today(),
            Ticket.status.notin_(("done", "closed", "cancelled")),
        )
    stmt = stmt.order_by(Ticket.updated_at.desc()).limit(limit + 1)
    tickets = (await ctx.db.execute(stmt)).scalars().all()

    truncated = len(tickets) > limit
    shown = tickets[:limit]
    if not shown:
        return ToolOutput("No tickets matched.", refs=_refs())

    assigned_user_ids = {t.assigned_user_id for t in shown if t.assigned_user_id}
    assigned_users = (
        {
            u.id: u
            for u in (
                await ctx.db.execute(
                    select(User).where(User.id.in_(sorted(assigned_user_ids)))
                )
            ).scalars().all()
        }
        if assigned_user_ids
        else {}
    )
    # Full teammate emails only on projects where the caller has read
    # standing (not merely a captured session).
    standing_by_project: dict[str, bool] = {}
    project_by_id = {p.id: p for p in projects}
    for pid in {t.project_id for t in shown if t.assigned_user_id}:
        standing_by_project[pid] = await user_has_read_standing(
            ctx.db, ctx.user.id, project_by_id[pid]
        )
    text = "\n".join(
        _format_ticket_line(
            t,
            project_names.get(t.project_id, t.project_id),
            _assignee_label(
                t, assigned_users, show_email=standing_by_project.get(t.project_id, False)
            ),
        )
        for t in shown
    )
    if truncated:
        text += f"\n[showing {limit} of more results; narrow the filter or pass project_id]"
    # Only the projects that actually returned rows (the audit fans out one
    # event per project in refs).
    return ToolOutput(
        text,
        refs=_refs(
            project_ids=[t.project_id for t in shown],
            ticket_ids=[t.id for t in shown],
        ),
    )


def _jira_comment_author(comment: TicketComment) -> str:
    """The Jira author name for a mirrored comment, without the ``(Jira)`` tag.

    Inbound Jira comments store ``author_persona`` as ``"<name> (Jira)"`` (the
    label used across the ticket surface).  The helper already prefixes the
    line with "Jira comment by", so the tag is stripped here to avoid
    "Jira comment by Alice (Jira)".  Control characters and brackets in the
    stored name are neutralised first so a mirrored display name can't forge a
    citation marker or break the one-line rendering.
    """
    name = _clean_meta(comment.author_persona or "")
    if name.endswith(jira_sync.JIRA_SUFFIX):
        name = name[: -len(jira_sync.JIRA_SUFFIX)].strip()
    return name or "a Jira user"


async def _get_ticket(ctx: HelperContext, args: dict[str, Any]) -> ToolOutput:
    ticket_id = _require_str(args, "ticket_id")
    ticket = (
        await ctx.db.execute(select(Ticket).where(Ticket.id == ticket_id))
    ).scalar_one_or_none()
    if ticket is None:
        return ToolOutput("Ticket not found.", refs=_refs())
    project = await _accessible_project(ctx, ticket.project_id)
    if project is None or not await _feature_allowed(ctx, FEATURE_TICKETS, project):
        return ToolOutput("Ticket not found.", refs=_refs())

    dep_ids = list(
        (
            await ctx.db.execute(
                select(TicketDependency.depends_on_id).where(
                    TicketDependency.ticket_id == ticket.id
                )
            )
        ).scalars().all()
    )
    # Parent / dependency ids are only shown (and become citable) when they
    # resolve to tickets the caller can read in this conversation's scope.
    linked = {t for t in (ticket.parent_ticket_id, *dep_ids) if t}
    readable = await _readable_ticket_ids(ctx, linked)
    parent_id = ticket.parent_ticket_id if ticket.parent_ticket_id in readable else None
    deps = sorted(d for d in dep_ids if d in readable)

    comments = list(
        (
            await ctx.db.execute(
                select(TicketComment)
                .where(TicketComment.ticket_id == ticket.id)
                .order_by(TicketComment.created_at.desc(), TicketComment.id.desc())
                .limit(MAX_TICKET_COMMENTS)
            )
        ).scalars().all()
    )

    lines = [f"[ticket:{ticket.id}] {ticket.title}"]
    lines.append(f"project: {project.name} [project:{ticket.project_id}]")
    lines.append(
        f"kind: {ticket.kind} | status: {ticket.status} | "
        f"priority: {ticket.priority or '—'}"
    )
    # Batch-load the assignee, creator and resolver users (one query for the
    # provenance trio) so the new creator/resolver lines don't N+1.
    provenance_ids = {
        uid for uid in (
            ticket.assigned_user_id, ticket.created_by_user_id, ticket.resolver_user_id
        ) if uid
    }
    provenance_users = (
        {
            u.id: u
            for u in (
                await ctx.db.execute(
                    select(User).where(User.id.in_(sorted(provenance_ids)))
                )
            ).scalars().all()
        }
        if provenance_ids
        else {}
    )
    assigned_user = provenance_users.get(ticket.assigned_user_id)
    users = {ticket.assigned_user_id: assigned_user} if assigned_user is not None else {}
    # GitHub #113 — read standing decides email visibility for creator/resolver
    # (and timeline actors) whether or not the ticket currently has an assignee.
    show_email = await user_has_read_standing(ctx.db, ctx.user.id, project)
    lines.append(f"assignee: {_assignee_label(ticket, users, show_email=show_email)}")
    # GitHub #113 — creator/resolver provenance (masked emails without read
    # standing), scheduling fields, then the most recent events.
    lines.append(
        "creator: "
        + _user_reference_label(
            provenance_users.get(ticket.created_by_user_id),
            ticket.created_by_user_id,
            show_email=show_email,
            persona=ticket.created_by_persona,
        )
    )
    if ticket.resolver_user_id:
        lines.append(
            "resolver: "
            + _user_reference_label(
                provenance_users.get(ticket.resolver_user_id),
                ticket.resolver_user_id,
                show_email=show_email,
            )
        )
    lines.append(f"due: {ticket.due_date.isoformat() if ticket.due_date else '—'}")
    lines.append(f"estimate: {ticket.estimated_effort or '—'}")
    # A live Jira link is shown up top so the model knows the issue key/url
    # (and can cite them); a stale/removed link is simply absent.
    jira_target = await jira_sync._sync_target(ctx.db, ticket.id)
    if jira_target is not None:
        jira_link, _jira_conn = jira_target
        lines.append(
            f"jira: {jira_link.issue_key} ({jira_link.issue_url}) — linked"
        )
    lines.append(f"created: {_fmt_dt_with_age(ticket.created_at)}")
    lines.append(f"updated: {_fmt_dt_with_age(ticket.updated_at)}")
    if ticket.resolved_at:
        lines.append(f"resolved: {_fmt_dt_with_age(ticket.resolved_at)}")
    lines.append(f"parent: [ticket:{parent_id}]" if parent_id else "parent: none")
    shown_deps = deps[:MAX_TICKET_DEPENDENCIES]
    deps_text = ", ".join(f"[ticket:{d}]" for d in shown_deps) if shown_deps else "none"
    if len(deps) > len(shown_deps):
        deps_text += f" +{len(deps) - len(shown_deps)} more"
    lines.append("depends on: " + deps_text)

    # GitHub #113 — the last 10 timeline events as short lines ("assigned to
    # Alice by Bob"), newest first. Actor users are batch-loaded so the list
    # never hits the user table once per event.
    events = list(
        (
            await ctx.db.execute(
                select(TicketEvent)
                .where(TicketEvent.ticket_id == ticket.id)
                .order_by(TicketEvent.seq.desc())
                .limit(10)
            )
        ).scalars().all()
    )
    if events:
        actor_ids = {e.actor_user_id for e in events if e.actor_user_id}
        actor_users = (
            {
                u.id: u
                for u in (
                    await ctx.db.execute(
                        select(User).where(User.id.in_(sorted(actor_ids)))
                    )
                ).scalars().all()
            }
            if actor_ids
            else {}
        )
        lines.append("events (last %d):" % len(events))
        for e in events:
            lines.append(
                f"- {_fmt_dt(e.created_at)} {_event_subject(e, actor_users)} "
                f"by {_event_actor_name(e, actor_users, show_email=show_email)}"
            )
    else:
        lines.append("events: none")

    criteria = _loads_json_list(ticket.acceptance_criteria)
    if criteria:
        lines.append("acceptance criteria:")
        lines.extend(
            f"- {_cap(str(c), MAX_TICKET_CRITERION_CHARS)}"
            for c in criteria[:MAX_TICKET_CRITERIA]
        )
        if len(criteria) > MAX_TICKET_CRITERIA:
            lines.append(f"[showing {MAX_TICKET_CRITERIA} of {len(criteria)} criteria]")

    description = (ticket.description or "").strip()
    lines.append("description:")
    lines.append(_cap(description, MAX_TICKET_DESCRIPTION_CHARS) if description else "(none)")

    if comments:
        lines.append(f"comments (last {len(comments)}):")
        for c in reversed(comments):
            body = _cap(c.content or "", MAX_TICKET_COMMENT_CHARS)
            if c.source == "jira":
                lines.append(
                    f"- [{_fmt_dt_with_age(c.created_at)}] Jira comment by "
                    f"{_jira_comment_author(c)}: {body}"
                )
            else:
                author = c.author_persona or "user"
                lines.append(f"- [{_fmt_dt_with_age(c.created_at)}] {author}: {body}")
    else:
        lines.append("comments: none")

    return ToolOutput(
        "\n".join(lines),
        refs=_refs(
            project_ids=[ticket.project_id],
            # Only the ids this result actually shows become citable.
            ticket_ids=sorted(
                {ticket.id, *shown_deps, *([parent_id] if parent_id else [])}
            ),
        ),
    )


async def _get_jira_issue(ctx: HelperContext, args: dict[str, Any]) -> ToolOutput:
    """Read the Jira issue linked to a sessionfs ticket (read-only).

    Takes a ticket id, never a free-form Jira key: the key is resolved from the
    ticket's live link, so this can only ever read an issue the ticket already
    points at.  Re-checks project access (inside the conversation's scope), the
    org-membership rule the Jira routes enforce (acting through the org's
    credential), the ``jira_integration`` plan gate, and the live link.
    """
    ticket_id = _require_str(args, "ticket_id")
    ticket = (
        await ctx.db.execute(select(Ticket).where(Ticket.id == ticket_id))
    ).scalar_one_or_none()
    if ticket is None:
        return ToolOutput("Ticket not found.", refs=_refs())
    project = await _accessible_project(ctx, ticket.project_id)
    if project is None or not await _feature_allowed(ctx, FEATURE_TICKETS, project):
        return ToolOutput("Ticket not found.", refs=_refs())
    if not await _feature_allowed(ctx, FEATURE_JIRA, project):
        return ToolOutput(
            "Jira integration is not available for this project's plan.",
            refs=_refs(project_ids=[ticket.project_id], ticket_ids=[ticket.id]),
        )

    target = await jira_sync._sync_target(ctx.db, ticket.id)
    if target is None:
        return ToolOutput(
            f"Ticket {ticket.id} has no linked Jira issue.",
            refs=_refs(project_ids=[ticket.project_id], ticket_ids=[ticket.id]),
        )
    link, conn = target

    # Reading through the org's Jira credential needs org membership, not just
    # project read access (same rule as the Jira routes for a non-member who
    # can still read the project through a captured session).
    member = (
        await ctx.db.execute(
            select(OrgMember.id).where(
                OrgMember.org_id == link.org_id, OrgMember.user_id == ctx.user.id
            )
        )
    ).first()
    if member is None:
        return ToolOutput(
            "Only members of this project's organization can read its Jira issues.",
            refs=_refs(project_ids=[ticket.project_id], ticket_ids=[ticket.id]),
        )

    client = jira_sync._client(conn)
    try:
        detail = await asyncio.wait_for(
            client.get_issue_detail(link.issue_key),
            timeout=JIRA_ISSUE_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        return ToolOutput(
            "Timed out reading the Jira issue.",
            refs=_refs(project_ids=[ticket.project_id], ticket_ids=[ticket.id]),
        )
    except TrackerError as exc:
        return ToolOutput(
            f"Could not read the Jira issue: {exc}",
            refs=_refs(project_ids=[ticket.project_id], ticket_ids=[ticket.id]),
        )
    finally:
        await client.aclose()

    # The summary and comment bodies are remote Jira content, so they are framed
    # as data fields (not folded into the header) and attributed like the
    # mirrored comments in get_ticket ("Jira comment by <author>").
    lines = [f"Jira issue {detail.key}"]
    lines.append(f"url: {detail.url}")
    lines.append(f"summary: {detail.summary or '(no summary)'}")
    if detail.issue_type:
        lines.append(f"type: {detail.issue_type}")
    lines.append(f"status: {detail.status or '—'}")
    lines.append(f"assignee: {_clean_meta(detail.assignee) or 'unassigned'}")
    lines.append(f"priority: {detail.priority or '—'}")
    lines.append(f"labels: {', '.join(detail.labels) if detail.labels else 'none'}")
    lines.append(
        f"components: {', '.join(detail.components) if detail.components else 'none'}"
    )
    lines.append(f"updated: {_fmt_dt_with_age(detail.updated_at) or '—'}")
    comments = detail.comments or []
    if comments:
        lines.append(f"comments (last {len(comments)}):")
        for c in comments:
            author = _clean_meta(c.author_name) or "a Jira user"
            body = _cap(c.body_text, MAX_TICKET_COMMENT_CHARS)
            lines.append(f"- [{_fmt_dt_with_age(c.created)}] Jira comment by {author}: {body}")
    else:
        lines.append("comments: none")

    return ToolOutput(
        "\n".join(lines),
        refs=_refs(
            project_ids=[ticket.project_id],
            ticket_ids=[ticket.id],
            jira_issue_keys=[detail.key],
        ),
    )


async def _list_wiki_pages(ctx: HelperContext, args: dict[str, Any]) -> ToolOutput:
    project_id = _require_str(args, "project_id")
    project = await _accessible_project(ctx, project_id)
    if project is None:
        return ToolOutput("Project not found.", refs=_refs())

    stmt = (
        select(KnowledgePage)
        .where(KnowledgePage.project_id == project_id)
        .order_by(KnowledgePage.title)
        .limit(MAX_LIST_WIKI_PAGES + 1)
    )
    pages = (await ctx.db.execute(stmt)).scalars().all()
    if not pages:
        return ToolOutput("No wiki pages.", refs=_refs())

    truncated = len(pages) > MAX_LIST_WIKI_PAGES
    shown = pages[:MAX_LIST_WIKI_PAGES]
    text = "\n".join(
        f"[wiki:{project_id}/{p.slug}] {p.title} {_fmt_dt_with_age(p.updated_at)}"
        for p in shown
    )
    if truncated:
        text += f"\n[showing {MAX_LIST_WIKI_PAGES} of more pages]"
    return ToolOutput(
        text,
        refs=_refs(
            project_ids=[project_id],
            wiki_refs=[(project_id, p.slug) for p in shown],
        ),
    )


async def _wiki_editor_name(
    ctx: HelperContext, rev: WikiPageRevision | None
) -> str | None:
    """The latest revision's author: persona name, else the user's display
    name, else "a project member".  Never an email address."""
    if rev is None:
        return None
    if rev.persona_name:
        return rev.persona_name
    if rev.user_id:
        user = (
            await ctx.db.execute(select(User).where(User.id == rev.user_id))
        ).scalar_one_or_none()
        if user is not None and user.display_name:
            return user.display_name
    return "a project member"


async def _get_wiki_page(ctx: HelperContext, args: dict[str, Any]) -> ToolOutput:
    project_id = _require_str(args, "project_id")
    slug = _require_str(args, "slug")
    project = await _accessible_project(ctx, project_id)
    if project is None:
        return ToolOutput("Project not found.", refs=_refs())

    page = (
        await ctx.db.execute(
            select(KnowledgePage).where(
                KnowledgePage.project_id == project_id,
                KnowledgePage.slug == slug,
            )
        )
    ).scalar_one_or_none()
    if page is None:
        return ToolOutput("Wiki page not found.", refs=_refs())

    latest_rev = (
        await ctx.db.execute(
            select(WikiPageRevision)
            .where(
                WikiPageRevision.project_id == project_id,
                WikiPageRevision.page_slug == slug,
            )
            .order_by(WikiPageRevision.revised_at.desc(), WikiPageRevision.id.desc())
            .limit(1)
        )
    ).scalars().first()
    editor = await _wiki_editor_name(ctx, latest_rev)

    body = page.content or ""
    truncated = len(body) > MAX_WIKI_BODY_CHARS
    if truncated:
        body = body[:MAX_WIKI_BODY_CHARS] + "\n…truncated"

    lines = [f"[wiki:{project_id}/{slug}] {page.title}"]
    lines.append(f"updated: {_fmt_dt_with_age(page.updated_at)}")
    if editor:
        lines.append(f"last editor: {editor}")
    lines.append(body)
    return ToolOutput(
        "\n".join(lines),
        refs=_refs(project_ids=[project_id], wiki_refs=[(project_id, slug)]),
    )


def _format_finding(f: dict[str, Any]) -> str:
    severity = f.get("severity") or ""
    title = f.get("rule") or f.get("title") or ""
    location = f.get("file") or ""
    if f.get("line") is not None:
        location = f"{location}:{f['line']}" if location else str(f["line"])
    message = _cap(str(f.get("message") or f.get("description") or ""), MAX_FINDING_MESSAGE_CHARS)
    parts = [str(p) for p in (severity, title, location, message) if p]
    if parts:
        return " | ".join(parts)
    return _cap(json.dumps(f, sort_keys=True, default=str), MAX_FINDING_MESSAGE_CHARS)


def _run_ticket_text(run: AgentRun, readable: set[str]) -> str | None:
    """The run's ticket as a citable marker only when it resolves to a ticket
    the caller can read here; otherwise the raw id, flagged."""
    if not run.ticket_id:
        return None
    if run.ticket_id in readable:
        return f"ticket: [ticket:{run.ticket_id}]"
    return f"ticket: {run.ticket_id} (not accessible)"


def _format_agent_run(run: AgentRun, readable: set[str]) -> str:
    parts = [f"{run.id} | persona: {run.persona_name} | status: {run.status}"]
    ticket = _run_ticket_text(run, readable)
    if ticket:
        parts.append(ticket)
    if run.severity:
        parts.append(f"severity: {run.severity}")
    parts.append(f"findings: {run.findings_count}")
    if run.policy_result:
        parts.append(f"policy: {run.policy_result}")
    parts.append(f"created: {_fmt_dt_with_age(run.created_at)}")
    if run.completed_at:
        parts.append(f"completed: {_fmt_dt_with_age(run.completed_at)}")
    line = " ".join(parts)
    if run.result_summary:
        line += f" — {_cap(run.result_summary, MAX_RUN_SUMMARY_LINE_CHARS)}"
    return line


async def _agent_run_project(
    ctx: HelperContext, project_id: str
) -> tuple[Project | None, ToolOutput | None]:
    """The run's project, or the result to return instead (not found /
    not on this plan)."""
    project = await _accessible_project(ctx, project_id)
    if project is None:
        return None, ToolOutput("Project not found.", refs=_refs())
    if not await _feature_allowed(ctx, FEATURE_AGENT_RUNS, project):
        return None, ToolOutput(
            "Agent runs are not available on this project's plan.", refs=_refs()
        )
    return project, None


async def _list_agent_runs(ctx: HelperContext, args: dict[str, Any]) -> ToolOutput:
    project_id = _require_str(args, "project_id")
    status = _optional_str(args, "status")
    persona = _optional_str(args, "persona")
    limit = _capped_int(args, "limit", 50, 1, MAX_LIST_AGENT_RUNS)

    project, early = await _agent_run_project(ctx, project_id)
    if early is not None:
        return early

    stmt = select(AgentRun).where(AgentRun.project_id == project_id)
    if status is not None:
        stmt = stmt.where(AgentRun.status == status)
    if persona is not None:
        stmt = stmt.where(func.lower(AgentRun.persona_name) == persona.lower())
    stmt = stmt.order_by(AgentRun.created_at.desc()).limit(limit + 1)
    runs = (await ctx.db.execute(stmt)).scalars().all()

    truncated = len(runs) > limit
    shown = runs[:limit]
    if not shown:
        return ToolOutput("No agent runs matched.", refs=_refs())

    readable = await _readable_ticket_ids(ctx, {r.ticket_id for r in shown if r.ticket_id})
    text = "\n".join(_format_agent_run(r, readable) for r in shown)
    if truncated:
        text += f"\n[showing {limit} of more runs]"
    return ToolOutput(
        text,
        refs=_refs(project_ids=[project_id], ticket_ids=sorted(readable)),
    )


async def _get_agent_run(ctx: HelperContext, args: dict[str, Any]) -> ToolOutput:
    project_id = _require_str(args, "project_id")
    run_id = _require_str(args, "run_id")
    project, early = await _agent_run_project(ctx, project_id)
    if early is not None:
        return early

    run = (
        await ctx.db.execute(
            select(AgentRun).where(
                AgentRun.id == run_id,
                AgentRun.project_id == project_id,
            )
        )
    ).scalar_one_or_none()
    if run is None:
        return ToolOutput("Agent run not found.", refs=_refs())

    readable = await _readable_ticket_ids(ctx, {run.ticket_id} if run.ticket_id else set())
    lines = [f"{run.id} | persona: {run.persona_name} | tool: {run.tool}"]
    lines.append(f"status: {run.status} | trigger: {run.trigger_source}")
    ticket = _run_ticket_text(run, readable)
    if ticket:
        lines.append(ticket)
    lines.append(f"created: {_fmt_dt_with_age(run.created_at)}")
    if run.started_at:
        lines.append(f"started: {_fmt_dt_with_age(run.started_at)}")
    if run.completed_at:
        lines.append(f"completed: {_fmt_dt_with_age(run.completed_at)}")
    if run.duration_seconds is not None:
        lines.append(f"duration: {run.duration_seconds}s")
    if run.severity:
        lines.append(f"severity: {run.severity}")
    lines.append(f"findings_count: {run.findings_count}")
    if run.fail_on:
        lines.append(f"fail_on: {run.fail_on}")
    if run.policy_result:
        lines.append(f"policy_result: {run.policy_result}")
    if run.result_summary:
        lines.append(f"result_summary: {_cap(run.result_summary, MAX_RUN_SUMMARY_CHARS)}")
    findings = [f for f in _loads_json_list(run.findings) if isinstance(f, dict)]
    if findings:
        lines.append("findings:")
        lines.extend(f"- {_format_finding(f)}" for f in findings[:MAX_AGENT_RUN_FINDINGS])
        if len(findings) > MAX_AGENT_RUN_FINDINGS:
            lines.append(f"[showing {MAX_AGENT_RUN_FINDINGS} of {len(findings)} findings]")

    return ToolOutput(
        "\n".join(lines),
        refs=_refs(project_ids=[project_id], ticket_ids=sorted(readable)),
    )


def _format_persona(p: AgentPersona, represented_name: str | None) -> str:
    scope = "org" if p.org_id is not None else "project"
    parts = [
        p.name,
        f"role: {p.role}",
        f"scope: {scope}",
        f"active: {'yes' if p.is_active else 'no'}",
        f"updated: {_fmt_dt_with_age(p.updated_at)}",
    ]
    if represented_name:
        parts.append(f"represents: {represented_name}")
    line = " | ".join(parts)
    specs = _loads_json_list(p.specializations)
    if specs:
        line += "\n  specializations: " + ", ".join(str(s) for s in specs[:20])
    return line


async def _list_personas(ctx: HelperContext, args: dict[str, Any]) -> ToolOutput:
    project_id = _optional_str(args, "project_id")

    projects = await _accessible_projects(ctx)
    if project_id is not None:
        projects = [p for p in projects if p.id == project_id]
        if projects and not await _feature_allowed(ctx, FEATURE_PERSONAS, projects[0]):
            return ToolOutput(
                "Personas are not available on this project's plan.", refs=_refs()
            )
    else:
        projects = [p for p in projects if await _feature_allowed(ctx, FEATURE_PERSONAS, p)]
    if not projects:
        return ToolOutput("No personas accessible.", refs=_refs())

    # Union project + visible-org personas across the searched projects,
    # de-duplicated by id (an org persona may appear from several projects).
    # Remember which projects each persona came from so the audit names only
    # the projects that actually returned rows.
    seen: dict[str, AgentPersona] = {}
    sources: dict[str, set[str]] = {}
    for p in projects:
        visible_org = await visible_persona_org_id(ctx.db, p, user_id=ctx.user.id)
        effective = await list_effective_personas(
            ctx.db, p, org_id=visible_org, include_inactive=True
        )
        for e in effective:
            seen.setdefault(e.persona.id, e.persona)
            sources.setdefault(e.persona.id, set()).add(p.id)

    personas = list(seen.values())
    personas.sort(key=lambda p: (p.name, p.org_id is not None))

    represented_ids = {p.represented_user_id for p in personas if p.represented_user_id}
    represented_names: dict[str, str] = {}
    if represented_ids:
        users = (
            await ctx.db.execute(select(User).where(User.id.in_(represented_ids)))
        ).scalars().all()
        # Display name only — never email.
        represented_names = {u.id: (u.display_name or "a project member") for u in users}

    truncated = len(personas) > MAX_LIST_PERSONAS
    shown = personas[:MAX_LIST_PERSONAS]
    if not shown:
        return ToolOutput("No personas found.", refs=_refs())

    text = "\n".join(
        _format_persona(p, represented_names.get(p.represented_user_id or ""))
        for p in shown
    )
    if truncated:
        text += f"\n[showing {MAX_LIST_PERSONAS} of {len(personas)} personas]"
    return ToolOutput(
        text,
        refs=_refs(project_ids=sorted({pid for p in shown for pid in sources[p.id]})),
    )


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


# The metadata filters shared by the session-finding tools and the trash
# proposal.  Model/provider match case-insensitively by substring
# ("deepseek" matches deepseek-v4-pro and deepseek-flash); several values OR.
# An array (not anyOf) so every provider's tool-schema dialect accepts it; the
# handler also accepts a bare string.
_FILTER_VALUES_SCHEMA: dict[str, Any] = {
    "type": "array",
    "items": {"type": "string", "maxLength": MAX_MODEL_FILTER_CHARS},
    "maxItems": MAX_MODEL_FILTER_VALUES,
}
_SESSION_FILTER_PROPERTIES: dict[str, Any] = {
    "model": {
        **_FILTER_VALUES_SCHEMA,
        "description": (
            "Model ids or families, case-insensitive substring; any of them "
            "matches. \"unknown\" selects sessions with no recorded model."
        ),
    },
    "provider": {
        **_FILTER_VALUES_SCHEMA,
        "description": "Model providers, case-insensitive substring; \"unknown\" = none recorded.",
    },
    "source_tool": {"type": "string", "description": "Capturing tool, e.g. claude-code."},
    "days": {
        "type": "integer",
        "minimum": 1,
        "maximum": MAX_FILTER_DAYS,
        "description": "Only sessions active in the last N days (UTC).",
    },
    "since": {
        "type": "string",
        "description": "Active on or after this date (YYYY-MM-DD, UTC; start of day).",
    },
    "until": {
        "type": "string",
        "description": "Active on or before this date (YYYY-MM-DD, UTC; the whole day counts).",
    },
}


TOOLS: dict[str, HelperTool] = {
    t.name: t
    for t in [
        HelperTool(
            name="search_sessions",
            description=(
                "Search the caller's own sessions. With 'query': full-text "
                "search (limit at most 20 per page). Without 'query' but with a "
                "metadata filter (model, provider, source_tool, days, since, "
                "until): every matching session, newest first, up to 100 per "
                "page. Results end with 'Showing N–M of T'; page with 'offset'."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "limit": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": MAX_RECENT_RESULTS,
                        "description": (
                            f"At most {MAX_SEARCH_RESULTS} with a query; up to "
                            f"{MAX_RECENT_RESULTS} for a filter-only listing."
                        ),
                    },
                    "offset": {"type": "integer", "minimum": 0},
                    "project_id": {"type": "string"},
                    **_SESSION_FILTER_PROPERTIES,
                },
                "additionalProperties": False,
            },
            handler=_search_sessions,
        ),
        HelperTool(
            name="list_recent_sessions",
            description=(
                "The caller's sessions, most recently active first, optionally "
                "filtered by model, provider, tool and dates. Up to 100 per "
                "page; results end with 'Showing N–M of T'; page with 'offset'."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "minimum": 1, "maximum": MAX_RECENT_RESULTS},
                    "offset": {"type": "integer", "minimum": 0},
                    "project_id": {"type": "string"},
                    **_SESSION_FILTER_PROPERTIES,
                },
                "additionalProperties": False,
            },
            handler=_list_recent_sessions,
        ),
        HelperTool(
            name="list_session_models",
            description=(
                "Which AI models (LLMs) the caller's sessions used, with session "
                "counts, the capturing tools and first/last dates. Call this "
                "first for any question about models or providers, then filter "
                "the session tools by model or provider."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "project_id": {"type": "string"},
                    "source_tool": _SESSION_FILTER_PROPERTIES["source_tool"],
                    "days": _SESSION_FILTER_PROPERTIES["days"],
                    "since": _SESSION_FILTER_PROPERTIES["since"],
                    "until": _SESSION_FILTER_PROPERTIES["until"],
                },
                "additionalProperties": False,
            },
            handler=_list_session_models,
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
                    **_SESSION_FILTER_PROPERTIES,
                },
                "required": ["path"],
                "additionalProperties": False,
            },
            handler=_sessions_touching_path,
        ),
        HelperTool(
            name="list_projects",
            description="Projects the caller can access, with scope, plan tier and auto_narrative.",
            input_schema={"type": "object", "properties": {}, "additionalProperties": False},
            handler=_list_projects,
        ),
        HelperTool(
            name="get_project_settings",
            description="A project's auto_narrative and knowledge-base settings.",
            input_schema={
                "type": "object",
                "properties": {"project_id": {"type": "string"}},
                "required": ["project_id"],
                "additionalProperties": False,
            },
            handler=_get_project_settings,
        ),
        HelperTool(
            name="set_project_auto_narrative",
            description=(
                "PROPOSE turning a project's auto-narrative on or off. Nothing "
                "changes until the user confirms the proposal in the dashboard."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "project_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "minItems": 1,
                        "maxItems": MAX_ACTION_TARGETS,
                    },
                    "enabled": {"type": "boolean"},
                },
                "required": ["project_ids", "enabled"],
                "additionalProperties": False,
            },
            handler=_set_project_auto_narrative,
        ),
        HelperTool(
            name="search_sessionfs_docs",
            description=(
                "Search the official SessionFS docs for how-to, setup, commands, "
                "settings, plans and troubleshooting questions."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "limit": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": MAX_DOC_SEARCH_RESULTS,
                    },
                },
                "required": ["query"],
                "additionalProperties": False,
            },
            handler=_search_sessionfs_docs,
        ),
        HelperTool(
            name="get_sessionfs_doc",
            description=(
                "Return one SessionFS docs page, or a single section when the "
                "anchor is given."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "slug": {"type": "string"},
                    "anchor": {"type": "string"},
                },
                "required": ["slug"],
                "additionalProperties": False,
            },
            handler=_get_sessionfs_doc,
        ),
        HelperTool(
            name="list_tickets",
            description=(
                "List sessionfs tickets (tk_…) across the caller's accessible "
                "projects. These are the project's own work items — NOT Jira "
                "issues (KEY-123). Newest-updated first, capped at 50. "
                "Pass overdue=true for only past-due, non-terminal tickets."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "project_id": {"type": "string"},
                    "status": {"type": "string"},
                    "kind": {"type": "string", "enum": ["issue", "task"]},
                    "assignee": {"type": "string"},
                    "assigned_to_me": {"type": "boolean"},
                    "overdue": {"type": "boolean"},
                    "query": {"type": "string"},
                    "limit": {"type": "integer", "minimum": 1, "maximum": MAX_LIST_TICKETS},
                },
                "additionalProperties": False,
            },
            handler=_list_tickets,
        ),
        HelperTool(
            name="get_ticket",
            description=(
                "Full detail for one sessionfs ticket (tk_…): description, "
                "acceptance criteria, status, assignee, dependencies, parent, "
                "creator/resolver, timestamps, due date, estimate, the last "
                "10 timeline events and the last 10 comments."
            ),
            input_schema={
                "type": "object",
                "properties": {"ticket_id": {"type": "string"}},
                "required": ["ticket_id"],
                "additionalProperties": False,
            },
            handler=_get_ticket,
        ),
        HelperTool(
            name="get_jira_issue",
            description=(
                "Read-only view of the Jira issue linked to one sessionfs ticket "
                "(tk_…): summary, status, assignee, priority, labels, components, "
                "updated time and the latest comments. Takes a ticket id, never a "
                "free-form Jira key; the issue is resolved from the ticket's live "
                "link, so this can only read what the ticket already points at."
            ),
            input_schema={
                "type": "object",
                "properties": {"ticket_id": {"type": "string"}},
                "required": ["ticket_id"],
                "additionalProperties": False,
            },
            handler=_get_jira_issue,
        ),
        HelperTool(
            name="list_wiki_pages",
            description="List a project's wiki pages (title + updated date).",
            input_schema={
                "type": "object",
                "properties": {"project_id": {"type": "string"}},
                "required": ["project_id"],
                "additionalProperties": False,
            },
            handler=_list_wiki_pages,
        ),
        HelperTool(
            name="get_wiki_page",
            description="One wiki page's body (capped) with its updated date and last editor.",
            input_schema={
                "type": "object",
                "properties": {
                    "project_id": {"type": "string"},
                    "slug": {"type": "string"},
                },
                "required": ["project_id", "slug"],
                "additionalProperties": False,
            },
            handler=_get_wiki_page,
        ),
        HelperTool(
            name="list_agent_runs",
            description=(
                "List a project's agent runs: status, severity/findings summary, "
                "ticket, persona and timestamps."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "project_id": {"type": "string"},
                    "status": {"type": "string"},
                    "persona": {"type": "string"},
                    "limit": {"type": "integer", "minimum": 1, "maximum": MAX_LIST_AGENT_RUNS},
                },
                "required": ["project_id"],
                "additionalProperties": False,
            },
            handler=_list_agent_runs,
        ),
        HelperTool(
            name="get_agent_run",
            description="One agent run's full detail (status, verdict, findings, ticket, timestamps).",
            input_schema={
                "type": "object",
                "properties": {
                    "project_id": {"type": "string"},
                    "run_id": {"type": "string"},
                },
                "required": ["project_id", "run_id"],
                "additionalProperties": False,
            },
            handler=_get_agent_run,
        ),
        HelperTool(
            name="list_personas",
            description=(
                "List a project's (or all accessible projects') personas: name, "
                "role, specializations, the human they represent (display name "
                "only) and whether they are active."
            ),
            input_schema={
                "type": "object",
                "properties": {"project_id": {"type": "string"}},
                "additionalProperties": False,
            },
            handler=_list_personas,
        ),
        HelperTool(
            name="trash_sessions",
            description=(
                "PROPOSE moving the caller's own sessions to Trash (restorable for "
                "30 days). Nothing changes until the user confirms in the "
                "dashboard. Pass either 'session_ids' the session tools returned, "
                "or filters (model, provider, project_id, source_tool, days, "
                "since, until) that the server resolves to sessions — use filters "
                "for \"all sessions that used model X\". At most 100 sessions per "
                "proposal; when more match, the first 100 (most recently active "
                "first) are proposed and the result says how many remain."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "session_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "minItems": 1,
                        "maxItems": MAX_ACTION_TARGETS,
                    },
                    "project_id": {"type": "string"},
                    **_SESSION_FILTER_PROPERTIES,
                    "reason": {"type": "string"},
                },
                "additionalProperties": False,
            },
            handler=_trash_sessions,
        ),
        HelperTool(
            name="propose_ticket",
            description=(
                "PROPOSE creating a ticket. Nothing changes until the user "
                "confirms in the dashboard. Never claim the ticket exists "
                "until it is confirmed; do not invent requirements — every "
                "field must be grounded in the conversation. Set "
                "publish_to_jira=true only when the user asks for the new "
                "ticket to go to Jira."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "project_id": {"type": "string"},
                    "title": {"type": "string", "maxLength": MAX_TITLE_CHARS},
                    "description": {"type": "string", "maxLength": MAX_DESCRIPTION_CHARS},
                    "acceptance_criteria": {
                        "type": "array",
                        "items": {"type": "string", "maxLength": MAX_CRITERION_CHARS},
                        "maxItems": MAX_CRITERIA,
                    },
                    "priority": {"type": "string", "enum": sorted(_VALID_PRIORITIES)},
                    "kind": {"type": "string", "enum": sorted(_VALID_TICKET_KINDS)},
                    "assignee_persona": {"type": "string", "description": "Persona name, or a teammate email/user id (direct assignment), or a display name (their represented persona when one exists, else the person)."},
                    "due_date": {"type": "string", "description": "Optional due date as YYYY-MM-DD."},
                    "estimated_effort": {"type": "string", "description": "Optional Jira-duration estimate like '3d', '2d 4h' or '30m'."},
                    "publish_to_jira": {"type": "boolean"},
                    "jira_project_key": {"type": "string", "description": "With publish_to_jira=true, publish into this Jira project key instead of the target project's mapping. Must be a key some project in the org maps to."},
                    "jira_template": {"type": "string", "description": "With publish_to_jira=true, publish with this built-in or org-custom Jira template slug instead of the inherited template."},
                    "jira_sprint_id": {"type": "integer", "description": "With publish_to_jira=true, place the issue into this Agile sprint id."},
                    "sources": {
                        "type": "object",
                        "properties": {
                            "title": {"type": "array", "items": {"type": "string"}, "minItems": 1},
                            "description": {"type": "array", "items": {"type": "string"}, "minItems": 1},
                            "acceptance_criteria": {"type": "array", "items": {"type": "string"}, "minItems": 1},
                            "priority": {"type": "array", "items": {"type": "string"}},
                            "kind": {"type": "array", "items": {"type": "string"}},
                            "assignee_persona": {"type": "array", "items": {"type": "string"}},
                        },
                        "required": ["title", "description", "acceptance_criteria"],
                        "additionalProperties": False,
                    },
                },
                "required": ["project_id", "title", "description", "acceptance_criteria", "sources"],
                "additionalProperties": False,
            },
            handler=_propose_ticket,
        ),
        HelperTool(
            name="propose_assignment",
            description=(
                "PROPOSE assigning a ticket to a persona, or directly to a "
                "teammate. An assignee name resolves in order: an exact "
                "persona name; a teammate's email or user id (direct person "
                "assignment); a teammate's display name to the persona "
                "representing them when exactly one does; otherwise a display "
                "name to that teammate. Nothing changes until the user "
                "confirms. Only the ticket creator or a project admin may "
                "assign, and only a project editor may reassign a ticket that "
                "is in progress or held by a work queue."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "ticket_id": {"type": "string"},
                    "persona_name": {"type": "string", "description": "Persona name, or a teammate email/user id (direct assignment), or a display name (their represented persona when one exists, else the person)."},
                },
                "required": ["ticket_id", "persona_name"],
                "additionalProperties": False,
            },
            handler=_propose_assignment,
        ),
        HelperTool(
            name="propose_comment",
            description=(
                "PROPOSE posting a comment on a ticket as the user. Nothing "
                "changes until the user confirms. The comment is a plain user "
                "comment — it never carries a review verdict. If send_to_jira "
                "is true and the ticket has a live Jira link plus comment "
                "forwarding, the comment will also be posted to the linked Jira "
                "issue once confirmed."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "ticket_id": {"type": "string"},
                    "body": {"type": "string", "maxLength": MAX_COMMENT_CHARS},
                    "send_to_jira": {"type": "boolean", "default": False},
                    "sources": {
                        "type": "object",
                        "properties": {
                            "body": {"type": "array", "items": {"type": "string"}, "minItems": 1},
                        },
                        "required": ["body"],
                        "additionalProperties": False,
                    },
                },
                "required": ["ticket_id", "body", "sources"],
                "additionalProperties": False,
            },
            handler=_propose_comment,
        ),
        HelperTool(
            name="propose_jira_link",
            description=(
                "PROPOSE linking a ticket to an existing Jira issue by its key "
                "(e.g. ABC-123). Nothing changes until the user confirms. "
                "Linking reads the issue to verify it but never writes to Jira; "
                "it is how a ticket gets attached to a Jira issue that already "
                "exists instead of publishing a new one."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "ticket_id": {"type": "string"},
                    "issue_key": {"type": "string"},
                },
                "required": ["ticket_id", "issue_key"],
                "additionalProperties": False,
            },
            handler=_propose_jira_link,
        ),
        HelperTool(
            name="propose_jira_overwrite",
            description=(
                "PROPOSE replacing one side of a linked ticket↔Jira pair with "
                "the other. Nothing changes until the user confirms. Direction "
                "'to_jira' overwrites the linked Jira issue's summary and "
                "description from the ticket (re-publishing it); 'from_jira' "
                "overwrites the ticket's title and description from the Jira "
                "issue. An empty remote summary or description (from_jira) "
                "leaves the corresponding ticket field unchanged rather than "
                "blanking it."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "ticket_id": {"type": "string"},
                    "direction": {"type": "string", "enum": ["to_jira", "from_jira"]},
                },
                "required": ["ticket_id", "direction"],
                "additionalProperties": False,
            },
            handler=_propose_jira_overwrite,
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

    # Duplicate retrieval (GitHub #104 part 1): an identical (tool, args)
    # pair within one turn returns a marker instead of re-running the handler
    # and writing a second retrieval-audit row.  The data is already in the
    # conversation; no new retrieval happened, so no audit event is recorded.
    cache_key = (call.name, json.dumps(safe_args, sort_keys=True, default=str))
    if cache_key in ctx.result_cache:
        return ToolResult(
            tool_call_id=call.id,
            content=(
                "You already retrieved this with the same arguments earlier in "
                "this turn — the result is above. No new retrieval was performed."
            ),
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

    # Global per-result cap (after redaction, so a cut never splits a secret
    # the redactor would have masked), then the per-turn budget.
    if len(text) > MAX_TOOL_RESULT_CHARS:
        text = (
            text[:MAX_TOOL_RESULT_CHARS]
            + f"\n[truncated: tool result exceeded {MAX_TOOL_RESULT_CHARS} characters]"
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
    # Remember the ids this result surfaced so the turn handler can accept
    # ``[ticket:…]`` / ``[wiki:…]`` citations and a proposal's ``sources`` can
    # cite ``[session:…]`` / ``[doc:…]`` markers — only once the result was
    # actually delivered (redacted and audited).  Handlers put an id in refs
    # only after resolving it to a readable, in-scope row.
    if output.refs.get("ticket_ids"):
        ctx.seen_ticket_ids.update(output.refs["ticket_ids"])
    if output.refs.get("wiki_refs"):
        ctx.seen_wiki_refs.update((pid, slug) for pid, slug in output.refs["wiki_refs"])
    if output.refs.get("session_ids"):
        ctx.seen_session_ids.update(output.refs["session_ids"])
    if output.refs.get("message_refs"):
        ctx.seen_message_refs.update(
            (sid, idx) for sid, idx in output.refs["message_refs"]
        )
    if output.refs.get("doc_slugs"):
        ctx.seen_doc_slugs.update(output.refs["doc_slugs"])
    ctx.result_cache[cache_key] = text
    return ToolResult(tool_call_id=call.id, content=text)
