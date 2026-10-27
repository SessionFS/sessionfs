"""Two-way Jira sync + completion evidence (GitHub #98 remainder, part B).

This module is the single home for the shared sync logic that both the ticket
routes (outbound) and the Jira webhook route (inbound) call. It deliberately
imports nothing from ``routes.jira`` / ``routes.tickets`` at module import time:
both route modules import *this* module, so every cross-module dependency is
resolved lazily inside a function to avoid an import cycle.

Sync model (migration 100):

* ``org_tracker_connections`` gains the two-way toggles (``sync_status``,
  ``sync_comments``, ``status_map``), the inbound webhook secret and the
  connection's own Atlassian ``jira_account_id`` (echo suppression).
* ``tracker_sync_events`` is the ledger AND the durable outbound outbox. A
  ticket write that should reach Jira inserts a ``pending`` row in the request
  transaction; after commit a background drain delivers it, and the admin
  sweep (``POST /api/v1/admin/sweeps/jira-sync``) delivers anything the drain
  lost (instance shutdown) or that failed, with exponential backoff through
  ``next_attempt_at`` and at most ``MAX_OUTBOUND_RETRIES`` attempts. Terminal
  states: ``done`` / ``exhausted`` / ``skipped``. Inbound rows are ``done``.
* Inbound status changes go through ``services.ticket_transitions`` — the same
  functions the ticket routes call — so ``resolved_at``, dependent enrichment,
  watcher notifications and the FSM all apply; a refused transition is
  recorded as an error event, never forced.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import re
import secrets
import time
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server.db.models import (
    AgentRun,
    KnowledgeEntry,
    OrgTrackerConnection,
    Organization,
    Project,
    Ticket,
    TicketComment,
    TicketTrackerLink,
    TrackerSyncEvent,
)
from sessionfs.server.integrations.trackers import JiraClient
from sessionfs.server.llm_keys import decrypt_secret

logger = logging.getLogger("sessionfs.jira_sync")

# The valid ``sync_comments`` modes. ``off`` disables mirroring entirely;
# ``inbound``/``outbound``/``both`` pick which direction(s) to mirror.
SYNC_COMMENT_MODES = frozenset({"off", "inbound", "outbound", "both"})
OUTBOUND_COMMENT_MODES = frozenset({"outbound", "both"})
INBOUND_COMMENT_MODES = frozenset({"inbound", "both"})

# The inbound webhook body is streamed and refused once it passes this size.
MAX_INBOUND_BODY_BYTES = 1 * 1024 * 1024

# Inbound text caps. Comments match the ticket comment limit; field-change
# excerpts are short by design (the change is announced, not copied).
INBOUND_COMMENT_MAX_CHARS = 10_000
FIELD_EXCERPT_MAX_CHARS = 500
JIRA_SUFFIX = " (Jira)"
AUTHOR_PERSONA_MAX = 50  # ticket_comments.author_persona width

# Only these Jira fields become "Edited in Jira" system comments.
SYSTEM_COMMENT_FIELDS = frozenset({"summary", "description", "priority"})

# Evidence comment caps: at most this many sessions are inspected, file entries
# listed and KB claim titles listed.
EVIDENCE_SESSION_CAP = 5
EVIDENCE_FILE_CAP = 20
EVIDENCE_KB_CAP = 5

# ADF budget for anything we post (Jira Cloud caps a comment near 32,767
# characters; we stop well short).
EVIDENCE_ADF_BUDGET = 24000
OUTBOUND_COMMENT_ADF_BUDGET = 24000

# Each outbound Jira call gets a bounded timeout + one retry on timeout.
OUTBOUND_TIMEOUT_SECONDS = 30.0
OUTBOUND_RETRY_DELAY_SECONDS = 1.0

# Outbox delivery: at most this many attempts per event, exponential backoff
# between attempts, and a claim lease so a concurrent drain/sweep can't
# deliver the same row twice while it is in flight.
MAX_OUTBOUND_RETRIES = 5
BACKOFF_BASE_SECONDS = 60
BACKOFF_FACTOR = 4
BACKOFF_MAX_SECONDS = 6 * 3600
CLAIM_LEASE_SECONDS = 300
SYNC_EVENT_RETENTION_DAYS = 90
SWEEP_DEFAULT_LIMIT = 100
SWEEP_DEFAULT_BUDGET_SECONDS = 50.0

# Echo suppression: an inbound status change that matches an outbound
# transition we applied within this window is our own write coming back.
ECHO_WINDOW_SECONDS = 300

# Webhook secret shape: URL-safe token with enough entropy for an HMAC key.
_WEBHOOK_SECRET_BYTES = 32

# Event states.
STATE_PENDING = "pending"
STATE_DONE = "done"
STATE_EXHAUSTED = "exhausted"
STATE_SKIPPED = "skipped"

# Blob-store handle for loading a session's messages in the evidence job. Set
# from the app lifespan (there is no request to read ``request.app.state`` in a
# spawned task); None when unavailable.
_blob_store = None


def set_jira_sync_blob_store(store) -> None:
    """Bind the app's blob store so the evidence job can read session archives."""
    global _blob_store
    _blob_store = store


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


# ── Webhook secret helpers ──


def generate_webhook_secret() -> str:
    """A fresh, URL-safe inbound webhook secret (HMAC key)."""
    return secrets.token_urlsafe(_WEBHOOK_SECRET_BYTES)


def hash_webhook_secret(secret: str) -> str:
    """sha256 hex of a webhook secret (integrity / rotation fingerprint)."""
    return hashlib.sha256(secret.encode()).hexdigest()


def webhook_secret_last4(secret: str) -> str:
    """The write-only display hint: the last 4 characters of the secret."""
    return secret[-4:] if len(secret) >= 4 else secret


def verify_webhook_signature(secret: str, body: bytes, signature: str | None) -> bool:
    """Constant-time HMAC-SHA256 check of an ``X-Hub-Signature`` header.

    ``signature`` must be ``sha256=<hex>`` (Jira sends the same shape as
    GitHub). Returns False for a missing/malformed header, never raises, so the
    caller can fail closed with a uniform response.
    """
    if not secret or not signature:
        return False
    prefix = "sha256="
    if not signature.startswith(prefix):
        return False
    expected = prefix + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


# ── Status map ──

NONE = "none"
INBOUND_ACTIONS = frozenset({"resolve", "start", "reopen", NONE})
TRANSITION_TARGETS = (
    "open",
    "in_progress",
    "blocked",
    "review",
    "done",
    "cancelled",
    "closed",
)
STATUS_CATEGORIES = ("new", "indeterminate", "done")


def default_status_map() -> dict:
    """The built-in status map used when a connection has none configured.

    ``transitions`` maps a SessionFS *target* status to the Jira transition
    display name that drives the issue there (outbound). The defaults cover
    only what a stock Jira workflow has (To Do / In Progress / Done); statuses
    without a stock equivalent are ``"none"`` (not synced).

    ``categories`` maps a Jira status-category **key** (``new`` /
    ``indeterminate`` / ``done`` — stable and non-localized) to a SessionFS
    action (inbound). By default only Done acts (``resolve``): moving an issue
    back to To Do / In Progress in Jira never rewinds SessionFS unless an admin
    maps it.
    """
    return {
        "transitions": {
            "open": "To Do",
            "in_progress": "In Progress",
            "blocked": NONE,
            "review": NONE,
            "done": "Done",
            "cancelled": NONE,
            "closed": "Done",
        },
        "categories": {
            "new": NONE,
            "indeterminate": NONE,
            "done": "resolve",
        },
    }


def parse_status_map(text: str | None) -> dict:
    """Parse a stored ``status_map`` JSON column into a dict, or the default.

    Malformed JSON falls back to the default rather than raising: a bad stored
    map must not break the webhook or the outbound push. Unknown keys and
    values are dropped.
    """
    merged = default_status_map()
    if not text:
        return merged
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return merged
    if not isinstance(data, dict):
        return merged
    transitions = data.get("transitions")
    if isinstance(transitions, dict):
        for target, name in transitions.items():
            if target in merged["transitions"] and isinstance(name, str) and name.strip():
                merged["transitions"][target] = name.strip()
    categories = data.get("categories")
    if isinstance(categories, dict):
        for category, action in categories.items():
            if category in merged["categories"] and action in INBOUND_ACTIONS:
                merged["categories"][category] = action
    return merged


def mapped_transition(status_map: dict, status: str) -> str | None:
    """The Jira transition name for a SessionFS status, or None when unmapped."""
    name = (status_map.get("transitions") or {}).get(status)
    if not isinstance(name, str) or not name.strip() or name.strip().lower() == NONE:
        return None
    return name.strip()


def mapped_action(status_map: dict, category: str | None) -> str | None:
    """The SessionFS action for a Jira status category, or None when unmapped."""
    if not category:
        return None
    action = (status_map.get("categories") or {}).get(category)
    if action not in INBOUND_ACTIONS or action == NONE:
        return None
    return action


def validate_status_map(status_map: dict | None) -> dict:
    """Validate a client-supplied status map, returning the full normalized map.

    Omitted keys keep their defaults. A transition value of ``"none"``, an
    empty string or null means "not synced"; a category action may be
    ``resolve`` / ``start`` / ``reopen`` / ``none`` (or null/empty → none).
    Raises ``HTTPException(422)`` for anything else.
    """
    from fastapi import HTTPException

    def bad(message: str) -> HTTPException:
        return HTTPException(422, {"error": "status_map_invalid", "message": message})

    normalized = default_status_map()
    if status_map is None:
        return normalized
    if not isinstance(status_map, dict):
        raise bad("status_map must be an object")
    unknown = set(status_map) - {"transitions", "categories"}
    if unknown:
        raise bad(f"unknown status_map keys: {sorted(unknown)}")
    transitions = status_map.get("transitions")
    if transitions is not None:
        if not isinstance(transitions, dict):
            raise bad("status_map.transitions must be an object")
        for target, name in transitions.items():
            if target not in TRANSITION_TARGETS:
                raise bad(f"unknown status {target!r} in status_map.transitions")
            if name is None or (isinstance(name, str) and not name.strip()):
                normalized["transitions"][target] = NONE
                continue
            if not isinstance(name, str):
                raise bad(f"transition name for {target!r} must be a string")
            if len(name) > 120:
                raise bad(f"transition name for {target!r} is too long")
            cleaned = name.strip()
            normalized["transitions"][target] = NONE if cleaned.lower() == NONE else cleaned
    categories = status_map.get("categories")
    if categories is not None:
        if not isinstance(categories, dict):
            raise bad("status_map.categories must be an object")
        for category, action in categories.items():
            if category not in STATUS_CATEGORIES:
                raise bad(f"unknown category {category!r} in status_map.categories")
            if action is None or action == "":
                normalized["categories"][category] = NONE
                continue
            if action not in INBOUND_ACTIONS:
                raise bad(f"unknown action {action!r} for category {category!r}")
            normalized["categories"][category] = action
    return normalized


def validate_sync_comments(value: str | None) -> str:
    """Normalize/validate the ``sync_comments`` toggle; raise 422 on bad input."""
    from fastapi import HTTPException

    mode = (value or "off").strip().lower()
    if mode not in SYNC_COMMENT_MODES:
        raise HTTPException(
            422,
            {"error": "sync_comments_invalid", "message": f"sync_comments must be one of {sorted(SYNC_COMMENT_MODES)}"},
        )
    return mode


# ── DLP redaction ──


def redact_for_jira(text: str, org: Organization | None) -> str:
    """DLP-redact text crossing the Jira boundary (either direction).

    Secrets are always redacted; a configured org DLP policy adds its own
    categories (phi) and custom patterns on top — same rule as the promotion
    source redaction.
    """
    from sessionfs.server.dlp import get_org_dlp_policy, redact_text, scan_dlp_full

    if not text:
        return text
    policy = get_org_dlp_policy(org) if org is not None else None
    categories = {"secrets"}
    custom_patterns: list[dict] | None = None
    if policy is not None:
        categories |= set(policy.get("categories") or [])
        custom_patterns = policy.get("custom_patterns") or None
    findings = scan_dlp_full(text, categories=sorted(categories), custom_patterns=custom_patterns)
    if not findings:
        return text
    return redact_text(text, findings, custom_patterns)


def redact_then_cap(text: str, org: Organization | None, cap: int) -> str:
    """Redact first, then cap — capping first could cut a secret in half so the
    scanner no longer recognizes it and the surviving prefix leaks."""
    redacted = redact_for_jira(text, org)
    if len(redacted) <= cap:
        return redacted
    return redacted[: max(0, cap - 1)] + "…"


def jira_author_label(display_name: str, org: Organization | None) -> str:
    """``"<name> (Jira)"`` within the author_persona column width, DLP'd."""
    name = redact_for_jira(display_name or "a Jira user", org).strip() or "a Jira user"
    room = AUTHOR_PERSONA_MAX - len(JIRA_SUFFIX)
    if len(name) > room:
        name = name[: room - 1] + "…"
    return name + JIRA_SUFFIX


# ── Entitlement ──


async def org_has_jira(db: AsyncSession, org_id: str) -> bool:
    """True while the org's plan still includes ``jira_integration``."""
    from sessionfs.server.tier_gate import resolve_org_plan_tier
    from sessionfs.server.tiers import get_features_for_tier

    try:
        plan = await resolve_org_plan_tier(org_id, db)
    except Exception:  # noqa: BLE001 — a lookup failure is "not entitled"
        return False
    return "jira_integration" in get_features_for_tier(plan)


# ── Sync-event ledger ──


def record_sync_event(
    db: AsyncSession,
    *,
    org_id: str,
    link_id: str,
    ticket_id: str,
    direction: str,
    kind: str,
    payload: dict | None = None,
    jira_event_id: str | None = None,
    applied: bool = False,
    error: str | None = None,
    state: str = STATE_DONE,
    next_attempt_at: datetime | None = None,
    dedupe_key: str | None = None,
) -> TrackerSyncEvent:
    """Append one sync event to the ledger (no flush/commit; the caller commits)."""
    now = _now()
    event = TrackerSyncEvent(
        id=f"tse_{secrets.token_hex(16)}",
        org_id=org_id,
        link_id=link_id,
        ticket_id=ticket_id,
        direction=direction,
        kind=kind,
        payload=json.dumps(payload or {}, separators=(",", ":")),
        jira_event_id=jira_event_id,
        applied=applied,
        error=error,
        state=state,
        retry_count=0,
        next_attempt_at=next_attempt_at,
        updated_at=now,
        dedupe_key=dedupe_key,
        created_at=now,
    )
    db.add(event)
    return event


async def inbound_event_exists(db: AsyncSession, link_id: str, jira_event_id: str) -> bool:
    """True when an inbound event with this Jira id was already recorded.

    ``jira_event_id`` is unique per link (see migration 100), so this is the
    idempotency guard for replayed Jira webhooks.
    """
    row = (
        await db.execute(
            select(TrackerSyncEvent.id).where(
                TrackerSyncEvent.link_id == link_id,
                TrackerSyncEvent.jira_event_id == jira_event_id,
            )
        )
    ).first()
    return row is not None


async def recent_outbound_transition(
    db: AsyncSession,
    link_id: str,
    *,
    to_name: str | None,
    to_status: str | None = None,
    within_seconds: int = ECHO_WINDOW_SECONDS,
) -> bool:
    """True when we pushed a transition named like ``to_name`` very recently
    (and, when ``to_status`` is given, for that same SessionFS status).

    This is the echo test for inbound status changes: a delivery is our own
    write coming back only when it matches something we actually pushed. Jira's
    stock transitions share the target status' name (To Do / In Progress /
    Done), so a case-insensitive match on the changelog's new status is the
    signal. The connection's accountId is never enough on its own — admins
    often connect with a personal token, and their real changes must apply.
    """
    if not to_name:
        return False
    since = _now() - timedelta(seconds=within_seconds)
    rows = (
        await db.execute(
            select(TrackerSyncEvent.payload).where(
                TrackerSyncEvent.link_id == link_id,
                TrackerSyncEvent.direction == "outbound",
                TrackerSyncEvent.kind == "status",
                TrackerSyncEvent.applied.is_(True),
                or_(
                    TrackerSyncEvent.updated_at >= since,
                    TrackerSyncEvent.created_at >= since,
                ),
            )
        )
    ).scalars().all()
    wanted = to_name.strip().lower()
    for raw in rows:
        try:
            payload = json.loads(raw or "{}")
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(payload, dict):
            continue
        if str(payload.get("transition", "")).strip().lower() != wanted:
            continue
        if to_status is not None and payload.get("to_status") != to_status:
            continue
        return True
    return False


# ── Outbound markers (idempotent comment posts, echo recognition) ──

_MARKER_PREFIX = "SessionFS ref "
# The reference exactly as we append it: its own final line, in brackets.
_MARKER_LINE_RE = re.compile(r"\[SessionFS ref (tse_[0-9a-f]{32})\]")
PUSHED_COMMENT_PREFIX = "pushed-comment:"


def outbox_marker(event_id: str) -> str:
    """The reference line embedded in every comment SessionFS posts. It makes a
    retried or redelivered post detectable on the issue (no duplicate) and lets
    the webhook recognise the echo even if the post's id was never recorded."""
    return f"{_MARKER_PREFIX}{event_id}"


def _with_marker(adf: dict, marker: str | None) -> dict:
    if not marker:
        return adf
    content = list(adf.get("content") or [])
    content.append(
        {"type": "paragraph", "content": [{"type": "text", "text": f"[{marker}]"}]}
    )
    return {**adf, "content": content}


async def is_pushed_comment(
    db: AsyncSession,
    link_id: str,
    jira_comment_id: str | None,
    text: str,
    *,
    author_account_id: str | None = None,
    own_account_id: str | None = None,
) -> bool:
    """True when an inbound Jira comment is one SessionFS posted on this link.

    The primary signal is the Jira comment id recorded when we posted it. The
    outbox reference is a fallback for a post whose id was never recorded
    (lost commit), and it counts only when it could really be ours: it is the
    comment's final line (where we always put it), and the author is the
    connection's own account (or that account is unknown). A human who quotes
    or pastes a reference into their own reply is therefore still mirrored.
    """
    if jira_comment_id:
        row = (
            await db.execute(
                select(TrackerSyncEvent.id).where(
                    TrackerSyncEvent.link_id == link_id,
                    TrackerSyncEvent.jira_event_id == f"{PUSHED_COMMENT_PREFIX}{jira_comment_id}",
                )
            )
        ).first()
        if row is not None:
            return True
    if own_account_id and author_account_id and author_account_id != own_account_id:
        return False
    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    if not lines:
        return False
    match = _MARKER_LINE_RE.fullmatch(lines[-1])
    if match is None:
        return False
    row = (
        await db.execute(
            select(TrackerSyncEvent.id).where(
                TrackerSyncEvent.link_id == link_id,
                TrackerSyncEvent.direction == "outbound",
                TrackerSyncEvent.id == match.group(1),
            )
        )
    ).first()
    return row is not None


# ── Inbound status application ──


def terminal_status_for_kind(kind: str) -> str:
    """The completed/resolved status for a ticket kind (evidence trigger)."""
    return "closed" if kind == "issue" else "done"


def is_terminal_status(status: str, kind: str) -> bool:
    return status == terminal_status_for_kind(kind)


def action_target(action: str, kind: str) -> str | None:
    return {
        "resolve": terminal_status_for_kind(kind),
        "start": "in_progress",
        "reopen": "open",
    }.get(action)


async def apply_inbound_action(
    db: AsyncSession, project: Project, ticket: Ticket, action: str
) -> tuple[str, str | None]:
    """Apply a Jira-derived action through the shared ticket transitions.

    Returns ``("applied", None)``, ``("noop", None)`` when the ticket is
    already where the action would take it (e.g. the echo of our own
    transition), or ``("refused", reason)`` when the FSM / ticket kind / a
    concurrent update refused it. Nothing is written on refusal.

    Mapping: ``resolve`` → accept (Task in review → done) or close (Issue in
    progress → closed); ``start`` → open → in_progress; ``reopen`` → back to
    open. See ``services.ticket_transitions`` for why the org-configured
    mapping may act without a user.
    """
    from sessionfs.server.services import ticket_transitions as tt

    target = action_target(action, ticket.kind)
    if target is None:
        return "refused", f"unknown action {action!r}"
    if ticket.status == target:
        return "noop", None
    try:
        if action == "resolve":
            if ticket.kind == "issue":
                await tt.close_issue(
                    db, project=project, ticket=ticket, actor_user_id=None, actor_type="jira"
                )
            else:
                await tt.accept_ticket(
                    db,
                    project_id=ticket.project_id,
                    ticket_id=ticket.id,
                    lease_epoch=None,
                    actor_user_id=None,
                    actor_type="jira",
                )
        elif action == "start":
            await tt.start_ticket(
                db, ticket, allowed_from={"open"}, actor_user_id=None, actor_type="jira"
            )
        else:  # reopen
            await tt.reopen_ticket(
                db, project=project, ticket=ticket, actor_user_id=None, actor_type="jira"
            )
    except tt.TransitionRefused as exc:
        return "refused", exc.detail[:500]
    return "applied", None


# ── Outbox: enqueue (request transaction) ──


async def _sync_target(
    db: AsyncSession, ticket_id: str
) -> tuple[TicketTrackerLink, OrgTrackerConnection] | None:
    """The live (link, connection) pair for a ticket, or None when the ticket
    is not linked, the connection is gone/disabled, or the link points at a
    different Jira site than the connection (never write across sites)."""
    link = (
        await db.execute(
            select(TicketTrackerLink)
            .where(TicketTrackerLink.ticket_id == ticket_id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if link is None or link.status != "linked" or not link.issue_key:
        return None
    conn = (
        await db.execute(
            select(OrgTrackerConnection)
            .where(OrgTrackerConnection.org_id == link.org_id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if conn is None or not conn.enabled or link.site_url != conn.site_url:
        return None
    # A project that left the link's org (transfer, merge, admin move) keeps
    # its stale link; never push its tickets to the former org's Jira.
    project_org_id = (
        await db.execute(
            select(Project.org_id)
            .join(Ticket, Ticket.project_id == Project.id)
            .where(Ticket.id == ticket_id)
        )
    ).scalar_one_or_none()
    if project_org_id != link.org_id:
        return None
    return link, conn


def _evidence_dedupe_key(ticket: Ticket) -> str:
    """One evidence comment per completion: keyed on the completion time, so a
    reopen + re-resolve (new ``resolved_at``) posts fresh evidence."""
    stamp = _aware(ticket.resolved_at) or _aware(ticket.updated_at) or _now()
    return f"evidence:{stamp.astimezone(timezone.utc).strftime('%Y%m%dT%H%M%S%f')}"


async def enqueue_evidence(
    db: AsyncSession, ticket: Ticket, link: TicketTrackerLink
) -> str | None:
    """Claim the unique evidence row for this completion; None if already claimed."""
    try:
        async with db.begin_nested():
            event = record_sync_event(
                db,
                org_id=link.org_id,
                link_id=link.ticket_id,
                ticket_id=ticket.id,
                direction="outbound",
                kind="evidence",
                payload={"status": ticket.status, "kind": ticket.kind},
                state=STATE_PENDING,
                next_attempt_at=_now(),
                dedupe_key=_evidence_dedupe_key(ticket),
            )
            await db.flush()
    except IntegrityError:
        return None
    return event.id


async def enqueue_status_change(db: AsyncSession, ticket: Ticket) -> list[str]:
    """Queue the outbound push for a ticket's new status (and the completion
    evidence when it just resolved). Returns the queued event ids.

    Nothing is queued when the ticket isn't linked, the connection is off, or
    the new status has no mapped transition. Callers wrap this in their own
    best-effort guard; it never commits.
    """
    async with db.begin_nested():
        return await _enqueue_status_change(db, ticket)


async def _enqueue_status_change(db: AsyncSession, ticket: Ticket) -> list[str]:
    target = await _sync_target(db, ticket.id)
    if target is None:
        return []
    link, conn = target
    if not conn.sync_status:
        return []
    ids: list[str] = []
    transition = mapped_transition(parse_status_map(conn.status_map), ticket.status)
    if transition:
        event = record_sync_event(
            db,
            org_id=link.org_id,
            link_id=link.ticket_id,
            ticket_id=ticket.id,
            direction="outbound",
            kind="status",
            payload={"to_status": ticket.status, "transition": transition},
            state=STATE_PENDING,
            next_attempt_at=_now(),
        )
        ids.append(event.id)
    if is_terminal_status(ticket.status, ticket.kind):
        evidence_id = await enqueue_evidence(db, ticket, link)
        if evidence_id:
            ids.append(evidence_id)
    await db.flush()
    return ids


async def comment_forwarding_enabled(db: AsyncSession, ticket_id: str) -> bool:
    """True when a comment on this ticket may be sent to Jira (linked + the
    org mirrors comments outbound)."""
    target = await _sync_target(db, ticket_id)
    if target is None:
        return False
    return (target[1].sync_comments or "off") in OUTBOUND_COMMENT_MODES


async def enqueue_comment(
    db: AsyncSession, ticket: Ticket, comment_id: str, *, reason: str
) -> list[str]:
    """Queue one outbound comment push; best-effort (never raises)."""
    try:
        async with db.begin_nested():
            target = await _sync_target(db, ticket.id)
            if target is None:
                return []
            link, conn = target
            if (conn.sync_comments or "off") not in OUTBOUND_COMMENT_MODES:
                return []
            event = record_sync_event(
                db,
                org_id=link.org_id,
                link_id=link.ticket_id,
                ticket_id=ticket.id,
                direction="outbound",
                kind="comment",
                payload={"comment_id": comment_id, "reason": reason},
                state=STATE_PENDING,
                next_attempt_at=_now(),
            )
            await db.flush()
            return [event.id]
    except Exception as exc:  # noqa: BLE001 — never fail the comment request
        logger.warning("jira comment enqueue failed for ticket %s: %s", ticket.id, type(exc).__name__)
        return []


# ── Outbox: drain + deliver ──

_background_tasks: set[asyncio.Task] = set()


def _spawn(coro) -> None:
    task = asyncio.create_task(coro)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


def schedule_drain(event_ids: list[str]) -> None:
    """Deliver freshly queued outbox rows in the background, after commit.

    A lost task (instance shutdown) is harmless: the rows stay ``pending`` and
    the sweep delivers them.
    """
    if event_ids:
        _spawn(_drain_job(list(event_ids)))


async def _drain_job(event_ids: list[str]) -> None:
    from sessionfs.server.db.engine import _session_factory

    if _session_factory is None:
        return
    async with _session_factory() as db:
        for event_id in event_ids:
            try:
                await process_event(db, event_id)
            except Exception as exc:  # noqa: BLE001 — best-effort; never raise
                logger.warning("jira outbox drain failed for %s: %s", event_id, type(exc).__name__)


def backoff_seconds(retry_count: int) -> int:
    """Delay before attempt ``retry_count + 1``: 1 min, 4 min, 16 min, 64 min."""
    n = max(1, retry_count)
    return min(BACKOFF_MAX_SECONDS, BACKOFF_BASE_SECONDS * BACKOFF_FACTOR ** (n - 1))


async def _claim(db: AsyncSession, event_id: str, now: datetime) -> bool:
    """Atomically take a due pending row (pushes ``next_attempt_at`` out by the
    claim lease so a concurrent drain/sweep skips it while in flight)."""
    result = await db.execute(
        update(TrackerSyncEvent)
        .where(
            TrackerSyncEvent.id == event_id,
            TrackerSyncEvent.state == STATE_PENDING,
            TrackerSyncEvent.retry_count < MAX_OUTBOUND_RETRIES,
            or_(
                TrackerSyncEvent.next_attempt_at.is_(None),
                TrackerSyncEvent.next_attempt_at <= now,
            ),
        )
        .values(
            next_attempt_at=now + timedelta(seconds=CLAIM_LEASE_SECONDS),
            updated_at=now,
        )
        .execution_options(synchronize_session=False)
    )
    await db.commit()
    return result.rowcount == 1


def _error_text(exc: BaseException) -> str:
    from sessionfs.server.integrations.trackers.base import TrackerError

    if isinstance(exc, asyncio.TimeoutError):
        return "Jira request timed out"
    if isinstance(exc, TrackerError):
        return (str(exc) or "Jira request failed")[:500]
    # Unknown failures: the type only — never echo internals into the ledger.
    return f"internal error ({type(exc).__name__})"


async def _mark_failure(db: AsyncSession, event_id: str, exc: BaseException) -> str:
    event = await db.get(TrackerSyncEvent, event_id, populate_existing=True)
    if event is None:
        return "missing"
    now = _now()
    event.retry_count = int(event.retry_count or 0) + 1
    event.error = _error_text(exc)
    event.applied = False
    event.updated_at = now
    if event.retry_count >= MAX_OUTBOUND_RETRIES:
        event.state = STATE_EXHAUSTED
        event.next_attempt_at = None
        outcome = "exhausted"
    else:
        event.state = STATE_PENDING
        event.next_attempt_at = now + timedelta(seconds=backoff_seconds(event.retry_count))
        outcome = "failed"
    await db.commit()
    return outcome


async def process_event(db: AsyncSession, event_id: str) -> str:
    """Claim and deliver one outbox row, committing its outcome.

    Returns ``not_due`` / ``done`` / ``skipped`` / ``failed`` (retry
    scheduled) / ``exhausted``. Any exception while delivering is caught and
    recorded on the row — one bad event never stops the drain or the sweep.
    """
    if not await _claim(db, event_id, _now()):
        return "not_due"
    try:
        event = await db.get(TrackerSyncEvent, event_id, populate_existing=True)
        if event is None:
            return "skipped"
        outcome = await _deliver(db, event)
        await db.commit()
        return outcome
    except Exception as exc:  # noqa: BLE001 — recorded on the row
        await db.rollback()
        return await _mark_failure(db, event_id, exc)


def _finish(event: TrackerSyncEvent, state: str, *, error: str | None = None, reason: str | None = None) -> str:
    event.state = state
    event.applied = state == STATE_DONE
    event.error = error
    event.next_attempt_at = None
    event.updated_at = _now()
    if reason:
        payload = _event_payload(event)
        payload["skip_reason"] = reason
        event.payload = json.dumps(payload, separators=(",", ":"))
    return state


def _event_payload(event: TrackerSyncEvent) -> dict:
    try:
        payload = json.loads(event.payload or "{}")
    except (json.JSONDecodeError, TypeError):
        return {}
    return payload if isinstance(payload, dict) else {}


async def _call_with_retry(factory):
    """One bounded call, retried once after a timeout. A second timeout raises."""
    try:
        return await asyncio.wait_for(factory(), timeout=OUTBOUND_TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        await asyncio.sleep(OUTBOUND_RETRY_DELAY_SECONDS)
        return await asyncio.wait_for(factory(), timeout=OUTBOUND_TIMEOUT_SECONDS)


def _client(conn: OrgTrackerConnection) -> JiraClient:
    return JiraClient(conn.site_url, conn.account_email, decrypt_secret(conn.encrypted_api_token))


async def _remember_account_id(client: JiraClient, conn: OrgTrackerConnection) -> None:
    """Persist the connection's own accountId (echo suppression) if unknown."""
    if conn.jira_account_id:
        return
    try:
        account_id = await asyncio.wait_for(client.myself_account_id(), timeout=OUTBOUND_TIMEOUT_SECONDS)
    except Exception:  # noqa: BLE001 — a best-effort hint only
        return
    if account_id:
        conn.jira_account_id = account_id[:128]


async def _deliver(db: AsyncSession, event: TrackerSyncEvent) -> str:
    """Deliver one claimed outbox row (mutates it; the caller commits)."""
    if event.direction != "outbound" or event.kind not in {"status", "comment", "evidence"}:
        return _finish(event, STATE_SKIPPED, reason="not_outbound")
    # Fresh reads: the sweep runs on a long-lived session whose identity map
    # may hold an older copy of the ticket.
    ticket = await db.get(Ticket, event.ticket_id, populate_existing=True)
    target = await _sync_target(db, event.ticket_id)
    if ticket is None or target is None:
        return _finish(event, STATE_SKIPPED, reason="unlinked")
    link, conn = target
    if not await org_has_jira(db, link.org_id):
        return _finish(
            event,
            STATE_SKIPPED,
            error="The org's plan no longer includes the Jira integration.",
            reason="not_entitled",
        )
    org = await db.get(Organization, link.org_id)
    payload = _event_payload(event)

    if event.kind == "status":
        if not conn.sync_status:
            return _finish(event, STATE_SKIPPED, reason="sync_off")
        if payload.get("to_status") and payload.get("to_status") != ticket.status:
            # The ticket moved on since this row was queued; a newer row
            # carries the current status.
            return _finish(event, STATE_SKIPPED, reason="superseded")
        transition = mapped_transition(parse_status_map(conn.status_map), ticket.status)
        if not transition:
            return _finish(event, STATE_SKIPPED, reason="unmapped")
        async def remember_target(status_name: str) -> None:
            # Persist the transition's target status before the POST, so a
            # redelivery after a lost commit (when Jira no longer offers the
            # transition) can still recognise the issue is already there.
            payload["target_status"] = status_name
            event.payload = json.dumps(payload, separators=(",", ":"))
            await db.commit()

        client = _client(conn)
        try:
            await _remember_account_id(client, conn)
            result = await _transition_idempotent(
                client,
                link.issue_key,
                transition,
                expected_target=payload.get("target_status"),
                remember_target=remember_target,
            )
        finally:
            await client.aclose()
        link.last_synced_at = _now()
        payload["transition"] = transition
        if result == "already_at_target":
            payload["result"] = result
        event.payload = json.dumps(payload, separators=(",", ":"))
        return _finish(event, STATE_DONE)

    if event.kind == "comment":
        if (conn.sync_comments or "off") not in OUTBOUND_COMMENT_MODES:
            return _finish(event, STATE_SKIPPED, reason="comment_sync_off")
        comment = await db.get(TicketComment, payload.get("comment_id") or "")
        if comment is None or comment.ticket_id != ticket.id:
            return _finish(event, STATE_SKIPPED, reason="comment_missing")
        adf = outbound_comment_adf(comment.content, org, marker=outbox_marker(event.id))
    else:  # evidence
        if not conn.sync_status:
            return _finish(event, STATE_SKIPPED, reason="sync_off")
        adf = await build_evidence_adf(db, ticket, org, marker=outbox_marker(event.id))

    client = _client(conn)
    try:
        await _remember_account_id(client, conn)
        jira_comment_id = await _post_comment_idempotent(
            client, link.issue_key, adf, outbox_marker(event.id)
        )
    finally:
        await client.aclose()
    link.last_synced_at = _now()
    if jira_comment_id:
        # Recorded so the webhook drops the comment_created echo of this post.
        event.jira_event_id = f"{PUSHED_COMMENT_PREFIX}{jira_comment_id}"[:120]
        payload["jira_comment_id"] = jira_comment_id
        event.payload = json.dumps(payload, separators=(",", ":"))
    return _finish(event, STATE_DONE)


async def _bounded(coro):
    return await asyncio.wait_for(coro, timeout=OUTBOUND_TIMEOUT_SECONDS)


async def _post_comment_idempotent(
    client: JiraClient, issue_key: str, adf: dict, marker: str
) -> str | None:
    """Post a comment unless one carrying ``marker`` is already on the issue.

    The lookup runs before every attempt — the first delivery, the retry after
    a client-side timeout (the POST may have landed), and any redelivery after
    a lost commit — so one outbox row never produces two Jira comments.
    """

    from sessionfs.server.integrations.trackers.base import TrackerError

    async def attempt() -> str | None:
        try:
            existing = await client.find_comment_with_marker(issue_key, marker)
        except TrackerError as exc:
            # The lookup is a duplicate guard, not a precondition: if it fails
            # (e.g. the page of comments is over the response cap), post
            # rather than fail the delivery on every retry.
            logger.info("jira marker lookup failed for %s: %s", issue_key, exc)
            existing = None
        if existing:
            return existing
        return await client.add_comment(issue_key, adf) or None

    return await _call_with_retry(attempt)


async def _issue_at_status(client: JiraClient, issue_key: str, status_name: str) -> bool:
    try:
        issue = await _bounded(client.get_issue(issue_key))
    except Exception:  # noqa: BLE001 — unknown state; let the caller decide
        return False
    return (issue.status or "").strip().lower() == status_name.strip().lower()


async def _transition_idempotent(
    client: JiraClient,
    issue_key: str,
    transition: str,
    *,
    expected_target: str | None = None,
    remember_target=None,
) -> str:
    """Apply a transition, tolerating one that already happened.

    A timed-out POST may have landed, and a redelivery after a lost commit
    re-runs a transition that succeeded; either way the workflow no longer
    offers it. The target status comes from the transition itself (Jira's
    transitions API returns its ``to`` status), is persisted through
    ``remember_target`` before the POST, and is what the issue is compared
    against — so a custom map whose transition and status names differ
    ("Resolve" → "Resolved") is recognised too. Without a known target the
    transition name is used (stock workflows share the names).
    """
    from sessionfs.server.integrations.trackers.base import TrackerError

    state = {"target": expected_target}

    def target() -> str:
        return state["target"] or transition

    async def attempt() -> str:
        chosen = await client.find_transition(issue_key, transition)
        if chosen is None:
            if await _issue_at_status(client, issue_key, target()):
                return "already_at_target"
            raise TrackerError(
                f"no transition named {transition!r} is available for issue {issue_key}"
            )
        to_name = chosen.get("to_name")
        if to_name and to_name != state["target"]:
            state["target"] = to_name
            if remember_target is not None:
                await remember_target(to_name)
        try:
            await client.apply_transition(issue_key, chosen["id"])
        except TrackerError:
            if await _issue_at_status(client, issue_key, target()):
                return "already_at_target"
            raise
        return "transitioned"

    try:
        return await _bounded(attempt())
    except asyncio.TimeoutError:
        await asyncio.sleep(OUTBOUND_RETRY_DELAY_SECONDS)
        if await _issue_at_status(client, issue_key, target()):
            return "already_at_target"
        return await _bounded(attempt())


def _plain_adf(text: str) -> dict:
    return {
        "type": "doc",
        "version": 1,
        "content": [{"type": "paragraph", "content": [{"type": "text", "text": text}]}],
    }


def _fit_adf(text: str, budget: int) -> dict:
    """Markdown → ADF within ``budget`` characters of text (truncates)."""
    from sessionfs.server.integrations.trackers.jira import markdown_to_adf
    from sessionfs.server.integrations.trackers.promote import adf_text_length

    try:
        adf = markdown_to_adf(text)
    except Exception:  # noqa: BLE001 — fall back to one plain paragraph
        adf = _plain_adf(text)
    if adf_text_length(adf) <= budget:
        return adf
    marker = "\n\n… (truncated — see SessionFS for the full text)"
    cut = text[: max(0, budget - len(marker) - 64)] + marker
    try:
        adf = markdown_to_adf(cut)
    except Exception:  # noqa: BLE001
        adf = _plain_adf(cut)
    if adf_text_length(adf) <= budget:
        return adf
    return _plain_adf(cut[:budget])


_MARKER_RESERVE = 120


def outbound_comment_adf(content: str, org: Organization | None, *, marker: str | None = None) -> dict:
    """A SessionFS comment as a Jira ADF body: prefixed, DLP'd, within budget,
    ending with the outbox reference line."""
    text = redact_for_jira(f"via SessionFS — {content}", org)
    return _with_marker(_fit_adf(text, OUTBOUND_COMMENT_ADF_BUDGET - _MARKER_RESERVE), marker)


# ── Completion evidence ──


async def build_evidence_adf(
    db: AsyncSession, ticket: Ticket, org: Organization | None, *, marker: str | None = None
) -> dict:
    """Build the completion-evidence ADF comment.

    Sections: agent-run verdict + persona + trusted badge, per-file change line
    counts from the ticket's linked sessions (capped at ``EVIDENCE_FILE_CAP``),
    KB claims recorded on the ticket (count + titles capped at
    ``EVIDENCE_KB_CAP``), and links back to the ticket / sessions when the
    dashboard origin is configured. Only data the ticket links in its own
    project is read; the whole comment is DLP-redacted and fits the budget.
    """
    from sessionfs.server.config import ServerConfig, dashboard_link_base

    run = await _resolver_agent_run(db, ticket)
    verdict = _run_verdict(run)
    persona = ticket.assigned_to or (run.persona_name if run is not None else None)
    trust_badge = "trusted reviewer" if await _ticket_resolved_by_trusted(db, ticket) else "agent"
    lines: list[str] = [
        f"**SessionFS completion evidence** — {ticket.kind} resolved",
        f"- Verdict: {verdict}",
    ]
    if persona:
        lines.append(f"- Persona: {persona}")
    lines.append(f"- Badge: {trust_badge}")

    sessions = await linked_sessions(db, ticket)
    files = await _ticket_file_changes(sessions)
    if files:
        lines.append("")
        lines.append("**Files changed**")
        for f in files[:EVIDENCE_FILE_CAP]:
            lines.append(f"- {f['path']} ({f['operation']}: +{f['added']}/-{f['removed']})")
        if len(files) > EVIDENCE_FILE_CAP:
            lines.append(f"- … and {len(files) - EVIDENCE_FILE_CAP} more")

    claims = await ticket_claims(db, ticket)
    if claims:
        lines.append("")
        lines.append(f"**Knowledge base claims** ({len(claims)})")
        for title in claims[:EVIDENCE_KB_CAP]:
            lines.append(f"- {title}")

    try:
        dashboard_url = dashboard_link_base(ServerConfig())
    except Exception:  # noqa: BLE001
        dashboard_url = ""
    if dashboard_url:
        lines.append("")
        lines.append(
            f"Source ticket: {dashboard_url}/projects/{ticket.project_id}"
            f"?tab=work&sub=tickets&ticket={ticket.id}"
        )
        for s in sessions:
            lines.append(f"Session: {dashboard_url}/sessions/{s.id}")

    text = redact_for_jira("\n".join(lines), org)
    return _with_marker(_fit_adf(text, EVIDENCE_ADF_BUDGET - _MARKER_RESERVE), marker)


async def _resolver_agent_run(db: AsyncSession, ticket: Ticket) -> AgentRun | None:
    """The latest agent run recorded against the ticket in its own project."""
    return (
        await db.execute(
            select(AgentRun)
            .where(AgentRun.ticket_id == ticket.id, AgentRun.project_id == ticket.project_id)
            .order_by(AgentRun.completed_at.desc().nullslast(), AgentRun.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


def _run_verdict(run: AgentRun | None) -> str:
    if run is None:
        return "completed"
    if run.status == "passed":
        return "passed"
    if run.severity:
        return f"{run.status} ({run.severity})"
    return run.status or "completed"


async def _ticket_resolved_by_trusted(db: AsyncSession, ticket: Ticket) -> bool:
    """True when a trusted reviewer posted a VERIFIED-CLEAN verdict on the ticket."""
    from sessionfs.server.services.ticket_watchers import parse_verdict

    rows = (
        await db.execute(
            select(TicketComment.content).where(
                TicketComment.ticket_id == ticket.id,
                TicketComment.verdict_trusted.is_(True),
            )
        )
    ).scalars().all()
    return any(parse_verdict(content or "") == "VERIFIED-CLEAN" for content in rows)


def _json_list(text: str | None) -> list:
    try:
        data = json.loads(text or "[]")
    except (json.JSONDecodeError, TypeError):
        return []
    return data if isinstance(data, list) else []


async def linked_sessions(db: AsyncSession, ticket: Ticket) -> list:
    """Sessions the ticket itself links (its resolver session + related
    sessions), restricted to the ticket's project and not deleted."""
    from sessionfs.server.db.models import Session

    ids: list[str] = []
    for value in [ticket.resolver_session_id, *_json_list(ticket.related_sessions)]:
        if isinstance(value, str) and value and value not in ids:
            ids.append(value)
    ids = ids[:EVIDENCE_SESSION_CAP]
    if not ids:
        return []
    rows = (
        await db.execute(
            select(Session).where(
                Session.id.in_(ids),
                Session.project_id == ticket.project_id,
                Session.is_deleted.is_not(True),
            )
        )
    ).scalars().all()
    by_id = {s.id: s for s in rows}
    return [by_id[i] for i in ids if i in by_id]


async def _ticket_file_changes(sessions: list) -> list[dict]:
    """Per-file line counts across the linked sessions (merged by path).

    Reads each archive from the bound blob store; a missing store, archive or
    unparsable session simply contributes nothing.
    """
    if not sessions or _blob_store is None:
        return []
    from sessionfs.server.services.session_archive import extract_archive
    from sessionfs.server.services.session_changes import derive_session_changes

    merged: dict[str, dict] = {}
    order: list[str] = []
    for session in sessions:
        if not session.blob_key:
            continue
        try:
            data = await _blob_store.get(session.blob_key)
            if not data:
                continue
            archive = extract_archive(data)
            messages = archive.get("messages") or []
            if not messages:
                continue
            derived = derive_session_changes(messages, archive.get("workspace"))
        except Exception:  # noqa: BLE001 — evidence is best-effort per session
            continue
        for f in derived.get("files") or []:
            path = f.get("path")
            if not isinstance(path, str):
                continue
            entry = merged.get(path)
            if entry is None:
                entry = {"path": path, "operation": f.get("operation") or "modified", "added": 0, "removed": 0}
                merged[path] = entry
                order.append(path)
            entry["added"] += int(f.get("added_lines") or 0)
            entry["removed"] += int(f.get("removed_lines") or 0)
    return [merged[p] for p in order]


def _int_ids(values: list) -> list[int]:
    out: list[int] = []
    for v in values:
        if isinstance(v, bool):
            continue
        if isinstance(v, int):
            out.append(v)
        elif isinstance(v, str) and v.strip().isdigit():
            out.append(int(v.strip()))
    return list(dict.fromkeys(out))


async def ticket_claims(db: AsyncSession, ticket: Ticket) -> list[str]:
    """First-line titles of the live KB claims the ticket records
    (``knowledge_entry_ids``), restricted to the ticket's project."""
    ids = _int_ids(_json_list(ticket.knowledge_entry_ids))
    if not ids:
        return []
    rows = (
        await db.execute(
            select(KnowledgeEntry.content)
            .where(
                KnowledgeEntry.id.in_(ids),
                KnowledgeEntry.project_id == ticket.project_id,
                KnowledgeEntry.dismissed.is_(False),
                KnowledgeEntry.superseded_by.is_(None),
            )
            .order_by(KnowledgeEntry.id)
        )
    ).scalars().all()
    titles: list[str] = []
    for content in rows:
        first_line = (content or "").strip().splitlines()[0].strip() if (content or "").strip() else ""
        if first_line:
            titles.append(first_line[:200])
    return titles


# ── Sweep ──


async def run_jira_sync_sweep(
    db: AsyncSession,
    *,
    limit: int = SWEEP_DEFAULT_LIMIT,
    budget_seconds: float = SWEEP_DEFAULT_BUDGET_SECONDS,
) -> dict:
    """Deliver due outbox rows and expire old ledger rows.

    Only retryable rows are selected (``state='pending' AND retry_count < 5
    AND next_attempt_at <= now``), so terminal rows can never starve new
    failures. Each event is claimed, delivered and committed on its own; a
    per-event exception is recorded on that row. The run stops after
    ``limit`` events or ``budget_seconds`` of wall clock, whichever is first.
    """
    started = time.monotonic()
    now = _now()
    cutoff = now - timedelta(days=SYNC_EVENT_RETENTION_DAYS)
    expired = (
        await db.execute(
            delete(TrackerSyncEvent).where(
                TrackerSyncEvent.created_at < cutoff,
                TrackerSyncEvent.state != STATE_PENDING,
            )
            .execution_options(synchronize_session=False)
        )
    ).rowcount or 0
    # Defensive: a pending row that already used every attempt is terminal.
    await db.execute(
        update(TrackerSyncEvent)
        .where(
            TrackerSyncEvent.state == STATE_PENDING,
            TrackerSyncEvent.retry_count >= MAX_OUTBOUND_RETRIES,
        )
        .values(state=STATE_EXHAUSTED, next_attempt_at=None, updated_at=now)
        .execution_options(synchronize_session=False)
    )
    await db.commit()

    due_ids = (
        await db.execute(
            select(TrackerSyncEvent.id)
            .where(
                TrackerSyncEvent.state == STATE_PENDING,
                TrackerSyncEvent.retry_count < MAX_OUTBOUND_RETRIES,
                or_(
                    TrackerSyncEvent.next_attempt_at.is_(None),
                    TrackerSyncEvent.next_attempt_at <= now,
                ),
            )
            .order_by(TrackerSyncEvent.next_attempt_at.asc().nullsfirst(), TrackerSyncEvent.id)
            .limit(limit)
        )
    ).scalars().all()

    counts = {"done": 0, "failed": 0, "exhausted": 0, "skipped": 0, "not_due": 0}
    attempted = 0
    budget_hit = False
    for event_id in due_ids:
        if time.monotonic() - started >= budget_seconds:
            budget_hit = True
            break
        attempted += 1
        try:
            outcome = await process_event(db, event_id)
        except Exception as exc:  # noqa: BLE001 — never abort the sweep
            await db.rollback()
            logger.warning("jira sweep failed on %s: %s", event_id, type(exc).__name__)
            outcome = "failed"
        counts[outcome if outcome in counts else "skipped"] += 1

    return {
        "expired_events": expired,
        "due": len(due_ids),
        "attempted": attempted,
        "succeeded": counts["done"],
        "failed": counts["failed"],
        "exhausted": counts["exhausted"],
        "skipped": counts["skipped"],
        "not_due": counts["not_due"],
        "budget_exhausted": budget_hit,
    }
