"""Daemon auto-audit on sync (tk_e3eecad8c3874d92).

The server audits every session inside the request, so an audit call can
take minutes. The daemon never waits for it on its main loop: audits go to
one worker thread (one in flight, a bounded queue with one entry per
session), large sessions are skipped with a log line, and a timeout is a
warning rather than silent.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import httpx

from sessionfs.daemon import auto_audit
from sessionfs.daemon.auto_audit import (
    AUTO_AUDIT_MAX_MESSAGES,
    AuditTarget,
    AutoAuditWorker,
)
from sessionfs.daemon.config import DaemonConfig
from sessionfs.daemon.main import DaemonSyncer

_CLIENT = SimpleNamespace(api_url="https://api.example", api_key="k")


def _syncer(tmp_path: Path, manifest: dict | None, messages: int | None = None) -> DaemonSyncer:
    config = DaemonConfig(
        store_dir=tmp_path / ".sessionfs",
        sync={"enabled": True, "api_key": "test", "auto": "all", "debounce": 1},
    )
    (tmp_path / ".sessionfs").mkdir(exist_ok=True)
    store = MagicMock()
    store.get_session_manifest.return_value = manifest
    session_dir = tmp_path / "ses_audit.sfs"
    session_dir.mkdir()
    if messages is not None:
        (session_dir / "messages.jsonl").write_text("{}\n" * messages)
    store.get_session_dir.return_value = session_dir
    syncer = DaemonSyncer(config, store)
    syncer._audit_trigger = "on_sync"
    syncer.auto_audit = MagicMock()
    return syncer


# ── what gets queued ──────────────────────────────────────────────────


def test_large_session_is_skipped_with_a_log_line(tmp_path, caplog):
    syncer = _syncer(tmp_path, {"stats": {"message_count": AUTO_AUDIT_MAX_MESSAGES}})
    with caplog.at_level(logging.INFO, logger="sfsd"):
        asyncio.run(syncer._maybe_auto_audit("ses_audit", _CLIENT))
    syncer.auto_audit.submit.assert_not_called()
    assert any("Auto-audit skipped" in r.getMessage() for r in caplog.records)


def test_count_falls_back_to_the_transcript(tmp_path):
    syncer = _syncer(tmp_path, {}, messages=AUTO_AUDIT_MAX_MESSAGES + 1)
    asyncio.run(syncer._maybe_auto_audit("ses_audit", _CLIENT))
    syncer.auto_audit.submit.assert_not_called()


def test_unknown_size_is_skipped(tmp_path):
    syncer = _syncer(tmp_path, None)
    asyncio.run(syncer._maybe_auto_audit("ses_audit", _CLIENT))
    syncer.auto_audit.submit.assert_not_called()


def test_small_session_is_queued_not_awaited(tmp_path):
    syncer = _syncer(tmp_path, {"stats": {"message_count": 40}})
    asyncio.run(syncer._maybe_auto_audit("ses_audit", _CLIENT))
    syncer.auto_audit.submit.assert_called_once_with(
        "ses_audit", AuditTarget(api_url="https://api.example", api_key="k")
    )


def test_manual_trigger_never_queues(tmp_path):
    syncer = _syncer(tmp_path, {"stats": {"message_count": 40}})
    syncer._audit_trigger = "manual"
    asyncio.run(syncer._maybe_auto_audit("ses_audit", _CLIENT))
    syncer.auto_audit.submit.assert_not_called()


# ── the worker ────────────────────────────────────────────────────────

_T = AuditTarget(api_url="https://api.example", api_key="k1")


def test_submit_never_blocks_and_one_audit_runs_at_a_time():
    gate = threading.Event()
    running = 0
    peak = 0
    seen: list[str] = []
    lock = threading.Lock()

    def send(session_id, target):
        nonlocal running, peak
        with lock:
            running += 1
            peak = max(peak, running)
        gate.wait(5)
        with lock:
            running -= 1
            seen.append(session_id)

    worker = AutoAuditWorker(send=send)
    started = time.monotonic()
    for i in range(5):
        worker.submit(f"ses_{i}", _T)
    assert time.monotonic() - started < 0.5  # the caller never waited
    gate.set()
    assert worker.wait_idle(5)
    worker.stop()
    assert peak == 1
    assert sorted(seen) == [f"ses_{i}" for i in range(5)]


def test_queue_keeps_one_entry_per_session_and_is_bounded():
    gate = threading.Event()
    seen: list[tuple[str, str]] = []

    def send(session_id, target):
        if session_id == "busy":
            gate.wait(5)
        seen.append((session_id, target.api_key))

    worker = AutoAuditWorker(send=send, max_pending=3)
    worker.submit("busy", _T)
    deadline = time.monotonic() + 5
    while worker.in_flight != "busy" and time.monotonic() < deadline:
        time.sleep(0.01)
    worker.submit("a", _T)
    worker.submit("b", _T)
    worker.submit("a", AuditTarget(api_url="https://api.example", api_key="k2"))
    assert worker.pending() == ["a", "b"]  # 'a' kept its place
    worker.submit("c", _T)
    worker.submit("d", _T)  # full: the oldest waiting entry ('a') is dropped
    assert worker.pending() == ["b", "c", "d"]
    gate.set()
    assert worker.wait_idle(5)
    worker.stop()
    assert seen == [("busy", "k1"), ("b", "k1"), ("c", "k1"), ("d", "k1")]


def test_stop_drops_the_queue_and_returns_promptly():
    gate = threading.Event()
    worker = AutoAuditWorker(send=lambda s, t: gate.wait(5))
    worker.submit("ses_1", _T)
    worker.submit("ses_2", _T)
    started = time.monotonic()
    worker.stop(timeout=0.2)
    assert time.monotonic() - started < 1.0
    assert worker.pending() == []
    worker.submit("ses_3", _T)  # ignored after stop
    assert worker.pending() == []
    gate.set()


def test_post_timeout_is_a_warning(monkeypatch, caplog):
    class _Client:
        def __init__(self, *, timeout):
            self.timeout = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, **kwargs):
            raise httpx.ReadTimeout("slow")

    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    with caplog.at_level(logging.WARNING, logger="sfsd.auto_audit"):
        auto_audit._post_audit("ses_slow", _T)
    assert any(
        r.levelno == logging.WARNING and "gave no answer" in r.getMessage()
        for r in caplog.records
    )
