"""GitHub and GitLab webhook handlers for PR/MR events."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import re
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, HTTPException, Request
from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError
from sessionfs.server.db.models import (
    GitHubInstallation,
    GitHubWebhookDelivery,
    GitLabSettings,
    PRComment,
    Session,
)
from sessionfs.server.github_app import (
    GITHUB_APP_ID,
    GITHUB_WEBHOOK_SECRET,
    find_marked_comment,
    get_installation_token,
    normalize_git_remote,
    post_or_update_comment,
)
from sessionfs.server.pr_comment import build_pr_comment

logger = logging.getLogger("sessionfs.webhooks")
router = APIRouter(prefix="/webhooks", tags=["webhooks"])


# Explicit local-development escape hatch. Unsigned GitHub webhooks are
# accepted ONLY when this is set to a true value; there is no implicit
# "no secret configured means dev mode" any more.
_ALLOW_UNSIGNED_ENV = "SFS_GITHUB_WEBHOOK_ALLOW_UNSIGNED"
_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})

# Log the misconfiguration once per process, not once per delivery.
_missing_secret_logged = False
_unsigned_opt_in_logged = False


def _unsigned_webhooks_allowed() -> bool:
    return os.environ.get(_ALLOW_UNSIGNED_ENV, "").strip().lower() in _TRUE_VALUES


def _verify_signature(body: bytes, signature: str | None) -> None:
    """Verify GitHub webhook HMAC-SHA256 signature. Fails closed.

    With no SFS_GITHUB_WEBHOOK_SECRET configured, every delivery is refused
    with 503 before the payload is parsed, so a forged installation or PR
    event can never create, delete or comment on anything. Local development
    can opt in to unsigned deliveries with SFS_GITHUB_WEBHOOK_ALLOW_UNSIGNED=1.
    """
    global _missing_secret_logged, _unsigned_opt_in_logged
    if not GITHUB_WEBHOOK_SECRET:
        if _unsigned_webhooks_allowed():
            if not _unsigned_opt_in_logged:
                logger.warning(
                    "Accepting UNSIGNED GitHub webhooks because %s is set. "
                    "Never enable this outside local development.",
                    _ALLOW_UNSIGNED_ENV,
                )
                _unsigned_opt_in_logged = True
            return
        if not _missing_secret_logged:
            logger.error(
                "Refusing GitHub webhooks: SFS_GITHUB_WEBHOOK_SECRET is not set. "
                "Configure the App's webhook secret (or set %s=1 for local "
                "development only).",
                _ALLOW_UNSIGNED_ENV,
            )
            _missing_secret_logged = True
        raise HTTPException(
            status_code=503, detail="GitHub webhooks are not configured on this server"
        )
    if not signature:
        raise HTTPException(status_code=401, detail="Missing webhook signature")
    expected = "sha256=" + hmac.new(
        GITHUB_WEBHOOK_SECRET.encode(), body, hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(expected, signature):
        raise HTTPException(status_code=401, detail="Invalid webhook signature")


def _signed_sender_id(payload: dict) -> int | None:
    """GitHub user id of the webhook sender, only when signatures are checked.

    Without a webhook secret anyone can post a forged payload, so the sender
    must not be recorded (it feeds the installer admin signal of the
    installation claim flow).
    """
    if not GITHUB_WEBHOOK_SECRET:
        return None
    sender = payload.get("sender")
    if not isinstance(sender, dict):
        return None
    sender_id = sender.get("id")
    if isinstance(sender_id, bool) or not isinstance(sender_id, int):
        return None
    return sender_id


async def _upsert_installation_metadata(
    db,
    *,
    installation_id: int,
    account_login: str | None,
    account_type: str | None,
    account_id: int | None,
    installer_github_user_id: int | None,
) -> None:
    """Record installation metadata in ONE atomic INSERT ... ON CONFLICT.

    The claim callback may create the row at the same moment (it learns the
    installation from GitHub's user-token API). A select-then-insert would
    lose this event's metadata to that race, so the database merges it:
    new non-null values win, nulls keep what is stored. user_id and the
    claim columns are never touched (claims come only from the OAuth flow).
    """
    from sqlalchemy import func

    dialect = db.bind.dialect.name if db.bind is not None else ""
    if dialect == "postgresql":
        from sqlalchemy.dialects.postgresql import insert as dialect_insert
    else:
        from sqlalchemy.dialects.sqlite import insert as dialect_insert

    table = GitHubInstallation.__table__
    stmt = dialect_insert(table).values(
        id=installation_id,
        account_login=account_login,
        account_type=account_type,
        account_id=account_id,
        installer_github_user_id=installer_github_user_id,
        auto_comment=True,
        include_trust_score=True,
        include_session_links=True,
    )
    excluded = stmt.excluded
    stmt = stmt.on_conflict_do_update(
        index_elements=[table.c.id],
        set_={
            "account_login": func.coalesce(excluded.account_login, table.c.account_login),
            "account_type": func.coalesce(excluded.account_type, table.c.account_type),
            "account_id": func.coalesce(excluded.account_id, table.c.account_id),
            "installer_github_user_id": func.coalesce(
                excluded.installer_github_user_id, table.c.installer_github_user_id
            ),
        },
    )
    await db.execute(stmt)
    await db.commit()


# Stored payloads (only needed to retry failed work) are dropped after
# this long ...
WEBHOOK_PAYLOAD_RETENTION = timedelta(days=30)
# ... but the fingerprint tombstone is kept much longer: a validly signed
# body never expires on its own, so forgetting the fingerprint would let a
# captured delivery be replayed. Rotating the webhook secret (at least
# yearly) invalidates every signature older than the rotation.
WEBHOOK_TOMBSTONE_RETENTION = timedelta(days=400)
# Backwards-compatible name.
WEBHOOK_DELIVERY_RETENTION = WEBHOOK_PAYLOAD_RETENTION
# A delivery whose worker has not finished within this window may be taken
# over (the worker is assumed dead).
WEBHOOK_PROCESSING_STALE_AFTER = timedelta(minutes=10)
# Automatic retries of failed work (the server has no scheduler: retries run
# opportunistically after each webhook delivery and from the admin sweeper).
# Backoff after attempt N is _RETRY_BACKOFF[N-1].
WEBHOOK_MAX_ATTEMPTS = 5
_RETRY_BACKOFF = (
    timedelta(minutes=1),
    timedelta(minutes=5),
    timedelta(minutes=15),
    timedelta(hours=1),
)
# Retries started per incoming delivery (keeps the webhook response fast).
_OPPORTUNISTIC_RETRY_LIMIT = 2
# X-GitHub-Delivery is informational only (it is not signed).
_DELIVERY_ID_RE = re.compile(r"[A-Za-z0-9-]{1,64}")
_PR_ACTIONS = frozenset({"opened", "edited", "synchronize", "reopened", "closed"})
# The session-link comment pipeline only reacts to new or updated code.
_PR_COMMENT_ACTIONS = frozenset({"opened", "synchronize", "reopened"})
_INSTALLATION_ACTIONS = frozenset({"created", "deleted", "suspend", "unsuspend"})
_RELEASE_ACTIONS = frozenset({"published"})


def _delivery_fingerprint(body: bytes) -> str:
    """Replay key over exactly what the HMAC authenticates: the raw body.

    Nothing unsigned (X-GitHub-Event, X-GitHub-Delivery) takes part, so a
    captured body re-sent under another event header is the SAME delivery.
    """
    return hashlib.sha256(body).hexdigest()


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _valid_pr_event(payload: dict) -> bool:
    pr = payload.get("pull_request")
    repo = payload.get("repository")
    inst = payload.get("installation")
    return (
        payload.get("action") in _PR_ACTIONS
        and isinstance(pr, dict)
        and isinstance(pr.get("head"), dict)
        and isinstance(pr["head"].get("ref"), str)
        and _is_int(pr.get("number"))
        and isinstance(repo, dict)
        and isinstance(repo.get("full_name"), str)
        and isinstance(repo.get("clone_url"), str)
        and isinstance(inst, dict)
        and _is_int(inst.get("id"))
    )


def _valid_installation_event(payload: dict) -> bool:
    """The full installation-event shape, not just action + installation id.

    Other App events (e.g. repository.deleted) also carry ``action`` and an
    ``installation`` reference, but only installation events carry the full
    installation object (account, app_id, target_type) and no top-level
    ``repository``. When the App id is configured it must match.
    """
    inst = payload.get("installation")
    if payload.get("action") not in _INSTALLATION_ACTIONS or "repository" in payload:
        return False
    if not isinstance(inst, dict) or not _is_int(inst.get("id")):
        return False
    account = inst.get("account")
    if not (
        isinstance(account, dict)
        and isinstance(account.get("login"), str)
        and isinstance(account.get("type"), str)
        and _is_int(account.get("id"))
    ):
        return False
    if not _is_int(inst.get("app_id")) or not isinstance(inst.get("target_type"), str):
        return False
    if GITHUB_APP_ID.isdigit() and inst["app_id"] != int(GITHUB_APP_ID):
        return False
    return True


def _valid_release_event(payload: dict) -> bool:
    """A ``release.published`` event whose repo can map to a project."""
    release = payload.get("release")
    repo = payload.get("repository")
    return (
        payload.get("action") in _RELEASE_ACTIONS
        and isinstance(release, dict)
        and isinstance(release.get("tag_name"), str)
        and isinstance(repo, dict)
        and isinstance(repo.get("full_name"), str)
        and isinstance(repo.get("clone_url"), str)
    )


def _stored_payload(body: bytes) -> str | None:
    try:
        return body.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _retry_eligible(row: GitHubWebhookDelivery, now: datetime) -> bool:
    if row.status == "completed" or row.attempts >= WEBHOOK_MAX_ATTEMPTS:
        return False
    if row.status == "failed":
        return row.next_attempt_at is None or _aware(row.next_attempt_at) <= now
    return _aware(row.claimed_at) < now - WEBHOOK_PROCESSING_STALE_AFTER  # stale processing


async def _take_over(
    db, fingerprint: str, generation: int, status: str, now: datetime, **extra
) -> int | None:
    """Fenced takeover of an existing delivery. Compare-and-set on the
    monotonic lease_generation AND the observed status, never touching a
    completed row; returns the new generation (the lease) or None."""
    new_generation = generation + 1
    res = await db.execute(
        update(GitHubWebhookDelivery)
        .where(
            GitHubWebhookDelivery.fingerprint == fingerprint,
            GitHubWebhookDelivery.lease_generation == generation,
            GitHubWebhookDelivery.status == status,
            GitHubWebhookDelivery.status != "completed",
        )
        .values(status="processing", claimed_at=now, lease_generation=new_generation, **extra)
        .execution_options(synchronize_session=False)
    )
    if res.rowcount != 1:  # type: ignore[attr-defined]
        await db.rollback()
        return None
    await db.commit()
    return new_generation


async def _claim_delivery(
    fingerprint: str, delivery_id: str | None, event: str | None, payload: str | None
) -> tuple[str, int]:
    """Claim a signed payload for processing.

    Returns (outcome, lease). outcome is 'claimed' (this request must do the
    work), 'duplicate' (already completed: permanent no-op), 'in_progress'
    (another worker holds a fresh claim) or 'deferred' (a failed delivery
    whose backoff has not elapsed or that reached WEBHOOK_MAX_ATTEMPTS: a
    redelivery cannot bypass either; an admin can force a retry). lease is
    the delivery's lease_generation, which only ever increases; outcomes are
    recorded only for the current generation.
    """
    from sessionfs.server.db.engine import _session_factory

    if _session_factory is None:
        raise HTTPException(status_code=503, detail="Database not initialized")
    now = datetime.now(timezone.utc)
    async with _session_factory() as db:
        db.add(GitHubWebhookDelivery(
            fingerprint=fingerprint,
            delivery_id=delivery_id,
            event=event,
            status="processing",
            attempts=1,
            lease_generation=1,
            received_at=now,
            claimed_at=now,
            payload=payload,
        ))
        try:
            await db.commit()
            result = ("claimed", 1)
        except IntegrityError:
            await db.rollback()
            row = await db.get(GitHubWebhookDelivery, fingerprint, populate_existing=True)
            if row is None:
                await db.rollback()
                return ("in_progress", 0)
            seen_status, seen_generation = row.status, row.lease_generation
            if seen_status == "completed":
                await db.rollback()
                return ("duplicate", seen_generation)
            if not _retry_eligible(row, now):
                await db.rollback()
                return ("deferred" if seen_status == "failed" else "in_progress", seen_generation)
            seen_attempts = row.attempts
            generation = await _take_over(
                db, fingerprint, seen_generation, seen_status, now,
                attempts=seen_attempts + 1,
                delivery_id=delivery_id,
                payload=func.coalesce(GitHubWebhookDelivery.payload, payload),
            )
            if generation is None:
                return ("in_progress", 0)
            result = ("claimed", generation)
        await prune_deliveries(db, now=now)
    return result


async def prune_deliveries(
    db,
    *,
    now: datetime | None = None,
    payload_retention: timedelta = WEBHOOK_PAYLOAD_RETENTION,
    tombstone_retention: timedelta = WEBHOOK_TOMBSTONE_RETENTION,
) -> tuple[int, int]:
    """Drop old payloads (keeping the fingerprint tombstone) and, much
    later, the tombstones themselves. Returns (payloads, tombstones).
    Tombstones never expire before payloads."""
    now = now or datetime.now(timezone.utc)
    tombstone_retention = max(tombstone_retention, payload_retention)
    payloads = await db.execute(
        update(GitHubWebhookDelivery)
        .where(
            GitHubWebhookDelivery.received_at < now - payload_retention,
            GitHubWebhookDelivery.payload.is_not(None),
        )
        .values(payload=None)
        .execution_options(synchronize_session=False)
    )
    tombstones = await db.execute(
        delete(GitHubWebhookDelivery)
        .where(GitHubWebhookDelivery.received_at < now - tombstone_retention)
        .execution_options(synchronize_session=False)
    )
    await db.commit()
    return (payloads.rowcount or 0, tombstones.rowcount or 0)


async def force_retry_delivery(fingerprint: str) -> str:
    """Admin override for a delivery that hit its attempt limit or backoff.

    Resets the retry counter (attempts) but ADVANCES the lease generation,
    so a stale worker can never finalize the new attempt; a completed row is
    never reopened. Retries now from the stored payload. Returns
    'not_found', 'completed' (nothing to do), 'no_payload' (payload already
    purged: GitHub may now redeliver), 'busy', or 'succeeded' / 'failed'.
    """
    from sessionfs.server.db.engine import _session_factory

    if _session_factory is None:
        return "not_found"
    now = datetime.now(timezone.utc)
    async with _session_factory() as db:
        row = await db.get(GitHubWebhookDelivery, fingerprint, populate_existing=True)
        if row is None:
            await db.rollback()
            return "not_found"
        if row.status == "completed":
            await db.rollback()
            return "completed"
        if row.status == "processing" and _aware(row.claimed_at) > now - WEBHOOK_PROCESSING_STALE_AFTER:
            await db.rollback()
            return "busy"
        event, raw = row.event, row.payload
        if raw is None:
            res = await db.execute(
                update(GitHubWebhookDelivery)
                .where(
                    GitHubWebhookDelivery.fingerprint == fingerprint,
                    GitHubWebhookDelivery.lease_generation == row.lease_generation,
                    GitHubWebhookDelivery.status == row.status,
                    GitHubWebhookDelivery.status != "completed",
                )
                .values(
                    status="failed", attempts=0, next_attempt_at=None,
                    lease_generation=row.lease_generation + 1,
                )
                .execution_options(synchronize_session=False)
            )
            if res.rowcount != 1:  # type: ignore[attr-defined]
                await db.rollback()
                return "busy"
            await db.commit()
            return "no_payload"
        generation = await _take_over(
            db, fingerprint, row.lease_generation, row.status, now, attempts=1, next_attempt_at=None
        )
    if generation is None:
        return "busy"
    payload = _parse_payload(raw)
    if payload is None:
        await _finish_delivery(fingerprint, False, generation)
        return "failed"
    ok = await _process_delivery(event, payload, fingerprint, generation)
    return "succeeded" if ok else "failed"


async def _finish_delivery(fingerprint: str, ok: bool, lease: int) -> bool:
    """Record the outcome of the attempt holding ``lease`` (generation).
    Returns False (and changes nothing) when a newer attempt has taken the
    delivery over."""
    from sessionfs.server.db.engine import _session_factory

    if _session_factory is None:
        return False
    now = datetime.now(timezone.utc)
    try:
        async with _session_factory() as db:
            row = await db.get(GitHubWebhookDelivery, fingerprint, populate_existing=True)
            if row is None or row.lease_generation != lease or row.status != "processing":
                await db.rollback()
                return False
            backoff = _RETRY_BACKOFF[min(max(row.attempts, 1), len(_RETRY_BACKOFF)) - 1]
            values: dict = (
                {"status": "completed", "completed_at": now, "next_attempt_at": None, "payload": None}
                if ok
                else {"status": "failed", "completed_at": None, "next_attempt_at": now + backoff}
            )
            res = await db.execute(
                update(GitHubWebhookDelivery)
                .where(
                    GitHubWebhookDelivery.fingerprint == fingerprint,
                    GitHubWebhookDelivery.lease_generation == lease,
                    GitHubWebhookDelivery.status == "processing",
                )
                .values(**values)
                .execution_options(synchronize_session=False)
            )
            await db.commit()
            return res.rowcount == 1  # type: ignore[attr-defined]
    except Exception:
        logger.warning("Failed to record GitHub delivery outcome %s", fingerprint[:12], exc_info=True)
        return False


def _parse_payload(raw: str | None) -> dict | None:
    try:
        payload = json.loads(raw or "")
    except ValueError:
        return None
    return payload if isinstance(payload, dict) else None


def _canonical_event(payload: dict) -> str | None:
    """The supported event a signed body IS, from its validated shape.

    The shapes are mutually exclusive (a PR event requires a top-level
    repository, an installation event forbids one), so the unsigned
    X-GitHub-Event header never decides which handler runs.
    """
    if _valid_pr_event(payload):
        return "pull_request"
    if _valid_installation_event(payload):
        return "installation"
    if _valid_release_event(payload):
        return "release"
    return None


async def _process_delivery(
    event: str | None, payload: dict, fingerprint: str, lease: int
) -> bool:
    """Do the work for one claimed delivery and record its outcome.

    Dispatches on the payload's canonical event (``event``, the unsigned
    header, is informational only), so a supported body is never completed
    without its handler running. The PR handler is fenced on the delivery
    generation.
    """
    canonical = _canonical_event(payload)
    if event and canonical and event != canonical:
        logger.info("GitHub webhook header %r disagrees with payload shape %r", event, canonical)
    ok = True
    if canonical == "pull_request":
        if payload.get("action") in _PR_COMMENT_ACTIONS:
            ok = await _handle_pr_event(payload, fence=(fingerprint, lease))
        # The PR ingest (upsert + auto-link + merge-check refresh) runs beside
        # the session-comment pipeline for the same delivery.
        ok = (await _handle_pr_ingest(payload, fence=(fingerprint, lease))) and ok
    elif canonical == "installation":
        try:
            await _apply_installation_event(payload)
        except Exception:
            logger.warning("Failed to apply GitHub installation event", exc_info=True)
            ok = False
    elif canonical == "release":
        try:
            await _handle_release_event(payload, fence=(fingerprint, lease))
        except Exception:
            logger.warning("Failed to apply GitHub release event", exc_info=True)
            ok = False
    await _finish_delivery(fingerprint, ok, lease)
    return ok


async def retry_due_deliveries(limit: int) -> dict[str, int]:
    """Retry failed (backoff elapsed) and stale processing deliveries from
    their stored payloads, each through a fenced takeover so concurrent
    sweepers never run the same delivery twice. Deliveries past
    WEBHOOK_MAX_ATTEMPTS are left for an admin forced retry."""
    from sessionfs.server.db.engine import _session_factory

    stats = {"retried": 0, "succeeded": 0, "failed": 0}
    if _session_factory is None or limit <= 0:
        return stats
    now = datetime.now(timezone.utc)
    work: list[tuple[str, str | None, str, int]] = []
    async with _session_factory() as db:
        due = (
            await db.execute(
                select(GitHubWebhookDelivery)
                .where(
                    GitHubWebhookDelivery.attempts < WEBHOOK_MAX_ATTEMPTS,
                    GitHubWebhookDelivery.payload.is_not(None),
                    GitHubWebhookDelivery.status != "completed",
                )
                .order_by(GitHubWebhookDelivery.received_at)
                .limit(limit * 4)
            )
        ).scalars().all()
        # Plain snapshots: ORM rows expire on rollback.
        candidates = [
            (row.fingerprint, row.lease_generation, row.status, row.attempts, row.event, row.payload)
            for row in due
            if _retry_eligible(row, now)
        ][:limit]
        await db.rollback()
        for fp, generation_seen, status, attempts, event, raw in candidates:
            generation = await _take_over(db, fp, generation_seen, status, now, attempts=attempts + 1)
            if generation is not None:
                work.append((fp, event, raw or "", generation))
    for fingerprint, event, raw, generation in work:
        stats["retried"] += 1
        payload = _parse_payload(raw)
        if payload is None:
            await _finish_delivery(fingerprint, False, generation)
            stats["failed"] += 1
            continue
        if await _process_delivery(event, payload, fingerprint, generation):
            stats["succeeded"] += 1
        else:
            stats["failed"] += 1
    return stats


async def _apply_installation_event(payload: dict) -> None:
    """installation.created / .deleted / .suspend / .unsuspend (shape
    already validated).
    Raises on failure (no swallowing)."""
    from sessionfs.server.db.engine import _session_factory

    action = payload["action"]
    installation = payload["installation"]
    installation_id = installation["id"]
    account = installation["account"]
    if _session_factory is None:
        raise RuntimeError("database not initialized")

    if action == "created":
        logger.info("GitHub App installed: %s (id=%s)", account.get("login"), installation_id)
        async with _session_factory() as db:
            await _upsert_installation_metadata(
                db,
                installation_id=installation_id,
                account_login=account["login"],
                account_type=account["type"],
                account_id=account["id"],
                installer_github_user_id=_signed_sender_id(payload),
            )
    elif action == "deleted":
        logger.info("GitHub App uninstalled: %s (id=%s)", account.get("login"), installation_id)
        async with _session_factory() as db:
            from sessionfs.server.services.publishers import handle_installation_event

            # Marketplace publisher badges backed by this installation drop
            # immediately (§4.3), recorded durably under the installation
            # advisory lock - only for a SIGNED delivery; unsigned
            # (development) deliveries leave it to the sweeper's live App API
            # check. Lock order: installation advisory lock -> publisher rows
            # (inside the handler) -> installation row -> pr_comments.
            if GITHUB_WEBHOOK_SECRET:
                await handle_installation_event(db, int(installation_id), "deleted")
            # Fence pending comment work for the vanished installation in
            # the same transaction, so the sweeper never tries to publish
            # against an installation that no longer exists. Lock the
            # installation row first (lock order: installation ->
            # pr_comments, as everywhere else).
            await db.execute(
                select(GitHubInstallation.id)
                .where(GitHubInstallation.id == installation_id)
                .with_for_update()
            )
            await cancel_pending_comment_work(db, installation_id)
            await db.execute(
                delete(GitHubInstallation).where(GitHubInstallation.id == installation_id)
            )
            await db.commit()
    elif action in ("suspend", "unsuspend"):
        logger.info("GitHub App %s: %s (id=%s)", action, account.get("login"), installation_id)
        if GITHUB_WEBHOOK_SECRET:
            async with _session_factory() as db:
                from sessionfs.server.services.publishers import handle_installation_event

                # Durable state + revocation under the installation lock
                # (unsuspend clears the state; badges revoked by a
                # suspension are not restored automatically).
                await handle_installation_event(db, int(installation_id), action)
                await db.commit()


_background_tasks: set[asyncio.Task] = set()


def _spawn(coro) -> None:
    task = asyncio.create_task(coro)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


@router.post("/github")
async def github_webhook(request: Request):
    """Receive GitHub webhook events."""
    body = await request.body()
    signature = request.headers.get("X-Hub-Signature-256")
    _verify_signature(body, signature)

    event = request.headers.get("X-GitHub-Event")
    event = event[:64] if event else None
    raw_delivery = request.headers.get("X-GitHub-Delivery", "")
    delivery_id = raw_delivery if _DELIVERY_ID_RE.fullmatch(raw_delivery) else None

    # Replay protection over the signed body only: each distinct payload is
    # completed at most once, whatever unsigned headers accompany it.
    fingerprint = _delivery_fingerprint(body)
    claim, lease = await _claim_delivery(fingerprint, delivery_id, event, _stored_payload(body))
    if claim == "duplicate":
        logger.info("Ignoring replayed GitHub payload (delivery %s)", delivery_id)
        return {"status": "duplicate"}
    if claim == "in_progress":
        return {"status": "in_progress"}
    if claim == "deferred":
        return {"status": "deferred"}

    payload = _parse_payload(_stored_payload(body))
    if payload is None:
        await _finish_delivery(fingerprint, False, lease)
        raise HTTPException(status_code=400, detail="Invalid JSON payload")

    if _canonical_event(payload) == "pull_request":
        # Run in background -- don't block the webhook response. The task
        # records completion, or failure for a later retry, and then gives a
        # couple of older failed deliveries another chance.
        _spawn(_process_then_retry(event, payload, fingerprint, lease))
        return {"status": "ok"}

    ok = await _process_delivery(event, payload, fingerprint, lease)
    # Opportunistic retries of earlier failed work (no server scheduler),
    # started only after this delivery's own work is recorded.
    _spawn(retry_due_deliveries(_OPPORTUNISTIC_RETRY_LIMIT))
    if not ok:
        raise HTTPException(status_code=500, detail="Failed to apply webhook event")
    return {"status": "ok"}


async def _process_then_retry(event: str | None, payload: dict, fingerprint: str, lease: int) -> None:
    await _process_delivery(event, payload, fingerprint, lease)
    await retry_due_deliveries(_OPPORTUNISTIC_RETRY_LIMIT)


# comment_id placeholder while a worker is creating the comment.
_PENDING_COMMENT_ID = 0
_REPO_FULL_NAME_RE = re.compile(r"[A-Za-z0-9._-]{1,100}/[A-Za-z0-9._-]{1,100}")

PUBLISH_LEASE_TTL = timedelta(seconds=60)
# Every GitHub call made while creating or editing a comment is bounded well
# below the lease TTL, and the lease is renewed before each call, so a lease
# can only be taken over after its holder's last call has given up.
PUBLISH_CALL_TIMEOUT = 20.0


class _Superseded(Exception):
    """The delivery this worker is processing has been taken over by a newer
    attempt; the stale worker must not leave any durable side effect."""


def _comment_marker(installation_id: int, repo: str, pr_number: int) -> str:
    """Hidden, stable marker that identifies SessionFS's comment on a PR,
    so a comment created before a crash (or a lost response) is adopted
    instead of duplicated."""
    return f"<!-- sessionfs-pr:{installation_id}:{repo}:{pr_number} -->"


async def _github_call(awaitable):
    return await asyncio.wait_for(awaitable, timeout=PUBLISH_CALL_TIMEOUT)


async def _hold_delivery_fence(db, fence: tuple[str, int] | None) -> None:
    """Inside the caller's transaction: prove this worker still holds the
    delivery's current generation and LOCK that row until commit (a no-op
    UPDATE: PostgreSQL row lock / SQLite write lock), so a takeover cannot
    commit between this check and the caller's writes."""
    if fence is None:
        return
    fingerprint, generation = fence
    res = await db.execute(
        update(GitHubWebhookDelivery)
        .where(
            GitHubWebhookDelivery.fingerprint == fingerprint,
            GitHubWebhookDelivery.lease_generation == generation,
            GitHubWebhookDelivery.status == "processing",
        )
        .values(claimed_at=GitHubWebhookDelivery.claimed_at)
        .execution_options(synchronize_session=False)
    )
    if res.rowcount != 1:  # type: ignore[attr-defined]
        await db.rollback()
        raise _Superseded()


async def _record_desired_comment(
    db,
    installation_id: int,
    repo: str,
    pr_number: int,
    body: str,
    session_ids: str,
    fence: tuple[str, int] | None = None,
    *,
    owner_user_id: str,
) -> tuple[str, str, int | None, str | None] | None:
    """Record this event's desired comment, then decide who publishes.

    One transaction, fenced on the delivery generation (``fence``): a
    superseded worker raises _Superseded and writes nothing. Every event
    stores its body and bumps body_version, so content is never lost.

    ``owner_user_id`` is the installation owner whose sessions ``body``
    was built from. In the same transaction the installation row is
    re-read under a lock (lock order: user -> installation -> pr_comments,
    as in _bind_installation); if the installation changed hands or comments
    were turned off while the body was being built and the token fetched,
    the work is abandoned as superseded and nothing is recorded.
    Returns:
      ('create', row_id, None, lease)  caller holds the comment lease and
                                       creates (or adopts) the comment
      ('edit', row_id, comment_id, None)  caller publishes the newest version
      None                             another worker holds the lease and
                                       will publish this version too
    """
    now = datetime.now(timezone.utc)
    await _hold_delivery_fence(db, fence)
    current = (await db.execute(
        select(GitHubInstallation.user_id, GitHubInstallation.auto_comment)
        .where(GitHubInstallation.id == installation_id)
        .with_for_update()
    )).first()
    if current is None or current.user_id != owner_user_id or not current.auto_comment:
        await db.rollback()
        raise _Superseded()
    dialect = db.bind.dialect.name if db.bind is not None else ""
    if dialect == "postgresql":
        from sqlalchemy.dialects.postgresql import insert as dialect_insert
    else:
        from sqlalchemy.dialects.sqlite import insert as dialect_insert

    lease = uuid.uuid4().hex
    table = PRComment.__table__
    inserted = await db.execute(
        dialect_insert(table)
        .values(
            id=str(uuid.uuid4()),
            installation_id=installation_id,
            repo_full_name=repo,
            pr_number=pr_number,
            comment_id=_PENDING_COMMENT_ID,
            session_ids=session_ids,
            desired_body=body,
            body_version=1,
            published_version=0,
            created_at=now,
            updated_at=now,
            # The creator holds the same lease editing uses.
            publish_lease=lease,
            publish_lease_until=now + PUBLISH_LEASE_TTL,
            owner_user_id=owner_user_id,
        )
        .on_conflict_do_nothing(
            index_elements=[table.c.installation_id, table.c.repo_full_name, table.c.pr_number]
        )
    )
    created = inserted.rowcount == 1  # type: ignore[attr-defined]
    key = (
        (PRComment.installation_id == installation_id)
        & (PRComment.repo_full_name == repo)
        & (PRComment.pr_number == pr_number)
    )
    if not created:
        await db.execute(
            update(PRComment)
            .where(key)
            .values(
                desired_body=body,
                session_ids=session_ids,
                body_version=PRComment.body_version + 1,
                owner_user_id=owner_user_id,
            )
            .execution_options(synchronize_session=False)
        )
    row = (
        await db.execute(select(PRComment).where(key).execution_options(populate_existing=True))
    ).scalar_one()
    row_id, comment_id = row.id, row.comment_id
    if created:
        await db.commit()
        return ("create", row_id, None, lease)
    if comment_id != _PENDING_COMMENT_ID:
        await db.commit()
        return ("edit", row_id, comment_id, None)
    # Creation pending: take the lease if its holder is gone.
    taken = await db.execute(
        update(PRComment)
        .where(
            PRComment.id == row_id,
            PRComment.publish_lease.is_(None) | (PRComment.publish_lease_until < now),
        )
        .values(publish_lease=lease, publish_lease_until=now + PUBLISH_LEASE_TTL)
        .execution_options(synchronize_session=False)
    )
    await db.commit()
    if taken.rowcount != 1:  # type: ignore[attr-defined]
        return None
    return ("create", row_id, None, lease)


async def _try_publish_lease(db, row_id: str) -> str | None:
    """Atomically take the comment's lease (free or expired). Committed
    immediately: nothing is held across GitHub calls."""
    now = datetime.now(timezone.utc)
    token = uuid.uuid4().hex
    res = await db.execute(
        update(PRComment)
        .where(
            PRComment.id == row_id,
            PRComment.publish_lease.is_(None) | (PRComment.publish_lease_until < now),
        )
        .values(publish_lease=token, publish_lease_until=now + PUBLISH_LEASE_TTL)
        .execution_options(synchronize_session=False)
    )
    if res.rowcount != 1:  # type: ignore[attr-defined]
        await db.rollback()
        return None
    await db.commit()
    return token


async def _renew_publish_lease(db, row_id: str, lease: str) -> bool:
    res = await db.execute(
        update(PRComment)
        .where(PRComment.id == row_id, PRComment.publish_lease == lease)
        .values(publish_lease_until=datetime.now(timezone.utc) + PUBLISH_LEASE_TTL)
        .execution_options(synchronize_session=False)
    )
    await db.commit()
    return res.rowcount == 1  # type: ignore[attr-defined]


async def _release_lease(db, row_id: str, lease: str) -> None:
    await db.rollback()
    await db.execute(
        update(PRComment)
        .where(PRComment.id == row_id, PRComment.publish_lease == lease)
        .values(publish_lease=None, publish_lease_until=None)
        .execution_options(synchronize_session=False)
    )
    await db.commit()


async def _release_if_clean(db, row_id: str, lease: str) -> bool:
    """Release only if nothing is pending; otherwise the holder keeps going.
    This single atomic statement is what guarantees no version is ever
    stranded: an event that bumped the version before it either keeps the
    holder publishing, or happened after the release and finds the lease
    free (and publishes itself)."""
    res = await db.execute(
        update(PRComment)
        .where(
            PRComment.id == row_id,
            PRComment.publish_lease == lease,
            PRComment.published_version >= PRComment.body_version,
        )
        .values(publish_lease=None, publish_lease_until=None)
        .execution_options(synchronize_session=False)
    )
    await db.commit()
    return res.rowcount == 1  # type: ignore[attr-defined]


async def _mark_dirty(db, row_id: str, lease: str | None) -> None:
    """GitHub may now show something other than the newest body (a call
    failed, or the lease was lost while a call was in flight): force a
    republish of the newest body by the next holder or the sweeper."""
    await db.rollback()
    await db.execute(
        update(PRComment)
        .where(PRComment.id == row_id)
        .values(published_version=0)
        .execution_options(synchronize_session=False)
    )
    if lease is not None:
        await db.execute(
            update(PRComment)
            .where(PRComment.id == row_id, PRComment.publish_lease == lease)
            .values(publish_lease=None, publish_lease_until=None)
            .execution_options(synchronize_session=False)
        )
    await db.commit()


async def _installation_allows_comments(db, installation_id: int) -> bool:
    """Revalidate an installation's current opt-out/detached state
    immediately before leasing comment work or writing to GitHub
    (tk_b6c5e184ba8d4aa1): the installation must still exist and still
    have auto_comment on. Detachment always flips auto_comment off, so
    this one check covers both. GitLab rows (installation_id 0) are
    exempt — they are not published through this pipeline."""
    if installation_id == 0:
        return True
    row = (await db.execute(
        select(GitHubInstallation.auto_comment).where(
            GitHubInstallation.id == installation_id
        )
    )).first()
    await db.rollback()
    return row is not None and bool(row.auto_comment)


async def _installation_owner_is(db, installation_id: int, owner_user_id: str | None) -> bool:
    """True when the installation is still owned by the user a comment body
    was built for. A body recorded for a previous owner (or with no owner
    recorded) must never be published under a new one."""
    if installation_id == 0:
        return True
    if owner_user_id is None:
        await db.rollback()
        return False
    current = (await db.execute(
        select(GitHubInstallation.user_id).where(GitHubInstallation.id == installation_id)
    )).scalar_one_or_none()
    await db.rollback()
    return current == owner_user_id


async def _drop_stale_owner_body(db, row_id: str, owner_user_id: str | None) -> bool:
    """Fence one comment row whose body belongs to a previous installation
    owner: mark it published (nothing left to post) and drop its lease.
    Conditional on the owner still being the stale one, so a body recorded
    meanwhile for the current owner is kept (returns False then)."""
    owner_match = (
        PRComment.owner_user_id.is_(None)
        if owner_user_id is None
        else PRComment.owner_user_id == owner_user_id
    )
    res = await db.execute(
        update(PRComment)
        .where(PRComment.id == row_id, owner_match)
        .values(
            published_version=PRComment.body_version,
            publish_lease=None,
            publish_lease_until=None,
        )
        .execution_options(synchronize_session=False)
    )
    await db.commit()
    return res.rowcount == 1  # type: ignore[attr-defined]


async def cancel_pending_comment_work(db, installation_id: int) -> int:
    """Fence pending comment work for an installation being disabled,
    detached or deleted: every behind row is marked as published (so the
    sweeper and any future lease holder stop) and its publish lease is
    dropped. A publisher with a GitHub call already in flight loses the
    lease, fails its next renewal, and revalidates before any further
    write — so at most the one in-flight call can land after the flip."""
    res = await db.execute(
        update(PRComment)
        .where(
            PRComment.installation_id == installation_id,
            PRComment.published_version < PRComment.body_version,
        )
        .values(
            published_version=PRComment.body_version,
            publish_lease=None,
            publish_lease_until=None,
        )
        .execution_options(synchronize_session=False)
    )
    return res.rowcount or 0  # type: ignore[attr-defined,return-value]


async def _publish_with_lease(
    db, token: str, row_id: str, lease: str, repo: str, pr_number: int, comment_id: int
) -> bool:
    """Lease holder loop: publish until clean, then release-if-clean.

    Renews the lease before every (bounded) GitHub call, records
    published_version only while still holding it, and has no round cap.
    No database transaction is open during any GitHub call. If the lease is
    lost or a call fails, the row is marked dirty for the sweeper.
    """
    while True:
        if not await _renew_publish_lease(db, row_id, lease):
            await _mark_dirty(db, row_id, None)
            return False
        row = await db.get(PRComment, row_id, populate_existing=True)
        if row is None:
            await db.commit()
            return True
        version, body, published = row.body_version, row.desired_body, row.published_version
        installation_id = row.installation_id
        body_owner = row.owner_user_id
        await db.commit()
        if published >= version or not body:
            if await _release_if_clean(db, row_id, lease):
                return True
            continue
        # Revalidate before the external write: the installation may have
        # been disabled or detached since the work was leased.
        if not await _installation_allows_comments(db, installation_id):
            await cancel_pending_comment_work(db, installation_id)
            await db.commit()
            return True
        # ...or handed to a different owner than the one this body (built
        # from the owner's sessions) was recorded for.
        if not await _installation_owner_is(db, installation_id, body_owner):
            if await _drop_stale_owner_body(db, row_id, body_owner):
                return True
            continue  # a body for the current owner arrived meanwhile
        try:
            await _github_call(post_or_update_comment(token, repo, pr_number, body, comment_id))
        except Exception:
            await _mark_dirty(db, row_id, lease)
            raise
        res = await db.execute(
            update(PRComment)
            .where(PRComment.id == row_id, PRComment.publish_lease == lease)
            .values(published_version=version, updated_at=datetime.now(timezone.utc))
            .execution_options(synchronize_session=False)
        )
        await db.commit()
        if res.rowcount != 1:  # type: ignore[attr-defined]
            # Lease lost while our call was in flight: GitHub may now show
            # our (possibly older) body after someone else's newer one.
            await _mark_dirty(db, row_id, None)
            return False


async def _publish_newest(db, token: str, row_id: str, repo: str, pr_number: int, comment_id: int) -> bool:
    """Make the created comment show the newest desired version. Returns
    True if it is clean on exit (or a live lease holder owns it)."""
    lease = await _try_publish_lease(db, row_id)
    if lease is None:
        return True  # a live holder will publish (it only leaves when clean)
    return await _publish_with_lease(db, token, row_id, lease, repo, pr_number, comment_id)


async def _create_or_adopt_comment(
    db, token: str, row_id: str, lease: str, installation_id: int, repo: str, pr_number: int
) -> bool:
    """Create (or adopt) the PR comment while holding the comment lease.

    The lease is renewed before each bounded GitHub call and the result is
    recorded only while it is still held, so a reclaimed creation claim can
    never be finalized by its previous holder. A comment from an earlier
    attempt (crash, lost response) is adopted via the hidden marker.
    """
    marker = _comment_marker(installation_id, repo, pr_number)
    try:
        if not await _renew_publish_lease(db, row_id, lease):
            return False
        row = await db.get(PRComment, row_id, populate_existing=True)
        if row is None:
            await db.commit()
            return True
        version, body = row.body_version, row.desired_body or ""
        body_owner = row.owner_user_id
        await db.commit()  # no transaction stays open across the GitHub calls
        # Revalidate before the external writes: the installation may have
        # been disabled or detached since the work was leased...
        if not await _installation_allows_comments(db, installation_id):
            await cancel_pending_comment_work(db, installation_id)
            await db.commit()
            return True
        # ...or handed to a different owner than the body was built for.
        if not await _installation_owner_is(db, installation_id, body_owner):
            if not await _drop_stale_owner_body(db, row_id, body_owner):
                # A body for the current owner arrived meanwhile: let the
                # next event or the sweeper create the comment with it.
                await _release_lease(db, row_id, lease)
                return False
            return True
        existing = await _github_call(find_marked_comment(token, repo, pr_number, marker))
        if not await _renew_publish_lease(db, row_id, lease):
            return False
        comment_id = await _github_call(
            post_or_update_comment(token, repo, pr_number, body, existing)
        )
    except Exception:
        # Keep the desired content; free the lease so the next event or the
        # sweeper retries at once (and adopts anything that was created).
        await _release_lease(db, row_id, lease)
        raise
    res = await db.execute(
        update(PRComment)
        .where(PRComment.id == row_id, PRComment.publish_lease == lease)
        .values(
            comment_id=comment_id,
            published_version=version,
            updated_at=datetime.now(timezone.utc),
        )
        .execution_options(synchronize_session=False)
    )
    await db.commit()
    if res.rowcount != 1:  # type: ignore[attr-defined]
        logger.warning("Lost the comment lease on %s#%d while creating it", repo, pr_number)
        return False
    # Events that arrived while we were creating it.
    return await _publish_with_lease(db, token, row_id, lease, repo, pr_number, comment_id)


async def republish_dirty_comments(limit: int) -> dict[str, int]:
    """Sweeper: finish comments with no live lease that are behind
    (published_version < body_version) or whose creation never completed."""
    from sessionfs.server.db.engine import _session_factory

    stats = {"comments_republished": 0, "comment_republish_failed": 0}
    if _session_factory is None or limit <= 0:
        return stats
    now = datetime.now(timezone.utc)
    async with _session_factory() as db:
        rows = (
            await db.execute(
                select(
                    PRComment.id, PRComment.installation_id, PRComment.repo_full_name,
                    PRComment.pr_number, PRComment.comment_id,
                )
                .where(
                    PRComment.installation_id != 0,  # GitLab rows are not ours to publish here
                    PRComment.published_version < PRComment.body_version,
                    PRComment.desired_body.is_not(None),
                    PRComment.publish_lease.is_(None) | (PRComment.publish_lease_until < now),
                )
                .limit(limit)
            )
        ).all()
        await db.rollback()
        for row_id, installation_id, repo, pr_number, comment_id in rows:
            try:
                # Revalidate the installation's current opt-out/detached
                # state before leasing (tk_b6c5e184ba8d4aa1): a disabled
                # or detached installation's pending work is fenced, not
                # published.
                if not await _installation_allows_comments(db, installation_id):
                    await cancel_pending_comment_work(db, installation_id)
                    await db.commit()
                    continue
                token = await _github_call(get_installation_token(installation_id))
                if comment_id == _PENDING_COMMENT_ID:
                    lease = await _try_publish_lease(db, row_id)
                    ok = lease is not None and await _create_or_adopt_comment(
                        db, token, row_id, lease, installation_id, repo, pr_number
                    )
                else:
                    ok = await _publish_newest(db, token, row_id, repo, pr_number, comment_id)
                stats["comments_republished" if ok else "comment_republish_failed"] += 1
            except Exception:
                logger.warning("Republishing PR comment %s failed", row_id, exc_info=True)
                stats["comment_republish_failed"] += 1
    return stats


async def _handle_pr_event(payload: dict, fence: tuple[str, int] | None = None) -> bool:
    """Match PR to sessions and post/update comment.

    Returns True when the event was handled (including intentional skips)
    and False on failure, so the delivery can be retried.
    """
    try:
        pr = payload["pull_request"]
        repo = payload["repository"]["full_name"]
        branch = pr["head"]["ref"]
        installation_id = payload["installation"]["id"]
        pr_number = pr["number"]
        if not isinstance(repo, str) or not _REPO_FULL_NAME_RE.fullmatch(repo):
            logger.warning("Ignoring PR event with unexpected repository name")
            return True
        if isinstance(installation_id, bool) or not isinstance(installation_id, int):
            return True
        if isinstance(pr_number, bool) or not isinstance(pr_number, int):
            return True

        clone_url = payload["repository"]["clone_url"]
        normalized = normalize_git_remote(clone_url)

        # Get DB session
        from sessionfs.server.db.engine import _session_factory as async_session_factory

        if async_session_factory is None:
            logger.error("DB session factory not initialized")
            return False

        async with async_session_factory() as db:
            # Check installation settings. Only a bound installation may
            # comment: a detached (user_id NULL) row can no longer attribute
            # sessions to an owner, so it must not match sessions globally.
            inst = await db.execute(
                select(GitHubInstallation).where(
                    GitHubInstallation.id == installation_id
                )
            )
            installation = inst.scalar_one_or_none()
            if installation is None or installation.user_id is None:
                return True
            if not installation.auto_comment:
                return True

            include_trust = installation.include_trust_score
            include_links = installation.include_session_links
            # The owner whose sessions this comment is built from; recording
            # re-checks it under a lock after the GitHub token fetch below.
            body_owner_id: str = installation.user_id

            # C-site (§2.2C C5): webhook PR-matching filters sessions
            # by denormalized git_remote_normalized tag — NOT project
            # resolution.  Sessions carry their own remote tag; this
            # filters to find sessions for the PR's repo+branch, scoped
            # to the installation's owner so one installation can never
            # comment on another user's sessions.
            result = await db.execute(
                select(Session)
                .where(
                    Session.user_id == installation.user_id,
                    Session.git_remote_normalized == normalized,
                    Session.git_branch == branch,
                    Session.is_deleted == False,  # noqa: E712
                )
                .order_by(Session.created_at.desc())
                .limit(5)
            )
            sessions = list(result.scalars().all())

            if not sessions:
                return True

            # Auto-audit on PR if configured
            for s in sessions:
                try:
                    from sessionfs.server.db.models import User, UserJudgeSettings
                    user_result = await db.execute(select(User).where(User.id == s.user_id))
                    session_owner = user_result.scalar_one_or_none()
                    if session_owner and getattr(session_owner, "audit_trigger", "manual") == "on_pr":
                        judge_result = await db.execute(
                            select(UserJudgeSettings).where(UserJudgeSettings.user_id == s.user_id)
                        )
                        judge_settings = judge_result.scalar_one_or_none()
                        if judge_settings and judge_settings.encrypted_api_key:
                                # TODO(v0.9.8): Wire blob store access into background
                            # task so audit can actually run. For now, mark as pending.
                            logger.info(
                                "Auto-audit on PR requested for session %s (user %s has on_pr trigger + judge key)",
                                s.id, s.user_id,
                            )
                except Exception:
                    logger.debug("Auto-audit check failed for session %s", s.id, exc_info=True)

            # Build session data for comment
            session_data = []
            for s in sessions:
                d = {
                    "session_id": s.id,
                    "title": s.title,
                    "source_tool": s.source_tool,
                    "model_id": s.model_id,
                    "message_count": s.message_count,
                    "trust_score": None,
                }
                session_data.append(d)

            comment_body = build_pr_comment(
                session_data, include_trust, include_links
            )
            comment_body = (
                f"{comment_body}\n\n{_comment_marker(installation_id, repo, pr_number)}"
            )
            # Plain values only past this point: later steps may roll the
            # session back, which expires loaded ORM objects.
            session_id_list = json.dumps([s.id for s in sessions])
            session_count = len(sessions)

            # Get installation token (end the read transaction first: no
            # transaction stays open across GitHub calls).
            await db.commit()
            token = await _github_call(get_installation_token(installation_id))

            try:
                decision = await _record_desired_comment(
                    db, installation_id, repo, pr_number, comment_body, session_id_list,
                    fence=fence, owner_user_id=body_owner_id,
                )
            except _Superseded:
                logger.info("Delivery superseded by a newer attempt; leaving %s#%d alone",
                            repo, pr_number)
                return True
            if decision is None:
                logger.info(
                    "Comment on %s#%d is being created or edited by another worker; it "
                    "will publish this update", repo, pr_number,
                )
                return True
            mode, row_id, comment_id, lease = decision
            if mode == "create":
                assert lease is not None
                await _create_or_adopt_comment(
                    db, token, row_id, lease, installation_id, repo, pr_number
                )
            else:
                assert comment_id is not None
                await _publish_newest(db, token, row_id, repo, pr_number, comment_id)

            logger.info(
                "Posted AI Context comment on %s#%d (%d sessions)",
                repo,
                pr_number,
                session_count,
            )
            return True

    except Exception as exc:
        logger.error("Failed to handle PR event: %s", exc)
        return False


def _parse_iso(value: object) -> datetime | None:
    """Parse a GitHub ISO timestamp (with or without a trailing Z)."""
    if not isinstance(value, str) or not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _is_full_sha(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) in (40, 64)
        and all(c in "0123456789abcdefABCDEF" for c in value)
    )


async def _project_id_for_remote(db, normalized: str) -> str | None:
    """The project that owns a normalized git remote via a *verified* GitHub
    App repo row (unmapped or unverified -> None).

    Unverified / legacy rows are ignored: any tenant can register another
    tenant's remote, so only a repo whose ownership was proven through the
    GitHub App installation may receive PRs and releases.
    """
    from sessionfs.server.db.models import ProjectRepo

    return (
        await db.execute(
            select(ProjectRepo.project_id)
            .where(
                ProjectRepo.git_remote_normalized == normalized,
                ProjectRepo.verified.is_(True),
                ProjectRepo.verification_method == "github_app",
            )
            .order_by(ProjectRepo.is_primary.desc(), ProjectRepo.id)
            .limit(1)
        )
    ).scalar_one_or_none()


async def _project_has_loop_plan(db, project_id: str) -> bool:
    """Loop/release recording is a Team+ feature: skip (still 200) otherwise."""
    from sessionfs.server.db.models import Project
    from sessionfs.server.tier_gate import project_plan_has_feature

    project = await db.get(Project, project_id)
    return await project_plan_has_feature(project, "agent_tickets", db)


async def _handle_pr_ingest(payload: dict, fence: tuple[str, int] | None = None) -> bool:
    """Upsert the PR (+ auto-link tickets) and refresh merge checks.

    Runs beside the session-comment pipeline for the same delivery. Repos that
    don't map to a project are ignored. The upsert is idempotent and the
    merge-check refresh is best-effort, so a failed refresh never fails the
    delivery.
    """
    from sessionfs.server.db.engine import _session_factory as async_session_factory
    from sessionfs.server.routes.loop_checklist import _sanitize_text
    from sessionfs.server.services import merge_checks, releases

    try:
        pr = payload["pull_request"]
        repository = payload["repository"]
        full_name = repository["full_name"]
        action = payload.get("action")
        number = pr["number"]
        head = pr.get("head") or {}
        branch = head.get("ref") or ""
        head_sha = head.get("sha") or None
        merged = bool(pr.get("merged"))
        state = "merged" if merged else ("closed" if pr.get("state") == "closed" else "open")
        title = _sanitize_text(pr.get("title") or "")
        body = _sanitize_text(pr.get("body") or "")
        normalized = normalize_git_remote(repository["clone_url"])

        if async_session_factory is None:
            logger.error("DB session factory not initialized")
            return False

        async with async_session_factory() as db:
            await _hold_delivery_fence(db, fence)
            project_id = await _project_id_for_remote(db, normalized)
            if project_id is None or not await _project_has_loop_plan(db, project_id):
                await db.rollback()
                return True  # unmapped repo or plan without agent_tickets — ignore
            result = await releases.upsert_pull_request(
                db,
                project_id=project_id,
                provider="github",
                repo=full_name,
                number=number,
                title=title,
                state=state,
                branch=branch,
                head_sha=head_sha,
                merged_at=_parse_iso(pr.get("merged_at")),
                merge_commit_sha=pr.get("merge_commit_sha") or None,
                external_id=str(number),
                body=body,
                provider_updated_at=_parse_iso(pr.get("updated_at")),
            )
            linked = result.get("linked_ticket_ids") or []
            if action in ("opened", "synchronize") and head_sha and linked:
                # One shared check run per PR: evaluates every linked ticket.
                await merge_checks.refresh_merge_check(
                    db,
                    project_id=project_id,
                    repo=full_name,
                    head_sha=head_sha,
                )
            return True
    except _Superseded:
        return True
    except Exception as exc:
        logger.error("Failed to ingest PR event: %s", exc)
        return False


async def _handle_release_event(payload: dict, fence: tuple[str, int] | None = None) -> bool:
    """Record a GitHub release for the repo's project (unmapped repos ignored)."""
    from sessionfs.server.db.engine import _session_factory as async_session_factory
    from sessionfs.server.services import releases

    try:
        release = payload["release"]
        repository = payload["repository"]
        normalized = normalize_git_remote(repository["clone_url"])

        if async_session_factory is None:
            logger.error("DB session factory not initialized")
            return False

        version = release["tag_name"]
        target = release.get("target_commitish")
        tag_sha = target if _is_full_sha(target) else ""
        released_at = _parse_iso(release.get("published_at"))

        async with async_session_factory() as db:
            await _hold_delivery_fence(db, fence)
            project_id = await _project_id_for_remote(db, normalized)
            if project_id is None or not await _project_has_loop_plan(db, project_id):
                await db.rollback()
                return True  # unmapped repo or plan without agent_tickets — ignore
            await releases.record_release(
                db,
                project_id=project_id,
                version=version,
                tag_sha=tag_sha,
                released_at=released_at,
                source="github_release",
            )
            return True
    except _Superseded:
        return True
    except Exception as exc:
        logger.error("Failed to ingest release event: %s", exc)
        return False


# ---- GitLab Webhooks ----


@router.post("/gitlab")
async def gitlab_webhook(request: Request):
    """Handle GitLab webhook events (merge request).

    Authentication:
      - Global secret from SFS_GITLAB_WEBHOOK_SECRET authorizes ALL sessions
        (trusted deployment where the secret is a shared system-wide value).
      - Per-user secret from GitLabSettings.webhook_secret authorizes ONLY
        sessions belonging to that specific user. We bind the request to the
        matching user's id so a forged payload for a repo belonging to a
        different user cannot use another user's credentials.
    """
    token = request.headers.get("X-Gitlab-Token", "")
    if not token:
        raise HTTPException(401, "Missing webhook token")

    # auth_user_id == None means "global secret matched, trust sender";
    # otherwise it is the id of the user whose per-user secret matched and all
    # downstream session/credential lookups MUST be scoped to that user.
    auth_user_id: str | None = None

    global_secret = os.environ.get("SFS_GITLAB_WEBHOOK_SECRET", "")
    if global_secret and hmac.compare_digest(token, global_secret):
        pass  # Global secret matches — auth_user_id stays None
    else:
        from sessionfs.server.db.engine import _session_factory
        if _session_factory is None:
            raise HTTPException(503, "Database not initialized")
        async with _session_factory() as db:
            from sessionfs.server.db.models import GitLabSettings as _GLS
            # Compare all rows in constant time to avoid leaking which
            # user's secret is being probed via response-time side channels.
            rows = (
                await db.execute(select(_GLS.user_id, _GLS.webhook_secret))
            ).all()
            for row_user_id, row_secret in rows:
                if row_secret and hmac.compare_digest(token, row_secret):
                    auth_user_id = row_user_id
                    # Don't break — finish the loop so timing is constant.
        if auth_user_id is None:
            raise HTTPException(401, "Invalid webhook token")

    payload = await request.json()
    event_type = payload.get("object_kind")

    if event_type != "merge_request":
        return {"status": "ignored", "reason": f"Not a merge_request event: {event_type}"}

    action = payload.get("object_attributes", {}).get("action", "")
    if action not in ("open", "update", "reopen"):
        return {"status": "ignored", "reason": f"Action not handled: {action}"}

    asyncio.ensure_future(_handle_gitlab_mr(payload, request, auth_user_id))
    return {"status": "processing"}


async def _handle_gitlab_mr(
    payload: dict,
    request: Request,
    auth_user_id: str | None,
):
    """Match MR to sessions and post comment."""
    try:
        from sessionfs.server.db.engine import _session_factory
        if _session_factory is None:
            logger.error("GitLab webhook: DB session factory not initialized")
            return
        async with _session_factory() as db:
            mr = payload.get("object_attributes", {})
            project = payload.get("project", {})

            repo_url = project.get("git_http_url", "")
            ssh_url = project.get("git_ssh_url", "")
            branch = mr.get("source_branch", "")
            mr_iid = mr.get("iid")
            project_id = project.get("id")

            if not branch or not mr_iid or not project_id:
                return

            # Normalize both URL formats
            normalized = normalize_git_remote(repo_url)
            if not normalized:
                normalized = normalize_git_remote(ssh_url)
            if not normalized:
                return

            # C-site (§2.2C C5): same webhook PR-matching pattern as
            # the GitHub path above — sessions filtered by denormalized
            # tag, NOT project resolution.
            # When the webhook was authenticated via a per-user secret,
            # scope to that user so a forged payload for a repo belonging
            # to a DIFFERENT user can't use another user's credentials.
            stmt = select(Session).where(
                Session.git_remote_normalized == normalized,
                Session.git_branch == branch,
                Session.is_deleted == False,  # noqa: E712
            )
            if auth_user_id is not None:
                stmt = stmt.where(Session.user_id == auth_user_id)
            stmt = stmt.order_by(Session.created_at.desc()).limit(10)

            result = await db.execute(stmt)
            sessions = result.scalars().all()

            if not sessions:
                return

            # Build session data for comment
            session_data = []
            for s in sessions:
                session_data.append({
                    "session_id": s.id,
                    "title": s.title,
                    "source_tool": s.source_tool,
                    "model_id": s.model_id,
                    "message_count": s.message_count,
                    "trust_score": None,
                })

            comment_body = build_pr_comment(session_data)

            # Find GitLab token. For per-user secret auth, use that user's
            # settings directly (not sessions[0].user_id which could belong
            # to another user if the session filter found more than one owner
            # in global mode).
            owner_id = auth_user_id if auth_user_id is not None else sessions[0].user_id
            gitlab_stmt = select(GitLabSettings).where(GitLabSettings.user_id == owner_id)
            gitlab_result = await db.execute(gitlab_stmt)
            gitlab_settings = gitlab_result.scalar_one_or_none()

            if not gitlab_settings:
                logger.warning("No GitLab token for user %s, skipping MR comment", owner_id)
                return

            # Decrypt token
            from sessionfs.server.llm_keys import decrypt_secret

            access_token = decrypt_secret(gitlab_settings.encrypted_access_token)

            # Post or update comment (avoid duplicates on MR updates)
            from sessionfs.server.gitlab_client import GitLabClient

            client = GitLabClient(gitlab_settings.instance_url, access_token)

            # Check for existing comment tracking (scoped to GitLab via installation_id=0)
            existing_comment = await db.execute(
                select(PRComment).where(
                    PRComment.repo_full_name == normalized,
                    PRComment.pr_number == mr_iid,
                    PRComment.installation_id == 0,
                )
            )
            mr_comment = existing_comment.scalar_one_or_none()

            if mr_comment and mr_comment.comment_id:
                await client.update_mr_comment(project_id, mr_iid, mr_comment.comment_id, comment_body)
                mr_comment.session_ids = json.dumps([s.id for s in sessions])
                mr_comment.updated_at = datetime.now(timezone.utc)
            else:
                note_resp = await client.post_mr_comment(project_id, mr_iid, comment_body)
                note_id = note_resp.get("id")
                if mr_comment:
                    mr_comment.comment_id = note_id
                    mr_comment.session_ids = json.dumps([s.id for s in sessions])
                    mr_comment.updated_at = datetime.now(timezone.utc)
                else:
                    db.add(PRComment(
                        id=str(uuid.uuid4()),
                        installation_id=0,
                        repo_full_name=normalized,
                        pr_number=mr_iid,
                        comment_id=note_id,
                        session_ids=json.dumps([s.id for s in sessions]),
                    ))
            await db.commit()

            logger.info("Posted AI Context comment on GitLab MR %s!%d (%d sessions)", normalized, mr_iid, len(sessions))

    except Exception as exc:
        logger.error("Failed to handle GitLab MR event: %s", exc)
