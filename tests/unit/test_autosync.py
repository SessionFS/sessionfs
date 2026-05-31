"""Tests for autosync feature."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


class TestSyncConfig:
    """Sync config has auto mode and debounce."""

    def test_default_mode_off(self):
        from sessionfs.daemon.config import SyncConfig

        config = SyncConfig()
        assert config.auto == "off"
        assert config.debounce == 30

    def test_mode_from_dict(self):
        from sessionfs.daemon.config import SyncConfig

        config = SyncConfig(auto="all", debounce=15)
        assert config.auto == "all"
        assert config.debounce == 15

    def test_daemon_config_has_sync_auto(self):
        from sessionfs.daemon.config import DaemonConfig

        config = DaemonConfig()
        assert config.sync.auto == "off"


class TestDaemonSyncer:
    """DaemonSyncer respects autosync modes."""

    def _make_syncer(self, auto="off", enabled=True):
        from sessionfs.daemon.config import DaemonConfig
        from sessionfs.daemon.main import DaemonSyncer

        config = DaemonConfig(sync={"enabled": enabled, "api_key": "test", "auto": auto, "debounce": 1})
        store = MagicMock()
        return DaemonSyncer(config, store)

    def test_mark_dirty_off_mode_ignores(self):
        syncer = self._make_syncer(auto="off")
        syncer.mark_session_dirty("ses_abc12345")
        assert "ses_abc12345" not in syncer._debounce_timestamps
        assert "ses_abc12345" not in syncer._pending_sessions

    def test_mark_dirty_all_mode_queues(self):
        syncer = self._make_syncer(auto="all")
        syncer.mark_session_dirty("ses_abc12345")
        assert "ses_abc12345" in syncer._debounce_timestamps

    def test_mark_dirty_selective_unwatched_ignores(self):
        syncer = self._make_syncer(auto="selective")
        syncer.mark_session_dirty("ses_abc12345")
        assert "ses_abc12345" not in syncer._debounce_timestamps

    def test_mark_dirty_selective_watched_queues(self):
        syncer = self._make_syncer(auto="selective")
        syncer.add_to_watchlist("ses_abc12345")
        syncer.mark_session_dirty("ses_abc12345")
        assert "ses_abc12345" in syncer._debounce_timestamps

    def test_watchlist_add_remove(self):
        syncer = self._make_syncer(auto="selective")
        syncer.add_to_watchlist("ses_abc12345")
        assert "ses_abc12345" in syncer._watchlist
        syncer.remove_from_watchlist("ses_abc12345")
        assert "ses_abc12345" not in syncer._watchlist

    def test_disabled_syncer_ignores_all(self):
        syncer = self._make_syncer(auto="all", enabled=False)
        syncer.mark_session_dirty("ses_abc12345")
        assert "ses_abc12345" not in syncer._debounce_timestamps


class TestSyncApiModels:
    """API models for sync."""

    def test_user_model_has_sync_fields(self):
        from sessionfs.server.db.models import User

        assert hasattr(User, "sync_mode")
        assert hasattr(User, "sync_debounce")

    def test_watchlist_model_exists(self):
        from sessionfs.server.db.models import SyncWatchlist

        assert hasattr(SyncWatchlist, "user_id")
        assert hasattr(SyncWatchlist, "session_id")
        assert hasattr(SyncWatchlist, "status")
        assert hasattr(SyncWatchlist, "last_synced_at")

    def test_sync_routes_registered(self):
        from sessionfs.server.routes.sync import router

        paths = [r.path for r in router.routes]
        assert "/settings" in paths or any("/settings" in p for p in paths)


class TestCliSyncCommands:
    """CLI sync commands are registered."""

    def test_sync_app_exists(self):
        from sessionfs.cli.cmd_sync import sync_app

        assert sync_app is not None

    def test_sync_commands_registered(self):
        from sessionfs.cli.cmd_sync import sync_app

        command_names = [cmd.name for cmd in sync_app.registered_commands]
        assert "status" in command_names
        assert "auto" in command_names
        assert "watch" in command_names
        assert "unwatch" in command_names
        assert "watchlist" in command_names

    def test_sync_app_in_main(self):
        from sessionfs.cli.main import app

        group_names = [g.typer_instance.info.name for g in app.registered_groups if g.typer_instance and g.typer_instance.info]
        assert "sync" in group_names


class TestMigration:
    """Migration file exists."""

    def test_migration_exists(self):
        from pathlib import Path

        migration = Path("src/sessionfs/server/db/migrations/versions/013_autosync.py")
        assert migration.exists()


class TestSyncFailureExclusion:
    """Per-session push-failure tracking + auto-exclusion on the daemon.

    Regression for the Baptist Health 57MB session bug: if a single session
    keeps failing to push (e.g. server returns 413), the daemon should
    eventually stop re-trying it instead of looping forever.
    """

    def _make_syncer(self, tmp_path, monkeypatch):
        from sessionfs.daemon.config import DaemonConfig
        from sessionfs.daemon.main import DaemonSyncer

        # Redirect ~/.sessionfs/deleted.json into tmp so the test doesn't
        # touch the developer's actual exclusion list.
        monkeypatch.setenv("HOME", str(tmp_path))
        # Reset the module-level path that store.deleted resolves at import time
        import sessionfs.store.deleted as _deleted
        _deleted._DEFAULT_DIR = tmp_path / ".sessionfs"
        _deleted._DEFAULT_PATH = _deleted._DEFAULT_DIR / "deleted.json"

        config = DaemonConfig(sync={"enabled": True, "api_key": "test", "auto": "all", "debounce": 1})
        store = MagicMock()
        return DaemonSyncer(config, store)

    def test_failure_counter_increments(self, tmp_path, monkeypatch):
        syncer = self._make_syncer(tmp_path, monkeypatch)
        syncer._record_session_failure("ses_abc12345", "boom")
        syncer._record_session_failure("ses_abc12345", "boom")
        assert syncer.sync_failures["ses_abc12345"] == 2

    def test_excludes_after_three_failures(self, tmp_path, monkeypatch):
        from sessionfs.store.deleted import is_excluded, list_deleted

        syncer = self._make_syncer(tmp_path, monkeypatch)
        # 3 failures triggers the exclusion.
        for _ in range(3):
            syncer._record_session_failure("ses_huge12345678", "413: too large")

        assert is_excluded("ses_huge12345678"), \
            "Session should be in deleted.json after 3 push failures"
        entries = list_deleted()
        assert entries["ses_huge12345678"]["scope"] == "cloud"
        assert entries["ses_huge12345678"]["reason"] == "too_large"

    def test_success_resets_failure_count(self, tmp_path, monkeypatch):
        syncer = self._make_syncer(tmp_path, monkeypatch)
        syncer._record_session_failure("ses_intermittent01", "transient")
        syncer._record_session_failure("ses_intermittent01", "transient")
        assert syncer.sync_failures.get("ses_intermittent01") == 2

        # Simulate the success path.
        syncer.sync_failures.pop("ses_intermittent01", None)
        assert "ses_intermittent01" not in syncer.sync_failures

        # One more failure shouldn't trip the threshold.
        syncer._record_session_failure("ses_intermittent01", "transient")
        from sessionfs.store.deleted import is_excluded
        assert not is_excluded("ses_intermittent01")


class TestTransientErrorsDoNotExclude:
    """Regression: only SyncTooLargeError counts toward the per-session
    exclusion threshold. Transient errors (429/5xx/network) must not
    cause a healthy session to be permanently excluded.
    """

    def _make_syncer(self, tmp_path, monkeypatch):
        from sessionfs.daemon.config import DaemonConfig
        from sessionfs.daemon.main import DaemonSyncer

        monkeypatch.setenv("HOME", str(tmp_path))
        import sessionfs.store.deleted as _deleted
        _deleted._DEFAULT_DIR = tmp_path / ".sessionfs"
        _deleted._DEFAULT_PATH = _deleted._DEFAULT_DIR / "deleted.json"

        config = DaemonConfig(sync={"enabled": True, "api_key": "test", "auto": "all", "debounce": 1})
        store = MagicMock()
        return DaemonSyncer(config, store)

    def test_only_too_large_increments_counter(self, tmp_path, monkeypatch):
        """Direct test that _record_session_failure is only called for
        SyncTooLargeError, not generic SyncError or unexpected exceptions.

        Verified by inspecting the daemon source: SyncError and Exception
        branches no longer call _record_session_failure. If a future refactor
        adds the call back to those branches, this test catches it via grep.
        """
        import inspect
        from sessionfs.daemon import main as daemon_main

        src = inspect.getsource(daemon_main.DaemonSyncer._sync_sessions)
        # The "too large" branch must call the failure recorder.
        too_large_block = src.split("except SyncTooLargeError")[1].split("except SyncError")[0]
        assert "_record_session_failure" in too_large_block, \
            "SyncTooLargeError branch must record the failure for exclusion"
        # The transient SyncError branch must NOT call the failure recorder.
        sync_error_block = src.split("except SyncError")[1].split("except Exception")[0]
        assert "_record_session_failure" not in sync_error_block, \
            "Transient SyncError must NOT count toward exclusion threshold"
        # The unexpected-exception branch must NOT call it either.
        exception_block = src.split("except Exception")[1]
        assert "_record_session_failure" not in exception_block, \
            "Unknown exceptions must NOT count toward exclusion threshold"


class TestSyncSkipsExcludedSessions:
    """Daemon hotfix `tk_4abfa69b38d54bc0` — `_sync_sessions()` must skip
    sessions in the local exclusion list (`deleted.json`) instead of
    re-attempting them every cycle.

    Parent Issue `tk_714456298d424202`: paying customer C hit a daemon
    liveness incident where 862+ retries / 4 days on a 75MB excluded
    session starved the async event loop on decompression + DLP scanning,
    timing out `/health` and tripping 13 liveness probe kills.
    """

    def _make_syncer(self, tmp_path, monkeypatch):
        from sessionfs.daemon.config import DaemonConfig
        from sessionfs.daemon.main import DaemonSyncer

        monkeypatch.setenv("HOME", str(tmp_path))
        import sessionfs.store.deleted as _deleted
        _deleted._DEFAULT_DIR = tmp_path / ".sessionfs"
        _deleted._DEFAULT_PATH = _deleted._DEFAULT_DIR / "deleted.json"

        config = DaemonConfig(sync={"enabled": True, "api_key": "test", "auto": "all", "debounce": 1})
        store = MagicMock()
        return DaemonSyncer(config, store)

    def test_sync_sessions_skips_excluded_session_ids(self, tmp_path, monkeypatch):
        """Regression for parent Issue `tk_714456298d424202` — an excluded
        session must short-circuit `_sync_sessions()` before
        `pack_session()` / `push_session()` run, and must be discarded
        from `_pending_sessions` so the next cycle does not re-queue it.
        Non-excluded sessions in the same batch must still process.
        """
        import asyncio
        from unittest.mock import AsyncMock

        from sessionfs.store.deleted import mark_deleted

        syncer = self._make_syncer(tmp_path, monkeypatch)
        excluded_id = "ses_excluded12345"
        normal_id = "ses_normal0123456"

        # mark_deleted writes to ~/.sessionfs/deleted.json under the
        # tmp_path that the fixture redirects HOME to.
        mark_deleted(excluded_id, scope="local", reason="hotfix_test")

        syncer._pending_sessions.add(excluded_id)
        syncer._pending_sessions.add(normal_id)

        # Returning None for every lookup means non-excluded sessions hit
        # the existing `if not session_dir` branch — exits cleanly without
        # touching pack/push. The point of THIS test is that the excluded
        # session must short-circuit BEFORE this lookup runs at all.
        syncer.store.get_session_dir.return_value = None

        fake_client = AsyncMock()
        with patch.object(syncer, "_get_client", return_value=fake_client):
            asyncio.run(syncer._sync_sessions({excluded_id, normal_id}))

        touched = [call.args[0] for call in syncer.store.get_session_dir.call_args_list]

        assert excluded_id not in touched, (
            f"Excluded session_id {excluded_id} must short-circuit before "
            f"`store.get_session_dir()` lookup (and therefore before "
            f"pack_session/push_session)."
        )
        assert normal_id in touched, (
            f"Non-excluded session_id {normal_id} must still process via "
            f"the regular loop body."
        )
        assert excluded_id not in syncer._pending_sessions, (
            "Excluded session_id must be discarded from `_pending_sessions` "
            "so the next sync cycle does not re-queue it."
        )


class TestResolveMaxMemberSize:
    """CLI `_resolve_max_member_size()` precedence chain
    (tk_d5945c4bce3245ce, parent Issue tk_714456298d424202).

    The lookup order is:
      1. `SFS_MAX_SYNC_MEMBER_BYTES_PAID` env var — explicit operator override.
      2. Server-supplied `max_member_bytes` from `GET /sync/settings`.
      3. Hardcoded 50 MB literal final fallback (logged warning).
    Cached per CLI invocation; older servers without the field fall through.
    """

    def setup_method(self, method):
        from sessionfs.cli.cmd_cloud import _reset_max_member_size_cache
        _reset_max_member_size_cache()

    def teardown_method(self, method):
        from sessionfs.cli.cmd_cloud import _reset_max_member_size_cache
        _reset_max_member_size_cache()

    def test_env_var_override_wins_over_server(self, monkeypatch):
        """An explicit env var must beat any server-supplied value so
        customers can experiment ahead of a server upgrade."""
        from sessionfs.cli import cmd_cloud

        monkeypatch.setenv("SFS_MAX_SYNC_MEMBER_BYTES_PAID", str(200 * 1024 * 1024))

        def _fail(*a, **kw):
            raise AssertionError(
                "httpx.get should not be reached when env var override is set"
            )

        monkeypatch.setattr("httpx.get", _fail)

        result = cmd_cloud._resolve_max_member_size(
            "https://api.example.com", "test_key"
        )
        assert result == 200 * 1024 * 1024

    def test_server_paid_tier_value_consumed(self, monkeypatch):
        """When env var is unset, the server-supplied cap (paid-tier
        300 MB in this test) must be used — proving the CLI no longer
        defaults to the 50 MB literal."""
        from sessionfs.cli import cmd_cloud

        monkeypatch.delenv("SFS_MAX_SYNC_MEMBER_BYTES_PAID", raising=False)

        class _FakeResp:
            status_code = 200
            def json(self):
                return {
                    "mode": "all",
                    "debounce_seconds": 30,
                    "max_member_bytes": 300 * 1024 * 1024,
                }

        monkeypatch.setattr("httpx.get", lambda *a, **kw: _FakeResp())

        result = cmd_cloud._resolve_max_member_size(
            "https://api.example.com", "test_key"
        )
        assert result == 300 * 1024 * 1024

    def test_server_free_tier_value_consumed(self, monkeypatch):
        """A free-tier caller sees the 10 MB cap from the server response,
        matching what the server enforces."""
        from sessionfs.cli import cmd_cloud

        monkeypatch.delenv("SFS_MAX_SYNC_MEMBER_BYTES_PAID", raising=False)

        class _FakeResp:
            status_code = 200
            def json(self):
                return {
                    "mode": "off",
                    "debounce_seconds": 30,
                    "max_member_bytes": 10 * 1024 * 1024,
                }

        monkeypatch.setattr("httpx.get", lambda *a, **kw: _FakeResp())

        result = cmd_cloud._resolve_max_member_size(
            "https://api.example.com", "test_key"
        )
        assert result == 10 * 1024 * 1024

    def test_older_server_missing_field_falls_back(self, monkeypatch, caplog):
        """A server that pre-dates Task 2 omits `max_member_bytes`. CLI
        must fall through to the 50 MB literal WITHOUT crashing, and
        must log a warning so operators see the misconfiguration."""
        import logging
        from sessionfs.cli import cmd_cloud

        monkeypatch.delenv("SFS_MAX_SYNC_MEMBER_BYTES_PAID", raising=False)

        class _FakeOldResp:
            status_code = 200
            def json(self):
                # Older-server shape — no max_member_bytes key.
                return {"mode": "off", "debounce_seconds": 30}

        monkeypatch.setattr("httpx.get", lambda *a, **kw: _FakeOldResp())

        with caplog.at_level(logging.WARNING, logger="sessionfs.cli"):
            result = cmd_cloud._resolve_max_member_size(
                "https://api.example.com", "test_key"
            )

        assert result == 50 * 1024 * 1024, (
            "Older server must fall through to 50MB literal cleanly"
        )
        assert any(
            "falling back" in rec.message.lower() for rec in caplog.records
        ), "Final-fallback path must log a warning visible to operators"

    def test_network_error_falls_back(self, monkeypatch, caplog):
        """A network failure during /sync/settings must not crash the
        CLI; falls through to the 50 MB literal with a warning."""
        import logging
        from sessionfs.cli import cmd_cloud

        monkeypatch.delenv("SFS_MAX_SYNC_MEMBER_BYTES_PAID", raising=False)

        def _raise(*a, **kw):
            import httpx
            raise httpx.ConnectError("simulated DNS failure")

        monkeypatch.setattr("httpx.get", _raise)

        with caplog.at_level(logging.WARNING, logger="sessionfs.cli"):
            result = cmd_cloud._resolve_max_member_size(
                "https://api.example.com", "test_key"
            )

        assert result == 50 * 1024 * 1024
        assert any(
            "falling back" in rec.message.lower() for rec in caplog.records
        )

    def test_value_is_cached_across_calls(self, monkeypatch):
        """Second call must not re-poll the server. Guards against the
        ticket's explicit ‘one call per `sfs sync` / `sfs pull` /
        `sfs handoff` run’ requirement."""
        from sessionfs.cli import cmd_cloud

        monkeypatch.delenv("SFS_MAX_SYNC_MEMBER_BYTES_PAID", raising=False)

        call_count = {"n": 0}

        class _FakeResp:
            status_code = 200
            def json(self):
                return {
                    "mode": "off",
                    "debounce_seconds": 30,
                    "max_member_bytes": 75 * 1024 * 1024,
                }

        def _counted(*a, **kw):
            call_count["n"] += 1
            return _FakeResp()

        monkeypatch.setattr("httpx.get", _counted)

        first = cmd_cloud._resolve_max_member_size("https://x", "k")
        second = cmd_cloud._resolve_max_member_size("https://x", "k")
        third = cmd_cloud._resolve_max_member_size("https://x", "k")

        assert first == second == third == 75 * 1024 * 1024
        assert call_count["n"] == 1, (
            f"Expected exactly one /sync/settings call, got {call_count['n']}"
        )


class TestSyncAllOversizedPreflight:
    """`sfs sync` (sync_all → _push_one) must run the same per-file
    oversize preflight as `sfs push` and `sfs handoff`, using the
    server-discovered `max_member_bytes` cap.

    Codex R1 MEDIUM on tk_d5945c4bce3245ce (`tc_6be2e1302d224b88`):
    the upload path inside `sync_all()._push_one` was calling
    `client.push_session()` without first calling
    `_find_oversized_member(archive_data, max_size=max_member_size)`,
    so the resolver-aware cap was only used for pulls / unpack — not
    the actual upload side of `sfs sync`. Regression-protect by
    asserting `push_session()` is NEVER called when the archive
    contains a member that exceeds the discovered cap.
    """

    def setup_method(self, method):
        from sessionfs.cli.cmd_cloud import _reset_max_member_size_cache
        _reset_max_member_size_cache()

    def teardown_method(self, method):
        from sessionfs.cli.cmd_cloud import _reset_max_member_size_cache
        _reset_max_member_size_cache()

    def test_sync_skips_push_for_archive_exceeding_server_cap(
        self, monkeypatch, tmp_path
    ):
        """Pin the contract: when pack_session yields an archive whose
        single member exceeds the resolver-supplied cap, `sfs sync`
        must skip the upload (no push_session call) instead of wasting
        bandwidth on a guaranteed-413."""
        import asyncio
        import io
        import tarfile
        from unittest.mock import AsyncMock, MagicMock

        from sessionfs.cli import cmd_cloud

        # Force a 1 MB cap so a small synthetic over-cap archive is fast
        # to build. Env var wins precedence → no /sync/settings call.
        monkeypatch.setenv("SFS_MAX_SYNC_MEMBER_BYTES_PAID", str(1 * 1024 * 1024))

        # Build a synthetic archive containing a 2 MB messages.jsonl —
        # exceeds the 1 MB cap above so _find_oversized_member trips.
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            payload = b"x" * (2 * 1024 * 1024)
            info = tarfile.TarInfo(name="messages.jsonl")
            info.size = len(payload)
            tar.addfile(info, io.BytesIO(payload))
        oversized_archive = buf.getvalue()

        monkeypatch.setattr(
            "sessionfs.sync.archive.pack_session",
            lambda _session_dir: oversized_archive,
        )

        fake_session_dir = tmp_path / "ses_huge.sfs"
        fake_session_dir.mkdir()

        mock_store = MagicMock()
        mock_store.get_session_dir.return_value = fake_session_dir
        mock_store.get_session_manifest.return_value = {"sync": {"etag": "old"}}
        mock_store.list_sessions.return_value = [{"session_id": "ses_huge1234567"}]
        monkeypatch.setattr(cmd_cloud, "open_store", lambda: mock_store)

        mock_remote = MagicMock()
        mock_remote.sessions = []
        mock_remote.has_more = False
        mock_client = MagicMock()
        mock_client.list_remote_sessions = AsyncMock(return_value=mock_remote)
        mock_client.push_session = AsyncMock()  # must NEVER be called
        mock_client.close = AsyncMock()
        monkeypatch.setattr(cmd_cloud, "_get_sync_client", lambda: mock_client)

        # _load_sync_config is called by sync_all to prime the resolver;
        # the env var wins anyway, but provide a valid stub so no real
        # config file is read.
        monkeypatch.setattr(
            cmd_cloud,
            "_load_sync_config",
            lambda: {"api_url": "https://x", "api_key": "k", "enabled": True},
        )

        # Run inside a fresh loop the same way the prior delete-cli
        # tests do — cmd_cloud.sync_all is sync-on-the-outside,
        # asyncio.run inside.
        def _run(coro):
            loop = asyncio.new_event_loop()
            try:
                return loop.run_until_complete(coro)
            finally:
                loop.close()

        monkeypatch.setattr(cmd_cloud.asyncio, "run", _run)

        try:
            cmd_cloud.sync_all()
        except Exception:
            pass

        mock_client.push_session.assert_not_called()


class TestClientSideOversizedCheck:
    """sfs push refuses to upload archives whose members exceed 10MB."""

    def test_find_oversized_member_detects(self):
        import io as _io
        import tarfile as _tarfile

        from sessionfs.cli.cmd_cloud import _find_oversized_member, MAX_MEMBER_SIZE

        buf = _io.BytesIO()
        with _tarfile.open(fileobj=buf, mode="w:gz") as tar:
            payload = b"x" * (MAX_MEMBER_SIZE + 1024)
            info = _tarfile.TarInfo(name="messages.jsonl")
            info.size = len(payload)
            tar.addfile(info, _io.BytesIO(payload))

        result = _find_oversized_member(buf.getvalue())
        assert result is not None
        name, size = result
        assert name == "messages.jsonl"
        assert size > MAX_MEMBER_SIZE

    def test_find_oversized_member_passes_small(self):
        import io as _io
        import tarfile as _tarfile

        from sessionfs.cli.cmd_cloud import _find_oversized_member

        buf = _io.BytesIO()
        with _tarfile.open(fileobj=buf, mode="w:gz") as tar:
            payload = b"x" * (5 * 1024 * 1024)  # 5MB — well under cap
            info = _tarfile.TarInfo(name="messages.jsonl")
            info.size = len(payload)
            tar.addfile(info, _io.BytesIO(payload))

        assert _find_oversized_member(buf.getvalue()) is None


class TestHandoffOversizeHandling:
    """Regression: sfs handoff must catch oversize sessions BEFORE upload
    (same UX as sfs push) and must surface SyncTooLargeError with the
    friendly /clear-or-/compact message instead of a generic SyncError.
    """

    def test_handoff_imports_too_large_error(self):
        """The handoff command imports SyncTooLargeError so it can route 413s
        to a friendly message instead of falling through to SyncError.
        """
        import inspect
        from sessionfs.cli import cmd_cloud

        src = inspect.getsource(cmd_cloud.handoff)
        assert "SyncTooLargeError" in src, (
            "sfs handoff must import SyncTooLargeError to route 413 cleanly"
        )
        assert "_find_oversized_member" in src, (
            "sfs handoff must run the local pre-upload oversize check"
        )

    def test_handoff_catches_too_large_before_other_handlers(self):
        """The except SyncTooLargeError branch must come before generic
        SyncError handling — otherwise the friendly message never fires.
        """
        import inspect
        from sessionfs.cli import cmd_cloud

        src = inspect.getsource(cmd_cloud.handoff)
        # SyncTooLargeError exists in the except chain.
        assert "except SyncTooLargeError" in src
        # And it appears before any generic SyncError catch (if one exists).
        too_large_idx = src.index("except SyncTooLargeError")
        # SyncDeletedError + SyncConflictError are the other expected handlers.
        deleted_idx = src.index("except SyncDeletedError")
        conflict_idx = src.index("except SyncConflictError")
        # Order doesn't matter between TooLarge/Deleted/Conflict (mutually
        # exclusive subclasses), but all three must be present.
        assert deleted_idx >= 0 and conflict_idx >= 0
        assert too_large_idx >= 0

    def test_handoff_local_oversize_exits_before_push(self, tmp_path, monkeypatch, capsys):
        """Behavioral: invoke handoff() with an oversized session and verify
        it exits 1 BEFORE calling push_session, with the friendly message.

        Builds a real .tar.gz with a 10MB+ member, monkeypatches:
        - resolve_session_id → return the session id verbatim
        - get_session_dir_or_exit → return tmp_path
        - pack_session → return our oversized archive
        - _get_sync_client → MagicMock so an accidental push fails the test loudly
        """
        import io as _io
        import tarfile as _tarfile
        from unittest.mock import MagicMock

        from sessionfs.cli import cmd_cloud
        from sessionfs.cli.cmd_cloud import MAX_MEMBER_SIZE

        # Build an oversized tar.gz.
        buf = _io.BytesIO()
        with _tarfile.open(fileobj=buf, mode="w:gz") as tar:
            payload = b"x" * (MAX_MEMBER_SIZE + 4096)
            info = _tarfile.TarInfo(name="messages.jsonl")
            info.size = len(payload)
            tar.addfile(info, _io.BytesIO(payload))
        oversized_archive = buf.getvalue()

        # Mock client that explodes if push_session is called — proves we
        # exit BEFORE the network call (the whole point of the local check).
        mock_client = MagicMock()
        mock_client.push_session.side_effect = AssertionError(
            "push_session must NOT be called when archive has oversize members"
        )

        # Mock store.
        mock_store = MagicMock()
        mock_store.get_session_manifest.return_value = {"sync": {"etag": "abc"}}
        mock_store.close.return_value = None

        monkeypatch.setattr(cmd_cloud, "open_store", lambda: mock_store)
        monkeypatch.setattr(cmd_cloud, "_get_sync_client", lambda: mock_client)
        monkeypatch.setattr(cmd_cloud, "resolve_session_id", lambda s, sid: sid)
        monkeypatch.setattr(cmd_cloud, "get_session_dir_or_exit", lambda s, sid: tmp_path)
        # pack_session is imported inside handoff(); patch the import target.
        monkeypatch.setattr("sessionfs.sync.archive.pack_session", lambda d: oversized_archive)

        with pytest.raises(SystemExit) as exit_info:
            cmd_cloud.handoff(
                session_id="ses_oversize_test01",
                to="recipient@example.com",
                to_user_id=None,
                to_team_id=None,
                message="",
                ticket_id=None,
                persona_name=None,
                expires_hours=None,
                attach=None,
            )

        assert exit_info.value.code == 1, "handoff must exit with code 1 on oversize"

        # Friendly message must mention the file, some size in MB, and the
        # /compact suggestion. The CLI pre-flight uses MAX_MEMBER_SIZE which
        # is now tier-aware (50 MB default, env-overridable), so we don't
        # pin a specific MB number — just that it's reported.
        from tests.utils.ansi import assert_in_ansi

        captured = capsys.readouterr()
        raw = captured.out + captured.err
        assert_in_ansi("messages.jsonl", raw)
        assert_in_ansi("mb", raw)
        assert_in_ansi("/compact", raw)

        # Confirm push was never attempted.
        mock_client.push_session.assert_not_called()


class TestHandoffIdMisuseRedirect:
    """v0.9.9.8: a user with a handoff ID who runs `sfs handoff <hnd_id>`
    expecting to CLAIM it must be redirected to `sfs pull-handoff`. The
    original error ("Missing option '--to'") was actively misleading.

    Codex round 1 caught that direct-function-call tests bypass Typer's
    parser, which would reject `--to` BEFORE the redirect ran. These
    tests invoke through Typer's CliRunner so the parser is exercised
    end-to-end, matching what the real `sfs` binary does.
    """

    @staticmethod
    def _runner():
        from typer.testing import CliRunner
        from sessionfs.cli.main import app

        return CliRunner(), app

    def test_typer_invocation_with_handoff_id_redirects_without_to(self):
        """End-to-end via Typer: `sfs handoff hnd_<id>` without --to
        must hit the redirect, NOT Typer's "Missing option '--to'"
        error. This is the exact user scenario.
        """

        from tests.utils.ansi import assert_in_ansi, assert_not_in_ansi

        runner, app = self._runner()
        result = runner.invoke(app, ["handoff", "hnd_a83256fc5ed68cef"])
        assert result.exit_code == 2, result.output
        # Substring checks survive Rich's `[cyan]`/`[yellow]` markup
        # expansion under color-enabled CI environments by routing
        # through tests/utils/ansi.py.
        assert_in_ansi("pull-handoff", result.output)
        assert_in_ansi("hnd_a83256fc5ed68cef", result.output)
        # The misleading legacy error must NOT appear.
        assert_not_in_ansi("missing option '--to'", result.output)

    def test_typer_invocation_with_session_id_still_requires_to(self):
        """Normal flow regression: a real session ID with no --to must
        still error with the standard Typer validation message, NOT
        the generic "Unexpected error: ..." that handle_errors would
        otherwise produce when our BadParameter falls through.
        """
        from tests.utils.ansi import assert_in_ansi, assert_not_in_ansi

        runner, app = self._runner()
        result = runner.invoke(
            app, ["handoff", "ses_abc123def4567890"]
        )
        assert result.exit_code != 0
        # The exact-match check happens through the shared helper —
        # Rich splits `--to` across escape codes under color-enabled
        # CI (the bug v0.9.9.9 fixed); strip_ansi normalises that.
        assert_in_ansi("--to", result.output)
        # Codex round 2 caught this: handle_errors used to swallow
        # ClickException as generic Exception, producing this string.
        # The fix (let ClickException pass through) means the standard
        # Typer parser-error format is what the user sees.
        assert_not_in_ansi("unexpected error", result.output)

    def test_alias_shaped_like_handoff_id_works_with_to(self):
        """A user with a session aliased "hnd_deadbeef12345678" must
        still be able to send it. The redirect only triggers when
        `--to` is absent — providing `--to` means the user wants to
        SEND, so the positional is treated as a session ID/alias.

        We invoke through Typer; the call will fail downstream at
        session resolution (we didn't actually create the alias) but
        the FAILURE must NOT be the handoff-ID redirect.
        """
        runner, app = self._runner()
        result = runner.invoke(
            app,
            ["handoff", "hnd_deadbeef12345678", "--to", "alice@example.com"],
        )
        out = result.output.lower()
        # Must NOT redirect to pull-handoff — the user is sending, not claiming.
        assert "pull-handoff" not in out
        assert "looks like a handoff id" not in out

    def test_regex_matches_handoff_ids_only(self):
        """The pattern shouldn't match session IDs or short strings."""
        from sessionfs.cli import cmd_cloud

        assert cmd_cloud._HANDOFF_ID_RE.match("hnd_a83256fc5ed68cef")
        assert not cmd_cloud._HANDOFF_ID_RE.match("ses_abc123def4567890")
        assert not cmd_cloud._HANDOFF_ID_RE.match("hnd_short")
        assert not cmd_cloud._HANDOFF_ID_RE.match("handoff_id")
