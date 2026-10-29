"""Helper execution context, retrieval budget, and retrieval-audit sink."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Protocol

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.retrieval_audit import sanitize_arguments
from sessionfs.server.db.models import RetrievalAuditEvent, Session, User
from sessionfs.server.storage.base import BlobStore

logger = logging.getLogger("sessionfs.helper")

# Per-turn retrieval budget: 24k tokens of tool output, estimated at chars/4.
DEFAULT_TOKENS_PER_TURN = 24_000

# Audit JSON cap mirrors routes/retrieval_audit.py.
MAX_AUDIT_JSON_BYTES = 16 * 1024


@dataclass(frozen=True)
class HelperScope:
    """Hard-filter scope for one turn.

    When ``project_id`` / ``group_id`` / ``session_ids`` are set, handlers
    treat them as non-negotiable boundaries — the model can narrow them
    further but never widen them.  Exactly one of the three is set by the
    routes layer; the dataclass itself stays permissive so stored scopes can
    be rehydrated best-effort.
    """

    project_id: str | None = None
    group_id: str | None = None
    session_ids: tuple[str, ...] = ()


class RetrievalBudget:
    """Track the per-turn tool-output budget, estimated at ``len(text) // 4``.

    Handlers call :meth:`charge` on the text they are about to return; the
    budget truncates (with a note) once the 24k-token ceiling is reached.
    """

    def __init__(self, token_limit: int = DEFAULT_TOKENS_PER_TURN) -> None:
        self.token_limit = token_limit
        self._used_tokens = 0

    @staticmethod
    def estimate_tokens(text: str) -> int:
        """Estimate tokens as ceil(chars / 4)."""
        if not text:
            return 0
        return (len(text) + 3) // 4

    @property
    def used(self) -> int:
        return self._used_tokens

    @property
    def remaining(self) -> int:
        return max(0, self.token_limit - self._used_tokens)

    def charge(self, text: str) -> str:
        """Charge ``text`` against the budget, truncating with a note when it
        doesn't fit.  Returns the (possibly truncated) text to emit."""
        cost = self.estimate_tokens(text)
        if cost <= self.remaining:
            self._used_tokens += cost
            return text

        note = "\n[truncated: per-turn retrieval budget reached]"
        note_cost = self.estimate_tokens(note)
        keep = self.remaining - note_cost
        if keep <= 0:
            self._used_tokens = self.token_limit
            return note.strip()

        keep_chars = keep * 4
        self._used_tokens = self.token_limit
        return text[:keep_chars] + note


class AuditSink(Protocol):
    """Receives one record per tool call.  Implemented by ``HelperAuditSink``."""

    async def record(
        self,
        tool_name: str,
        sanitized_args: dict[str, Any],
        returned_refs: dict[str, Any],
    ) -> None: ...


def _json_capped(value: dict[str, Any]) -> str:
    raw = json.dumps(value, sort_keys=True, default=str)
    if len(raw.encode("utf-8")) <= MAX_AUDIT_JSON_BYTES:
        return raw
    marker = {
        "_truncated": True,
        "_original_bytes": len(raw.encode("utf-8")),
    }
    return json.dumps(marker, sort_keys=True)


class HelperAuditSink:
    """Write one ``RetrievalAuditEvent`` per (tool call, project), ``source="helper"``.

    The surrounding ``RetrievalAuditContext`` is created by the turn handler
    (one per turn).  A tool call may reference sessions in several projects, so
    the sink resolves the tool's ``session_ids``/``project_ids`` to project ids
    and emits one event per distinct project (a project-less retrieval emits a
    single event with ``project_id = NULL``).  The per-project fan-out is
    deliberate: each project's retrieval-audit trail shows exactly what the
    helper read from it.  Tools therefore put a project in ``project_ids`` only
    when that project returned rows — an unscoped list over many projects
    audits just the projects it surfaced data from.  ``key_source`` and the
    ``conversation_id`` are recorded with the sanitized tool arguments — the
    only free-form JSON field on the event (the context has no metadata column).
    The sink only appends events; it never commits — the turn handler owns the
    transaction.
    """

    def __init__(
        self,
        db: AsyncSession,
        *,
        context_id: str,
        user_id: str,
        key_source: str | None = None,
        conversation_id: str | None = None,
        persist: bool = True,
    ) -> None:
        self.db = db
        self.context_id = context_id
        self.user_id = user_id
        self.key_source = key_source
        self.conversation_id = conversation_id
        # ``persist=False`` makes the sink capture-only: events are still
        # resolved and appended to ``events`` in memory, but never flushed to
        # ``db``.  The turn handler uses this so a single
        # ``materialize_into`` write is the only audit-write path — flushing to
        # the agent-loop session would double-write once the shared SQLite
        # connection commits the fresh session's transaction (H2).
        self.persist = persist
        # Captured in-memory so the turn handler can re-persist the events in a
        # fresh session after the agent-loop session was discarded (H2).
        self.events: list[RetrievalAuditEvent] = []

    async def _project_ids(self, refs: dict[str, Any]) -> set[str]:
        project_ids = {
            p for p in (refs.get("project_ids") or []) if isinstance(p, str) and p
        }
        session_ids = [
            s for s in (refs.get("session_ids") or []) if isinstance(s, str) and s
        ]
        if session_ids:
            rows = await self.db.execute(
                select(Session.project_id).where(
                    Session.id.in_(session_ids),
                    Session.project_id.isnot(None),
                )
            )
            project_ids.update(pid for (pid,) in rows.all() if pid)
        return project_ids

    async def record(
        self,
        tool_name: str,
        sanitized_args: dict[str, Any],
        returned_refs: dict[str, Any],
    ) -> None:
        """Record one tool call: one event per project it read from.

        The project set is ``returned_refs["project_ids"]`` plus the projects
        of any ``returned_refs["session_ids"]``.  Every event carries the same
        arguments and refs; the fan-out exists so each project's audit trail
        lists the retrievals that touched it.  Repeating an identical call in
        one turn writes nothing (``execute`` answers it from the per-turn
        cache), so the fan-out is the only source of multiple rows per call.
        """
        # Defense-in-depth: sanitize again even though callers already did.
        safe_args = sanitize_arguments(sanitized_args or {})
        try:
            refs: dict[str, Any] = returned_refs or {}
        except TypeError:
            refs = {}
        # Record turn provenance alongside the sanitized tool arguments (L7).
        audit_args = dict(safe_args)
        if self.key_source:
            audit_args["key_source"] = self.key_source
        if self.conversation_id:
            audit_args["conversation_id"] = self.conversation_id

        project_ids: set[str | None] = await self._project_ids(refs)
        if not project_ids:
            project_ids = {None}

        for project_id in sorted(project_ids, key=lambda p: p or ""):
            event = RetrievalAuditEvent(
                context_id=self.context_id,
                project_id=project_id,
                session_id=None,
                tool_name=(tool_name or "")[:100],
                arguments=_json_capped(audit_args),
                returned_refs=_json_capped(refs),
                source="helper",
                caller_user_id=self.user_id,
                actor_type="user",
            )
            # Capture the event before the flush so a cancellation that
            # interrupts the savepoint still leaves the in-memory copy for the
            # turn handler to re-persist in a fresh session (H2).
            self.events.append(event)
            if not self.persist:
                continue
            # A failed audit write must never poison the shared session: the
            # insert runs in a nested transaction (savepoint) so a flush
            # failure rolls back cleanly and leaves the session usable for the
            # next call.
            async with self.db.begin_nested():
                self.db.add(event)
                await self.db.flush()

    def materialize_into(self, db: AsyncSession) -> None:
        """Re-add the captured events to ``db`` as fresh objects.

        The agent-loop session may be left in a broken (pending-rollback) state
        when a client disconnect interrupts an in-flight statement, so the turn
        handler persists from this in-memory copy into a fresh session instead
        of reusing the (now unusable) session the events were originally
        flushed to.  ``id`` is omitted so the target session assigns new keys.
        """
        for original in self.events:
            db.add(
                RetrievalAuditEvent(
                    context_id=original.context_id,
                    project_id=original.project_id,
                    session_id=original.session_id,
                    tool_name=original.tool_name,
                    arguments=original.arguments,
                    returned_refs=original.returned_refs,
                    source=original.source,
                    caller_user_id=original.caller_user_id,
                    actor_type=original.actor_type,
                )
            )


@dataclass
class HelperContext:
    """Everything a tool handler needs to run one call, access-checked."""

    user: User
    db: AsyncSession
    org_id: str | None
    scope: HelperScope
    budget: RetrievalBudget
    audit: AuditSink | None
    # Injected by piece 4 from ``request.app.state.blob_store``; None lets
    # DB-only tools (search/list/KB) run without a blob store.
    blob_store: BlobStore | None = None
    # Resolved DLP policy for redaction; None means the default policy.
    dlp_policy: dict | None = None
    # The turn's conversation id — write-action proposals need it to link the
    # pending HelperAction row to its thread.
    conversation_id: str | None = None
    # Write-action proposals queued by tool handlers.  Handlers append the
    # proposal dict (id/kind/params/preview/created_at/expires_at); the turn
    # handler takes them off right after the tool call and, only when the call
    # succeeded, commits them as pending HelperAction rows in their own short
    # session before emitting ``action_proposed`` (the agent-loop session is
    # read-only for writes).
    pending_actions: list[Any] = field(default_factory=list)
    # Per-turn dedup cache: (tool_name, sorted-json args) -> the exact text
    # already returned this turn.  A repeat identical call returns a marker
    # result and writes NO second retrieval-audit event (GitHub #104 part 1).
    result_cache: dict[tuple[str, str], str] = field(default_factory=dict)
    # Ticket ids and (project_id, slug) wiki refs the conversation's tool
    # results have surfaced as structured refs.  Citation validation in the
    # turn handler only accepts ``[ticket:…]`` / ``[wiki:…]`` markers for ids
    # in these sets, and the proposal ``sources`` validation reuses them.  The
    # turn handler seeds them (and the session/message/doc sets below) from
    # prior turns' stored structured refs and confirmed helper-action results
    # — never from marker text inside tool output, which stored content
    # (a ticket, wiki page or session) could carry.
    seen_ticket_ids: set[str] = field(default_factory=set)
    seen_wiki_refs: set[tuple[str, str]] = field(default_factory=set)
    # Session ids, (session_id, message index) pairs and docs slugs the
    # conversation's tool results have surfaced — used to validate
    # ``[session:…]`` (with optional ``msg:…``) and ``[doc:…]`` proposal
    # sources.  A message index is only meaningful within its own session.
    seen_session_ids: set[str] = field(default_factory=set)
    seen_message_refs: set[tuple[str, int]] = field(default_factory=set)
    seen_doc_slugs: set[str] = field(default_factory=set)
    # Meeting ids tool results surfaced; [meeting:… L<a>-<b>] citations
    # are only kept for these (and only when still readable).
    seen_meeting_ids: set[str] = field(default_factory=set)
    # Plan-gate results for this turn, keyed (feature, project_id), and the
    # caller's tier context they are resolved against (built lazily).
    feature_cache: dict[tuple[str, str], bool] = field(default_factory=dict)
    user_context: Any = None
