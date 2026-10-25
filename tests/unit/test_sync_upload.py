"""Unit tests for the chunked, resumable upload client (GitHub #91).

Exercises ``sessionfs.sync.upload`` against a mock HTTP transport: the happy
path, resume (only missing parts are resent), and the single-shot fallback
when the server does not implement the chunked endpoints.
"""

from __future__ import annotations

import hashlib

import httpx
import pytest

from sessionfs.sync import upload as upload_mod
from sessionfs.sync.client import (
    SyncClient,
    SyncConflictError,
    SyncDeletedError,
    SyncError,
    SyncTooLargeError,
)
from sessionfs.sync.upload import (
    _initiate,
    _parse_complete_result,
    _save_state,
    _state_path,
    push_session_chunked,
)


class MockTransport(httpx.AsyncBaseTransport):
    def __init__(self, handler):
        self._handler = handler

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        await request.aread()
        return self._handler(request)


@pytest.fixture(autouse=True)
def _clear_chunked_cache():
    upload_mod._supports_chunked.clear()
    yield
    upload_mod._supports_chunked.clear()


_PART_SIZE = 5 * 1024 * 1024


def _complete_body(archive: bytes, session_id: str) -> dict:
    return {
        "session_id": session_id,
        "etag": hashlib.sha256(archive).hexdigest(),
        "blob_size_bytes": len(archive),
        "synced_at": "2026-01-01T00:00:00Z",
    }


@pytest.mark.asyncio
async def test_chunked_happy_path(tmp_path):
    archive = b"x" * (12 * 1024 * 1024)  # three 5 MiB parts (last short)
    part_count = 3
    uploaded: list[int] = []

    def handler(request: httpx.Request):
        method, path = request.method, request.url.path
        if method == "POST" and path.endswith("/uploads"):
            return httpx.Response(
                201,
                json={
                    "upload_id": "up_new1234",
                    "part_size": _PART_SIZE,
                    "part_count": part_count,
                    "expires_at": "2026-01-02T00:00:00Z",
                },
            )
        if method == "PUT" and "/parts/" in path:
            uploaded.append(int(path.rsplit("/", 1)[1]))
            return httpx.Response(200, json={"ok": True})
        if method == "POST" and path.endswith("/complete"):
            return httpx.Response(201, json=_complete_body(archive, "ses_abc123"))
        raise AssertionError(f"unexpected {method} {path}")

    client = SyncClient("http://localhost:8000", "test-key")
    client._client = httpx.AsyncClient(transport=MockTransport(handler))

    result = await push_session_chunked(
        client, "ses_abc123", archive, etag=None, extra_headers=None, store_dir=tmp_path
    )
    await client.close()

    assert uploaded == [1, 2, 3]
    assert result.created is True
    assert result.etag == hashlib.sha256(archive).hexdigest()
    assert not _state_path(tmp_path, "ses_abc123").exists()


@pytest.mark.asyncio
async def test_resume_sends_only_missing_parts(tmp_path):
    archive = b"y" * (12 * 1024 * 1024)
    part_count = 3
    sha256 = hashlib.sha256(archive).hexdigest()
    uploaded: list[int] = []

    # Simulate a prior interrupted run: parts 1 and 3 reached the server.
    _save_state(_state_path(tmp_path, "ses_abc123"), "up_abc123", sha256, len(archive))

    def handler(request: httpx.Request):
        method, path = request.method, request.url.path
        if method == "GET" and path.endswith("/uploads/up_abc123"):
            return httpx.Response(
                200,
                json={
                    "status": "pending",
                    "part_size": _PART_SIZE,
                    "part_count": part_count,
                    "received": [1, 3],
                },
            )
        if method == "PUT" and "/parts/" in path:
            uploaded.append(int(path.rsplit("/", 1)[1]))
            return httpx.Response(200, json={"ok": True})
        if method == "POST" and path.endswith("/complete"):
            return httpx.Response(201, json=_complete_body(archive, "ses_abc123"))
        raise AssertionError(f"unexpected {method} {path}")

    client = SyncClient("http://localhost:8000", "test-key")
    client._client = httpx.AsyncClient(transport=MockTransport(handler))

    result = await push_session_chunked(
        client, "ses_abc123", archive, etag=None, extra_headers=None, store_dir=tmp_path
    )
    await client.close()

    assert uploaded == [2]
    assert result.etag == sha256
    assert not _state_path(tmp_path, "ses_abc123").exists()


@pytest.mark.asyncio
async def test_stale_resume_state_aborts_and_restarts(tmp_path):
    archive = b"z" * (12 * 1024 * 1024)
    part_count = 3
    sha256 = hashlib.sha256(archive).hexdigest()
    aborted: list[str] = []
    uploaded: list[int] = []

    # Prior run's state points at an upload the server no longer has pending.
    _save_state(_state_path(tmp_path, "ses_abc123"), "up_stale123", sha256, len(archive))

    def handler(request: httpx.Request):
        method, path = request.method, request.url.path
        if method == "GET" and path.endswith("/uploads/up_stale123"):
            return httpx.Response(404, json={"detail": "Not Found"})
        if method == "DELETE" and path.endswith("/uploads/up_stale123"):
            aborted.append("up_stale123")
            return httpx.Response(200, json={"status": "aborted"})
        if method == "POST" and path.endswith("/uploads"):
            return httpx.Response(
                201,
                json={
                    "upload_id": "up_fresh123",
                    "part_size": _PART_SIZE,
                    "part_count": part_count,
                    "expires_at": "2026-01-02T00:00:00Z",
                },
            )
        if method == "PUT" and "/parts/" in path:
            uploaded.append(int(path.rsplit("/", 1)[1]))
            return httpx.Response(200, json={"ok": True})
        if method == "POST" and path.endswith("/complete"):
            return httpx.Response(201, json=_complete_body(archive, "ses_abc123"))
        raise AssertionError(f"unexpected {method} {path}")

    client = SyncClient("http://localhost:8000", "test-key")
    client._client = httpx.AsyncClient(transport=MockTransport(handler))

    result = await push_session_chunked(
        client, "ses_abc123", archive, etag=None, extra_headers=None, store_dir=tmp_path
    )
    await client.close()

    assert aborted == ["up_stale123"]
    assert uploaded == [1, 2, 3]
    assert result.etag == sha256


@pytest.mark.asyncio
async def test_push_session_falls_back_to_single_shot(tmp_path):
    archive = b"q" * (9 * 1024 * 1024)  # above the chunk threshold
    single_shot = False

    def handler(request: httpx.Request):
        nonlocal single_shot
        method, path = request.method, request.url.path
        if method == "POST" and path.endswith("/uploads"):
            return httpx.Response(404, json={"detail": "Not Found"})
        if method == "PUT" and path.endswith("/sync"):
            single_shot = True
            return httpx.Response(201, json=_complete_body(archive, "ses_abc123"))
        raise AssertionError(f"unexpected {method} {path}")

    client = SyncClient("http://localhost:8000", "test-key")
    client._client = httpx.AsyncClient(transport=MockTransport(handler))

    result = await client.push_session("ses_abc123", archive, store_dir=tmp_path)
    await client.close()

    assert single_shot is True
    assert result.created is True
    assert upload_mod._supports_chunked[client.api_url] is False


@pytest.mark.asyncio
async def test_too_many_pending_falls_back_to_single_shot(tmp_path):
    """Finding #2e: a 409 too_many_pending_uploads at initiate falls back to
    single-shot (which doesn't use the pending-upload budget)."""
    archive = b"q" * (9 * 1024 * 1024)
    single_shot = False

    def handler(request: httpx.Request):
        nonlocal single_shot
        method, path = request.method, request.url.path
        if method == "POST" and path.endswith("/uploads"):
            return httpx.Response(
                409,
                json={
                    "error": {
                        "code": "too_many_pending_uploads",
                        "message": "Too many pending uploads",
                    }
                },
            )
        if method == "PUT" and path.endswith("/sync"):
            single_shot = True
            return httpx.Response(201, json=_complete_body(archive, "ses_abc123"))
        raise AssertionError(f"unexpected {method} {path}")

    client = SyncClient("http://localhost:8000", "test-key")
    client._client = httpx.AsyncClient(transport=MockTransport(handler))

    result = await client.push_session("ses_abc123", archive, store_dir=tmp_path)
    await client.close()

    assert single_shot is True
    assert result.created is True
    # The cap is transient, not a "server lacks chunked uploads" signal.
    assert upload_mod._supports_chunked.get(client.api_url) is not False


@pytest.mark.asyncio
async def test_complete_terminal_error_aborts_and_clears_state(tmp_path):
    """Finding #1: a terminal complete error aborts the upload and clears the
    resume state so a retry starts clean."""
    archive = b"x" * (12 * 1024 * 1024)
    part_count = 3
    sha256 = hashlib.sha256(archive).hexdigest()
    aborted: list[str] = []
    uploaded: list[int] = []

    _save_state(_state_path(tmp_path, "ses_abc123"), "up_abc123", sha256, len(archive))

    def handler(request: httpx.Request):
        method, path = request.method, request.url.path
        if method == "GET" and path.endswith("/uploads/up_abc123"):
            return httpx.Response(
                200,
                json={
                    "status": "pending",
                    "part_size": _PART_SIZE,
                    "part_count": part_count,
                    "received": [],
                },
            )
        if method == "PUT" and "/parts/" in path:
            uploaded.append(int(path.rsplit("/", 1)[1]))
            return httpx.Response(200, json={"ok": True})
        if method == "POST" and path.endswith("/complete"):
            return httpx.Response(
                409,
                json={
                    "error": {
                        "code": "etag_mismatch",
                        "message": "ETag mismatch",
                        "details": {"current_etag": "new"},
                    }
                },
            )
        if method == "DELETE" and path.endswith("/uploads/up_abc123"):
            aborted.append("up_abc123")
            return httpx.Response(200, json={"status": "aborted"})
        raise AssertionError(f"unexpected {method} {path}")

    client = SyncClient("http://localhost:8000", "test-key")
    client._client = httpx.AsyncClient(transport=MockTransport(handler))

    with pytest.raises(SyncConflictError):
        await push_session_chunked(
            client, "ses_abc123", archive, etag=None, extra_headers=None, store_dir=tmp_path
        )
    await client.close()

    assert uploaded == [1, 2, 3]
    assert aborted == ["up_abc123"]
    assert not _state_path(tmp_path, "ses_abc123").exists()


@pytest.mark.asyncio
async def test_sha_mismatch_aborts_old_upload_and_restarts(tmp_path):
    """Finding #2a: when the archive changed since the interrupted run, the old
    upload (staged for different content) is aborted before restarting."""
    archive = b"y" * (12 * 1024 * 1024)
    part_count = 3
    sha256 = hashlib.sha256(archive).hexdigest()
    aborted: list[str] = []
    uploaded: list[int] = []

    _save_state(_state_path(tmp_path, "ses_abc123"), "up_old123", "0" * 64, len(archive))

    def handler(request: httpx.Request):
        method, path = request.method, request.url.path
        if method == "DELETE" and path.endswith("/uploads/up_old123"):
            aborted.append("up_old123")
            return httpx.Response(200, json={"status": "aborted"})
        if method == "POST" and path.endswith("/uploads"):
            return httpx.Response(
                201,
                json={
                    "upload_id": "up_new1234",
                    "part_size": _PART_SIZE,
                    "part_count": part_count,
                    "expires_at": "2026-01-02T00:00:00Z",
                },
            )
        if method == "PUT" and "/parts/" in path:
            uploaded.append(int(path.rsplit("/", 1)[1]))
            return httpx.Response(200, json={"ok": True})
        if method == "POST" and path.endswith("/complete"):
            return httpx.Response(201, json=_complete_body(archive, "ses_abc123"))
        raise AssertionError(f"unexpected {method} {path}")

    client = SyncClient("http://localhost:8000", "test-key")
    client._client = httpx.AsyncClient(transport=MockTransport(handler))

    result = await push_session_chunked(
        client, "ses_abc123", archive, etag=None, extra_headers=None, store_dir=tmp_path
    )
    await client.close()

    assert aborted == ["up_old123"]
    assert uploaded == [1, 2, 3]
    assert result.etag == sha256


@pytest.mark.asyncio
async def test_expired_status_aborts_and_restarts(tmp_path):
    """Finding #2c: a server-reported `expired` status is treated as start-over."""
    archive = b"z" * (12 * 1024 * 1024)
    part_count = 3
    sha256 = hashlib.sha256(archive).hexdigest()
    aborted: list[str] = []
    uploaded: list[int] = []

    _save_state(_state_path(tmp_path, "ses_abc123"), "up_exp123", sha256, len(archive))

    def handler(request: httpx.Request):
        method, path = request.method, request.url.path
        if method == "GET" and path.endswith("/uploads/up_exp123"):
            return httpx.Response(
                200,
                json={
                    "status": "expired",
                    "part_size": _PART_SIZE,
                    "part_count": part_count,
                    "received": [],
                },
            )
        if method == "DELETE" and path.endswith("/uploads/up_exp123"):
            aborted.append("up_exp123")
            return httpx.Response(200, json={"status": "aborted"})
        if method == "POST" and path.endswith("/uploads"):
            return httpx.Response(
                201,
                json={
                    "upload_id": "up_fresh123",
                    "part_size": _PART_SIZE,
                    "part_count": part_count,
                    "expires_at": "2026-01-02T00:00:00Z",
                },
            )
        if method == "PUT" and "/parts/" in path:
            uploaded.append(int(path.rsplit("/", 1)[1]))
            return httpx.Response(200, json={"ok": True})
        if method == "POST" and path.endswith("/complete"):
            return httpx.Response(201, json=_complete_body(archive, "ses_abc123"))
        raise AssertionError(f"unexpected {method} {path}")

    client = SyncClient("http://localhost:8000", "test-key")
    client._client = httpx.AsyncClient(transport=MockTransport(handler))

    result = await push_session_chunked(
        client, "ses_abc123", archive, etag=None, extra_headers=None, store_dir=tmp_path
    )
    await client.close()

    assert aborted == ["up_exp123"]
    assert uploaded == [1, 2, 3]
    assert result.etag == sha256


@pytest.mark.asyncio
async def test_completed_status_replays_stored_result(tmp_path):
    """Finding #4: when the server says completed, the client calls complete
    again to fetch the stored result (the earlier response was lost)."""
    archive = b"w" * (12 * 1024 * 1024)
    part_count = 3
    sha256 = hashlib.sha256(archive).hexdigest()
    completed: list[str] = []

    _save_state(_state_path(tmp_path, "ses_abc123"), "up_done123", sha256, len(archive))

    def handler(request: httpx.Request):
        method, path = request.method, request.url.path
        if method == "GET" and path.endswith("/uploads/up_done123"):
            return httpx.Response(
                200,
                json={
                    "status": "completed",
                    "part_size": _PART_SIZE,
                    "part_count": part_count,
                    "received": [1, 2, 3],
                },
            )
        if method == "POST" and path.endswith("/complete"):
            completed.append("up_done123")
            return httpx.Response(201, json=_complete_body(archive, "ses_abc123"))
        raise AssertionError(f"unexpected {method} {path}")

    client = SyncClient("http://localhost:8000", "test-key")
    client._client = httpx.AsyncClient(transport=MockTransport(handler))

    result = await push_session_chunked(
        client, "ses_abc123", archive, etag=None, extra_headers=None, store_dir=tmp_path
    )
    await client.close()

    assert completed == ["up_done123"]
    assert result.etag == sha256
    assert not _state_path(tmp_path, "ses_abc123").exists()


def _resp(status: int, body: dict) -> httpx.Response:
    return httpx.Response(status, json=body)


@pytest.mark.asyncio
async def test_initiate_stale_etag_maps_to_conflict():
    """Finding #2: an initiate 409 etag_mismatch maps to SyncConflictError with
    the remote etag, like single-shot push — not a bare SyncError."""
    def handler(request: httpx.Request):
        return httpx.Response(
            409,
            json={
                "error": {
                    "code": "etag_mismatch",
                    "message": "ETag mismatch — session has been updated",
                    "details": {"current_etag": "remote_etag_abc"},
                }
            },
        )

    client = SyncClient("http://localhost:8000", "test-key")
    client._client = httpx.AsyncClient(transport=MockTransport(handler))

    with pytest.raises(SyncConflictError) as exc_info:
        await _initiate(
            client, "ses_abc123", 100, "a" * 64, etag="stale", extra_headers=None
        )
    assert exc_info.value.current_etag == "remote_etag_abc"
    await client.close()


@pytest.mark.asyncio
async def test_initiate_deleted_maps_to_deleted():
    """Finding #2: an initiate 410 maps to SyncDeletedError so the daemon's
    deleted-session cleanup runs, like single-shot push."""
    def handler(request: httpx.Request):
        return httpx.Response(
            410,
            json={
                "error": {
                    "code": "410",
                    "message": "Session is deleted. Send X-SessionFS-Undelete: true to restore.",
                }
            },
        )

    client = SyncClient("http://localhost:8000", "test-key")
    client._client = httpx.AsyncClient(transport=MockTransport(handler))

    with pytest.raises(SyncDeletedError):
        await _initiate(
            client, "ses_abc123", 100, "a" * 64, etag=None, extra_headers=None
        )
    await client.close()


@pytest.mark.asyncio
async def test_complete_sends_if_match(tmp_path):
    """Finding #1: complete sends If-Match built from the caller's current etag,
    so an existing session can't be overwritten by an unconditional complete."""
    archive = b"x" * (12 * 1024 * 1024)
    part_count = 3
    complete_if_match: list[str] = []

    def handler(request: httpx.Request):
        method, path = request.method, request.url.path
        if method == "POST" and path.endswith("/uploads"):
            return httpx.Response(
                201,
                json={
                    "upload_id": "up_new1234",
                    "part_size": _PART_SIZE,
                    "part_count": part_count,
                    "expires_at": "2026-01-02T00:00:00Z",
                },
            )
        if method == "PUT" and "/parts/" in path:
            return httpx.Response(200, json={"ok": True})
        if method == "POST" and path.endswith("/complete"):
            complete_if_match.append(request.headers.get("If-Match"))
            return httpx.Response(201, json=_complete_body(archive, "ses_abc123"))
        raise AssertionError(f"unexpected {method} {path}")

    client = SyncClient("http://localhost:8000", "test-key")
    client._client = httpx.AsyncClient(transport=MockTransport(handler))

    result = await push_session_chunked(
        client, "ses_abc123", archive, etag="old_etag", extra_headers=None, store_dir=tmp_path
    )
    await client.close()

    assert complete_if_match == ['"old_etag"']
    assert result.etag == hashlib.sha256(archive).hexdigest()


@pytest.mark.asyncio
async def test_complete_with_etag_conflict_raises_sync_conflict(tmp_path):
    """Round 3 lost-update proof: the client keeps sending If-Match when it has an
    etag, so a push from another device (server 409 etag_mismatch at complete)
    still surfaces as SyncConflictError instead of an unconditional overwrite."""
    archive = b"x" * (12 * 1024 * 1024)
    part_count = 3
    uploaded: list[int] = []
    aborted: list[str] = []
    complete_if_match: list[str | None] = []

    def handler(request: httpx.Request):
        method, path = request.method, request.url.path
        if method == "POST" and path.endswith("/uploads"):
            return httpx.Response(
                201,
                json={
                    "upload_id": "up_race123",
                    "part_size": _PART_SIZE,
                    "part_count": part_count,
                    "expires_at": "2026-01-02T00:00:00Z",
                },
            )
        if method == "PUT" and "/parts/" in path:
            uploaded.append(int(path.rsplit("/", 1)[1]))
            return httpx.Response(200, json={"ok": True})
        if method == "POST" and path.endswith("/complete"):
            complete_if_match.append(request.headers.get("If-Match"))
            return httpx.Response(
                409,
                json={
                    "error": {
                        "code": "etag_mismatch",
                        "message": "ETag mismatch — session has been updated",
                        "details": {"current_etag": "remote_etag_abc"},
                    }
                },
            )
        if method == "DELETE" and path.endswith("/uploads/up_race123"):
            aborted.append("up_race123")
            return httpx.Response(200, json={"status": "aborted"})
        raise AssertionError(f"unexpected {method} {path}")

    client = SyncClient("http://localhost:8000", "test-key")
    client._client = httpx.AsyncClient(transport=MockTransport(handler))

    with pytest.raises(SyncConflictError) as exc_info:
        await push_session_chunked(
            client,
            "ses_abc123",
            archive,
            etag="stale_etag",
            extra_headers=None,
            store_dir=tmp_path,
        )
    await client.close()

    assert uploaded == [1, 2, 3]
    assert complete_if_match == ['"stale_etag"']
    assert exc_info.value.current_etag == "remote_etag_abc"
    assert aborted == ["up_race123"]
    assert not _state_path(tmp_path, "ses_abc123").exists()


def test_parse_complete_result_matches_single_shot_errors():
    """Finding #10: chunked complete maps to the same error types as single-shot."""
    with pytest.raises(SyncTooLargeError):
        _parse_complete_result(
            _resp(
                400,
                {
                    "error": {
                        "code": "PAYLOAD_TOO_LARGE",
                        "message": "Member too large in session",
                    }
                },
            ),
            "ses_x",
        )
    with pytest.raises(SyncTooLargeError):
        _parse_complete_result(
            _resp(413, {"error": {"message": "Session too large"}}),
            "ses_x",
        )
    with pytest.raises(SyncDeletedError):
        _parse_complete_result(
            _resp(410, {"error": {"message": "Session deleted on server"}}),
            "ses_x",
        )
    with pytest.raises(SyncConflictError):
        _parse_complete_result(
            _resp(
                409,
                {
                    "error": {
                        "code": "etag_mismatch",
                        "message": "ETag mismatch",
                        "details": {"current_etag": "abc"},
                    }
                },
            ),
            "ses_x",
        )
    with pytest.raises(SyncError):
        _parse_complete_result(
            _resp(400, {"error": {"code": "other", "message": "boom"}}),
            "ses_x",
        )
