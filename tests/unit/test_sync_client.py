"""Unit tests for the sync client (mocked HTTP)."""

from __future__ import annotations

import hashlib
import io
import json
import tarfile

import httpx
import pytest

from sessionfs.sync.client import (
    PullResult,
    RemoteListResult,
    RemoteSession,
    SyncAuthError,
    SyncClient,
    SyncConflictError,
    SyncError,
    SyncResult,
    SyncTooLargeError,
    _validate_url,
)


# ---------------------------------------------------------------------------
# URL validation
# ---------------------------------------------------------------------------


def test_validate_url_https_ok():
    _validate_url("https://api.sessionfs.dev")


def test_validate_url_localhost_http_ok():
    _validate_url("http://localhost:8000")
    _validate_url("http://127.0.0.1:8000")


def test_validate_url_rejects_remote_http():
    with pytest.raises(SyncError, match="HTTPS required"):
        _validate_url("http://api.sessionfs.dev")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_tar_bytes() -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        data = b'{"sfs_version": "0.1.0"}'
        info = tarfile.TarInfo(name="manifest.json")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


class MockTransport(httpx.AsyncBaseTransport):
    """Mock transport that returns canned responses."""

    def __init__(self, handler):
        self._handler = handler

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        # Read the streaming body so request.content is available
        await request.aread()
        return self._handler(request)


# ---------------------------------------------------------------------------
# Push tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_push_new_session():
    """Push to new session → 201."""
    archive = _make_tar_bytes()
    etag = hashlib.sha256(archive).hexdigest()

    def handler(request: httpx.Request):
        assert "session.tar.gz" in request.content.decode("latin-1", errors="replace")
        body = {
            "session_id": "ses_abc123",
            "etag": etag,
            "blob_size_bytes": len(archive),
            "synced_at": "2024-01-01T00:00:00Z",
        }
        return httpx.Response(201, json=body)

    client = SyncClient("http://localhost:8000", "test-key")
    client._client = httpx.AsyncClient(transport=MockTransport(handler))

    result = await client.push_session("ses_abc123", archive)
    assert result.session_id == "ses_abc123"
    assert result.etag == etag
    assert result.created is True
    await client.close()


@pytest.mark.asyncio
async def test_push_update_with_etag():
    """Push with If-Match → 200."""
    archive = _make_tar_bytes()
    new_etag = hashlib.sha256(archive).hexdigest()

    def handler(request: httpx.Request):
        assert request.headers.get("If-Match") == '"old_etag"'
        body = {
            "session_id": "ses_abc123",
            "etag": new_etag,
            "blob_size_bytes": len(archive),
            "synced_at": "2024-01-01T00:00:00Z",
        }
        return httpx.Response(200, json=body)

    client = SyncClient("http://localhost:8000", "test-key")
    client._client = httpx.AsyncClient(transport=MockTransport(handler))

    result = await client.push_session("ses_abc123", archive, etag="old_etag")
    assert result.created is False
    await client.close()


@pytest.mark.asyncio
async def test_push_conflict_raises():
    """Push with wrong etag → SyncConflictError."""

    def handler(request: httpx.Request):
        return httpx.Response(
            409,
            json={
                "error": {
                    "code": "etag_mismatch",
                    "message": "ETag mismatch",
                    "details": {"current_etag": "remote_etag_abc"},
                }
            },
        )

    client = SyncClient("http://localhost:8000", "test-key")
    client._client = httpx.AsyncClient(transport=MockTransport(handler))

    with pytest.raises(SyncConflictError) as exc_info:
        await client.push_session("ses_abc123", b"data", etag="wrong")
    assert exc_info.value.current_etag == "remote_etag_abc"
    await client.close()


@pytest.mark.asyncio
async def test_push_non_etag_409_raises_transient_sync_error():
    """A 409 without `code: etag_mismatch` is a transient failure, not a version
    conflict — the daemon retries it with backoff instead of storing an ETag."""
    def handler(request: httpx.Request):
        return httpx.Response(
            409,
            json={
                "error": {
                    "code": "upload_not_pending",
                    "message": "Upload is no longer pending",
                }
            },
        )

    client = SyncClient("http://localhost:8000", "test-key")
    client._client = httpx.AsyncClient(transport=MockTransport(handler))

    with pytest.raises(SyncError) as exc_info:
        await client.push_session("ses_abc123", b"data", etag=None)
    assert not isinstance(exc_info.value, SyncConflictError)
    assert "409" in str(exc_info.value)
    await client.close()


@pytest.mark.asyncio
async def test_push_auth_error():
    """Push with bad auth → SyncAuthError."""

    def handler(request: httpx.Request):
        return httpx.Response(401, json={"detail": "Unauthorized"})

    client = SyncClient("http://localhost:8000", "bad-key")
    client._client = httpx.AsyncClient(transport=MockTransport(handler))

    with pytest.raises(SyncAuthError):
        await client.push_session("ses_abc123", b"data")
    await client.close()


@pytest.mark.asyncio
async def test_push_member_too_large_maps_to_too_large():
    """Finding #10: single-shot push maps a 400 "Member too large" to
    SyncTooLargeError (same mapping the chunked complete shares)."""

    def handler(request: httpx.Request):
        return httpx.Response(
            400,
            json={
                "error": {
                    "code": "PAYLOAD_TOO_LARGE",
                    "message": "Member too large in session",
                }
            },
        )

    client = SyncClient("http://localhost:8000", "test-key")
    client._client = httpx.AsyncClient(transport=MockTransport(handler))

    with pytest.raises(SyncTooLargeError):
        await client.push_session("ses_abc123", b"data")
    await client.close()


# ---------------------------------------------------------------------------
# Pull tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pull_session():
    """Pull a session → 200 with data."""
    archive = _make_tar_bytes()

    def handler(request: httpx.Request):
        return httpx.Response(
            200,
            content=archive,
            headers={"ETag": '"abc123"', "Content-Type": "application/gzip"},
        )

    client = SyncClient("http://localhost:8000", "test-key")
    client._client = httpx.AsyncClient(transport=MockTransport(handler))

    result = await client.pull_session("ses_abc123")
    assert result.data == archive
    assert result.etag == "abc123"
    assert result.not_modified is False
    await client.close()


@pytest.mark.asyncio
async def test_pull_not_modified():
    """Pull with matching etag → 304."""

    def handler(request: httpx.Request):
        assert request.headers.get("If-None-Match") == '"current_etag"'
        return httpx.Response(304)

    client = SyncClient("http://localhost:8000", "test-key")
    client._client = httpx.AsyncClient(transport=MockTransport(handler))

    result = await client.pull_session("ses_abc123", etag="current_etag")
    assert result.not_modified is True
    assert result.data is None
    await client.close()


@pytest.mark.asyncio
async def test_pull_not_found():
    """Pull nonexistent session → SyncError."""

    def handler(request: httpx.Request):
        return httpx.Response(404, json={"detail": "Session not found"})

    client = SyncClient("http://localhost:8000", "test-key")
    client._client = httpx.AsyncClient(transport=MockTransport(handler))

    with pytest.raises(SyncError, match="not found"):
        await client.pull_session("ses_nonexistent")
    await client.close()


# ---------------------------------------------------------------------------
# List tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_remote_sessions():
    """List sessions from server."""

    def handler(request: httpx.Request):
        assert "page" in str(request.url)
        return httpx.Response(
            200,
            json={
                "sessions": [
                    {
                        "id": "ses_001",
                        "title": "Test",
                        "source_tool": "claude-code",
                        "model_id": "opus-4",
                        "message_count": 10,
                        "etag": "abc",
                        "created_at": "2024-01-01T00:00:00Z",
                        "updated_at": "2024-01-01T01:00:00Z",
                        "blob_size_bytes": 1024,
                    }
                ],
                "total": 1,
                "page": 1,
                "page_size": 50,
                "has_more": False,
            },
        )

    client = SyncClient("http://localhost:8000", "test-key")
    client._client = httpx.AsyncClient(transport=MockTransport(handler))

    result = await client.list_remote_sessions()
    assert len(result.sessions) == 1
    assert result.sessions[0].id == "ses_001"
    assert result.total == 1
    assert result.has_more is False
    await client.close()


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_health_check():
    """Health check returns server status."""

    def handler(request: httpx.Request):
        return httpx.Response(200, json={"status": "healthy", "version": "0.1.0"})

    client = SyncClient("http://localhost:8000", "test-key")
    client._client = httpx.AsyncClient(transport=MockTransport(handler))

    result = await client.health_check()
    assert result["status"] == "healthy"
    await client.close()


# ---------------------------------------------------------------------------
# Retry logic
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_retry_on_503():
    """Client retries on 503 then succeeds."""
    call_count = 0

    def handler(request: httpx.Request):
        nonlocal call_count
        call_count += 1
        if call_count < 3:
            return httpx.Response(503, text="Service Unavailable")
        return httpx.Response(200, json={"status": "healthy"})

    client = SyncClient("http://localhost:8000", "test-key")
    client._client = httpx.AsyncClient(transport=MockTransport(handler))

    result = await client.health_check()
    assert result["status"] == "healthy"
    assert call_count == 3
    await client.close()


@pytest.mark.asyncio
async def test_retry_exhausted():
    """Client raises after max retries."""

    def handler(request: httpx.Request):
        return httpx.Response(503, text="Service Unavailable")

    client = SyncClient("http://localhost:8000", "test-key")
    client._client = httpx.AsyncClient(transport=MockTransport(handler))

    with pytest.raises(SyncError, match="failed after"):
        await client.health_check()
    await client.close()


# ---------------------------------------------------------------------------
# Transfer timeout (GitHub #68)
# ---------------------------------------------------------------------------


from sessionfs.sync.client import (  # noqa: E402
    DEFAULT_TRANSFER_TIMEOUT,
    SyncTimeoutError,
    resolve_transfer_timeout,
)


def test_transfer_timeout_default(monkeypatch):
    monkeypatch.delenv("SFS_UPLOAD_TIMEOUT", raising=False)
    assert resolve_transfer_timeout() == DEFAULT_TRANSFER_TIMEOUT == 300.0


def test_transfer_timeout_precedence(monkeypatch):
    monkeypatch.setenv("SFS_UPLOAD_TIMEOUT", "120")
    assert resolve_transfer_timeout(explicit=45, configured=600) == 45
    assert resolve_transfer_timeout(configured=600) == 120
    monkeypatch.delenv("SFS_UPLOAD_TIMEOUT")
    assert resolve_transfer_timeout(configured=600) == 600


@pytest.mark.parametrize("bad", ["abc", "0", "-5", "nan", "inf", "99999"])
def test_transfer_timeout_bad_env_falls_back(monkeypatch, bad):
    monkeypatch.setenv("SFS_UPLOAD_TIMEOUT", bad)
    assert resolve_transfer_timeout(configured=90) == 90
    assert resolve_transfer_timeout() == DEFAULT_TRANSFER_TIMEOUT


@pytest.mark.parametrize("bad", [0, -1, 5000, True])
def test_transfer_timeout_bad_explicit_raises(bad):
    with pytest.raises(ValueError):
        resolve_transfer_timeout(explicit=bad)


def test_client_honours_env_timeout(monkeypatch):
    monkeypatch.setenv("SFS_UPLOAD_TIMEOUT", "150")
    assert SyncClient("http://localhost:8000", "k").transfer_timeout == 150
    assert SyncClient(
        "http://localhost:8000", "k", transfer_timeout=20
    ).transfer_timeout == 20


@pytest.mark.asyncio
async def test_push_uses_transfer_timeout_not_metadata_timeout():
    seen = {}

    def handler(request: httpx.Request):
        seen.update(request.extensions["timeout"])
        return httpx.Response(201, json={
            "session_id": "ses_abc123", "etag": "e",
            "blob_size_bytes": 1, "synced_at": "2024-01-01T00:00:00Z",
        })

    client = SyncClient("http://localhost:8000", "k", transfer_timeout=240)
    client._client = httpx.AsyncClient(transport=MockTransport(handler))
    await client.push_session("ses_abc123", _make_tar_bytes())
    await client.close()
    assert seen["read"] == 240 and seen["write"] == 240
    assert seen["connect"] == 10.0


@pytest.mark.asyncio
async def test_push_read_timeout_is_not_retried_and_explains_fix():
    calls = 0

    def handler(request: httpx.Request):
        nonlocal calls
        calls += 1
        raise httpx.ReadTimeout("", request=request)

    archive = _make_tar_bytes()
    client = SyncClient("http://localhost:8000", "k", transfer_timeout=42)
    client._client = httpx.AsyncClient(transport=MockTransport(handler))
    with pytest.raises(SyncTimeoutError) as info:
        await client.push_session("ses_abc123", archive)
    await client.close()

    assert calls == 1
    assert info.value.timeout_s == 42
    msg = str(info.value)
    assert "42s" in msg and f"{len(archive):,} bytes" in msg
    assert "--timeout" in msg and "SFS_UPLOAD_TIMEOUT" in msg


@pytest.mark.asyncio
async def test_pull_read_timeout_still_retried(monkeypatch):
    import asyncio as _asyncio

    async def _no_sleep(_s):
        return None

    monkeypatch.setattr(_asyncio, "sleep", _no_sleep)
    calls = 0

    def handler(request: httpx.Request):
        nonlocal calls
        calls += 1
        if calls < 3:
            raise httpx.ReadTimeout("", request=request)
        return httpx.Response(200, content=b"x", headers={"etag": '"e"'})

    client = SyncClient("http://localhost:8000", "k")
    client._client = httpx.AsyncClient(transport=MockTransport(handler))
    result = await client.pull_session("ses_abc123")
    await client.close()
    assert calls == 3 and result.data == b"x"


@pytest.mark.asyncio
async def test_exhausted_retries_never_report_a_blank_error(monkeypatch):
    import asyncio as _asyncio

    async def _no_sleep(_s):
        return None

    monkeypatch.setattr(_asyncio, "sleep", _no_sleep)

    def handler(request: httpx.Request):
        raise httpx.ConnectTimeout("", request=request)

    client = SyncClient("http://localhost:8000", "k")
    client._client = httpx.AsyncClient(transport=MockTransport(handler))
    with pytest.raises(SyncError) as info:
        await client.health_check()
    await client.close()
    assert str(info.value).endswith("ConnectTimeout")


def test_sync_config_bad_upload_timeout_does_not_break_config():
    from sessionfs.daemon.config import SyncConfig

    assert SyncConfig().upload_timeout == 300.0
    assert SyncConfig(upload_timeout=600).upload_timeout == 600
    assert SyncConfig(upload_timeout="soon").upload_timeout is None
    assert SyncConfig(upload_timeout=-3).upload_timeout is None
    assert resolve_transfer_timeout(configured=None) == DEFAULT_TRANSFER_TIMEOUT


def test_cli_sync_client_timeout_precedence(monkeypatch):
    from sessionfs.cli import cmd_cloud
    from sessionfs.daemon.config import DaemonConfig, SyncConfig

    monkeypatch.delenv("SFS_UPLOAD_TIMEOUT", raising=False)
    monkeypatch.setattr(
        cmd_cloud, "_load_sync_config",
        lambda: {"api_url": "http://localhost:8000", "api_key": "k", "enabled": True},
    )
    monkeypatch.setattr(
        "sessionfs.daemon.config.load_config",
        lambda *a, **k: DaemonConfig(sync=SyncConfig(upload_timeout=600)),
    )
    assert cmd_cloud._get_sync_client().transfer_timeout == 600
    assert cmd_cloud._get_sync_client(timeout=45).transfer_timeout == 45


def test_push_help_lists_timeout_flag():
    from typer.testing import CliRunner

    from sessionfs.cli.main import app

    from tests.utils.ansi import strip_ansi

    result = CliRunner().invoke(app, ["push", "--help"])
    assert result.exit_code == 0
    assert "--timeout" in strip_ansi(result.output)


@pytest.mark.asyncio
async def test_push_gateway_504_is_not_resent():
    calls = 0

    def handler(request: httpx.Request):
        nonlocal calls
        calls += 1
        return httpx.Response(504, text="Gateway Time-out")

    client = SyncClient("http://localhost:8000", "k")
    client._client = httpx.AsyncClient(transport=MockTransport(handler))
    with pytest.raises(SyncTimeoutError) as info:
        await client.push_session("ses_abc123", _make_tar_bytes())
    await client.close()
    assert calls == 1
    assert info.value.gateway is True
    assert "504" in str(info.value) and "apiProxyTimeout" in str(info.value)


def test_transfer_timeout_huge_number_does_not_break_config():
    from sessionfs.daemon.config import SyncConfig

    assert SyncConfig(upload_timeout=10**400).upload_timeout is None


def test_push_rejects_nan_timeout():
    from typer.testing import CliRunner

    from sessionfs.cli.main import app

    result = CliRunner().invoke(app, ["push", "ses_x", "--timeout", "nan"])
    assert result.exit_code == 2
    assert "Unexpected error" not in result.output


@pytest.mark.asyncio
async def test_push_write_timeout_is_not_resent():
    calls = 0

    def handler(request: httpx.Request):
        nonlocal calls
        calls += 1
        raise httpx.WriteTimeout("", request=request)

    client = SyncClient("http://localhost:8000", "k", transfer_timeout=30)
    client._client = httpx.AsyncClient(transport=MockTransport(handler))
    with pytest.raises(SyncTimeoutError) as info:
        await client.push_session("ses_abc123", _make_tar_bytes())
    await client.close()
    assert calls == 1 and "did not finish within 30s" in str(info.value)


def test_transfer_timeout_below_one_second_is_ignored(monkeypatch):
    monkeypatch.setenv("SFS_UPLOAD_TIMEOUT", "0.5")
    assert resolve_transfer_timeout() == DEFAULT_TRANSFER_TIMEOUT
