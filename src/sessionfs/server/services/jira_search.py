"""Read-only Jira issue search through an org's Jira connection (GitHub #144).

The Jira integration is otherwise sync-only. This module lets a person (or the
session helper on their behalf) search Jira with JQL, under one visibility rule
(CEO decision): results only ever come from the Jira projects that are mapped
to SessionFS projects the caller can read.

The rule is enforced twice:

1. The caller's JQL is wrapped as ``project in (<allowed keys>) AND (<jql>)``.
   Before wrapping, :func:`split_jql` scans the text the way Jira's lexer does
   (single and double quoted strings with backslash escapes, parentheses) and
   refuses anything that could escape the parentheses: an unclosed string, a
   ``)`` that closes more than it opened, a backslash outside a string, control
   characters, or an ``ORDER BY`` anywhere but at the top level. A top-level
   trailing ``ORDER BY`` is split off and must be a plain list of field names
   with optional ``ASC``/``DESC``; it is re-appended after the wrapper.
2. Every returned issue is checked again: an issue whose ``project.key`` is not
   in the allowed set is dropped, whatever Jira sent.

Shared by ``POST /api/v1/orgs/{org_id}/integrations/jira/search``, the
``search_jira_issues`` helper tool and (through the route) the MCP tool.

Per-process state, matching how other org-level burst limits work here
(``marketplace_scanning._org_rescan_limiter``): a per-org sliding-window limit
on Jira calls and a 60-second result cache. Neither is shared between API
replicas, so the effective org limit is per replica.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server.auth.project_access import accessible_project_ids_subquery
from sessionfs.server.auth.rate_limit import SlidingWindowRateLimiter
from sessionfs.server.db.models import (
    OrgAuditEvent,
    OrgMember,
    OrgTrackerConnection,
    Organization,
    Project,
    TrackerProjectMapping,
    User,
)
from sessionfs.server.dlp import get_org_dlp_policy, well_formed_custom_patterns
from sessionfs.server.integrations.trackers.base import TrackerError
from sessionfs.server.integrations.trackers.jira import (
    JiraClient,
    _display_name,
    adf_to_text,
)
from sessionfs.server.llm_keys import decrypt_secret

logger = logging.getLogger("sessionfs.api")


def _redaction():
    """``sessionfs.server.helper.redaction``, imported lazily: importing the
    helper package pulls in the helper tools, which import this module."""
    from sessionfs.server.helper import redaction

    return redaction

# ── Limits ──

JQL_MAX_CHARS = 2000
DEFAULT_MAX_RESULTS = 20
MAX_MAX_RESULTS = 50
# One search is one Jira request; bound it a little above the client's own
# 20 s per-request timeout so a stalled call can't hold a request open.
SEARCH_TIMEOUT_SECONDS = 30.0
# Jira calls (cache misses) per org per minute, per process.
SEARCH_PER_ORG_PER_MINUTE = 30
CACHE_TTL_SECONDS = 60.0
CACHE_MAX_ENTRIES = 256

SUMMARY_MAX_CHARS = 500
DESCRIPTION_MAX_CHARS = 2000
LABELS_MAX = 20
AUDIT_JQL_MAX_CHARS = 2000

# Public field name -> Jira field id. ``project`` is always requested (the
# post-filter needs it) and always returned as ``project_key``.
FIELD_MAP: dict[str, str] = {
    "summary": "summary",
    "status": "status",
    "assignee": "assignee",
    "reporter": "reporter",
    "priority": "priority",
    "issue_type": "issuetype",
    "labels": "labels",
    "components": "components",
    "created": "created",
    "updated": "updated",
    "due_date": "duedate",
    "resolution": "resolution",
    "description": "description",
}
ALLOWED_FIELDS: tuple[str, ...] = tuple(FIELD_MAP)
DEFAULT_FIELDS: tuple[str, ...] = (
    "summary",
    "status",
    "assignee",
    "priority",
    "issue_type",
    "updated",
)

# Mapping keys are validated on save; re-check before they go into JQL.
_PROJECT_KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]{1,9}$")
_ISSUE_KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]+-\d+$")
# Control characters other than tab / newline / carriage return.
_JQL_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f  ]")
_ORDER_BY_RE = re.compile(r"order\s+by\b", re.IGNORECASE)
_WORD_CHAR_RE = re.compile(r"\w")
# One ORDER BY term: a field name, a ``cf[12345]`` id or a short quoted name,
# with an optional direction.
_ORDER_FIELD = r'(?:[A-Za-z][A-Za-z0-9_.]{0,63}|cf\[\d{1,10}\]|"[^"\\\r\n]{1,100}")'
_ORDER_TERM = rf"{_ORDER_FIELD}(?:\s+(?:asc|desc))?"
_ORDER_CLAUSE_RE = re.compile(
    rf"^{_ORDER_TERM}(?:\s*,\s*{_ORDER_TERM}){{0,9}}$", re.IGNORECASE
)


# ── Errors ──


class JqlError(ValueError):
    """The caller's JQL was refused before anything was sent to Jira."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class SearchError(Exception):
    """A search that could not run (rate limit, Jira failure, DLP failure)."""

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message

    def detail(self) -> dict:
        return {"error": self.code, "code": self.code, "message": self.message}


# ── JQL handling ──


def split_jql(jql: str) -> tuple[str, str | None]:
    """Validate the caller's JQL and split off a trailing top-level ORDER BY.

    Returns ``(where, order_by)``; either may be empty / None. Raises
    :class:`JqlError` for anything that could escape the wrapper's
    parentheses or that this scanner can't read unambiguously.
    """
    if not isinstance(jql, str):
        raise JqlError("invalid_jql", "jql must be a string")
    if len(jql) > JQL_MAX_CHARS:
        raise JqlError("invalid_jql", f"jql must be at most {JQL_MAX_CHARS} characters")
    if _JQL_CONTROL_RE.search(jql):
        raise JqlError("invalid_jql", "jql must not contain control characters")

    depth = 0
    quote: str | None = None
    order_at: int | None = None
    order_end = 0
    i = 0
    n = len(jql)
    while i < n:
        c = jql[i]
        if quote is not None:
            if c == "\\":
                # An escape consumes the next character (Jira's string rule).
                i += 2
                continue
            if c == quote:
                quote = None
            i += 1
            continue
        if order_at is not None:
            # Nothing after ORDER BY may open a string or a group; the clause
            # itself is validated as a plain field list below.
            if c in "\"'()":
                if c == '"':
                    # Quoted field names are allowed in ORDER BY; skip them
                    # here and let the clause regex check them.
                    end = jql.find('"', i + 1)
                    if end == -1:
                        raise JqlError("invalid_jql", "jql has an unclosed quote")
                    i = end + 1
                    continue
                raise JqlError("invalid_jql", "ORDER BY must be a plain list of fields")
            i += 1
            continue
        if c in ("'", '"'):
            quote = c
        elif c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth < 0:
                raise JqlError("invalid_jql", "jql has unbalanced parentheses")
        elif c == "\\":
            raise JqlError("invalid_jql", "jql must not contain a backslash outside a quoted string")
        elif c in "oO" and (i == 0 or not _WORD_CHAR_RE.match(jql[i - 1])):
            match = _ORDER_BY_RE.match(jql, i)
            if match is not None:
                if depth != 0:
                    raise JqlError(
                        "invalid_jql", "ORDER BY is only allowed at the end of the query"
                    )
                order_at = i
                order_end = match.end()
                i = order_end
                continue
        i += 1
    if quote is not None:
        raise JqlError("invalid_jql", "jql has an unclosed quote")
    if depth != 0:
        raise JqlError("invalid_jql", "jql has unbalanced parentheses")

    if order_at is None:
        return jql.strip(), None
    where = jql[:order_at].strip()
    order = jql[order_end:].strip()
    if not order or not _ORDER_CLAUSE_RE.match(order):
        raise JqlError(
            "invalid_jql",
            "ORDER BY must be a list of field names, each optionally ASC or DESC",
        )
    return where, order


def build_jql(allowed_keys: list[str] | set[str], where: str, order_by: str | None) -> str:
    """``project in (<keys>) [AND (<where>)] [ORDER BY <order>]``."""
    keys = sorted({k for k in allowed_keys if _PROJECT_KEY_RE.match(k)})
    if not keys:
        raise ValueError("build_jql needs at least one valid project key")
    quoted = ", ".join(f'"{k}"' for k in keys)
    out = f"project in ({quoted})"
    if where:
        out += f" AND ({where})"
    if order_by:
        out += f" ORDER BY {order_by}"
    return out


def normalize_fields(fields: list[str] | None) -> list[str]:
    """Validate requested fields against the allowlist (None = defaults)."""
    if fields is None:
        return list(DEFAULT_FIELDS)
    if not isinstance(fields, list):
        raise JqlError("invalid_fields", "fields must be a list")
    out: list[str] = []
    for name in fields:
        if not isinstance(name, str) or name not in FIELD_MAP:
            raise JqlError(
                "invalid_fields",
                "fields may only contain: " + ", ".join(ALLOWED_FIELDS),
            )
        if name not in out:
            out.append(name)
    return out or list(DEFAULT_FIELDS)


def clamp_max_results(value: int | None) -> int:
    if value is None:
        return DEFAULT_MAX_RESULTS
    return max(1, min(MAX_MAX_RESULTS, int(value)))


# ── Visibility ──


async def allowed_project_keys(
    db: AsyncSession,
    *,
    org_id: str,
    user_id: str,
    service_project_ids: list[str] | None = None,
    restrict_project_ids: set[str] | None = None,
) -> dict[str, list[str]]:
    """Jira project key -> readable SessionFS project ids mapped to it.

    Only the org's own mappings, only projects still in that org and not
    merged away, only projects ``user_id`` can read (the project read
    predicate), narrowed by a service key's allowlist and by a helper
    conversation's scope when given.
    """
    if service_project_ids is not None and not service_project_ids:
        return {}
    if restrict_project_ids is not None and not restrict_project_ids:
        return {}
    readable = await accessible_project_ids_subquery(db, user_id)
    stmt = (
        select(TrackerProjectMapping.jira_project_key, TrackerProjectMapping.project_id)
        .join(Project, Project.id == TrackerProjectMapping.project_id)
        .where(
            TrackerProjectMapping.org_id == org_id,
            Project.org_id == org_id,
            Project.merged_into_project_id.is_(None),
            Project.id.in_(readable),
        )
    )
    if service_project_ids is not None:
        stmt = stmt.where(Project.id.in_(list(service_project_ids)))
    if restrict_project_ids is not None:
        stmt = stmt.where(Project.id.in_(sorted(restrict_project_ids)))
    out: dict[str, list[str]] = {}
    for key, project_id in (await db.execute(stmt)).all():
        key = (key or "").strip().upper()
        if not _PROJECT_KEY_RE.match(key):
            continue
        out.setdefault(key, []).append(project_id)
    return {k: sorted(set(v)) for k, v in out.items()}


async def is_active_member(db: AsyncSession, org_id: str, user_id: str) -> bool:
    row = (
        await db.execute(
            select(OrgMember.id)
            .join(User, User.id == OrgMember.user_id)
            .where(
                OrgMember.org_id == org_id,
                OrgMember.user_id == user_id,
                User.is_active.is_(True),
            )
        )
    ).first()
    return row is not None


async def caller_dlp_policy(db: AsyncSession, user_id: str, org_id: str) -> dict | None:
    """Strictest union of every org the caller belongs to plus ``org_id``
    (the same rule the session helper uses for a turn)."""
    org_ids = set(
        (await db.execute(select(OrgMember.org_id).where(OrgMember.user_id == user_id)))
        .scalars()
        .all()
    )
    org_ids.add(org_id)
    orgs = (
        await db.execute(select(Organization).where(Organization.id.in_(org_ids)))
    ).scalars().all()
    scope_names: set[str] = set()
    for org in orgs:
        if org.id == org_id:
            for pattern in well_formed_custom_patterns(get_org_dlp_policy(org)):
                scope_names.add(pattern["name"])
    return _redaction().union_dlp_policies(
        [get_org_dlp_policy(org) for org in orgs], scope_pattern_names=scope_names
    )


async def enabled_connection(db: AsyncSession, org_id: str) -> OrgTrackerConnection | None:
    conn = (
        await db.execute(
            select(OrgTrackerConnection).where(OrgTrackerConnection.org_id == org_id)
        )
    ).scalar_one_or_none()
    if conn is None or not conn.enabled:
        return None
    return conn


# ── Per-process limiter + cache ──

_limiter = SlidingWindowRateLimiter(max_requests=SEARCH_PER_ORG_PER_MINUTE, window_seconds=60.0)


@dataclass
class _CacheEntry:
    expires_at: float
    issues: list[dict]
    is_last: bool


_cache: dict[str, _CacheEntry] = {}


def reset_state() -> None:
    """Clear the limiter and cache (tests)."""
    global _limiter
    _limiter = SlidingWindowRateLimiter(
        max_requests=SEARCH_PER_ORG_PER_MINUTE, window_seconds=60.0
    )
    _cache.clear()


def _cache_key(
    org_id: str, conn: OrgTrackerConnection, final_jql: str, max_results: int, fields: list[str]
) -> str:
    raw = json.dumps(
        [org_id, conn.site_url, conn.account_email, final_jql, max_results, sorted(fields)],
        separators=(",", ":"),
    )
    return hashlib.sha256(raw.encode()).hexdigest()


def _cache_get(key: str) -> _CacheEntry | None:
    entry = _cache.get(key)
    if entry is None:
        return None
    if entry.expires_at <= time.monotonic():
        _cache.pop(key, None)
        return None
    return entry


def _cache_put(key: str, issues: list[dict], is_last: bool) -> None:
    now = time.monotonic()
    if len(_cache) >= CACHE_MAX_ENTRIES:
        for k in [k for k, e in _cache.items() if e.expires_at <= now]:
            _cache.pop(k, None)
        while len(_cache) >= CACHE_MAX_ENTRIES:
            _cache.pop(next(iter(_cache)))
    _cache[key] = _CacheEntry(now + CACHE_TTL_SECONDS, issues, is_last)


# ── Result shaping ──


def _name(raw: object) -> str | None:
    if isinstance(raw, dict):
        value = raw.get("name")
        if isinstance(value, str) and value.strip():
            return value.strip()[:100]
    return None


def _shape(raw: dict, fields: list[str], site_url: str) -> dict:
    """The public view of one issue (unredacted; :func:`_redact_issue` runs
    per request because the DLP policy depends on the caller)."""
    key = raw["key"]
    raw_fields = raw.get("fields")
    f: dict = raw_fields if isinstance(raw_fields, dict) else {}
    out: dict[str, Any] = {
        "key": key,
        "url": f"{site_url}/browse/{key}",
        "project_key": f["project"]["key"],
    }
    for name in fields:
        value = f.get(FIELD_MAP[name])
        if name == "summary":
            out[name] = value if isinstance(value, str) else None
        elif name in ("status", "priority", "issue_type", "resolution"):
            out[name] = _name(value)
        elif name in ("assignee", "reporter"):
            out[name] = _display_name(value) or None
        elif name == "labels":
            out[name] = (
                [str(v)[:100] for v in value if isinstance(v, str)][:LABELS_MAX]
                if isinstance(value, list)
                else []
            )
        elif name == "components":
            out[name] = (
                [n for n in (_name(v) for v in value) if n][:LABELS_MAX]
                if isinstance(value, list)
                else []
            )
        elif name in ("created", "updated", "due_date"):
            out[name] = value[:40] if isinstance(value, str) else None
        elif name == "description":
            if isinstance(value, dict):
                out[name] = adf_to_text(value)
            elif isinstance(value, str):
                out[name] = value
            else:
                out[name] = None
    return out


def _redact_then_cap(text: str | None, policy: dict | None, cap: int) -> str | None:
    if text is None:
        return None
    redacted = _redaction().redact(text, policy)
    if len(redacted) <= cap:
        return redacted
    return redacted[: max(0, cap - 1)] + "…"


def _redact_issue(issue: dict, policy: dict | None) -> dict:
    redact = _redaction().redact
    out = dict(issue)
    if "summary" in out:
        out["summary"] = _redact_then_cap(out["summary"], policy, SUMMARY_MAX_CHARS)
    if "description" in out:
        out["description"] = _redact_then_cap(out["description"], policy, DESCRIPTION_MAX_CHARS)
    for name in ("assignee", "reporter"):
        if out.get(name):
            out[name] = _redact_then_cap(out[name], policy, 200)
    if out.get("labels"):
        out["labels"] = [redact(v, policy) for v in out["labels"]]
    if out.get("components"):
        out["components"] = [redact(v, policy) for v in out["components"]]
    return out


def _post_filter(raw_issues: list[dict], allowed: set[str]) -> tuple[list[dict], int]:
    """Keep only issues whose project key is in ``allowed`` (and whose shape
    is readable). Returns ``(kept, dropped_count)``."""
    kept: list[dict] = []
    dropped = 0
    for raw in raw_issues:
        key = raw.get("key")
        fields = raw.get("fields")
        project = fields.get("project") if isinstance(fields, dict) else None
        project_key = project.get("key") if isinstance(project, dict) else None
        if (
            not isinstance(key, str)
            or not _ISSUE_KEY_RE.match(key)
            or not isinstance(project_key, str)
            or project_key.upper() not in allowed
        ):
            dropped += 1
            continue
        project["key"] = project_key.upper()
        kept.append(raw)
    return kept, dropped


# ── The search ──


@dataclass
class SearchOutcome:
    issues: list[dict] = field(default_factory=list)
    has_more: bool = False
    allowed_keys: list[str] = field(default_factory=list)
    project_ids_by_key: dict[str, list[str]] = field(default_factory=dict)
    effective_jql: str | None = None
    cached: bool = False
    reason: str | None = None
    dropped: int = 0


NO_ALLOWED_PROJECTS_REASON = "no_mapped_projects"
NO_ALLOWED_PROJECTS_MESSAGE = (
    "None of the Jira projects mapped in this organization belong to a "
    "SessionFS project you can read, so there is nothing to search."
)


def _default_client(conn: OrgTrackerConnection) -> JiraClient:
    return JiraClient(conn.site_url, conn.account_email, decrypt_secret(conn.encrypted_api_token))


ClientFactory = Callable[[OrgTrackerConnection], JiraClient]


async def run_search(
    *,
    conn: OrgTrackerConnection,
    org_id: str,
    project_ids_by_key: dict[str, list[str]],
    where: str,
    order_by: str | None,
    max_results: int,
    fields: list[str],
    dlp_policy: dict | None,
    client_factory: ClientFactory | None = None,
) -> SearchOutcome:
    """Run one search for an already-authorized caller.

    ``project_ids_by_key`` is the caller's allowed set
    (:func:`allowed_project_keys`). An empty set returns an empty outcome
    with ``reason`` and never calls Jira. Raises :class:`SearchError`.
    """
    allowed = set(project_ids_by_key)
    outcome = SearchOutcome(
        allowed_keys=sorted(allowed), project_ids_by_key=project_ids_by_key
    )
    if not allowed:
        outcome.reason = NO_ALLOWED_PROJECTS_REASON
        return outcome

    final_jql = build_jql(allowed, where, order_by)
    outcome.effective_jql = final_jql
    key = _cache_key(org_id, conn, final_jql, max_results, fields)
    entry = _cache_get(key)
    if entry is not None:
        shaped, is_last, outcome.cached = entry.issues, entry.is_last, True
    else:
        if not _limiter.is_allowed(org_id):
            raise SearchError(
                429,
                "jira_search_rate_limited",
                f"Too many Jira searches for this organization (limit "
                f"{SEARCH_PER_ORG_PER_MINUTE} per minute). Try again shortly.",
            )
        jira_fields = ["project", *(FIELD_MAP[f] for f in fields)]
        client = (client_factory or _default_client)(conn)
        try:
            raw, is_last = await asyncio.wait_for(
                client.search_issues(final_jql, jira_fields, max_results),
                timeout=SEARCH_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError as exc:
            raise SearchError(504, "jira_timeout", "Jira did not answer in time.") from exc
        except TrackerError as exc:
            if exc.status_code == 400:
                # Jira's own message can name values that don't exist (an
                # existence oracle for projects outside the allowed set), so a
                # rejected query gets a generic reason.
                raise SearchError(
                    422,
                    "jira_query_rejected",
                    "Jira rejected the query. Check the JQL syntax, field names "
                    "and values.",
                ) from exc
            raise SearchError(502, "jira_upstream_error", str(exc)) from exc
        finally:
            await client.aclose()
        kept, dropped = _post_filter(raw[:max_results], allowed)
        outcome.dropped = dropped
        if dropped:
            logger.warning(
                "jira search for org %s dropped %d issue(s) outside the allowed set",
                org_id,
                dropped,
            )
        shaped = [_shape(r, fields, conn.site_url) for r in kept]
        _cache_put(key, shaped, is_last)

    try:
        outcome.issues = [_redact_issue(issue, dlp_policy) for issue in shaped]
    except _redaction().RedactionError as exc:
        raise SearchError(
            503, "dlp_unavailable", "Jira results could not be redacted, so none are shown."
        ) from exc
    outcome.has_more = not is_last
    return outcome


def audit_event(
    *,
    org: Organization,
    actor: User,
    actor_role: str,
    jql: str,
    dlp_policy: dict | None,
    max_results: int,
    fields: list[str],
    outcome: SearchOutcome | None,
    result: str,
    service_key_id: str | None = None,
) -> OrgAuditEvent:
    """An ``org_audit_events`` row for one search. The JQL is DLP-redacted
    before it is stored; no issue content is ever recorded, only keys."""
    redaction = _redaction()
    try:
        stored_jql = redaction.redact(jql, dlp_policy)
    except redaction.RedactionError:
        stored_jql = "[REDACTED:dlp_unavailable]"
    if len(stored_jql) > AUDIT_JQL_MAX_CHARS:
        stored_jql = stored_jql[: AUDIT_JQL_MAX_CHARS - 1] + "…"
    after: dict[str, Any] = {
        "jql": stored_jql,
        "max_results": max_results,
        "fields": fields,
        "result": result,
    }
    if outcome is not None:
        after["result_count"] = len(outcome.issues)
        after["issue_keys"] = [i["key"] for i in outcome.issues]
        after["allowed_jira_projects"] = outcome.allowed_keys
        after["cached"] = outcome.cached
        if outcome.dropped:
            after["dropped_out_of_scope"] = outcome.dropped
    if service_key_id is not None:
        after["service_key_id"] = service_key_id
    return OrgAuditEvent(
        id=f"oae_{uuid.uuid4().hex[:24]}",
        org_id=org.id,
        org_name_snapshot=org.name,
        event_type="jira_search",
        actor_user_id=actor.id,
        actor_email_snapshot=actor.email,
        actor_role_at_time=actor_role[:20],
        target_type="jira_search",
        target_id=hashlib.sha256(stored_jql.encode()).hexdigest()[:32],
        after=json.dumps(after),
    )
