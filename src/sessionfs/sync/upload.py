"""Chunked, resumable upload client (GitHub #91).

Wraps a single-shot ``SyncClient.push_session`` call. When the archive exceeds
``CHUNK_THRESHOLD_BYTES`` and the server advertises the chunked endpoints, the
archive is split into parts and uploaded under a resume file in the store dir
(``<store_dir>/uploads/<session_id>.json``). Each part goes through the shared
retry helper (three attempts, exponential backoff); a later run re-reads the
resume file and sends only the parts the server has not recorded yet.

If the server lacks the endpoints (404/405 on initiate) the caller falls back
to single-shot; the result is cached per process so we don't probe again.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from pathlib import Path

import httpx

from sessionfs.sync.client import (
    SyncError,
    SyncResult,
    _raise_push_error,
)

logger = logging.getLogger("sessionfs.sync")

# Per-process: has this server's chunked-upload endpoint been seen to 404/405?
_supports_chunked: dict[str, bool] = {}


class ChunkedUnsupported(SyncError):
    """The server does not implement chunked uploads; fall back to single-shot."""


def _state_path(store_dir: str | os.PathLike, session_id: str) -> Path:
    return Path(store_dir) / "uploads" / f"{session_id}.json"


def _load_state(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    if not isinstance(data.get("upload_id"), str):
        return None
    if not isinstance(data.get("sha256"), str):
        return None
    if not isinstance(data.get("size"), int):
        return None
    return data


def _save_state(path: Path, upload_id: str, sha256: str, size: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps({"upload_id": upload_id, "sha256": sha256, "size": size}))
    os.replace(tmp, path)


def _clear_state(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass


def _safe_json(resp: httpx.Response) -> dict:
    try:
        data = resp.json()
        return data if isinstance(data, dict) else {}
    except ValueError:
        return {}


def _error_text(resp: httpx.Response, body: dict) -> str:
    err = body.get("error")
    if isinstance(err, dict) and err.get("message"):
        return str(err["message"])
    return resp.text or f"HTTP {resp.status_code}"


async def _initiate(
    client,
    session_id: str,
    total_size: int,
    sha256: str,
    etag: str | None,
    extra_headers: dict[str, str] | None,
) -> dict:
    headers: dict[str, str] = {}
    if etag:
        headers["If-Match"] = f'"{etag}"'
    if extra_headers:
        headers.update(extra_headers)

    resp = await client._request_with_retry(
        "POST",
        f"{client.api_url}/api/v1/sessions/{session_id}/uploads",
        json={"total_size": total_size, "sha256": sha256},
        headers=headers,
    )
    if resp.status_code in (404, 405):
        _supports_chunked[client.api_url] = False
        raise ChunkedUnsupported()
    client._check_auth(resp)

    body = _safe_json(resp)
    if resp.status_code == 409:
        err = body.get("error")
        if isinstance(err, dict) and err.get("code") == "too_many_pending_uploads":
            # The user has too many pending chunked uploads right now; the
            # single-shot path doesn't use that budget, so fall back to it.
            raise ChunkedUnsupported()
    if resp.status_code != 201:
        # Same error mapping as single-shot push and chunked complete, so the
        # daemon/CLI handle a stale ETag (409), deleted session (410) or
        # over-limit archive (413) identically on the initiate path.
        _raise_push_error(resp, session_id, size_bytes=total_size)
        raise AssertionError("unreachable")
    return resp.json()


async def _get_status(client, upload_id: str) -> dict | None:
    resp = await client._request_with_retry(
        "GET", f"{client.api_url}/api/v1/uploads/{upload_id}"
    )
    if resp.status_code == 404:
        return None
    client._check_auth(resp)
    if resp.status_code != 200:
        return None
    return resp.json()


async def _abort(client, upload_id: str) -> None:
    try:
        await client._request_with_retry(
            "DELETE", f"{client.api_url}/api/v1/uploads/{upload_id}"
        )
    except SyncError:
        pass


async def _put_part(client, upload_id: str, part_number: int, chunk: bytes) -> None:
    part_sha256 = hashlib.sha256(chunk).hexdigest()
    # The shared retry helper already gives us three attempts with exponential
    # backoff on transient 429/5xx/network failures, which is the part-level
    # retry contract. A 400/404/409 is a hard failure: nothing a retry fixes.
    resp = await client._request_with_retry(
        "PUT",
        f"{client.api_url}/api/v1/uploads/{upload_id}/parts/{part_number}",
        content=chunk,
        headers={"X-Part-SHA256": part_sha256},
        timeout=client._transfer_timeout(),
    )
    client._check_auth(resp)
    if resp.status_code == 200:
        return
    body = _safe_json(resp)
    raise SyncError(
        f"Part {part_number} upload failed ({resp.status_code}): {_error_text(resp, body)}"
    )


async def _complete(
    client,
    upload_id: str,
    extra_headers: dict[str, str] | None,
    etag: str | None,
) -> httpx.Response:
    headers = dict(extra_headers or {})
    if etag:
        headers["If-Match"] = f'"{etag}"'
    resp = await client._request_with_retry(
        "POST",
        f"{client.api_url}/api/v1/uploads/{upload_id}/complete",
        headers=headers,
        timeout=client._transfer_timeout(),
        retry_read_timeout=False,
    )
    client._check_auth(resp)
    return resp


def _parse_complete_result(resp: httpx.Response, session_id: str) -> SyncResult:
    if resp.status_code in (200, 201):
        data = resp.json()
        return SyncResult(
            session_id=data["session_id"],
            etag=data["etag"],
            blob_size_bytes=data["blob_size_bytes"],
            synced_at=data["synced_at"],
            created=resp.status_code == 201,
        )

    # Shared with single-shot push so a chunked complete raises the same
    # error types (SyncConflictError / SyncDeletedError / SyncTooLargeError)
    # the daemon and CLI already handle.
    _raise_push_error(resp, session_id)
    raise AssertionError("unreachable")


async def push_session_chunked(
    client,
    session_id: str,
    archive_data: bytes,
    *,
    etag: str | None,
    extra_headers: dict[str, str] | None,
    store_dir: str | os.PathLike,
) -> SyncResult:
    """Push ``archive_data`` via chunked, resumable uploads, resuming if possible."""
    if _supports_chunked.get(client.api_url) is False:
        raise ChunkedUnsupported()

    total_size = len(archive_data)
    sha256 = hashlib.sha256(archive_data).hexdigest()
    state_path = _state_path(store_dir, session_id)
    state = _load_state(state_path)

    upload_id: str | None = None
    part_size: int = 0
    part_count: int = 0
    received: set[int] = set()

    if state is not None:
        if state["sha256"] == sha256 and state["size"] == total_size:
            status = await _get_status(client, state["upload_id"])
            if status is None:
                # Gone: abort whatever is left behind and start fresh.
                await _abort(client, state["upload_id"])
                state = None
            elif status.get("status") == "pending":
                upload_id = state["upload_id"]
                part_size = int(status.get("part_size", 0))
                part_count = int(status.get("part_count", 0))
                received = {int(n) for n in status.get("received", [])}
            elif status.get("status") == "completed":
                # All parts were uploaded and completed earlier, but the
                # response was lost: replay the stored result.
                resp = await _complete(client, state["upload_id"], extra_headers, etag)
                result = _parse_complete_result(resp, session_id)
                _clear_state(state_path)
                return result
            else:
                # Expired / aborted / other terminal: start fresh.
                await _abort(client, state["upload_id"])
                state = None
        else:
            # The archive changed since the interrupted run: the old upload's
            # parts are for different content, so abort it and start fresh.
            await _abort(client, state["upload_id"])
            state = None

    if upload_id is None:
        init = await _initiate(
            client, session_id, total_size, sha256, etag, extra_headers
        )
        upload_id = init["upload_id"]
        part_size = int(init["part_size"])
        part_count = int(init["part_count"])
        received = set()
        _save_state(state_path, upload_id, sha256, total_size)

    for n in range(1, part_count + 1):
        if n in received:
            continue
        start = (n - 1) * part_size
        chunk = archive_data[start : start + part_size]
        await _put_part(client, upload_id, n, chunk)

    resp = await _complete(client, upload_id, extra_headers, etag)
    if resp.status_code not in (200, 201):
        # Terminal complete error: abort the upload and clear the resume
        # state so a retry starts clean, then map to the shared error type
        # (transient errors raised by _complete never reach this point).
        await _abort(client, upload_id)
        _clear_state(state_path)
    result = _parse_complete_result(resp, session_id)
    _clear_state(state_path)
    return result
