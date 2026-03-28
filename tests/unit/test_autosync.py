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
