"""Tests for resilient local store: self-healing index and handle_errors decorator."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest


def _make_sfs_session(sessions_dir: Path, session_id: str) -> Path:
    """Create a minimal .sfs session directory with a manifest."""
    sfs_dir = sessions_dir / f"{session_id}.sfs"
    sfs_dir.mkdir(parents=True)
    manifest = {
        "session_id": session_id,
        "title": f"Test session {session_id}",
        "source": {"tool": "claude-code"},
        "created_at": "2025-01-01T00:00:00Z",
        "stats": {"message_count": 5},
    }
    (sfs_dir / "manifest.json").write_text(json.dumps(manifest))
    return sfs_dir


class TestSelfHealingIndex:
    """Test that a corrupted index.db is automatically rebuilt."""

    def test_corrupted_index_auto_heals(self, tmp_path: Path) -> None:
        """Write garbage to index.db, initialize, verify it works."""
        store_dir = tmp_path / ".sessionfs"
        store_dir.mkdir()
        sessions_dir = store_dir / "sessions"
        sessions_dir.mkdir()

        # Create a session on disk
        _make_sfs_session(sessions_dir, "ses_test1234abcd")

        # Write garbage to index.db to simulate corruption
        index_path = store_dir / "index.db"
        index_path.write_bytes(b"this is not a valid sqlite database at all!!")

        # Initialize should auto-heal
        from sessionfs.store.local import LocalStore

        local_store = LocalStore(store_dir)
        local_store.initialize()

        # Verify the index is functional and the session was reindexed
        sessions = local_store.list_sessions()
        assert len(sessions) == 1
        assert sessions[0]["session_id"] == "ses_test1234abcd"

        local_store.close()

    def test_missing_index_creates_fresh(self, tmp_path: Path) -> None:
        """If no index.db exists, a fresh one is created normally."""
        store_dir = tmp_path / ".sessionfs"

        from sessionfs.store.local import LocalStore

        local_store = LocalStore(store_dir)
        local_store.initialize()

        sessions = local_store.list_sessions()
        assert sessions == []

        local_store.close()

    def test_valid_index_not_rebuilt(self, tmp_path: Path) -> None:
        """A healthy index.db is not rebuilt unnecessarily."""
        store_dir = tmp_path / ".sessionfs"

        from sessionfs.store.local import LocalStore

        local_store = LocalStore(store_dir)
        local_store.initialize()

        # _needs_reindex should be False
        assert local_store.index._needs_reindex is False

        local_store.close()


class TestHandleErrors:
    """Test the handle_errors decorator."""

    def test_catches_database_error(self) -> None:
        from sessionfs.cli.common import handle_errors

        @handle_errors
        def bad_db():
            raise sqlite3.DatabaseError("disk I/O error")

        with pytest.raises(SystemExit) as exc_info:
            bad_db()
        assert exc_info.value.code == 1

    def test_catches_keyboard_interrupt(self) -> None:
        from sessionfs.cli.common import handle_errors

        @handle_errors
        def interrupted():
            raise KeyboardInterrupt()

        with pytest.raises(SystemExit) as exc_info:
            interrupted()
        assert exc_info.value.code == 130

    def test_catches_generic_exception(self) -> None:
        from sessionfs.cli.common import handle_errors

        @handle_errors
        def explode():
            raise RuntimeError("something went wrong")

        with pytest.raises(SystemExit) as exc_info:
            explode()
        assert exc_info.value.code == 1

    def test_catches_connection_error(self) -> None:
        from sessionfs.cli.common import handle_errors

        @handle_errors
        def no_net():
            raise ConnectionError("refused")

        with pytest.raises(SystemExit) as exc_info:
            no_net()
        assert exc_info.value.code == 1

    def test_catches_permission_error(self) -> None:
        from sessionfs.cli.common import handle_errors

        @handle_errors
        def no_perm():
            raise PermissionError("access denied")

        with pytest.raises(SystemExit) as exc_info:
            no_perm()
        assert exc_info.value.code == 1

    def test_catches_file_not_found(self) -> None:
        from sessionfs.cli.common import handle_errors

        @handle_errors
        def missing():
            raise FileNotFoundError("config.toml")

        with pytest.raises(SystemExit) as exc_info:
            missing()
        assert exc_info.value.code == 1

    def test_passes_through_system_exit(self) -> None:
        from sessionfs.cli.common import handle_errors

        @handle_errors
        def normal_exit():
            raise SystemExit(0)

        with pytest.raises(SystemExit) as exc_info:
            normal_exit()
        assert exc_info.value.code == 0

    def test_successful_function_returns_value(self) -> None:
        from sessionfs.cli.common import handle_errors

        @handle_errors
        def ok():
            return 42

        assert ok() == 42
