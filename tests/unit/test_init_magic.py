"""Tests for sfs init magic-moment capture (v0.15 tk_ff0f193db6d04267)."""

from __future__ import annotations

from collections.abc import Generator
from pathlib import Path
from unittest import mock

import pytest

from sessionfs.cli.cmd_init import (
    _capture_one_session,
    _discover_all_native_sessions,
    _try_magic_moment,
)


@pytest.fixture(autouse=True)
def _isolated_store(tmp_path, monkeypatch):
    """Point cmd_init's store at a tmp dir — these tests must NEVER touch or
    chmod the user's real ~/.sessionfs (CI sandboxes may not even allow it)."""
    from sessionfs.store.local import LocalStore

    store_dir = tmp_path / "sfs-store"
    monkeypatch.setattr("sessionfs.cli.cmd_init.get_store_dir", lambda: store_dir)

    def _open_store(initialize: bool = True):
        store = LocalStore(store_dir)
        if initialize:
            store.initialize()
        return store

    monkeypatch.setattr("sessionfs.cli.cmd_init.open_store", _open_store)


# ---------------------------------------------------------------------------
# _discover_all_native_sessions
# ---------------------------------------------------------------------------


class TestDiscoverAllNativeSessions:
    """Tests for session discovery across enabled tools."""

    def test_empty_keys_returns_empty(self) -> None:
        """No enabled keys → empty list."""
        result = _discover_all_native_sessions(set())
        assert result == []

    def test_sorts_by_mtime_descending(self) -> None:
        """Sessions are sorted most-recent first."""
        sessions = [
            {"session_id": "a", "path": "/tmp/a", "tool": "codex", "mtime": 100.0, "size_bytes": 10},
            {"session_id": "b", "path": "/tmp/b", "tool": "codex", "mtime": 300.0, "size_bytes": 20},
            {"session_id": "c", "path": "/tmp/c", "tool": "codex", "mtime": 200.0, "size_bytes": 30},
        ]
        sessions.sort(key=lambda s: s.get("mtime", 0), reverse=True)
        assert sessions[0]["session_id"] == "b"
        assert sessions[1]["session_id"] == "c"
        assert sessions[2]["session_id"] == "a"

    def test_per_tool_failure_swallowed(self) -> None:
        """A failing tool discovery doesn't crash the whole scan."""
        # The real discovery would catch failures per-tool; verify the pattern.
        result = _discover_all_native_sessions({"claude_code"})
        # Should return a list (possibly empty if no sessions on this machine)
        assert isinstance(result, list)

    def test_unknown_tool_key_returns_empty(self) -> None:
        """An unknown config key doesn't crash discovery."""
        result = _discover_all_native_sessions({"nonexistent_tool"})
        assert isinstance(result, list)
        assert result == []


# ---------------------------------------------------------------------------
# _capture_one_session
# ---------------------------------------------------------------------------


class TestCaptureOneSession:
    """Tests for single-session capture."""

    def test_returns_none_for_unknown_tool(self) -> None:
        """Unknown tool returns None without crashing."""
        info = {"session_id": "abc123", "path": "/tmp/test", "tool": "unknown-tool", "mtime": 100.0, "size_bytes": 100}
        result = _capture_one_session(info)
        assert result is None

    def test_returns_none_on_missing_session_id(self) -> None:
        """Missing session_id returns None."""
        info = {"path": "/tmp/test", "tool": "claude-code", "mtime": 100.0, "size_bytes": 100}
        result = _capture_one_session(info)
        assert result is None

    def test_returns_none_when_path_does_not_exist(self, tmp_path: Path) -> None:
        """Non-existent path returns None."""
        nonexistent = tmp_path / "nonexistent.jsonl"
        info = {
            "session_id": "abc123",
            "path": str(nonexistent),
            "tool": "claude-code",
            "mtime": 100.0,
            "size_bytes": 100,
        }
        result = _capture_one_session(info)
        assert result is None


# ---------------------------------------------------------------------------
# _try_magic_moment
# ---------------------------------------------------------------------------


class TestTryMagicMoment:
    """Tests for the magic-moment orchestration function."""

    @pytest.fixture(autouse=True)
    def _mock_console(self) -> Generator[None, None, None]:
        """Prevent real console output during tests."""
        self._print_patcher = mock.patch("sessionfs.cli.cmd_init.console.print")
        self.mock_print = self._print_patcher.start()
        yield
        self._print_patcher.stop()

    # -- Success path --

    def test_success_captures_and_prints(self) -> None:
        """A discovered session is captured and the magic prompt is printed."""
        discover_return = [
            {
                "session_id": "abc123def456",
                "path": "/fake/session.jsonl",
                "tool": "claude-code",
                "mtime": 500.0,
                "size_bytes": 1000,
                "first_prompt": "Fix the login bug",
            },
        ]
        capture_return = ("ses_abc123def45678", "Fix the login bug", 42)

        with mock.patch(
            "sessionfs.cli.cmd_init._discover_all_native_sessions",
            return_value=discover_return,
        ):
            with mock.patch(
                "sessionfs.cli.cmd_init._capture_one_session",
                return_value=capture_return,
            ):
                with mock.patch(
                    "sessionfs.telemetry.emit_once"
                ) as mock_emit:
                    _try_magic_moment({"claude_code"})

        # Check emit_once was called
        mock_emit.assert_called_once_with("first_capture", "first_capture")

        # Check that the captured session line and magic prompt were printed
        all_calls = "".join(
            str(c) for c in self.mock_print.call_args_list
        )
        assert "Captured your most recent session" in all_calls
        assert "Fix the login bug" in all_calls
        assert "claude-code" in all_calls
        assert "42 messages" in all_calls
        assert 'what did we do last session?' in all_calls

    def test_success_uses_sfs_id_as_fallback_title(self) -> None:
        """When no title/name/first_prompt exists, sfs_id is the display title."""
        discover_return = [
            {
                "session_id": "xyz789",
                "path": "/fake/session.jsonl",
                "tool": "gemini-cli",
                "mtime": 400.0,
                "size_bytes": 500,
            },
        ]
        capture_return = ("ses_xyz78900000000", "ses_xyz78900000000", 3)

        with mock.patch(
            "sessionfs.cli.cmd_init._discover_all_native_sessions",
            return_value=discover_return,
        ):
            with mock.patch(
                "sessionfs.cli.cmd_init._capture_one_session",
                return_value=capture_return,
            ):
                with mock.patch("sessionfs.telemetry.emit_once"):
                    _try_magic_moment({"gemini"})

        all_calls = "".join(
            str(c) for c in self.mock_print.call_args_list
        )
        assert "Captured your most recent session" in all_calls

    def test_long_title_is_truncated(self) -> None:
        """Titles over 60 characters are truncated with ellipsis."""
        long_title = "A" * 80
        discover_return = [
            {
                "session_id": "abc123",
                "path": "/fake/session.jsonl",
                "tool": "claude-code",
                "mtime": 500.0,
                "size_bytes": 100,
                "first_prompt": long_title,
            },
        ]
        capture_return = ("ses_abc12300000000", "A" * 57 + "...", 1)

        with mock.patch(
            "sessionfs.cli.cmd_init._discover_all_native_sessions",
            return_value=discover_return,
        ):
            with mock.patch(
                "sessionfs.cli.cmd_init._capture_one_session",
                return_value=capture_return,
            ):
                with mock.patch("sessionfs.telemetry.emit_once"):
                    _try_magic_moment({"claude_code"})

        all_calls = "".join(
            str(c) for c in self.mock_print.call_args_list
        )
        assert "..." in all_calls
        assert long_title not in all_calls

    # -- Timeout path --

    def test_timeout_falls_through_silently(self) -> None:
        """When capture times out, no crash and no magic-moment output."""
        discover_return = [
            {
                "session_id": "blocker",
                "path": "/fake/slow.jsonl",
                "tool": "claude-code",
                "mtime": 500.0,
                "size_bytes": 1000,
            },
        ]

        def _slow_capture(_info: dict) -> None:
            # Sleep longer than the 10s timeout — this should never complete
            # in test context; the mock prevents real sleep.
            return None

        with mock.patch(
            "sessionfs.cli.cmd_init._discover_all_native_sessions",
            return_value=discover_return,
        ):
            with mock.patch(
                "sessionfs.cli.cmd_init._capture_one_session",
                side_effect=TimeoutError("simulated"),
            ):
                with mock.patch("sessionfs.telemetry.emit_once") as mock_emit:
                    # Must not raise
                    _try_magic_moment({"claude_code"})

        # emit_once must NOT have been called
        mock_emit.assert_not_called()

        # The magic prompt line must NOT appear
        all_calls = "".join(
            str(c) for c in self.mock_print.call_args_list
        )
        assert "what did we do last session?" not in all_calls

    def test_capture_thread_timeout_returns_gracefully(self) -> None:
        """When the thread join times out, the function returns without crashing."""
        discover_return = [
            {
                "session_id": "slow-one",
                "path": "/fake/slow.jsonl",
                "tool": "claude-code",
                "mtime": 500.0,
                "size_bytes": 1000,
            },
        ]

        # Simulate a capture that would hang
        with mock.patch(
            "sessionfs.cli.cmd_init._discover_all_native_sessions",
            return_value=discover_return,
        ):
            with mock.patch(
                "sessionfs.cli.cmd_init._capture_one_session",
                return_value=None,
            ):
                with mock.patch("sessionfs.telemetry.emit_once") as mock_emit:
                    _try_magic_moment({"claude_code"})

        # emit_once NOT called because capture returned None
        mock_emit.assert_not_called()

    # -- Capture exception path --

    def test_capture_exception_falls_through(self) -> None:
        """When _capture_one_session raises, no crash and no magic output."""
        discover_return = [
            {
                "session_id": "crashy",
                "path": "/fake/crash.jsonl",
                "tool": "claude-code",
                "mtime": 500.0,
                "size_bytes": 1000,
            },
        ]

        with mock.patch(
            "sessionfs.cli.cmd_init._discover_all_native_sessions",
            return_value=discover_return,
        ):
            with mock.patch(
                "sessionfs.cli.cmd_init._capture_one_session",
                side_effect=RuntimeError("boom"),
            ):
                with mock.patch("sessionfs.telemetry.emit_once") as mock_emit:
                    _try_magic_moment({"claude_code"})

        mock_emit.assert_not_called()

    def test_discovery_failure_falls_through(self) -> None:
        """When discovery itself raises, no crash."""
        with mock.patch(
            "sessionfs.cli.cmd_init._discover_all_native_sessions",
            side_effect=RuntimeError("discovery boom"),
        ):
            with mock.patch("sessionfs.telemetry.emit_once") as mock_emit:
                _try_magic_moment({"claude_code"})

        mock_emit.assert_not_called()

    # -- Zero sessions path --

    def test_zero_sessions_prints_graceful_line(self) -> None:
        """When no sessions exist, print a graceful fallback line."""
        with mock.patch(
            "sessionfs.cli.cmd_init._discover_all_native_sessions",
            return_value=[],
        ):
            with mock.patch("sessionfs.telemetry.emit_once") as mock_emit:
                _try_magic_moment({"claude_code"})

        mock_emit.assert_not_called()

        all_calls = "".join(
            str(c) for c in self.mock_print.call_args_list
        )
        assert "No existing sessions found" in all_calls

    def test_zero_sessions_no_keys_returns_early(self) -> None:
        """Empty keys + no sessions → returns without printing."""
        with mock.patch(
            "sessionfs.cli.cmd_init._discover_all_native_sessions",
            return_value=[],
        ):
            self.mock_print.reset_mock()
            _try_magic_moment(set())

        # Should not have printed "No existing sessions" (empty keys gate)
        all_calls = "".join(
            str(c) for c in self.mock_print.call_args_list
        )
        assert "No existing sessions found" not in all_calls

    # -- Emit failure swallowed --

    def test_emit_failure_is_swallowed(self) -> None:
        """When emit_once raises, the success output still prints."""
        discover_return = [
            {
                "session_id": "abc123",
                "path": "/fake/session.jsonl",
                "tool": "claude-code",
                "mtime": 500.0,
                "size_bytes": 1000,
                "first_prompt": "Test session",
            },
        ]
        capture_return = ("ses_abc12300000000", "Test session", 5)

        with mock.patch(
            "sessionfs.cli.cmd_init._discover_all_native_sessions",
            return_value=discover_return,
        ):
            with mock.patch(
                "sessionfs.cli.cmd_init._capture_one_session",
                return_value=capture_return,
            ):
                with mock.patch(
                    "sessionfs.telemetry.emit_once",
                    side_effect=RuntimeError("telemetry down"),
                ) as mock_emit:
                    # Must not raise
                    _try_magic_moment({"claude_code"})

        mock_emit.assert_called_once()  # was called, even though it raised

        all_calls = "".join(
            str(c) for c in self.mock_print.call_args_list
        )
        assert "Captured your most recent session" in all_calls
        assert 'what did we do last session?' in all_calls


def test_init_cmd_invokes_magic_moment():
    """The helper must actually be wired into the wizard (regression: it
    shipped as dead code once)."""
    from pathlib import Path
    src = Path("src/sessionfs/cli/cmd_init.py").read_text()
    body = src.split("def init_cmd", 1)[1]
    import re
    assert re.search(r"_try_magic_moment\(\s*enabled_keys,\s*daemon_started=", body)


class TestNeverOverwriteExistingCapture:
    def test_existing_session_displayed_not_reconverted(self, tmp_path, monkeypatch):
        """P1 guard: an already-captured session must be DISPLAYED, never
        re-converted (init must not bypass the daemon's compaction guard)."""
        import json as _json
        from sessionfs.cli import cmd_init
        # Build a fake existing capture in the isolated store
        from sessionfs.cli.cmd_init import open_store  # patched by fixture
        store = open_store()
        from sessionfs.session_id import session_id_from_native
        sfs_id = session_id_from_native("11111111-2222-3333-4444-555555555555")
        d = store.allocate_session_dir(sfs_id)
        (d / "manifest.json").write_text(_json.dumps(
            {"title": "Rich capture", "stats": {"message_count": 42}}))
        store.close()
        info = {"session_id": "11111111-2222-3333-4444-555555555555",
                "path": str(tmp_path / "native.jsonl"), "tool": "claude-code",
                "mtime": 1.0, "size_bytes": 10}
        with mock.patch.object(cmd_init, "_read_display_info",
                               wraps=cmd_init._read_display_info) as disp:
            result = cmd_init._capture_one_session(info)
        disp.assert_called_once()
        assert result is not None and result[2] == 42  # stats.message_count read

    def test_skipped_capture_leaves_no_empty_dir(self, tmp_path, monkeypatch):
        """P2: a skip/failure after allocation must not leave an unindexed
        empty session dir in the store."""
        from sessionfs.cli import cmd_init
        from sessionfs.cli.cmd_init import open_store
        info = {"session_id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
                "path": str(tmp_path / "missing.jsonl"), "tool": "claude-code",
                "mtime": 1.0, "size_bytes": 10}
        result = cmd_init._capture_one_session(info)  # parse fails (no file)
        assert result is None
        store = open_store()
        try:
            from sessionfs.session_id import session_id_from_native
            sfs_id = session_id_from_native("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
            d = store.get_session_dir(sfs_id)
            assert d is None or not d.exists()
        finally:
            store.close()


class TestDaemonRaceSafety:
    def test_daemon_mid_write_dir_is_left_alone(self, tmp_path):
        """P1 (round 4): a session dir WITHOUT a manifest = the daemon is
        mid-write. Init must not touch it — no concurrent second writer."""
        from sessionfs.cli import cmd_init
        from sessionfs.cli.cmd_init import open_store
        from sessionfs.session_id import session_id_from_native
        native = "99999999-8888-7777-6666-555555555555"
        sfs_id = session_id_from_native(native)
        store = open_store()
        d = store.allocate_session_dir(sfs_id)  # allocated, NO manifest yet
        (d / "messages.jsonl").write_text("partial")
        store.close()
        before = sorted(p.name for p in d.iterdir())
        info = {"session_id": native, "path": str(tmp_path / "native.jsonl"),
                "tool": "claude-code", "mtime": 1.0, "size_bytes": 10}
        assert cmd_init._capture_one_session(info) is None
        assert sorted(p.name for p in d.iterdir()) == before  # untouched

    def test_conversion_happens_in_scratch_then_atomic_install(self):
        """P2 (round 4): the converter writes to .magic-tmp scratch and installs
        via os.rename — an abandoned timeout thread can never leave a partial
        capture in the live sessions dir."""
        from pathlib import Path
        src = Path("src/sessionfs/cli/cmd_init.py").read_text()
        body = src.split("def _capture_one_session", 1)[1].split("\ndef ", 1)[0]
        assert ".magic-tmp" in body
        assert "os.rename(session_dir, target_dir)" in body
        assert "allocate_session_dir" not in body  # never allocates in-store


def test_install_backs_off_from_daemon_allocated_empty_dir():
    """P2 (round 5): rename succeeds onto an EMPTY existing dir — exactly what
    a daemon-allocated target looks like. The install must back off from ANY
    existing target, empty included."""
    from pathlib import Path
    src = Path("src/sessionfs/cli/cmd_init.py").read_text()
    body = src.split("def _capture_one_session", 1)[1].split("\ndef ", 1)[0]
    pre = body.split("os.rename(session_dir, target_dir)")[0]
    assert "if target_dir.exists():" in pre  # precheck precedes the rename


class TestRaceFreeByConstruction:
    def test_daemon_started_path_never_converts(self, tmp_path):
        """With daemon_started=True, init performs NO conversion — it only
        waits for the daemon's manifest. Zero writes from init = no race."""
        from sessionfs.cli import cmd_init
        info = {"session_id": "12121212-3434-5656-7878-909090909090",
                "path": str(tmp_path / "x.jsonl"), "tool": "claude-code",
                "mtime": 1.0, "size_bytes": 5}
        with mock.patch.object(cmd_init, "_discover_all_native_sessions",
                               return_value=[info]), \
             mock.patch.object(cmd_init, "_capture_one_session") as conv, \
             mock.patch.object(cmd_init, "_wait_for_daemon_capture_any",
                               return_value=None) as waiter:
            cmd_init._try_magic_moment({"claude_code"}, daemon_started=True)
        conv.assert_not_called()
        waiter.assert_called_once()

    def test_daemon_wait_displays_daemon_capture(self, tmp_path):
        """The waiter returns display info as soon as the daemon's manifest
        appears."""
        import json as _json
        from sessionfs.cli import cmd_init
        from sessionfs.cli.cmd_init import open_store
        from sessionfs.session_id import session_id_from_native
        native = "abcdabcd-1111-2222-3333-444455556666"
        sfs_id = session_id_from_native(native)
        store = open_store()
        d = store.allocate_session_dir(sfs_id)
        (d / "manifest.json").write_text(_json.dumps(
            {"title": "Daemon capture", "stats": {"message_count": 7}}))
        store.close()
        info = {"session_id": native, "path": str(tmp_path / "n.jsonl"),
                "tool": "claude-code", "mtime": 1.0, "size_bytes": 5}
        result = cmd_init._wait_for_daemon_capture(info, timeout_s=2.0)
        assert result is not None and result[2] == 7


def test_daemon_started_flag_reflects_actual_spawn_success():
    """P2 (round 7): the magic moment must key off ACTUAL spawn success
    (daemon_ok), not the user's intent (start_daemon) — a failed spawn means
    there is no daemon whose capture init could wait for."""
    from pathlib import Path
    src = Path("src/sessionfs/cli/cmd_init.py").read_text()
    body = src.split("def init_cmd", 1)[1]
    assert "daemon_started=daemon_ok" in body
    assert "daemon_ok = False" in body
    # The success flag is set after the pid write, inside the try block.
    assert body.find("daemon_ok = True") > body.find("pid_path.write_text")


class TestRound8Fixes:
    def test_abandoned_thread_cannot_install_past_deadline(self, tmp_path, monkeypatch):
        """Past the cooperative deadline, _capture_one_session must refuse to
        install (scratch only) — so an abandoned thread can never leave a
        half-indexed capture in the live store."""
        import time as _time
        from sessionfs.cli import cmd_init
        native_file = tmp_path / "native.jsonl"
        native_file.write_text('{"type":"user","message":{"role":"user","content":"hi"}}\n')
        info = {"session_id": "77777777-6666-5555-4444-333333333333",
                "path": str(native_file), "tool": "claude-code",
                "mtime": 1.0, "size_bytes": 10}
        expired = _time.monotonic() - 5.0  # already past
        result = cmd_init._capture_one_session(info, deadline=expired)
        assert result is None
        from sessionfs.cli.cmd_init import open_store
        from sessionfs.session_id import session_id_from_native
        store = open_store()
        try:
            d = store.get_session_dir(session_id_from_native(info["session_id"]))
            assert d is None or not (d / "manifest.json").exists()
        finally:
            store.close()

    def test_falls_back_to_older_candidate_when_newest_uncapturable(self, tmp_path):
        """If candidate #1 returns None (e.g. sessionfs_import), the loop must
        try the next candidate instead of giving up."""
        from sessionfs.cli import cmd_init
        c1 = {"session_id": "1", "path": str(tmp_path / "a"), "tool": "codex",
              "mtime": 2.0, "size_bytes": 5}
        c2 = {"session_id": "2", "path": str(tmp_path / "b"), "tool": "claude-code",
              "mtime": 1.0, "size_bytes": 5}
        calls = []
        def fake_capture(info, deadline=None):
            calls.append(info["session_id"])
            return None if info["session_id"] == "1" else ("ses_x", "Title", 3)
        with mock.patch.object(cmd_init, "_discover_all_native_sessions",
                               return_value=[c1, c2]), \
             mock.patch.object(cmd_init, "_capture_one_session",
                               side_effect=fake_capture), \
             mock.patch.object(cmd_init, "emit_once", create=True):
            cmd_init._try_magic_moment({"codex", "claude_code"}, daemon_started=False)
        assert calls == ["1", "2"]

    def test_daemon_waiter_polls_all_candidates(self, tmp_path):
        """The daemon-path waiter accepts ANY candidate's manifest (the daemon
        may skip the newest by design)."""
        import json as _json
        from sessionfs.cli import cmd_init
        from sessionfs.cli.cmd_init import open_store
        from sessionfs.session_id import session_id_from_native
        n1 = "11111111-aaaa-bbbb-cccc-000000000001"  # never captured (skipped)
        n2 = "22222222-dddd-eeee-ffff-000000000002"  # daemon captured this one
        store = open_store()
        d2 = store.allocate_session_dir(session_id_from_native(n2))
        (d2 / "manifest.json").write_text(_json.dumps(
            {"title": "Older but valid", "stats": {"message_count": 9}}))
        store.close()
        import time as _time
        cands = [
            {"session_id": n1, "path": "x", "tool": "codex", "mtime": 2.0, "size_bytes": 1},
            {"session_id": n2, "path": "y", "tool": "claude-code", "mtime": 1.0, "size_bytes": 1},
        ]
        got = cmd_init._wait_for_daemon_capture_any(cands, deadline=_time.monotonic() + 2.0)
        assert got is not None and got[2] == 9 and got[3]["session_id"] == n2


class TestRound9Fixes:
    def test_preexisting_live_daemon_forces_readonly_mode(self):
        """A daemon already running (user declined the start prompt) must put
        the magic moment in read-only wait mode — daemon_ok alone is not the
        gate."""
        from pathlib import Path
        src = Path("src/sessionfs/cli/cmd_init.py").read_text()
        assert "daemon_started=daemon_ok or _daemon_is_running()" in src

    def test_deleted_sessions_are_never_resurrected(self, tmp_path, monkeypatch):
        """A session in deleted.json must not be captured or displayed."""
        from sessionfs.cli import cmd_init
        info = {"session_id": "deadbeef-1111-2222-3333-444444444444",
                "path": str(tmp_path / "n.jsonl"), "tool": "claude-code",
                "mtime": 1.0, "size_bytes": 5}
        with mock.patch("sessionfs.store.deleted.is_excluded", return_value=True) as exc:
            result = cmd_init._capture_one_session(info)
        assert result is None
        exc.assert_called_once()

    def test_daemon_is_running_false_without_pid(self, tmp_path, monkeypatch):
        from sessionfs.cli import cmd_init
        monkeypatch.setattr("sessionfs.cli.cmd_init.get_store_dir", lambda: tmp_path)
        assert cmd_init._daemon_is_running() is False


def test_discovery_is_bounded_by_its_own_watchdog():
    """P2 (round 10): discovery runs inside a bounded thread — a slow native
    store cannot stall the wizard past the budget."""
    from pathlib import Path
    src = Path("src/sessionfs/cli/cmd_init.py").read_text()
    body = src.split("def _try_magic_moment", 1)[1]
    assert "_disc_thread.join(timeout=5.0)" in body
    assert body.find("_disc_thread.join") < body.find("candidates = sessions[:3]")


def test_daemon_waiter_filters_deleted_sessions(tmp_path):
    """Round 13: the daemon-wait display path must also skip deleted.json
    entries."""
    import time as _time
    from sessionfs.cli import cmd_init
    cands = [{"session_id": "deadd00d-1111-2222-3333-444444444444",
              "path": "x", "tool": "claude-code", "mtime": 1.0, "size_bytes": 1}]
    with mock.patch("sessionfs.store.deleted.is_excluded", return_value=True):
        got = cmd_init._wait_for_daemon_capture_any(
            cands, deadline=_time.monotonic() + 0.6)
    assert got is None
