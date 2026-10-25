"""Backoff for repeated transient sync failures (tk_14864c41f6344820).

One session the server could not accept was pushed 15,670 times, once per
push interval. Transient failures now back off (1 min, 5 min, 15 min, then
hourly), a long failing streak pauses the session until it changes, the
daemon restarts or the user asks, and the state shows in daemon status.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from sessionfs.daemon.sync_backoff import (
    BACKOFF_SCHEDULE_S,
    PAUSE_AFTER_FAILURES,
    PAUSE_AFTER_SECONDS,
    SyncBackoff,
    backoff_delay,
    session_fingerprint,
    take_retry_request,
    write_retry_request,
)


def test_delay_grows_then_caps():
    assert [backoff_delay(n) for n in range(1, 7)] == [60, 300, 900, 3600, 3600, 3600]
    assert backoff_delay(0) == 0
    assert BACKOFF_SCHEDULE_S[-1] == 3600


def test_waits_for_the_backoff_then_retries():
    b = SyncBackoff()
    b.record_failure("s1", "fp", "Push failed (500)", now_mono=1000.0, now_wall=1000.0)
    assert not b.should_attempt("s1", "fp", 1000.0 + 59)
    assert b.should_attempt("s1", "fp", 1000.0 + 60)
    b.record_failure("s1", "fp", "Push failed (500)", now_mono=1060.0, now_wall=1060.0)
    assert not b.should_attempt("s1", "fp", 1060.0 + 299)
    assert b.should_attempt("s1", "fp", 1060.0 + 300)


def test_success_resets():
    b = SyncBackoff()
    for i in range(3):
        b.record_failure("s1", "fp", "500", now_mono=float(i), now_wall=float(i))
    b.record_success("s1")
    assert b.should_attempt("s1", "fp", 0.0)
    entry = b.record_failure("s1", "fp", "500", now_mono=10.0, now_wall=10.0)
    assert entry.failures == 1


def test_long_streak_pauses_and_content_change_rearms():
    b = SyncBackoff()
    wall = 0.0
    entry = None
    for _ in range(PAUSE_AFTER_FAILURES):
        entry = b.record_failure("s1", "fp-old", "500", now_mono=wall, now_wall=wall)
        wall += PAUSE_AFTER_SECONDS / (PAUSE_AFTER_FAILURES - 1) + 1
    assert entry is not None and entry.paused
    # Paused: never due again for the same content, however long we wait.
    assert not b.should_attempt("s1", "fp-old", 10**9)
    # New content (the session was captured again) re-arms it immediately.
    assert b.should_attempt("s1", "fp-new", 0.0)
    assert "s1" not in b.entries


def test_many_quick_failures_do_not_pause_before_the_window():
    b = SyncBackoff()
    entry = None
    for i in range(PAUSE_AFTER_FAILURES * 2):
        entry = b.record_failure("s1", "fp", "500", now_mono=float(i), now_wall=float(i))
    assert entry is not None and not entry.paused


def test_clear_and_snapshot():
    b = SyncBackoff()
    b.record_failure("s1", "fp", "Push failed (500)", now_mono=0.0, now_wall=1_700_000_000.0)
    b.record_failure("s2", "fp", "timeout", now_mono=0.0, now_wall=1_700_000_000.0)
    rows = b.snapshot()
    assert {r["session_id"] for r in rows} == {"s1", "s2"}
    assert rows[0]["next_retry_at"] is not None and rows[0]["paused"] is False
    assert b.clear(["s1", "missing"]) == ["s1"]
    assert sorted(b.clear(None)) == ["s2"]
    assert b.snapshot() == []


def test_retry_request_round_trip(tmp_path):
    assert take_retry_request(tmp_path) is None
    write_retry_request(tmp_path, ["ses_a"])
    assert take_retry_request(tmp_path) == (False, ["ses_a"])
    assert take_retry_request(tmp_path) is None  # consumed
    write_retry_request(tmp_path, None)
    assert take_retry_request(tmp_path) == (True, [])


def test_fingerprint_changes_when_the_session_changes(tmp_path):
    session = tmp_path / "ses.sfs"
    session.mkdir()
    (session / "messages.jsonl").write_text("{}\n")
    before = session_fingerprint(session)
    (session / "messages.jsonl").write_text("{}\n{}\n")
    assert session_fingerprint(session) != before
    assert session_fingerprint(None) == ""


# ── daemon integration ──────────────────────────────────────────────────


def _make_syncer(tmp_path, monkeypatch):
    from sessionfs.daemon.config import DaemonConfig
    from sessionfs.daemon.main import DaemonSyncer

    monkeypatch.setenv("HOME", str(tmp_path))
    import sessionfs.store.deleted as _deleted

    _deleted._DEFAULT_DIR = tmp_path / ".sessionfs"
    _deleted._DEFAULT_PATH = _deleted._DEFAULT_DIR / "deleted.json"
    config = DaemonConfig(
        store_dir=tmp_path / ".sessionfs",
        sync={"enabled": True, "api_key": "test", "auto": "all", "debounce": 1},
    )
    (tmp_path / ".sessionfs").mkdir(exist_ok=True)
    return DaemonSyncer(config, MagicMock())


def test_daemon_does_not_retry_a_failing_session_every_cycle(tmp_path, monkeypatch):
    from sessionfs.sync.client import SyncError

    syncer = _make_syncer(tmp_path, monkeypatch)
    session_dir = tmp_path / "ses_failing.sfs"
    session_dir.mkdir()
    (session_dir / "messages.jsonl").write_text("{}\n")
    syncer.store.get_session_dir.return_value = session_dir
    syncer._pending_sessions.add("ses_failing")

    client = AsyncMock()
    client.push_session.side_effect = SyncError("Push failed (500)")
    clock = {"mono": 1000.0, "wall": 1_700_000_000.0}

    def fake_now():
        return clock["mono"], clock["wall"]

    with patch.object(syncer, "_get_client", return_value=client), patch(
        "sessionfs.daemon.sync_backoff.now_pair", fake_now
    ), patch("sessionfs.daemon.main.pack_session", create=True), patch(
        "sessionfs.sync.archive.pack_session", return_value=b"tar"
    ):
        asyncio.run(syncer._sync_sessions({"ses_failing"}))
        assert client.push_session.await_count == 1
        # Next cycle 30 s later: still backing off, so no push at all.
        clock["mono"] += 30
        asyncio.run(syncer._sync_sessions({"ses_failing"}))
        assert client.push_session.await_count == 1
        # After the 60 s backoff it is retried.
        clock["mono"] += 31
        asyncio.run(syncer._sync_sessions({"ses_failing"}))
        assert client.push_session.await_count == 2

    assert "ses_failing" in syncer._pending_sessions  # still wanted, just later
    rows = syncer.backoff.snapshot()
    assert rows[0]["session_id"] == "ses_failing" and rows[0]["failures"] == 2


def test_retry_request_makes_the_daemon_retry_now(tmp_path, monkeypatch):
    syncer = _make_syncer(tmp_path, monkeypatch)
    syncer.backoff.record_failure("ses_x", "fp", "500", now_mono=0.0, now_wall=0.0)
    write_retry_request(syncer.config.store_dir, None)
    syncer._apply_retry_request()
    assert "ses_x" not in syncer.backoff.entries
    assert "ses_x" in syncer._pending_sessions


def test_daemon_status_carries_backoff_rows():
    from sessionfs.daemon.status import DaemonStatus

    status = DaemonStatus(sync_backoff=[{"session_id": "s", "failures": 3, "paused": False}])
    assert DaemonStatus.model_validate_json(status.model_dump_json()).sync_backoff[0]["failures"] == 3


def test_database_errors_do_not_carry_bound_values():
    """The 500s behind this ticket logged every SQL parameter (session text
    included). The engine hides parameters in errors and logs."""
    import asyncio as _asyncio

    from sqlalchemy import text
    from sqlalchemy.exc import SQLAlchemyError

    from sessionfs.server.db import engine as engine_module

    async def run() -> str:
        eng = engine_module.init_engine("sqlite+aiosqlite://")
        try:
            async with eng.connect() as conn:
                await conn.execute(text("SELECT * FROM no_such_table WHERE x = :v"), {"v": "TOPSECRETVALUE"})
        except SQLAlchemyError as exc:
            return str(exc)
        finally:
            await engine_module.close_engine()
        return ""

    message = _asyncio.run(run())
    assert message
    assert "TOPSECRETVALUE" not in message


def test_rewriting_unchanged_content_keeps_the_failure_streak(tmp_path, monkeypatch):
    """Some tools rewrite unchanged sessions (new mtimes, same content). That
    must not reset the streak, or backoff never grows past 60 s."""
    import os

    syncer = _make_syncer(tmp_path, monkeypatch)
    session_dir = tmp_path / "ses_rewrite.sfs"
    session_dir.mkdir()
    messages = session_dir / "messages.jsonl"
    messages.write_text('{"a": 1}\n')

    first = syncer._content_fingerprint("ses_rewrite", session_dir)
    st = messages.stat()
    os.utime(messages, ns=(st.st_atime_ns + 10**9, st.st_mtime_ns + 10**9))
    messages.write_text('{"a": 1}\n')  # rewritten, identical content
    assert syncer._content_fingerprint("ses_rewrite", session_dir) == first

    for i in range(3):
        syncer.backoff.record_failure("ses_rewrite", first, "500", now_mono=float(i), now_wall=float(i))
    assert syncer.backoff.entries["ses_rewrite"].failures == 3

    messages.write_text('{"a": 2}\n')  # real change
    assert syncer._content_fingerprint("ses_rewrite", session_dir) != first


def test_terminal_outcomes_drop_backoff_rows(tmp_path, monkeypatch):
    """A session that later turns out missing, excluded or too large must not
    keep showing as 'backing off' in status and doctor."""
    from sessionfs.sync.client import SyncTooLargeError

    syncer = _make_syncer(tmp_path, monkeypatch)
    session_dir = tmp_path / "ses_big.sfs"
    session_dir.mkdir()
    (session_dir / "messages.jsonl").write_text("{}\n")
    syncer.store.get_session_dir.return_value = session_dir
    fp = syncer._content_fingerprint("ses_big", session_dir)
    syncer.backoff.record_failure("ses_big", fp, "500", now_mono=0.0, now_wall=0.0)
    syncer._pending_sessions.add("ses_big")

    client = AsyncMock()
    client.push_session.side_effect = SyncTooLargeError("ses_big", "too large")
    with patch.object(syncer, "_get_client", return_value=client), patch(
        "sessionfs.daemon.sync_backoff.now_pair", lambda: (10_000.0, 10_000.0)
    ), patch("sessionfs.sync.archive.pack_session", return_value=b"tar"):
        asyncio.run(syncer._sync_sessions({"ses_big"}))
    assert "ses_big" not in syncer.backoff.entries

    # Missing session directory.
    syncer.backoff.record_failure("ses_gone", "fp", "500", now_mono=0.0, now_wall=0.0)
    syncer.store.get_session_dir.return_value = None
    with patch.object(syncer, "_get_client", return_value=AsyncMock()), patch(
        "sessionfs.daemon.sync_backoff.now_pair", lambda: (10_000.0, 10_000.0)
    ):
        asyncio.run(syncer._sync_sessions({"ses_gone"}))
    assert "ses_gone" not in syncer.backoff.entries


def test_conflict_stores_remote_etag_and_does_not_back_off(tmp_path, monkeypatch):
    """A SyncConflictError is not a transient server failure: the daemon stores
    the remote etag (so the next push sends the right If-Match) and re-queues
    the session instead of waiting out the backoff schedule."""
    from sessionfs.sync.client import SyncConflictError

    syncer = _make_syncer(tmp_path, monkeypatch)
    session_dir = tmp_path / "ses_conflict.sfs"
    session_dir.mkdir()
    (session_dir / "messages.jsonl").write_text("{}\n")
    syncer.store.get_session_dir.return_value = session_dir
    syncer._pending_sessions.add("ses_conflict")

    client = AsyncMock()
    client.push_session.side_effect = SyncConflictError("remote_etag_abc")
    with patch.object(syncer, "_get_client", return_value=client), patch(
        "sessionfs.daemon.sync_backoff.now_pair", lambda: (10_000.0, 10_000.0)
    ), patch("sessionfs.sync.archive.pack_session", return_value=b"tar"), patch.object(
        syncer, "_get_local_etag", return_value=None
    ), patch.object(syncer, "_store_local_etag") as store_etag, patch.object(
        syncer, "_mark_session_dirty_flag"
    ) as mark_dirty, patch.object(syncer, "_maybe_auto_audit", AsyncMock()):
        asyncio.run(syncer._sync_sessions({"ses_conflict"}))

    store_etag.assert_called_once_with("ses_conflict", "remote_etag_abc")
    mark_dirty.assert_called_once_with("ses_conflict")
    # No backoff row: a conflict retries on the next cycle, not after a delay.
    assert syncer.backoff.snapshot() == []


def test_retry_accepts_a_unique_prefix():
    b = SyncBackoff()
    b.record_failure("ses_aaaa1111bbbb2222", "fp", "500", now_mono=0.0, now_wall=0.0)
    b.record_failure("ses_aaaa9999cccc3333", "fp", "500", now_mono=0.0, now_wall=0.0)
    assert b.clear(["ses_aaaa"]) == []  # ambiguous: clears nothing
    assert b.clear(["ses_aaaa1111"]) == ["ses_aaaa1111bbbb2222"]


def test_retry_sync_cli_targets_the_configured_store(tmp_path):
    from typer.testing import CliRunner

    from sessionfs.cli.cmd_daemon import daemon_app

    custom_store = tmp_path / "custom-store"
    config = tmp_path / "custom.toml"
    config.write_text(f'store_dir = "{custom_store}"\n')
    result = CliRunner().invoke(daemon_app, ["retry-sync", "--config", str(config)])
    assert result.exit_code == 0, result.output
    assert take_retry_request(custom_store) == (True, [])


def test_concurrent_retry_requests_are_all_honoured(tmp_path):
    write_retry_request(tmp_path, ["ses_a"])
    write_retry_request(tmp_path, ["ses_b"])
    retry_all, sessions = take_retry_request(tmp_path)
    assert retry_all is False
    assert sorted(sessions) == ["ses_a", "ses_b"]
    assert take_retry_request(tmp_path) is None


def test_paused_session_whose_folder_disappears_is_dropped(tmp_path, monkeypatch):
    syncer = _make_syncer(tmp_path, monkeypatch)
    entry = None
    wall = 0.0
    for _ in range(PAUSE_AFTER_FAILURES):
        entry = syncer.backoff.record_failure("ses_pruned", "fp", "500", now_mono=wall, now_wall=wall)
        wall += PAUSE_AFTER_SECONDS / (PAUSE_AFTER_FAILURES - 1) + 1
    assert entry is not None and entry.paused
    syncer._pending_sessions.add("ses_pruned")
    syncer.store.get_session_dir.return_value = None
    with patch.object(syncer, "_get_client", return_value=AsyncMock()), patch(
        "sessionfs.daemon.sync_backoff.now_pair", lambda: (wall, wall)
    ):
        asyncio.run(syncer._sync_sessions({"ses_pruned"}))
    assert "ses_pruned" not in syncer.backoff.entries
    assert "ses_pruned" not in syncer._pending_sessions


def test_an_unreadable_session_does_not_abort_the_batch(tmp_path, monkeypatch):
    """A fingerprint failure for one backed-off session must not stop the
    other due sessions from being pushed."""
    syncer = _make_syncer(tmp_path, monkeypatch)
    bad_dir = tmp_path / "ses_bad.sfs"
    good_dir = tmp_path / "ses_good.sfs"
    for d in (bad_dir, good_dir):
        d.mkdir()
        (d / "messages.jsonl").write_text("{}\n")
    syncer.backoff.record_failure("ses_bad", "old-fp", "500", now_mono=0.0, now_wall=0.0)
    syncer.store.get_session_dir.side_effect = lambda sid: {"ses_bad": bad_dir, "ses_good": good_dir}[sid]

    def boom(_dir):
        raise UnicodeEncodeError("utf-8", "x", 0, 1, "bad filename")

    client = AsyncMock()
    client.push_session.return_value = MagicMock(etag="e" * 16, blob_size_bytes=1)
    with patch.object(syncer, "_get_client", return_value=client), patch(
        "sessionfs.store.local._content_hash", boom
    ), patch("sessionfs.daemon.sync_backoff.now_pair", lambda: (10_000.0, 10_000.0)), patch(
        "sessionfs.sync.archive.pack_session", return_value=b"tar"
    ), patch.object(syncer, "_get_local_etag", return_value=None), patch.object(
        syncer, "_store_local_etag"
    ), patch.object(syncer, "_maybe_auto_audit", AsyncMock()):
        asyncio.run(syncer._sync_sessions({"ses_bad", "ses_good"}))
    pushed = {call.args[0] for call in client.push_session.await_args_list}
    assert "ses_good" in pushed


def test_marking_a_session_dirty_drops_its_cached_fingerprint(tmp_path, monkeypatch):
    syncer = _make_syncer(tmp_path, monkeypatch)
    syncer._content_fp_cache["ses_x"] = ("stat", "content")
    syncer.mark_session_dirty("ses_x")
    assert "ses_x" not in syncer._content_fp_cache
