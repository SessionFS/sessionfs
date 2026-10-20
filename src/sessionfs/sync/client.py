"""HTTP sync client for the SessionFS API server.

Used by both the daemon (background push) and CLI (push/pull/list commands)
to communicate with the server over HTTPS + ETags.
"""

from __future__ import annotations

import logging
import math
import os
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

import httpx

from sessionfs import __version__

logger = logging.getLogger("sessionfs.sync")

# Retry config
_MAX_RETRIES = 3
_BACKOFF_BASE = 1.0  # seconds
_RETRYABLE_STATUS = {429, 502, 503, 504}

# Timeouts
_METADATA_TIMEOUT = 10.0
_CONNECT_TIMEOUT = 10.0
# Session push/pull. A push waits for the server to DLP-scan, unpack and
# index the archive before it answers, which for a multi-MB session on a
# self-hosted deployment can take well over a minute (GitHub #68 — the old
# hardcoded 30 s made such sessions unpushable).
DEFAULT_TRANSFER_TIMEOUT = 300.0
MAX_TRANSFER_TIMEOUT = 3600.0
TRANSFER_TIMEOUT_ENV = "SFS_UPLOAD_TIMEOUT"


def _valid_transfer_timeout(value: object) -> float | None:
    """Return value as seconds if it is a usable transfer timeout, else None."""
    if isinstance(value, bool):
        return None
    try:
        seconds = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(seconds) or seconds < 1 or seconds > MAX_TRANSFER_TIMEOUT:
        return None
    return seconds


def resolve_transfer_timeout(
    explicit: float | None = None, configured: float | None = None
) -> float:
    """Seconds to wait on a session push/pull.

    Precedence: explicit (e.g. `sfs push --timeout`) > SFS_UPLOAD_TIMEOUT >
    configured (`[sync] upload_timeout` in config.toml) > 300 s default.
    An invalid env/config value is ignored with a warning rather than
    breaking sync; an invalid explicit value is the caller's bug.
    """
    if explicit is not None:
        seconds = _valid_transfer_timeout(explicit)
        if seconds is None:
            raise ValueError(
                f"transfer timeout must be between 1 and {MAX_TRANSFER_TIMEOUT:.0f} seconds"
            )
        return seconds
    raw_env = os.environ.get(TRANSFER_TIMEOUT_ENV, "").strip()
    if raw_env:
        seconds = _valid_transfer_timeout(raw_env)
        if seconds is not None:
            return seconds
        logger.warning(
            "Ignoring %s=%r: expected seconds between 1 and %.0f",
            TRANSFER_TIMEOUT_ENV, raw_env, MAX_TRANSFER_TIMEOUT,
        )
    if configured is not None:
        seconds = _valid_transfer_timeout(configured)
        if seconds is not None:
            return seconds
        logger.warning(
            "Ignoring [sync] upload_timeout=%r: expected seconds between 1 and %.0f",
            configured, MAX_TRANSFER_TIMEOUT,
        )
    return DEFAULT_TRANSFER_TIMEOUT


def _describe_exc(exc: BaseException | None) -> str:
    """Exception text that is never blank (httpx timeouts stringify to '')."""
    if exc is None:
        return "unknown error"
    text = str(exc)
    return f"{type(exc).__name__}: {text}" if text else type(exc).__name__


class SyncError(Exception):
    """Base error for sync operations."""


class SyncConflictError(SyncError):
    """ETag mismatch — remote session has been updated."""

    def __init__(self, current_etag: str, message: str = "ETag mismatch"):
        self.current_etag = current_etag
        super().__init__(message)


class SyncDeletedError(SyncError):
    """Server says the session has been deleted (410 Gone)."""

    def __init__(self, session_id: str, message: str = "Session deleted on server"):
        self.session_id = session_id
        super().__init__(message)


class SyncAuthError(SyncError):
    """Authentication failed."""


class SyncTimeoutError(SyncError):
    """A session upload ran out of time, at the client or at a proxy.

    Not retried automatically: resending the same archive against the same
    limits fails the same way (GitHub #68). ``gateway`` is True when a proxy
    or load balancer in front of the server gave up first (HTTP 504).
    """

    def __init__(
        self,
        timeout_s: float,
        size_bytes: int | None = None,
        *,
        gateway: bool = False,
    ):
        self.timeout_s = timeout_s
        self.size_bytes = size_bytes
        self.gateway = gateway
        size = f" ({size_bytes:,} bytes)" if size_bytes is not None else ""
        if gateway:
            message = (
                f"A proxy or load balancer in front of the server stopped waiting "
                f"(HTTP 504) before the upload{size} was stored. Large sessions can "
                f"take longer to scan and store: raise that proxy's timeout (Helm: "
                f"dashboard.apiProxyTimeout plus your ingress or load balancer idle "
                f"timeout) and keep {TRANSFER_TIMEOUT_ENV} at least as long."
            )
        else:
            message = (
                f"The upload{size} did not finish within {timeout_s:.0f}s. Large "
                f"sessions can take longer to send, scan and store. Raise the "
                f"limit with `sfs push --timeout <seconds>`, {TRANSFER_TIMEOUT_ENV}, "
                f"or `upload_timeout` under [sync] in config.toml."
            )
        super().__init__(message)


class SyncTooLargeError(SyncError):
    """Server rejected the upload because a file exceeds the per-member size cap.

    Distinct from generic SyncError so the daemon can permanently exclude
    these sessions (the user must /clear or /compact to fix), without
    permanently excluding sessions that hit transient 429/5xx/network errors.
    """

    def __init__(self, session_id: str, message: str):
        self.session_id = session_id
        super().__init__(message)


@dataclass
class SyncResult:
    """Result of a push operation."""

    session_id: str
    etag: str
    blob_size_bytes: int
    synced_at: str
    created: bool = False  # True if this was a new session


@dataclass
class PullResult:
    """Result of a pull operation."""

    session_id: str
    data: bytes | None = None  # None if 304 Not Modified
    etag: str = ""
    not_modified: bool = False


@dataclass
class RemoteSession:
    """Summary of a session on the server."""

    id: str
    title: str | None = None
    source_tool: str = ""
    model_id: str | None = None
    message_count: int = 0
    etag: str = ""
    created_at: str = ""
    updated_at: str = ""
    blob_size_bytes: int = 0


@dataclass
class RemoteListResult:
    """Result of listing remote sessions."""

    sessions: list[RemoteSession] = field(default_factory=list)
    total: int = 0
    page: int = 1
    has_more: bool = False


def _validate_url(api_url: str) -> None:
    """Enforce HTTPS for all server communication."""
    parsed = urlparse(api_url)
    if parsed.scheme == "http" and parsed.hostname not in ("localhost", "127.0.0.1", "::1"):
        raise SyncError(
            f"HTTPS required for server communication. Got: {api_url}. "
            "HTTP is only allowed for localhost."
        )


class SyncClient:
    """HTTP client for SessionFS API server."""

    def __init__(
        self, api_url: str, api_key: str, *, transfer_timeout: float | None = None
    ) -> None:
        _validate_url(api_url)
        self.api_url = api_url.rstrip("/")
        self.api_key = api_key
        # Callers that read config.toml resolve the full precedence chain and
        # pass the result; everyone else still honours SFS_UPLOAD_TIMEOUT.
        self.transfer_timeout = (
            transfer_timeout
            if transfer_timeout is not None
            else resolve_transfer_timeout()
        )
        self._client: httpx.AsyncClient | None = None

    def _transfer_timeout(self) -> httpx.Timeout:
        """Long read/write budget for archives; connecting stays fast-fail."""
        return httpx.Timeout(self.transfer_timeout, connect=_CONNECT_TIMEOUT)

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            import platform as _platform
            self._client = httpx.AsyncClient(
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "User-Agent": f"sessionfs-cli/{__version__}",
                    "X-Client-Version": __version__,
                    "X-Client-Platform": _platform.system(),
                    "X-Client-Device": _platform.node(),
                },
                timeout=httpx.Timeout(_METADATA_TIMEOUT),
            )
        return self._client

    async def close(self) -> None:
        """Close the underlying HTTP client."""
        if self._client and not self._client.is_closed:
            await self._client.aclose()
            self._client = None

    async def _request_with_retry(
        self,
        method: str,
        url: str,
        *,
        timeout: float | httpx.Timeout | None = None,
        retry_read_timeout: bool = True,
        **kwargs: Any,
    ) -> httpx.Response:
        """Make an HTTP request with exponential backoff retry on transient failures.

        With retry_read_timeout=False a read or write timeout, or a 504 from
        a gateway, raises SyncTimeoutError at once instead of resending.
        """
        import asyncio

        client = await self._get_client()
        last_exc: Exception | None = None

        for attempt in range(_MAX_RETRIES):
            try:
                resp = await client.request(
                    method,
                    url,
                    timeout=timeout or _METADATA_TIMEOUT,
                    **kwargs,
                )
                if resp.status_code not in _RETRYABLE_STATUS:
                    return resp
                if resp.status_code == 504 and not retry_read_timeout:
                    # A gateway gave up waiting on the server: same failure
                    # as a read timeout, one hop further out.
                    raise SyncTimeoutError(self.transfer_timeout, gateway=True)
                last_exc = SyncError(f"Server returned {resp.status_code}")
                # Respect Retry-After header from 429 responses
                retry_after = resp.headers.get("retry-after")
            except (httpx.ConnectError, httpx.TimeoutException, httpx.ReadError) as exc:
                if (
                    isinstance(exc, (httpx.ReadTimeout, httpx.WriteTimeout))
                    and not retry_read_timeout
                ):
                    # Sending the archive or waiting for the server to store
                    # it ran out of time; a resend hits the same limit.
                    read_s = (
                        timeout.read
                        if isinstance(timeout, httpx.Timeout)
                        else timeout
                    )
                    raise SyncTimeoutError(
                        read_s if read_s is not None else self.transfer_timeout
                    ) from exc
                last_exc = exc
                retry_after = None

            if attempt < _MAX_RETRIES - 1:
                delay = _BACKOFF_BASE * (2**attempt)
                if retry_after:
                    try:
                        delay = max(delay, float(retry_after))
                    except (ValueError, TypeError):
                        pass
                logger.warning(
                    "Sync request failed (attempt %d/%d), retrying in %.1fs: %s",
                    attempt + 1,
                    _MAX_RETRIES,
                    delay,
                    _describe_exc(last_exc),
                )
                await asyncio.sleep(delay)

        raise SyncError(
            f"Request failed after {_MAX_RETRIES} attempts: {_describe_exc(last_exc)}"
        )

    def _check_auth(self, resp: httpx.Response) -> None:
        if resp.status_code in (401, 403):
            raise SyncAuthError(f"Authentication failed: {resp.status_code}")

    async def health_check(self) -> dict[str, Any]:
        """Check server health. GET /health."""
        resp = await self._request_with_retry("GET", f"{self.api_url}/health")
        self._check_auth(resp)
        resp.raise_for_status()
        return resp.json()

    async def push_session(
        self, session_id: str, archive_data: bytes, etag: str | None = None,
        extra_headers: dict[str, str] | None = None,
    ) -> SyncResult:
        """Push a session archive to the server.

        Uses PUT /api/v1/sessions/{id}/sync with If-Match header for updates.
        Returns new ETag or raises SyncConflictError.
        """
        headers: dict[str, str] = {}
        if etag:
            headers["If-Match"] = f'"{etag}"'
        if extra_headers:
            headers.update(extra_headers)

        try:
            resp = await self._request_with_retry(
                "PUT",
                f"{self.api_url}/api/v1/sessions/{session_id}/sync",
                headers=headers,
                files={"file": ("session.tar.gz", archive_data, "application/gzip")},
                timeout=self._transfer_timeout(),
                retry_read_timeout=False,
            )
        except SyncTimeoutError as exc:
            raise SyncTimeoutError(
                exc.timeout_s, len(archive_data), gateway=exc.gateway
            ) from exc
        self._check_auth(resp)

        if resp.status_code == 409:
            body = resp.json()
            # Server wraps errors as {"error": {"code": ..., "message": ..., "details": {...}}}
            error = body.get("error", {})
            details = error.get("details", {})
            raise SyncConflictError(
                current_etag=details.get("current_etag", ""),
                message=error.get("message", "ETag mismatch"),
            )

        if resp.status_code == 410:
            raise SyncDeletedError(session_id)

        if resp.status_code not in (200, 201):
            # Parse friendly error message from server response
            try:
                body = resp.json()
                error = body.get("error", {})
                message = error.get("message", resp.text) if isinstance(error, dict) else str(error)
            except Exception:
                body = {}
                message = resp.text

            if "Member too large" in message or resp.status_code == 413:
                # Prefer the server's structured detail (which includes
                # tier-aware suggestion text) when available — otherwise
                # fall back to a generic message that matches the legacy
                # contract. Server detail since v0.9.9.7 carries:
                # detail.message, detail.suggestion, detail.limit_bytes.
                detail = body.get("detail") if isinstance(body, dict) else None
                if isinstance(detail, dict) and detail.get("message"):
                    server_msg = detail["message"]
                    suggestion = detail.get("suggestion") or (
                        "Try /compact in your AI tool to start a fresh session."
                    )
                    raise SyncTooLargeError(
                        session_id, f"{server_msg} {suggestion}"
                    )
                raise SyncTooLargeError(
                    session_id,
                    f"Session too large to sync ({len(archive_data) // (1024*1024)}MB). "
                    f"This session exceeds the upload limit. "
                    f"Try /compact in your AI tool to start a fresh session."
                )

            raise SyncError(f"Push failed ({resp.status_code}): {message}")

        data = resp.json()
        return SyncResult(
            session_id=data["session_id"],
            etag=data["etag"],
            blob_size_bytes=data["blob_size_bytes"],
            synced_at=data["synced_at"],
            created=resp.status_code == 201,
        )

    async def pull_session(
        self, session_id: str, etag: str | None = None
    ) -> PullResult:
        """Pull a session from the server.

        Uses GET /api/v1/sessions/{id}/sync with If-None-Match header.
        Returns archive bytes or not_modified=True if unchanged.
        """
        headers: dict[str, str] = {}
        if etag:
            headers["If-None-Match"] = f'"{etag}"'

        resp = await self._request_with_retry(
            "GET",
            f"{self.api_url}/api/v1/sessions/{session_id}/sync",
            headers=headers,
            timeout=self._transfer_timeout(),
        )
        self._check_auth(resp)

        if resp.status_code == 304:
            return PullResult(session_id=session_id, not_modified=True, etag=etag or "")

        if resp.status_code == 404:
            raise SyncError(f"Session {session_id} not found on server")

        if resp.status_code != 200:
            raise SyncError(f"Pull failed: {resp.status_code} {resp.text}")

        remote_etag = resp.headers.get("etag", "").strip('"')
        return PullResult(
            session_id=session_id,
            data=resp.content,
            etag=remote_etag,
        )

    async def list_remote_sessions(
        self,
        *,
        page: int = 1,
        page_size: int = 50,
        source_tool: str | None = None,
        tag: str | None = None,
    ) -> RemoteListResult:
        """List sessions on the server. GET /api/v1/sessions."""
        params: dict[str, Any] = {"page": page, "page_size": page_size}
        if source_tool:
            params["source_tool"] = source_tool
        if tag:
            params["tag"] = tag

        resp = await self._request_with_retry(
            "GET",
            f"{self.api_url}/api/v1/sessions",
            params=params,
        )
        self._check_auth(resp)
        resp.raise_for_status()

        data = resp.json()
        sessions = [
            RemoteSession(
                id=s["id"],
                title=s.get("title"),
                source_tool=s.get("source_tool", ""),
                model_id=s.get("model_id"),
                message_count=s.get("message_count", 0),
                etag=s.get("etag", ""),
                created_at=s.get("created_at", ""),
                updated_at=s.get("updated_at", ""),
                blob_size_bytes=s.get("blob_size_bytes", 0),
            )
            for s in data.get("sessions", [])
        ]
        return RemoteListResult(
            sessions=sessions,
            total=data.get("total", 0),
            page=data.get("page", 1),
            has_more=data.get("has_more", False),
        )

    async def get_session_detail(self, session_id: str) -> dict[str, Any]:
        """Get session metadata from the server. GET /api/v1/sessions/{id}."""
        resp = await self._request_with_retry(
            "GET",
            f"{self.api_url}/api/v1/sessions/{session_id}",
        )
        self._check_auth(resp)
        if resp.status_code == 404:
            raise SyncError(f"Session {session_id} not found on server")
        resp.raise_for_status()
        return resp.json()
