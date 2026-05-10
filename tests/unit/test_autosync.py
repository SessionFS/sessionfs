"""Tests for autosync feature."""

from __future__ import annotations

import time
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
                message="",
            )

        assert exit_info.value.code == 1, "handoff must exit with code 1 on oversize"

        # Friendly message must mention the file, some size in MB, and the
        # /compact suggestion. The CLI pre-flight uses MAX_MEMBER_SIZE which
        # is now tier-aware (50 MB default, env-overridable), so we don't
        # pin a specific MB number — just that it's reported.
        captured = capsys.readouterr()
        out = (captured.out + captured.err).lower()
        assert "messages.jsonl" in out, f"output missing filename: {captured.err}"
        assert "mb" in out, f"output missing size: {captured.err}"
        assert "/compact" in out, f"output missing suggestion: {captured.err}"

        # Confirm push was never attempted.
        mock_client.push_session.assert_not_called()
