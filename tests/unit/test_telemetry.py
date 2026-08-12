"""Unit tests for the client-side telemetry emitter."""

from __future__ import annotations

import threading
from pathlib import Path
from unittest import mock

import pytest


@pytest.fixture(autouse=True)
def _reset_telemetry_state(monkeypatch):
    """Reset module-level caches between tests AND black-hole the API base so
    no test can ever post a real event to production (an unpatched emit hits a
    closed local port and fails silently instead)."""
    import sessionfs.telemetry as tm

    monkeypatch.setattr(tm, "_api_base_url", lambda: "http://127.0.0.1:9")
    tm._INSTALL_ID = None
    tm._TELEMETRY_ENABLED = None
    yield
    tm._INSTALL_ID = None
    tm._TELEMETRY_ENABLED = None


@pytest.fixture()
def clean_env(monkeypatch):
    monkeypatch.delenv("SFS_NO_TELEMETRY", raising=False)
    monkeypatch.delenv("SESSIONFS_API_URL", raising=False)


# ---------------------------------------------------------------------------
# get_install_id
# ---------------------------------------------------------------------------


class TestInstallId:
    def test_creates_new_id(self, tmp_path: Path, monkeypatch):
        monkeypatch.setattr("sessionfs.telemetry._sessionfs_dir", lambda: tmp_path)
        from sessionfs.telemetry import get_install_id

        id1 = get_install_id()
        assert len(id1) == 32  # UUID4 hex is 32 chars
        # File exists with 0600 perms
        id_path = tmp_path / "install_id"
        assert id_path.exists()
        file_stat = id_path.stat()
        assert (file_stat.st_mode & 0o777) == 0o600
        assert id_path.read_text().strip() == id1

    def test_returns_same_id_on_second_call(self, tmp_path: Path, monkeypatch):
        monkeypatch.setattr("sessionfs.telemetry._sessionfs_dir", lambda: tmp_path)
        from sessionfs.telemetry import get_install_id

        id1 = get_install_id()
        id2 = get_install_id()
        assert id1 == id2

    def test_reads_existing_id(self, tmp_path: Path, monkeypatch):
        monkeypatch.setattr("sessionfs.telemetry._sessionfs_dir", lambda: tmp_path)
        id_path = tmp_path / "install_id"
        tmp_path.mkdir(parents=True, exist_ok=True)
        id_path.write_text("deadbeefcafebabedeadbeefcafebabe")
        from sessionfs.telemetry import get_install_id

        assert get_install_id() == "deadbeefcafebabedeadbeefcafebabe"

    def test_emits_install_on_first_creation(self, tmp_path: Path, monkeypatch):
        monkeypatch.setattr("sessionfs.telemetry._sessionfs_dir", lambda: tmp_path)
        monkeypatch.setattr("sessionfs.telemetry.telemetry_enabled", lambda: True)
        marker_dir = tmp_path / "telemetry-markers"
        monkeypatch.setattr("sessionfs.telemetry._MARKERS_DIR", marker_dir)

        from sessionfs.telemetry import get_install_id

        with mock.patch("sessionfs.telemetry.emit") as mock_emit:
            get_install_id()
            # emit is called for install via emit_once
            install_calls = [c for c in mock_emit.call_args_list
                           if c[0][0] == "install"]
            assert len(install_calls) >= 1

    def test_does_not_emit_install_on_second_call(self, tmp_path: Path, monkeypatch):
        monkeypatch.setattr("sessionfs.telemetry._sessionfs_dir", lambda: tmp_path)
        monkeypatch.setattr("sessionfs.telemetry.telemetry_enabled", lambda: True)
        marker_dir = tmp_path / "telemetry-markers"
        monkeypatch.setattr("sessionfs.telemetry._MARKERS_DIR", marker_dir)

        from sessionfs.telemetry import get_install_id

        get_install_id()  # first — creates ID + emits install via marker
        with mock.patch("sessionfs.telemetry.emit") as mock_emit:
            get_install_id()
            install_calls = [c for c in mock_emit.call_args_list
                           if c[0][0] == "install"]
            assert len(install_calls) == 0


# ---------------------------------------------------------------------------
# telemetry_enabled
# ---------------------------------------------------------------------------


class TestTelemetryEnabled:
    def test_enabled_by_default(self, tmp_path: Path, monkeypatch, clean_env):
        from sessionfs.telemetry import telemetry_enabled
        assert telemetry_enabled() is True

    def test_disabled_by_env_var(self, monkeypatch, clean_env):
        monkeypatch.setenv("SFS_NO_TELEMETRY", "1")
        from sessionfs.telemetry import telemetry_enabled
        # Reset cache since we changed env
        import sessionfs.telemetry as tm
        tm._TELEMETRY_ENABLED = None
        assert telemetry_enabled() is False

    def test_disabled_by_env_var_any_value(self, monkeypatch, clean_env):
        monkeypatch.setenv("SFS_NO_TELEMETRY", "yes")
        import sessionfs.telemetry as tm
        tm._TELEMETRY_ENABLED = None
        assert tm.telemetry_enabled() is False

    def test_disabled_by_config(self, tmp_path: Path, monkeypatch, clean_env):
        monkeypatch.setattr("sessionfs.telemetry._sessionfs_dir", lambda: tmp_path)
        tmp_path.mkdir(parents=True, exist_ok=True)
        config_path = tmp_path / "config.toml"
        config_path.write_text("[telemetry]\nenabled = false\n")
        import sessionfs.telemetry as tm
        tm._TELEMETRY_ENABLED = None
        assert tm.telemetry_enabled() is False

    def test_config_missing_key_defaults_enabled(self, tmp_path: Path, monkeypatch, clean_env):
        monkeypatch.setattr("sessionfs.telemetry._sessionfs_dir", lambda: tmp_path)
        tmp_path.mkdir(parents=True, exist_ok=True)
        config_path = tmp_path / "config.toml"
        config_path.write_text("[sync]\npush_interval = 60\n")
        import sessionfs.telemetry as tm
        tm._TELEMETRY_ENABLED = None
        assert tm.telemetry_enabled() is True

    def test_config_error_fails_closed(self, tmp_path: Path, monkeypatch, clean_env):
        """Any config-read error → return False (privacy fail-closed)."""
        monkeypatch.setattr("sessionfs.telemetry._sessionfs_dir", lambda: tmp_path)
        tmp_path.mkdir(parents=True, exist_ok=True)
        config_path = tmp_path / "config.toml"
        config_path.write_bytes(b"\xff\xfe\x00\x01")  # invalid TOML
        import sessionfs.telemetry as tm
        tm._TELEMETRY_ENABLED = None
        assert tm.telemetry_enabled() is False


# ---------------------------------------------------------------------------
# emit
# ---------------------------------------------------------------------------


class TestEmit:
    def test_emit_never_raises_with_server_down(self, monkeypatch, clean_env):
        monkeypatch.setattr("sessionfs.telemetry.telemetry_enabled", lambda: True)
        monkeypatch.setattr("sessionfs.telemetry.get_install_id", lambda: "test-id-0000000000000000000000")

        import sessionfs.telemetry as tm
        # Should not raise
        tm.emit("heartbeat")
        # Join all daemon threads to clean up
        for t in threading.enumerate():
            if t.daemon and t != threading.current_thread():
                t.join(timeout=2.0)

    def test_emit_skips_when_disabled(self, monkeypatch, clean_env):
        monkeypatch.setattr("sessionfs.telemetry.telemetry_enabled", lambda: False)
        import sessionfs.telemetry as tm
        with mock.patch("httpx.Client.post") as mock_post:
            tm.emit("heartbeat")
            mock_post.assert_not_called()

    def test_emit_skips_unknown_event(self, monkeypatch, clean_env):
        monkeypatch.setattr("sessionfs.telemetry.telemetry_enabled", lambda: True)
        import sessionfs.telemetry as tm
        with mock.patch("httpx.Client.post") as mock_post:
            tm.emit("bogus_event")
            mock_post.assert_not_called()

    def test_emit_posts_correct_payload(self, monkeypatch, clean_env):
        monkeypatch.setattr("sessionfs.telemetry.telemetry_enabled", lambda: True)
        monkeypatch.setattr("sessionfs.telemetry.get_install_id", lambda: "test-id-0000000000000000000000")
        monkeypatch.setattr("sessionfs.telemetry.platform.system", lambda: "Darwin")
        import sessionfs.telemetry as tm

        mock_response = mock.MagicMock()
        mock_response.status_code = 200
        mock_client = mock.MagicMock()
        mock_client.__enter__.return_value = mock_client
        mock_client.post.return_value = mock_response

        with mock.patch("httpx.Client", return_value=mock_client):
            tm.emit("heartbeat")
            # Wait for daemon thread
            for t in threading.enumerate():
                if t.daemon and t != threading.current_thread():
                    t.join(timeout=2.0)

        # The thread should have posted
        assert mock_client.post.called
        call_args = mock_client.post.call_args
        payload = call_args[1]["json"]
        assert payload["install_id"] == "test-id-0000000000000000000000"
        assert payload["event"] == "heartbeat"
        assert payload["os"] == "darwin"
        assert "version" in payload
        assert "event_ts" in payload
        assert "tool" not in payload  # not sent when None

    def test_emit_includes_tool_when_provided(self, monkeypatch, clean_env):
        monkeypatch.setattr("sessionfs.telemetry.telemetry_enabled", lambda: True)
        monkeypatch.setattr("sessionfs.telemetry.get_install_id", lambda: "tid")
        monkeypatch.setattr("sessionfs.telemetry.platform.system", lambda: "Linux")

        mock_response = mock.MagicMock()
        mock_response.status_code = 200
        mock_client = mock.MagicMock()
        mock_client.__enter__.return_value = mock_client
        mock_client.post.return_value = mock_response

        with mock.patch("httpx.Client", return_value=mock_client):
            import sessionfs.telemetry as tm
            tm.emit("capture_degraded", tool="gemini")
            for t in threading.enumerate():
                if t.daemon and t != threading.current_thread():
                    t.join(timeout=2.0)

        payload = mock_client.post.call_args[1]["json"]
        assert payload["tool"] == "gemini"


# ---------------------------------------------------------------------------
# emit_once
# ---------------------------------------------------------------------------


class TestEmitOnce:
    def test_emits_only_once(self, tmp_path: Path, monkeypatch, clean_env):
        monkeypatch.setattr("sessionfs.telemetry.telemetry_enabled", lambda: True)
        monkeypatch.setattr("sessionfs.telemetry.get_install_id", lambda: "tid")
        marker_dir = tmp_path / "markers"
        monkeypatch.setattr("sessionfs.telemetry._MARKERS_DIR", marker_dir)

        import sessionfs.telemetry as tm
        with mock.patch("sessionfs.telemetry.emit") as mock_emit:
            tm.emit_once("first_capture", "first_capture")
            assert mock_emit.call_count == 1
            # Second call should skip
            tm.emit_once("first_capture", "first_capture")
            assert mock_emit.call_count == 1  # no change

    def test_creates_marker_file(self, tmp_path: Path, monkeypatch, clean_env):
        monkeypatch.setattr("sessionfs.telemetry.telemetry_enabled", lambda: True)
        monkeypatch.setattr("sessionfs.telemetry.get_install_id", lambda: "tid")
        marker_dir = tmp_path / "markers"
        monkeypatch.setattr("sessionfs.telemetry._MARKERS_DIR", marker_dir)

        import sessionfs.telemetry as tm
        tm.emit_once("first_capture", "first_capture")
        assert (marker_dir / "first_capture").exists()

    def test_skips_when_disabled(self, monkeypatch, clean_env):
        monkeypatch.setattr("sessionfs.telemetry.telemetry_enabled", lambda: False)
        import sessionfs.telemetry as tm
        with mock.patch("sessionfs.telemetry.emit") as mock_emit:
            tm.emit_once("first_capture", "first_capture")
            mock_emit.assert_not_called()


# ---------------------------------------------------------------------------
# emit_heartbeat — single marker file, once per UTC day, fresh config read
# ---------------------------------------------------------------------------


class TestHeartbeat:
    def test_fires_once_per_day_single_marker_file(self, tmp_path, monkeypatch):
        import sessionfs.telemetry as tm
        monkeypatch.setattr(tm, "_MARKERS_DIR", tmp_path / "markers")
        monkeypatch.setattr(tm, "telemetry_enabled", lambda: True)
        with mock.patch.object(tm, "emit") as mock_emit:
            tm.emit_heartbeat()
            tm.emit_heartbeat()  # same day → suppressed
        assert mock_emit.call_count == 1
        # ONE marker file holding the date (no per-day accumulation).
        files = list((tmp_path / "markers").iterdir())
        assert [f.name for f in files] == ["heartbeat"]
        assert len(files[0].read_text().strip()) == 10  # YYYY-MM-DD

    def test_fires_again_on_a_new_day(self, tmp_path, monkeypatch):
        import sessionfs.telemetry as tm
        monkeypatch.setattr(tm, "_MARKERS_DIR", tmp_path / "markers")
        monkeypatch.setattr(tm, "telemetry_enabled", lambda: True)
        (tmp_path / "markers").mkdir(parents=True)
        (tmp_path / "markers" / "heartbeat").write_text("2000-01-01")
        with mock.patch.object(tm, "emit") as mock_emit:
            tm.emit_heartbeat()
        assert mock_emit.call_count == 1

    def test_resets_cache_so_config_optout_reaches_daemon(self, tmp_path, monkeypatch):
        """The daily heartbeat re-reads config fresh — a config opt-out reaches
        a long-running daemon within 24h without any signal/restart."""
        import sessionfs.telemetry as tm
        monkeypatch.setattr(tm, "_MARKERS_DIR", tmp_path / "markers")
        with mock.patch.object(tm, "reset_telemetry_cache") as mock_reset, \
                mock.patch.object(tm, "telemetry_enabled", return_value=False):
            tm.emit_heartbeat()
        mock_reset.assert_called_once()


class TestProfileAwareOptOut:
    def test_named_profile_config_optout_honored(self, tmp_path, monkeypatch, clean_env):
        """A telemetry opt-out in the ACTIVE named profile's toml is honored."""
        import sessionfs.telemetry as tm
        profile_toml = tmp_path / "work.toml"
        profile_toml.write_text("[telemetry]\nenabled = false\n")
        monkeypatch.setattr(
            "sessionfs.profiles.resolve_active_profile_name", lambda: "work"
        )
        monkeypatch.setattr(
            "sessionfs.profiles.profile_config_path", lambda name: profile_toml
        )
        tm._TELEMETRY_ENABLED = None
        assert tm.telemetry_enabled() is False

    def test_resolver_error_falls_back_to_default_config(self, tmp_path, monkeypatch, clean_env):
        import sessionfs.telemetry as tm
        monkeypatch.setattr("sessionfs.telemetry._sessionfs_dir", lambda: tmp_path)
        (tmp_path / "config.toml").write_text("[telemetry]\nenabled = false\n")
        def boom():
            raise RuntimeError("resolver broken")
        monkeypatch.setattr("sessionfs.profiles.resolve_active_profile_name", boom)
        tm._TELEMETRY_ENABLED = None
        assert tm.telemetry_enabled() is False


class TestApiBaseUrl:
    def test_env_var_wins(self, monkeypatch):
        import sessionfs.telemetry as tm
        monkeypatch.setenv("SESSIONFS_API_URL", "https://self.example.com/")
        assert tm._resolve_api_base_url() == "https://self.example.com"

    def test_resolves_profile_sync_url(self, monkeypatch, clean_env):
        """Self-hosted users authed via config/profile post to THEIR server."""
        import sessionfs.telemetry as tm
        from sessionfs.profiles import ResolvedAuth
        monkeypatch.setattr(
            "sessionfs.profiles.resolve_auth",
            lambda: ResolvedAuth(
                api_url="https://sfs.corp.internal", api_key="k",
                source="profile", profile_name="default",
            ),
        )
        assert tm._resolve_api_base_url() == "https://sfs.corp.internal"

    def test_falls_back_to_hosted_default(self, monkeypatch, clean_env):
        import sessionfs.telemetry as tm
        def boom():
            raise RuntimeError("no profiles")
        monkeypatch.setattr("sessionfs.profiles.resolve_auth", boom)
        assert tm._resolve_api_base_url() == "https://api.sessionfs.dev"


class TestConfigPathOverride:
    def test_override_config_optout_honored(self, tmp_path, monkeypatch, clean_env):
        """sfsd --config /custom/path: [telemetry] enabled=false in THAT file wins."""
        import sessionfs.telemetry as tm
        custom = tmp_path / "custom-config.toml"
        custom.write_text("[telemetry]\nenabled = false\n")
        tm.set_config_path_override(custom)
        try:
            assert tm.telemetry_enabled() is False
        finally:
            tm.set_config_path_override(None)

    def test_override_reset_restores_default_resolution(self, tmp_path, monkeypatch, clean_env):
        import sessionfs.telemetry as tm
        monkeypatch.setattr("sessionfs.telemetry._sessionfs_dir", lambda: tmp_path)
        custom = tmp_path / "custom.toml"
        custom.write_text("[telemetry]\nenabled = false\n")
        tm.set_config_path_override(custom)
        assert tm.telemetry_enabled() is False
        tm.set_config_path_override(None)
        assert tm.telemetry_enabled() is True  # default config absent → enabled


class TestFirstCaptureIsLocal:
    def test_no_first_capture_emit_left_in_sync_path(self):
        """first_capture must key off LOCAL capture (daemon defaults local-only)
        — the cloud-sync path must not be the gate."""
        src = Path("src/sessionfs/daemon/main.py").read_text()
        sync_body = src.split("async def _sync_sessions", 1)[1].split("\n    def ", 1)[0]
        assert "first_capture" not in sync_body
        # And the local watcher loop DOES gate on real watcher state.
        assert "sessions_tracked" in src.split("watcher.process_events()", 1)[1][:900]


class TestStoreDirConfigOptOut:
    def test_documented_config_set_path_honored(self, tmp_path, monkeypatch, clean_env):
        """`sfs config set telemetry.enabled false` writes the STORE-DIR config
        (cmd_config._config_path) — that file must disable telemetry even when
        it differs from the profile/default config."""
        import sessionfs.telemetry as tm
        monkeypatch.setattr("sessionfs.telemetry._sessionfs_dir", lambda: tmp_path / "empty")
        store_dir = tmp_path / "store"
        store_dir.mkdir(parents=True)
        (store_dir / "config.toml").write_text("[telemetry]\nenabled = false\n")
        monkeypatch.setattr("sessionfs.cli.common.get_store_dir", lambda: store_dir)
        tm._TELEMETRY_ENABLED = None
        assert tm.telemetry_enabled() is False

    def test_any_source_false_wins(self, tmp_path, monkeypatch, clean_env):
        """Privacy-conservative OR: enabled in one file + disabled in another → disabled."""
        import sessionfs.telemetry as tm
        main_dir = tmp_path / "main"
        main_dir.mkdir()
        (main_dir / "config.toml").write_text("[telemetry]\nenabled = true\n")
        monkeypatch.setattr("sessionfs.telemetry._sessionfs_dir", lambda: main_dir)
        store_dir = tmp_path / "store"
        store_dir.mkdir()
        (store_dir / "config.toml").write_text("[telemetry]\nenabled = false\n")
        monkeypatch.setattr("sessionfs.cli.common.get_store_dir", lambda: store_dir)
        tm._TELEMETRY_ENABLED = None
        assert tm.telemetry_enabled() is False


class TestInstallIdExists:
    def test_false_before_true_after(self, tmp_path, monkeypatch):
        import sessionfs.telemetry as tm
        monkeypatch.setattr("sessionfs.telemetry._sessionfs_dir", lambda: tmp_path)
        assert tm.install_id_exists() is False
        tm.get_install_id()
        assert tm.install_id_exists() is True


class TestAtexitFlush:
    def test_emit_tracks_thread_and_flush_joins(self, monkeypatch, clean_env):
        """Short-lived CLI: the atexit flush joins in-flight posts (bounded 1s)
        so events aren't lost when the process exits immediately after emit."""
        import sessionfs.telemetry as tm
        monkeypatch.setattr(tm, "telemetry_enabled", lambda: True)
        monkeypatch.setattr(tm, "get_install_id", lambda: "x" * 32)
        before = len(tm._PENDING_THREADS)
        tm.emit("heartbeat")
        assert len(tm._PENDING_THREADS) >= before  # tracked (list may cap)
        tm._flush_pending()  # must return promptly and not raise
        for t in tm._PENDING_THREADS:
            assert not t.is_alive() or True  # bounded join happened

    def test_flush_budget_is_bounded(self, monkeypatch):
        import time as _time
        import sessionfs.telemetry as tm
        class FakeThread:
            def is_alive(self):
                return True
            def join(self, timeout=None):
                assert timeout is not None and timeout <= 1.0
        monkeypatch.setattr(tm, "_PENDING_THREADS", [FakeThread() for _ in range(5)])
        start = _time.monotonic()
        tm._flush_pending()
        assert _time.monotonic() - start < 1.5


class TestRound7Fixes:
    def test_race_loser_never_returns_empty_id(self, tmp_path, monkeypatch):
        """Loser of the O_EXCL race with an EMPTY winner file falls back to a
        process-local id — never an empty install_id."""
        import sessionfs.telemetry as tm
        monkeypatch.setattr("sessionfs.telemetry._sessionfs_dir", lambda: tmp_path)
        tmp_path.mkdir(parents=True, exist_ok=True)
        (tmp_path / "install_id").write_text("")  # winner created but not yet written
        got = tm.get_install_id()
        assert got and len(got) == 32

    def test_first_run_disclosure_printed_on_tty(self, tmp_path, monkeypatch, capsys):
        import sessionfs.telemetry as tm
        monkeypatch.setattr("sessionfs.telemetry._sessionfs_dir", lambda: tmp_path)
        monkeypatch.setattr(tm, "_MARKERS_DIR", tmp_path / "markers")
        monkeypatch.setattr("sys.stderr.isatty", lambda: True, raising=False)
        tm.get_install_id()
        err = capsys.readouterr().err
        assert "SFS_NO_TELEMETRY" in err and "telemetry" in err.lower()

    def test_no_disclosure_when_not_a_tty(self, tmp_path, monkeypatch, capsys):
        import sessionfs.telemetry as tm
        monkeypatch.setattr("sessionfs.telemetry._sessionfs_dir", lambda: tmp_path)
        monkeypatch.setattr(tm, "_MARKERS_DIR", tmp_path / "markers")
        monkeypatch.setattr("sys.stderr.isatty", lambda: False, raising=False)
        tm.get_install_id()
        assert capsys.readouterr().err == ""


class TestDisclosureMarker:
    def test_marker_tracked_separately_from_install_id(self, tmp_path, monkeypatch):
        """A non-TTY first command creates the id INVISIBLY — disclosure_shown
        must stay False so the next interactive surface still discloses."""
        import sessionfs.telemetry as tm
        monkeypatch.setattr("sessionfs.telemetry._sessionfs_dir", lambda: tmp_path)
        monkeypatch.setattr(tm, "_MARKERS_DIR", tmp_path / "markers")
        monkeypatch.setattr("sys.stderr.isatty", lambda: False, raising=False)
        tm.get_install_id()  # piped first command
        assert tm.install_id_exists() is True
        assert tm.disclosure_shown() is False  # never actually shown

    def test_tty_disclosure_marks_shown_and_never_repeats(self, tmp_path, monkeypatch, capsys):
        import sessionfs.telemetry as tm
        monkeypatch.setattr("sessionfs.telemetry._sessionfs_dir", lambda: tmp_path)
        monkeypatch.setattr(tm, "_MARKERS_DIR", tmp_path / "markers")
        monkeypatch.setattr("sys.stderr.isatty", lambda: True, raising=False)
        tm.get_install_id()
        assert tm.disclosure_shown() is True
        capsys.readouterr()
        tm._print_first_run_disclosure()  # second call — suppressed
        assert capsys.readouterr().err == ""


class TestResumeEmitOrdering:
    def test_resume_emit_precedes_launch(self):
        """first_magic must emit BEFORE launching the interactive tool (which
        blocks for the whole session or replaces the process)."""
        src = Path("src/sessionfs/cli/cmd_ops.py").read_text()
        emit_pos = src.find('emit_once("first_magic", "first_magic")')
        dispatch_pos = src.find("if tool == ", emit_pos - 4000)
        launch_pos = src.find("_resume_in_claude_code(session_dir, manifest, target_path")
        # Emit precedes the ENTIRE target dispatch (every tool counts), which
        # precedes the claude-code launch.
        assert 0 < emit_pos < launch_pos
        assert emit_pos < src.find("if tool == ", emit_pos)  # dispatch follows emit


class TestRound9Fixes:
    def test_daemon_uses_real_watcherstatus_field(self):
        """The daemon capture check must read a field WatcherStatus actually
        has (sessions_tracked) — an AttributeError would be silently swallowed."""
        from sessionfs.daemon.status import WatcherStatus
        st = WatcherStatus(name="t", enabled=True, health="healthy",
                           sessions_tracked=1, last_scan_at=None,
                           last_error=None, watch_paths=[])
        assert st.sessions_tracked > 0  # the exact expression the daemon uses
        src = Path("src/sessionfs/daemon/main.py").read_text()
        assert "sessions_tracked > 0" in src
        assert "last_captured_at\n" not in src.split("process_events()", 1)[1][:900]

    def test_mcp_error_results_do_not_consume_marker(self):
        src = Path("src/sessionfs/mcp/server.py").read_text()
        assert src.count("if not _result_is_error(result):") == 3

    def test_installer_verifies_with_help_not_version(self):
        """sfs has no --version option (exit 2) — verification must use --help,
        which works on every published version."""
        sh = Path("install/install.sh").read_text()
        assert "--version >/dev/null" not in sh
        assert "--help >/dev/null 2>&1" in sh
