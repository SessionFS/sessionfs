"""Idempotency-Key support for POST routes (tk_bd80efd511da4a2b).

A client that sends ``Idempotency-Key: <1-255 printable ASCII>`` on a POST
gets at-most-once execution for that key: a retry of the same request
replays the first response instead of running the route again.

The middleware is pure ASGI and sits inside CORS, so every POST route is
covered without touching the routes themselves.

Flow for a keyed, authenticated POST to a covered route:

1. Buffer the request body (up to ``MAX_BODY_BYTES``) and hash it with the
   method, concrete path and query string (``request_hash``).
2. Insert an ``in_progress`` row keyed by ``scope_hash`` = sha256 of the
   credential hash, method, matched route TEMPLATE and the key, in its own
   short transaction. On a unique conflict, read the existing row:

   * different ``request_hash``  -> 422 ``idempotency_key_reused``
   * ``completed``, replayable   -> the stored status/headers/body plus
     ``Idempotent-Replayed: true``
   * ``completed``, not stored   -> 409 ``idempotency_response_unavailable``
   * ``in_progress``, lease live -> 409 ``idempotency_in_progress``
   * ``in_progress``, lease past -> taken over by a guarded UPDATE on the
     old ``lease_token``

3. Run the route with the buffered body handed on unchanged.
4. Store the response and mark the row completed on 2xx and on 4xx other
   than the transient ones in ``_RELEASE_STATUSES``; DELETE the row
   (fenced by ``lease_token``) on 5xx, an exception, a transient 4xx, a
   streamed (SSE) response, or when the request never authenticated, so the
   client can retry.

Ignored (the request runs exactly as it would without the header): non-POST
methods, requests with no ``Authorization: Bearer`` credential, requests
whose path matches no route, excluded routes (``EXCLUDED_TEMPLATES`` /
``EXCLUDED_PREFIXES``) and bodies over ``MAX_BODY_BYTES`` (passed through
unchanged, no 413 from this layer; the route applies its own cap).
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import secrets
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, AsyncIterator

from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.datastructures import Headers
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from sessionfs.server.db.models import IdempotencyKey
from sessionfs.server.route_introspection import match_route

logger = logging.getLogger("sessionfs.api.idempotency")

HEADER = "idempotency-key"
REPLAYED_HEADER = "Idempotent-Replayed"

# Bodies above this are passed through without idempotency (no 413 here).
# The largest JSON bodies on covered routes are chat exports (2 MiB raw).
MAX_BODY_BYTES = 4 * 1024 * 1024
# A response body above this is not stored; a reuse gets 409.
MAX_RESPONSE_BYTES = 1024 * 1024
DEFAULT_LEASE = timedelta(minutes=5)
# Routes that call an LLM provider or another slow upstream (extraction has a
# 240 s provider deadline, Jira publish a 240 s outbound budget, sweeps run up
# to the 1500 s request timeout) hold the lease longer, so a slow first
# attempt isn't taken over while it is still running.
LONG_LEASE = timedelta(minutes=30)
_LONG_LEASE_MARKERS = (
    "/extract",
    "/compile",
    "/rebuild",
    "/regenerate",
    "/summary",
    "/audit",
    "/jira/",
    "/confluence/",
    "/admin/",
    "/merge",
    "/llm-settings/",
    "/judge/",
)
RETENTION = timedelta(hours=24)
SWEEP_BATCH = 1000
SWEEP_MAX_BATCHES = 100

# 4xx answers that describe a passing condition (auth, timeouts, locks, busy
# slots, concurrency races, rate limits). Storing one would replay a refusal
# the client is meant to retry through, so the key is released instead.
_RELEASE_STATUSES = frozenset({401, 408, 409, 423, 425, 429})

# Route templates that ignore the header. Keep docs/ (API reference) in step.
EXCLUDED_PREFIXES: tuple[str, ...] = (
    # Webhook ingest: providers' own delivery ids already make it idempotent.
    "/api/v1/webhooks/",
    "/webhooks/",
)
EXCLUDED_TEMPLATES: frozenset[str] = frozenset(
    {
        # Streams Server-Sent Events; a stream can't be replayed.
        "/api/v1/helper/turns",
        # Session archive sync: ETag / resume / complete idempotency of its own,
        # and bodies far above MAX_BODY_BYTES.
        "/api/v1/sessions",
        "/api/v1/sessions/{session_id}/uploads",
        "/api/v1/uploads/{upload_id}/complete",
        # Unauthenticated, or authenticated by something other than an API key.
        "/api/v1/auth/signup",
        "/api/v1/auth/sso/start",
        "/api/v1/auth/sso/exchange",
        "/api/v1/org/activate",
        "/api/v1/org/activate/verify",
        "/api/v1/helm/validate",
        "/api/v1/telemetry",
        "/api/v1/share/{token}/view",
        "/api/v1/sessions/share/{token}",
        "/api/v1/marketplace/trusted-publishing/token",
        "/api/v1/marketplace/trusted-publishing/publish",
        # The response carries a secret shown once (API keys, webhook
        # secrets, share tokens, license keys, OAuth state). Never stored.
        "/api/v1/auth/keys",
        "/api/v1/auth/me/api-keys",
        "/api/v1/admin/users/{user_id}/api-keys",
        "/api/v1/admin/licenses/",
        "/api/v1/orgs/{org_id}/service-keys",
        "/api/v1/orgs/{org_id}/service-keys/{key_id}/rotate",
        "/api/v1/orgs/{org_id}/residents/{resident_id}/rotate-key",
        "/api/v1/orgs/{org_id}/delivery-webhooks/{provider}/rotate",
        "/api/v1/orgs/{org_id}/integrations/jira/webhook-secret",
        "/api/v1/projects/{project_id}/loop-share-links",
        "/api/v1/sessions/{session_id}/share",
        "/api/v1/settings/github/oauth/start",
        "/api/v1/settings/github/oauth/choose",
    }
)

# Defense in depth: a response that still holds a SessionFS API key is never
# stored, even on a route nobody remembered to exclude.
_SECRET_RE = re.compile(rb"sk_sfs_[0-9a-f]{32}")
_SAFE_RESPONSE_HEADERS = ("content-type", "etag", "location")
_KEY_RE = re.compile(r"^[\x20-\x7e]{1,255}$")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime | None) -> datetime | None:
    """SQLite hands back naive datetimes; they were written as UTC."""
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def is_excluded(template: str) -> bool:
    return template in EXCLUDED_TEMPLATES or template.startswith(EXCLUDED_PREFIXES)


def lease_for(template: str) -> timedelta:
    return LONG_LEASE if any(m in template for m in _LONG_LEASE_MARKERS) else DEFAULT_LEASE


def scope_hash(credential: str, method: str, template: str, key: str) -> str:
    cred = hashlib.sha256(credential.encode("utf-8")).hexdigest()
    material = "\x00".join((cred, method.upper(), template, key))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _canonical_body(body: bytes, content_type: str) -> bytes:
    """JSON bodies compare by value (key order and whitespace ignored)."""
    if body and "json" in content_type.lower():
        try:
            return json.dumps(
                json.loads(body), sort_keys=True, separators=(",", ":"), ensure_ascii=False
            ).encode("utf-8")
        except (ValueError, UnicodeDecodeError, RecursionError):
            return body
    return body


def request_hash(method: str, path: str, query: bytes, body: bytes, content_type: str) -> str:
    digest = hashlib.sha256()
    digest.update(method.upper().encode("ascii"))
    digest.update(b"\x00")
    digest.update(path.encode("utf-8", "surrogateescape"))
    digest.update(b"\x00")
    digest.update(query)
    digest.update(b"\x00")
    digest.update(_canonical_body(body, content_type))
    return digest.hexdigest()


def _match_template(scope: Scope) -> str | None:
    """The template of the route that will handle this request, if any.

    Same first-FULL-match walk the router does (through the included
    routers newer FastAPI keeps as wrappers); a method-only (PARTIAL) match
    or no match means the request will 404/405 and is ignored.
    """
    app = scope.get("app")
    if getattr(app, "router", None) is None:
        return None
    entry = match_route(app, scope)
    return entry.path if entry is not None else None


def _error(status: int, code: str, message: str) -> tuple[int, list[tuple[bytes, bytes]], bytes]:
    body = json.dumps(
        {"error": {"code": code, "message": message, "details": {}}}, separators=(",", ":")
    ).encode("utf-8")
    return status, [(b"content-type", b"application/json")], body


async def _send_response(
    send: Send, status: int, headers: list[tuple[bytes, bytes]], body: bytes
) -> None:
    headers = [h for h in headers if h[0].lower() != b"content-length"]
    headers.append((b"content-length", str(len(body)).encode("ascii")))
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": body, "more_body": False})


@asynccontextmanager
async def _db_session(scope: Scope) -> AsyncIterator[AsyncSession]:
    """A fresh session from the app's ``get_db`` (honouring test overrides)."""
    from sessionfs.server.db.engine import get_db

    app = scope.get("app")
    overrides = getattr(app, "dependency_overrides", None) or {}
    dependency = overrides.get(get_db, get_db)
    agen = dependency()
    session = await agen.__anext__()
    try:
        yield session
    finally:
        try:
            await agen.aclose()
        except Exception:  # pragma: no cover - closing must never mask the result
            logger.debug("idempotency session close failed", exc_info=True)


async def _credential_is_live(session: AsyncSession, raw_key: str) -> bool:
    """The bearer is an active, unrevoked, unexpired API key.

    Checked before any row is written, so a made-up or revoked credential
    can neither fill the table nor read back a response stored while it
    still worked. The route still runs its own full authentication.
    """
    from sessionfs.server.auth.keys import hash_api_key
    from sessionfs.server.db.models import ApiKey, User

    # The key's user must still be active too: a deactivated account whose
    # keys weren't revoked must not read back stored responses (the route's
    # own auth would refuse it, but a replay never reaches the route).
    key = (
        await session.execute(
            select(
                ApiKey.is_active,
                ApiKey.revoked_at,
                ApiKey.expires_at,
                User.is_active.label("user_active"),
            )
            .outerjoin(User, User.id == ApiKey.user_id)
            .where(ApiKey.key_hash == hash_api_key(raw_key))
        )
    ).one_or_none()
    await session.rollback()
    if key is None or not key.is_active or key.revoked_at is not None:
        return False
    if key.user_active is not None and not key.user_active:
        return False
    expires_at = _aware(key.expires_at)
    return expires_at is None or expires_at > _utcnow()


@dataclass
class _Reservation:
    outcome: str  # reserved | replay | mismatch | in_progress | unavailable
    token: str | None = None
    row: IdempotencyKey | None = None


async def _reserve(
    session: AsyncSession, s_hash: str, r_hash: str, lease: timedelta
) -> _Reservation:
    for _ in range(3):
        token = secrets.token_hex(16)
        now = _utcnow()
        session.add(
            IdempotencyKey(
                scope_hash=s_hash,
                request_hash=r_hash,
                status="in_progress",
                lease_token=token,
                created_at=now,
                locked_until=now + lease,
            )
        )
        try:
            await session.commit()
            return _Reservation("reserved", token=token)
        except IntegrityError:
            await session.rollback()

        row = (
            await session.execute(select(IdempotencyKey).where(IdempotencyKey.scope_hash == s_hash))
        ).scalar_one_or_none()
        if row is None:
            continue  # released between our insert and read; try again
        if row.request_hash != r_hash:
            return _Reservation("mismatch")
        if row.status == "completed":
            return _Reservation("replay" if row.replayable else "unavailable", row=row)
        locked_until = _aware(row.locked_until)
        if locked_until is not None and locked_until > now:
            return _Reservation("in_progress")
        # Stale lease: take it over only if nobody else did first.
        result = await session.execute(
            update(IdempotencyKey)
            .where(
                IdempotencyKey.scope_hash == s_hash,
                IdempotencyKey.status == "in_progress",
                IdempotencyKey.lease_token == row.lease_token,
            )
            .values(lease_token=token, locked_until=now + lease)
            .execution_options(synchronize_session=False)
        )
        await session.commit()
        if (result.rowcount or 0) == 1:
            return _Reservation("reserved", token=token)
        return _Reservation("in_progress")
    return _Reservation("in_progress")


async def _release(scope: Scope, s_hash: str, token: str) -> None:
    try:
        async with _db_session(scope) as session:
            await session.execute(
                delete(IdempotencyKey).where(
                    IdempotencyKey.scope_hash == s_hash, IdempotencyKey.lease_token == token
                )
            )
            await session.commit()
    except Exception:
        # The lease expires on its own; a retry after it can take the row over.
        logger.warning("idempotency key release failed", exc_info=True)


async def _complete(
    scope: Scope,
    s_hash: str,
    token: str,
    status: int,
    headers: list[tuple[bytes, bytes]] | None,
    body: bytes | None,
) -> None:
    replayable = body is not None and headers is not None
    stored_headers = None
    if replayable and headers is not None:
        stored_headers = json.dumps(
            [
                [k.decode("latin-1"), v.decode("latin-1")]
                for k, v in headers
                if k.decode("latin-1").lower() in _SAFE_RESPONSE_HEADERS
            ]
        )
    try:
        async with _db_session(scope) as session:
            await session.execute(
                update(IdempotencyKey)
                .where(IdempotencyKey.scope_hash == s_hash, IdempotencyKey.lease_token == token)
                .values(
                    status="completed",
                    lease_token=None,
                    locked_until=None,
                    completed_at=_utcnow(),
                    response_status=status,
                    response_headers=stored_headers,
                    response_body=body if replayable else None,
                    replayable=replayable,
                )
                .execution_options(synchronize_session=False)
            )
            await session.commit()
    except Exception:
        # The route already ran; leave the row in_progress (a retry gets 409
        # until the lease passes) rather than release it for a second write.
        logger.warning("idempotency response store failed", exc_info=True)


@dataclass
class _Captured:
    status: int | None = None
    headers: list[tuple[bytes, bytes]] = field(default_factory=list)
    chunks: list[bytes] = field(default_factory=list)
    size: int = 0
    too_large: bool = False
    streaming: bool = False
    finished: bool = False


class IdempotencyMiddleware:
    """ASGI middleware implementing the ``Idempotency-Key`` request header."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("method") != "POST":
            await self.app(scope, receive, send)
            return
        headers = Headers(scope=scope)
        key = headers.get(HEADER)
        if key is None:
            await self.app(scope, receive, send)
            return
        authorization = headers.get("authorization", "")
        if not authorization.startswith("Bearer ") or len(authorization) <= 7:
            await self.app(scope, receive, send)  # unauthenticated: ignored
            return
        template = _match_template(scope)
        if template is None or is_excluded(template):
            await self.app(scope, receive, send)
            return
        if not _KEY_RE.fullmatch(key):
            status, hdrs, body = _error(
                400,
                "idempotency_key_invalid",
                "Idempotency-Key must be 1-255 printable ASCII characters.",
            )
            await _send_response(send, status, hdrs, body)
            return

        declared = headers.get("content-length")
        if declared is not None:
            try:
                if int(declared) > MAX_BODY_BYTES:
                    await self.app(scope, receive, send)
                    return
            except ValueError:
                pass

        # Buffer the body (bounded), then hand the same messages on.
        messages: list[Message] = []
        size = 0
        oversized = False
        disconnected = False
        while True:
            message = await receive()
            messages.append(message)
            if message["type"] == "http.disconnect":
                disconnected = True
                break
            size += len(message.get("body", b""))
            if size > MAX_BODY_BYTES:
                oversized = True
                break
            if not message.get("more_body", False):
                break

        pending = list(messages)

        async def replay_receive() -> Message:
            if pending:
                return pending.pop(0)
            return await receive()

        if oversized or disconnected:
            await self.app(scope, replay_receive, send)
            return

        body = b"".join(m.get("body", b"") for m in messages if m["type"] == "http.request")
        s_hash = scope_hash(authorization[7:], "POST", template, key)
        r_hash = request_hash(
            "POST",
            scope.get("path", ""),
            scope.get("query_string", b""),
            body,
            headers.get("content-type", ""),
        )

        try:
            async with _db_session(scope) as session:
                if not await _credential_is_live(session, authorization[7:]):
                    # Not a usable API key: no row, no replay. The route
                    # answers exactly as it would without the header (401).
                    await self.app(scope, replay_receive, send)
                    return
                reservation = await _reserve(session, s_hash, r_hash, lease_for(template))
        except Exception:
            logger.warning("idempotency key reservation failed", exc_info=True)
            status, hdrs, err = _error(
                503,
                "idempotency_unavailable",
                "Idempotency-Key could not be recorded; nothing was run. Retry the request.",
            )
            await _send_response(send, status, hdrs, err)
            return

        if reservation.outcome != "reserved":
            await self._answer_existing(send, reservation)
            return
        token = reservation.token
        assert token is not None

        captured = _Captured()

        async def capture_send(message: Message) -> None:
            if message["type"] == "http.response.start":
                captured.status = int(message["status"])
                captured.headers = list(message.get("headers", []))
                for k, v in captured.headers:
                    if k.lower() == b"content-type" and b"text/event-stream" in v.lower():
                        captured.streaming = True
            elif message["type"] == "http.response.body":
                chunk = message.get("body", b"")
                if not captured.too_large and not captured.streaming:
                    captured.size += len(chunk)
                    if captured.size > MAX_RESPONSE_BYTES:
                        captured.too_large = True
                        captured.chunks = []
                    else:
                        captured.chunks.append(chunk)
                if not message.get("more_body", False):
                    captured.finished = True
            await send(message)

        scope.setdefault("state", {})
        try:
            await self.app(scope, replay_receive, capture_send)
        except Exception:
            await _release(scope, s_hash, token)
            raise
        # A cancelled request (CancelledError, a BaseException) is NOT
        # released: the route may already have written, so the row stays
        # in_progress until its lease passes.

        state = scope.get("state") or {}
        authenticated = state.get("auth_context") is not None
        status_code = captured.status
        if (
            status_code is None
            or not captured.finished
            or not authenticated
            or status_code >= 500
            or status_code in _RELEASE_STATUSES
            or captured.streaming
        ):
            await _release(scope, s_hash, token)
            return

        response_body: bytes | None = b"".join(captured.chunks)
        if captured.too_large or (response_body and _SECRET_RE.search(response_body)):
            response_body = None
        await _complete(scope, s_hash, token, status_code, captured.headers, response_body)

    @staticmethod
    async def _answer_existing(send: Send, reservation: _Reservation) -> None:
        if reservation.outcome == "replay" and reservation.row is not None:
            row = reservation.row
            stored: list[Any] = json.loads(row.response_headers or "[]")
            hdrs = [(str(k).encode("latin-1"), str(v).encode("latin-1")) for k, v in stored]
            hdrs.append((REPLAYED_HEADER.lower().encode("ascii"), b"true"))
            await _send_response(send, int(row.response_status or 200), hdrs, row.response_body or b"")
            return
        if reservation.outcome == "mismatch":
            result = _error(
                422,
                "idempotency_key_reused",
                "This Idempotency-Key was already used for a different request.",
            )
        elif reservation.outcome == "unavailable":
            result = _error(
                409,
                "idempotency_response_unavailable",
                "This request already ran, but its response was not stored, so it can't be "
                "replayed. Check the result before sending it again with a new key.",
            )
        else:
            result = _error(
                409,
                "idempotency_in_progress",
                "A request with this Idempotency-Key is still running. Retry later.",
            )
        await _send_response(send, *result)


async def sweep_idempotency_keys(
    db: AsyncSession, *, batch_size: int = SWEEP_BATCH, now: datetime | None = None
) -> dict[str, int]:
    """Delete rows older than 24 hours in bounded keyset batches (each commits)."""
    cutoff = (now or _utcnow()) - RETENTION
    deleted = 0
    batches = 0
    for _ in range(SWEEP_MAX_BATCHES):
        keys = (
            await db.execute(
                select(IdempotencyKey.scope_hash)
                .where(IdempotencyKey.created_at < cutoff)
                .order_by(IdempotencyKey.created_at, IdempotencyKey.scope_hash)
                .limit(batch_size)
            )
        ).scalars().all()
        if not keys:
            break
        result = await db.execute(
            delete(IdempotencyKey).where(
                IdempotencyKey.scope_hash.in_(list(keys)), IdempotencyKey.created_at < cutoff
            )
        )
        await db.commit()
        deleted += int(getattr(result, "rowcount", 0) or 0)
        batches += 1
        if len(keys) < batch_size:
            break
    return {"deleted": deleted, "batches": batches}
