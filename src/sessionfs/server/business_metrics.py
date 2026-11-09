"""Business metrics recording helpers (0.30.0, GitHub #131).

Thin, fail-safe wrappers around the families declared in ``metrics.py``. Every
function here swallows its own errors: a metrics failure must never fail (or
roll back) the request or job that triggered it. Label values are clamped to
closed vocabularies; ids only ever reach the optional tenant labels, which the
registry drops unless ``SFS_METRICS_TENANT_LABELS`` is on.

DB-derived gauges (approval requests by state, review-loop items by status)
are refreshed from the metrics scrape (``routes/metrics.py``) on the shared
30-second cache, never kept as in-memory counts: several API replicas run, and
only the database knows the cluster-wide truth.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from sessionfs.server import metrics as m

logger = logging.getLogger("sessionfs.metrics")


def _aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def _swallow(what: str) -> None:
    logger.debug("metrics: %s failed", what, exc_info=True)


# ── sweeps ──────────────────────────────────────────────────────────────────


def sweep_name_for_path(route_path: str | None) -> str:
    """The sweep label for a matched route template (``"other"`` if unknown)."""
    if not route_path:
        return m.OTHER
    return m.SWEEP_ROUTES.get(route_path, m.OTHER)


def sweep_name_for_request(request: Any) -> str:
    try:
        route = request.scope.get("route")
        return sweep_name_for_path(getattr(route, "path", None))
    except Exception:
        return m.OTHER


def sweep_denial_reason(status: int, detail: Any, *, stage: str) -> str:
    """Bounded reason for a refused sweep call.

    ``stage`` is where the refusal happened: ``auth`` (the credential itself
    was refused), ``admin`` (a user key that isn't a platform admin), ``scope``
    (a service key without ``platform_sweeps:run``) or ``minter`` (the key's
    owner/minter is no longer an active platform admin).
    """
    if stage == "auth":
        if status == 401:
            return "unauthenticated"
        if status == 429:
            return "rate_limited"
        return "auth_rejected"
    if stage == "admin":
        return "not_admin"
    if stage == "scope":
        return "insufficient_scope"
    if stage == "minter":
        reason = detail.get("reason") if isinstance(detail, dict) else None
        return m.clamp_label(reason, m.SWEEP_DENIAL_REASONS)
    return m.OTHER


def record_sweep_denied(sweep: str, reason: str) -> None:
    try:
        m.SWEEP_DENIED_TOTAL.inc(
            sweep=m.clamp_label(sweep, m.SWEEP_NAMES),
            reason=m.clamp_label(reason, m.SWEEP_DENIAL_REASONS),
        )
    except Exception:
        _swallow("sweep denial")


def record_sweep_run(sweep: str, status: int | None, seconds: float) -> None:
    try:
        name = m.clamp_label(sweep, m.SWEEP_NAMES)
        m.SWEEP_RUNS_TOTAL.inc(sweep=name, result=m.sweep_result(status))
        m.SWEEP_DURATION.observe(max(0.0, seconds), sweep=name)
    except Exception:
        _swallow("sweep run")


async def _oldest_due(db: Any, sweep: str, now: datetime) -> datetime | None:
    """When the oldest item the sweep would act on became due, or None.

    Only the sweeps whose backlog is one cheap indexed MIN() are measured;
    the rest report no lag.
    """
    from sqlalchemy import func, or_, select

    if sweep == "reminders-due":
        from sessionfs.server.db.models import Reminder

        return (
            await db.execute(
                select(func.min(Reminder.due_at)).where(
                    Reminder.status == "open",
                    Reminder.notified_at.is_(None),
                    Reminder.due_at <= now,
                )
            )
        ).scalar()
    if sweep == "jira-sync":
        from sessionfs.server.db.models import TrackerSyncEvent
        from sessionfs.server.services.jira_sync import (
            MAX_OUTBOUND_RETRIES,
            STATE_PENDING,
        )

        return (
            await db.execute(
                select(
                    func.min(func.coalesce(TrackerSyncEvent.next_attempt_at, TrackerSyncEvent.created_at))
                ).where(
                    TrackerSyncEvent.state == STATE_PENDING,
                    TrackerSyncEvent.retry_count < MAX_OUTBOUND_RETRIES,
                    or_(
                        TrackerSyncEvent.next_attempt_at.is_(None),
                        TrackerSyncEvent.next_attempt_at <= now,
                    ),
                )
            )
        ).scalar()
    if sweep == "ticket-notifications":
        from sessionfs.server.db.models import TicketNotification
        from sessionfs.server.routes.admin import (
            _DIGEST_SUBJECT_KINDS,
            _TICKET_EMAIL_MIN_AGE,
        )

        cutoff = now - _TICKET_EMAIL_MIN_AGE
        return (
            await db.execute(
                select(func.min(TicketNotification.updated_at)).where(
                    TicketNotification.subject_kind.in_(_DIGEST_SUBJECT_KINDS),
                    TicketNotification.read_at.is_(None),
                    TicketNotification.emailed_at.is_(None),
                    or_(
                        TicketNotification.subject_kind.in_(("reminder", "approval")),
                        TicketNotification.updated_at <= cutoff,
                    ),
                )
            )
        ).scalar()
    return None


LAG_SWEEPS: tuple[str, ...] = ("reminders-due", "jira-sync", "ticket-notifications")


async def observe_sweep_lag(db: Any, sweep: str) -> None:
    """Set ``sessionfs_sweep_lag_seconds`` for ``sweep`` from the database.

    Runs read-only, before the sweep does any work, in its OWN short session
    on the same engine: a failing query can then neither poison nor roll back
    the request's transaction (which would expire the authenticated user the
    sweep is about to read).
    """
    if sweep not in LAG_SWEEPS:
        return
    try:
        from sqlalchemy.ext.asyncio import AsyncSession

        now = datetime.now(timezone.utc)
        async with AsyncSession(db.bind, expire_on_commit=False) as own:
            oldest = _aware(await _oldest_due(own, sweep, now))
        lag = max(0.0, (now - oldest).total_seconds()) if oldest is not None else 0.0
        m.SWEEP_LAG.set(lag, sweep=sweep)
    except Exception:
        _swallow("sweep lag")


# ── webhooks ────────────────────────────────────────────────────────────────


def record_webhook_ingest(route_path: str | None, status: int | None) -> None:
    """Count one inbound webhook delivery (no-op for non-webhook routes)."""
    try:
        source = m.WEBHOOK_ROUTES.get(route_path or "")
        if source is None:
            return
        m.WEBHOOK_INGEST_TOTAL.inc(source=source, result=m.webhook_result(status))
    except Exception:
        _swallow("webhook ingest")


def observe_route(request: Any, status: int | None, seconds: float) -> None:
    """Middleware hook: sweep runs/durations and webhook ingest by route."""
    try:
        route = request.scope.get("route")
        path = getattr(route, "path", None)
        if path is None:
            return
        if path in m.WEBHOOK_ROUTES:
            record_webhook_ingest(path, status)
            return
        if path in m.SWEEP_ROUTES and getattr(request.state, "sweep_authorized", False):
            record_sweep_run(m.SWEEP_ROUTES[path], status, seconds)
    except Exception:
        _swallow("route observation")


# ── Jira / Confluence ───────────────────────────────────────────────────────


def record_jira_sync(
    direction: str,
    result: str,
    *,
    org_id: str | None = None,
    lag_seconds: float | None = None,
) -> None:
    try:
        d = m.clamp_label(direction, m.JIRA_DIRECTIONS)
        if d == m.OTHER:
            return
        m.JIRA_SYNC_TOTAL.inc(
            direction=d, result=m.clamp_label(result, m.JIRA_SYNC_RESULTS), org_id=org_id
        )
        if lag_seconds is not None:
            m.JIRA_SYNC_LAG.observe(max(0.0, float(lag_seconds)), direction=d, org_id=org_id)
    except Exception:
        _swallow("jira sync")


def jira_inbound_lag_seconds(payload: Any, now: datetime | None = None) -> float | None:
    """Seconds since the Jira event's own ``timestamp`` (epoch ms), if sane."""
    try:
        if not isinstance(payload, dict):
            return None
        ts = payload.get("timestamp")
        if isinstance(ts, bool) or not isinstance(ts, (int, float)):
            return None
        when = datetime.fromtimestamp(ts / 1000.0, tz=timezone.utc)
        now = now or datetime.now(timezone.utc)
        lag = (now - when).total_seconds()
        # Clock skew or a bogus timestamp: report nothing rather than nonsense.
        if lag < 0 or lag > timedelta(days=30).total_seconds():
            return None
        return lag
    except Exception:
        return None


def confluence_result_for_exception(exc: BaseException) -> str:
    """Bounded Confluence publish result for a failed publish."""
    from fastapi import HTTPException

    if not isinstance(exc, HTTPException):
        return "error"
    detail = exc.detail if isinstance(exc.detail, dict) else {}
    code = detail.get("error")
    if code == "publish_in_progress":
        return "in_progress"
    if code == "confluence_page_drifted":
        return "drifted"
    if code in ("content_too_large", "content_too_large_for_confluence"):
        return "too_large"
    if code == "confluence_timeout" or exc.status_code == 504:
        return "timeout"
    if exc.status_code == 409:
        return "conflict"
    if 400 <= exc.status_code < 500:
        return "rejected"
    return "error"


def record_confluence_publish(
    result: str, *, org_id: str | None = None, project_id: str | None = None
) -> None:
    try:
        m.CONFLUENCE_PUBLISH_TOTAL.inc(
            result=m.clamp_label(result, m.CONFLUENCE_RESULTS),
            org_id=org_id,
            project_id=project_id,
        )
    except Exception:
        _swallow("confluence publish")


# ── per-org concurrency caps ────────────────────────────────────────────────


def record_org_concurrency_rejection(route_class: str, org_id: str | None = None) -> None:
    """One 429 ``org_concurrency_limit``. ``org_id`` is recorded only with
    tenant labels on; a personal (user-keyed) scope never carries an id."""
    try:
        m.ORG_CONCURRENCY_REJECTED_TOTAL.inc(
            route_class=m.clamp_label(route_class, m.CONCURRENCY_ROUTE_CLASSES),
            org_id=org_id,
        )
    except Exception:
        _swallow("org concurrency rejection")


# ── agent runs ──────────────────────────────────────────────────────────────


def record_agent_tokens(
    *,
    provider: str | None,
    model: str | None,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    cache_read_tokens: int | None = None,
    cache_write_tokens: int | None = None,
    org_id: str | None = None,
    project_id: str | None = None,
) -> None:
    """Add the tokens one report contributed (never a run's running total)."""
    try:
        family = m.model_family(provider, model)
        for kind, amount in (
            ("input", input_tokens),
            ("output", output_tokens),
            ("cache_read", cache_read_tokens),
            ("cache_write", cache_write_tokens),
        ):
            if isinstance(amount, bool) or not isinstance(amount, int) or amount <= 0:
                continue
            m.AGENT_TOKENS_TOTAL.inc(
                float(amount),
                token_kind=kind,
                model_family=family,
                org_id=org_id,
                project_id=project_id,
            )
    except Exception:
        _swallow("agent tokens")


def record_agent_run_finished(
    *,
    status: str,
    role: str | None,
    org_id: str | None = None,
    project_id: str | None = None,
) -> None:
    try:
        st = m.clamp_label(status, m.AGENT_RUN_STATUSES)
        if st == m.OTHER:
            return
        m.AGENT_RUNS_TOTAL.inc(
            status=st,
            role=m.clamp_label(role or "none", m.AGENT_ROLES),
            org_id=org_id,
            project_id=project_id,
        )
    except Exception:
        _swallow("agent run")


# ── approvals ───────────────────────────────────────────────────────────────


def observe_approval_decision(
    *,
    state: str,
    requested_at: datetime | None,
    decided_at: datetime | None,
    org_id: str | None = None,
    project_id: str | None = None,
) -> None:
    """Request raised → approved/rejected. Other end states are not decisions."""
    try:
        if state not in m.APPROVAL_DECISIONS:
            return
        start, end = _aware(requested_at), _aware(decided_at)
        if start is None or end is None:
            return
        m.APPROVAL_DECISION_SECONDS.observe(
            max(0.0, (end - start).total_seconds()),
            decision=state,
            org_id=org_id,
            project_id=project_id,
        )
    except Exception:
        _swallow("approval decision")


# ── extraction ──────────────────────────────────────────────────────────────


def observe_extraction_server_time(
    *, kind: str | None, result: str, total_seconds: float, provider_seconds: float
) -> None:
    """Record the server's share of one extraction (wall time minus provider)."""
    try:
        m.EXTRACTION_SERVER_SECONDS.observe(
            max(0.0, float(total_seconds) - max(0.0, float(provider_seconds))),
            kind=m.clamp_label(kind or "meeting", m.EXTRACTION_KINDS),
            result=m.clamp_label(result, m.EXTRACTION_RESULTS),
        )
    except Exception:
        _swallow("extraction server time")


# ── DB-derived gauges (scrape time) ─────────────────────────────────────────


async def refresh_business_gauges(db: Any) -> None:
    """Rebuild the DB-derived business gauges. Never raises."""
    from sqlalchemy import select
    from sqlalchemy.orm import aliased

    from sessionfs.server.db.models import ApprovalRequest, Ticket, WorkQueueItem

    tenant = m.tenant_labels_enabled()

    try:
        cols: list[Any] = [ApprovalRequest.state]
        if tenant:
            cols += [ApprovalRequest.org_id, ApprovalRequest.project_id]
        stmt = select(*cols, m_count()).group_by(*cols)
        rows = (await db.execute(stmt)).all()
        m.APPROVAL_REQUESTS.clear()
        for state in m.APPROVAL_STATES:
            m.APPROVAL_REQUESTS.set(0.0, status=state)
        for row in rows:
            state = m.clamp_label(row[0], m.APPROVAL_STATES)
            if tenant:
                m.APPROVAL_REQUESTS.inc(
                    float(row[-1]), status=state, org_id=row[1], project_id=row[2]
                )
            else:
                m.APPROVAL_REQUESTS.inc(float(row[-1]), status=state)
    except Exception:
        _swallow("approval gauge")
        await _safe_rollback(db)

    try:
        # A review-loop item is a work-queue item whose ticket is a child of a
        # loop parent (an issue carrying loop_config).
        child = aliased(Ticket)
        parent = aliased(Ticket)
        cols = [WorkQueueItem.item_status]
        if tenant:
            cols += [child.project_id]
        stmt = (
            select(*cols, m_count())
            .select_from(WorkQueueItem)
            .join(child, child.id == WorkQueueItem.ticket_id)
            .join(parent, parent.id == child.parent_ticket_id)
            .where(parent.loop_config.is_not(None))
            .group_by(*cols)
        )
        rows = (await db.execute(stmt)).all()
        m.REVIEW_LOOP_ITEMS.clear()
        for status in m.REVIEW_LOOP_ITEM_STATUSES:
            m.REVIEW_LOOP_ITEMS.set(0.0, status=status)
        for row in rows:
            status = m.clamp_label(row[0], m.REVIEW_LOOP_ITEM_STATUSES)
            if tenant:
                m.REVIEW_LOOP_ITEMS.inc(float(row[-1]), status=status, project_id=row[1])
            else:
                m.REVIEW_LOOP_ITEMS.inc(float(row[-1]), status=status)
    except Exception:
        _swallow("review loop gauge")
        await _safe_rollback(db)


def m_count() -> Any:
    from sqlalchemy import func

    return func.count()


async def _safe_rollback(db: Any) -> None:
    try:
        await db.rollback()
    except Exception:
        _swallow("gauge rollback")
