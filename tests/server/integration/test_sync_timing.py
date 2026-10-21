"""GH #71: sync push timing (Server-Timing + one INFO line) and early 409."""

from __future__ import annotations

import io
import json
import logging
import subprocess
import sys
import time
import uuid

import pytest
from httpx import AsyncClient

from tests.utils.synthetic_session import build_session


async def _dlp_org(db_session, test_user, mode: str) -> None:
    from sessionfs.server.db.models import OrgMember, Organization

    org = Organization(
        id=f"org_{uuid.uuid4().hex[:12]}",
        name="DLP Org",
        slug=f"dlp-{uuid.uuid4().hex[:6]}",
        tier="team",
        seats_limit=10,
        settings=json.dumps({"dlp": {"enabled": True, "mode": mode, "categories": ["secrets"]}}),
    )
    db_session.add(org)
    db_session.add(OrgMember(org_id=org.id, user_id=test_user.id, role="admin"))
    await db_session.commit()


def _phases(header: str) -> dict[str, float]:
    out = {}
    for entry in header.split(","):
        name, _, dur = entry.strip().partition(";dur=")
        out[name] = float(dur)
    return out


async def _push(client: AsyncClient, headers: dict, session_id: str, archive: bytes, **extra):
    return await client.put(
        f"/api/v1/sessions/{session_id}/sync",
        headers={**headers, **extra},
        files={"file": ("s.tar.gz", io.BytesIO(archive), "application/gzip")},
    )


@pytest.mark.asyncio
async def test_push_reports_its_phases_in_server_timing_and_one_log_line(
    client: AsyncClient, auth_headers: dict, db_session, test_user, caplog
):
    await _dlp_org(db_session, test_user, "redact")
    session_id = "ses_timing123456ab"
    archive = build_session(session_id, scale=0.01, secret_scale=0.2)
    caplog.set_level(logging.INFO, logger="sessionfs.api")

    resp = await _push(client, auth_headers, session_id, archive)

    assert resp.status_code == 201, resp.text[:300]
    phases = _phases(resp.headers["server-timing"])
    for name in ("read", "validate", "extract", "dlp_scan", "dlp_redact", "blob_upload",
                 "blob_promote", "db_write", "total"):
        assert name in phases, (name, phases)
    assert all(v >= 0 for v in phases.values())

    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("sync_push ")]
    assert len(lines) == 1, lines
    line = lines[0]
    assert f"session={session_id}" in line and "status=201" in line
    assert "dlp_scan_ms=" in line and "total_ms=" in line and "dlp_mode=redact" in line
    assert f"bytes_in={len(archive)}" in line
    # Never content: the synthetic secrets and message text stay out of logs.
    for leaked in ("sk-proj-", "ghp_", "password", "Bearer ", "retry loop"):
        assert leaked not in line

    # Update path (200, pydantic response) carries the header too.
    resp2 = await _push(client, auth_headers, session_id, archive,
                        **{"If-Match": f'"{resp.json()["etag"]}"'})
    assert resp2.status_code == 200, resp2.text[:300]
    assert "total" in _phases(resp2.headers["server-timing"])


@pytest.mark.asyncio
async def test_blocked_push_still_reports_timing(
    client: AsyncClient, auth_headers: dict, db_session, test_user
):
    await _dlp_org(db_session, test_user, "block")
    session_id = "ses_timingblock1234"
    resp = await _push(client, auth_headers, session_id, build_session(session_id, scale=0.01, secret_scale=0.2))
    assert resp.status_code == 403, resp.text[:300]
    phases = _phases(resp.headers["server-timing"])
    assert "dlp_scan" in phases and "blob_upload" not in phases


@pytest.mark.asyncio
async def test_stale_if_match_fails_before_any_archive_work(
    client: AsyncClient, auth_headers: dict, sample_sfs_tar: bytes, monkeypatch
):
    """GH #71: a retried push with an out-of-date ETag spent its full DLP
    time before the 409. It now fails before the archive is processed."""
    from sessionfs.server.routes import sessions as routes

    session_id = "ses_earlyconflict12"
    first = await _push(client, auth_headers, session_id, sample_sfs_tar)
    assert first.status_code == 201

    def must_not_run(*args, **kwargs):
        raise AssertionError("archive processed for a stale push")

    monkeypatch.setattr(routes, "_prepare_sync_archive", must_not_run)
    resp = await _push(client, auth_headers, session_id, sample_sfs_tar, **{"If-Match": '"stale"'})
    assert resp.status_code == 409
    body = resp.json()["error"]
    assert body["code"] == "etag_mismatch"
    assert body["details"]["current_etag"] == first.json()["etag"]
    assert "validate" not in _phases(resp.headers["server-timing"])


def test_server_logs_info_lines_under_plain_uvicorn() -> None:
    """With nothing configured on the root logger (plain uvicorn), INFO
    lines from the API, such as the per-push timing line, reach stderr at
    SFS_LOG_LEVEL instead of being dropped."""
    script = (
        "import logging\n"
        "from sessionfs.server.app import create_app\n"
        "from sessionfs.server.config import ServerConfig\n"
        "create_app(ServerConfig(database_url='sqlite+aiosqlite://', log_level='INFO'))\n"
        "logging.getLogger('sessionfs.api').info('sync_push session=x status=201 total_ms=1')\n"
        "logging.getLogger('sessionfs.api').debug('hidden debug line')\n"
    )
    done = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stderr[-2000:]
    assert "sync_push session=x status=201" in done.stderr
    assert "hidden debug line" not in done.stderr


@pytest.mark.perf
@pytest.mark.asyncio
async def test_full_size_gh71_push_completes_in_seconds(
    client: AsyncClient, auth_headers: dict, db_session, test_user
):
    await _dlp_org(db_session, test_user, "redact")
    session_id = "ses_gh71fullsize123"
    archive = build_session(session_id, scale=1.0)
    started = time.perf_counter()
    resp = await _push(client, auth_headers, session_id, archive)
    elapsed = time.perf_counter() - started
    assert resp.status_code == 201, resp.text[:300]
    # ~5 s on a dev machine (the reported push took 841 s).
    assert elapsed < 10, (elapsed, resp.headers.get("server-timing"))
