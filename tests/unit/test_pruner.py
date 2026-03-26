"""Tests for session pruning engine."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sessionfs.store.pruner import (
    SessionPruner,
    StorageConfig,
    _human_bytes,
    parse_size,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _create_session(
    sessions_dir: Path,
    conn: sqlite3.Connection,
    session_id: str,
    *,
    age_days: int = 0,
    size_bytes: int = 1024,
    synced: bool = False,
    bookmarked: bool = False,
    alias: str | None = None,
    tool: str = "claude-code",
) -> Path:
    """Create a fake session on disk + index for testing."""
    created_at = (
        datetime.now(timezone.utc) - timedelta(days=age_days)
    ).isoformat()

    sfs_dir = sessions_dir / f"{session_id}.sfs"
    sfs_dir.mkdir(parents=True, exist_ok=True)

    manifest = {
        "session_id": session_id,
        "title": f"Test session {session_id}",
        "source": {"tool": tool},
        "model": {"provider": "anthropic", "model_id": "opus-4"},
        "stats": {"message_count": 10},
        "created_at": created_at,
        "updated_at": created_at,
        "tags": [],
    }
    if synced:
        manifest["sync"] = {"etag": "abc123", "last_sync_at": created_at}
    if bookmarked:
        manifest["bookmarked"] = True
    if alias:
        manifest["alias"] = alias

    (sfs_dir / "manifest.json").write_text(json.dumps(manifest))

    # Create a messages file of approximate target size
    msg_content = "x" * max(0, size_bytes - 200)  # rough
    (sfs_dir / "messages.jsonl").write_text(msg_content)
    (sfs_dir / "workspace.json").write_text("{}")
    (sfs_dir / "tools.json").write_text("{}")

    conn.execute(
        """
        INSERT OR REPLACE INTO sessions (
            session_id, title, source_tool, created_at, updated_at,
            message_count, sfs_dir_path, indexed_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            session_id,
            f"Test session {session_id}",
            tool,
            created_at,
            created_at,
            10,
            str(sfs_dir),
            datetime.now(timezone.utc).isoformat(),
        ),
    )
    conn.commit()

    return sfs_dir


def _setup_db(tmp_path: Path) -> sqlite3.Connection:
    """Create a fresh SQLite index for testing."""
    db_path = tmp_path / "index.db"
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS sessions (
            session_id TEXT PRIMARY KEY,
            title TEXT,
            source_tool TEXT NOT NULL,
            source_tool_version TEXT,
            original_session_id TEXT,
            project_path TEXT,
            model_provider TEXT,
            model_id TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT,
            message_count INTEGER DEFAULT 0,
            turn_count INTEGER DEFAULT 0,
            tool_use_count INTEGER DEFAULT 0,
            total_input_tokens INTEGER DEFAULT 0,
            total_output_tokens INTEGER DEFAULT 0,
            duration_ms INTEGER,
            tags TEXT DEFAULT '[]',
            sfs_dir_path TEXT NOT NULL,
            indexed_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS tracked_sessions (
            native_session_id TEXT PRIMARY KEY,
            tool TEXT NOT NULL,
            native_path TEXT NOT NULL,
            sfs_session_id TEXT,
            last_mtime REAL NOT NULL DEFAULT 0.0,
            last_size INTEGER NOT NULL DEFAULT 0,
            last_captured_at TEXT,
            project_path TEXT
        );
        """
    )
    conn.commit()
    return conn


# ---------------------------------------------------------------------------
# Tests: parse_size
# ---------------------------------------------------------------------------


class TestParseSize:
    def test_gigabytes(self):
        assert parse_size("2GB") == 2 * 1024**3

    def test_megabytes(self):
        assert parse_size("500MB") == 500 * 1024**2

    def test_kilobytes(self):
        assert parse_size("100KB") == 100 * 1024

    def test_bytes(self):
        assert parse_size("4096B") == 4096

    def test_bare_number(self):
        assert parse_size("1024") == 1024

    def test_lowercase(self):
        assert parse_size("1gb") == 1024**3

    def test_float(self):
        assert parse_size("1.5GB") == int(1.5 * 1024**3)


# ---------------------------------------------------------------------------
# Tests: _human_bytes
# ---------------------------------------------------------------------------


class TestHumanBytes:
    def test_bytes(self):
        assert _human_bytes(512) == "512 B"

    def test_kb(self):
        assert "KB" in _human_bytes(2048)

    def test_mb(self):
        assert "MB" in _human_bytes(5 * 1024**2)

    def test_gb(self):
        assert "GB" in _human_bytes(3 * 1024**3)


# ---------------------------------------------------------------------------
# Tests: StorageUsage
# ---------------------------------------------------------------------------


class TestCalculateUsage:
    def test_empty_store(self, tmp_path):
        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir()
        conn = _setup_db(tmp_path)
        pruner = SessionPruner(sessions_dir, conn)

        usage = pruner.calculate_usage()
        assert usage.session_count == 0
        assert usage.total_bytes == 0
        assert usage.synced_count == 0

    def test_counts_sessions(self, tmp_path):
        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir()
        conn = _setup_db(tmp_path)

        _create_session(sessions_dir, conn, "ses_aaa", synced=True)
        _create_session(sessions_dir, conn, "ses_bbb", synced=False)
        _create_session(sessions_dir, conn, "ses_ccc", synced=True)

        pruner = SessionPruner(sessions_dir, conn)
        usage = pruner.calculate_usage()

        assert usage.session_count == 3
        assert usage.synced_count == 2
        assert usage.unsynced_count == 1
        assert usage.total_bytes > 0


# ---------------------------------------------------------------------------
# Tests: get_prunable_sessions
# ---------------------------------------------------------------------------


class TestGetPrunableSessions:
    def test_nothing_prunable_when_young(self, tmp_path):
        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir()
        conn = _setup_db(tmp_path)

        _create_session(sessions_dir, conn, "ses_aaa", age_days=5, synced=True)
        _create_session(sessions_dir, conn, "ses_bbb", age_days=10, synced=False)

        pruner = SessionPruner(sessions_dir, conn)
        config = StorageConfig(local_retention_days=90, synced_retention_days=30)
        candidates = pruner.get_prunable_sessions(config)

        assert len(candidates) == 0

    def test_synced_sessions_prunable_after_synced_retention(self, tmp_path):
        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir()
        conn = _setup_db(tmp_path)

        _create_session(sessions_dir, conn, "ses_old", age_days=35, synced=True)
        _create_session(sessions_dir, conn, "ses_new", age_days=5, synced=True)

        pruner = SessionPruner(sessions_dir, conn)
        config = StorageConfig(synced_retention_days=30)
        candidates = pruner.get_prunable_sessions(config)

        assert len(candidates) == 1
        assert candidates[0].session_id == "ses_old"
        assert candidates[0].reason == "age"

    def test_unsynced_sessions_prunable_after_local_retention(self, tmp_path):
        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir()
        conn = _setup_db(tmp_path)

        _create_session(sessions_dir, conn, "ses_old", age_days=100, synced=False)
        _create_session(sessions_dir, conn, "ses_mid", age_days=50, synced=False)

        pruner = SessionPruner(sessions_dir, conn)
        config = StorageConfig(local_retention_days=90)
        candidates = pruner.get_prunable_sessions(config)

        assert len(candidates) == 1
        assert candidates[0].session_id == "ses_old"

    def test_storage_cap_triggers_additional_pruning(self, tmp_path):
        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir()
        conn = _setup_db(tmp_path)

        # Create sessions that exceed a tiny cap
        _create_session(
            sessions_dir, conn, "ses_aaa", age_days=5, size_bytes=5000, synced=True
        )
        _create_session(
            sessions_dir, conn, "ses_bbb", age_days=10, size_bytes=5000, synced=True
        )

        pruner = SessionPruner(sessions_dir, conn)
        # Set a very small cap
        config = StorageConfig(
            max_local_bytes=3000,
            local_retention_days=90,
            synced_retention_days=30,
        )
        candidates = pruner.get_prunable_sessions(config)

        # Should flag at least one for storage_cap
        assert any(c.reason == "storage_cap" for c in candidates)


# ---------------------------------------------------------------------------
# Tests: prune
# ---------------------------------------------------------------------------


class TestPrune:
    def test_prune_deletes_old_synced_sessions(self, tmp_path):
        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir()
        conn = _setup_db(tmp_path)

        sfs_dir = _create_session(
            sessions_dir, conn, "ses_old", age_days=35, synced=True
        )
        _create_session(sessions_dir, conn, "ses_new", age_days=5, synced=True)

        pruner = SessionPruner(sessions_dir, conn)
        config = StorageConfig(synced_retention_days=30)
        result = pruner.prune(config)

        assert result.pruned_count == 1
        assert result.freed_bytes > 0
        assert not sfs_dir.exists()
        # Still in index?
        row = conn.execute(
            "SELECT * FROM sessions WHERE session_id = 'ses_old'"
        ).fetchone()
        assert row is None

    def test_prune_skips_unsynced_without_force(self, tmp_path):
        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir()
        conn = _setup_db(tmp_path)

        _create_session(sessions_dir, conn, "ses_old", age_days=100, synced=False)

        pruner = SessionPruner(sessions_dir, conn)
        config = StorageConfig(local_retention_days=90)
        result = pruner.prune(config, force=False)

        assert result.pruned_count == 0
        assert result.skipped_unsynced == 1

    def test_prune_force_deletes_unsynced(self, tmp_path):
        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir()
        conn = _setup_db(tmp_path)

        sfs_dir = _create_session(
            sessions_dir, conn, "ses_old", age_days=100, synced=False
        )

        pruner = SessionPruner(sessions_dir, conn)
        config = StorageConfig(local_retention_days=90)
        result = pruner.prune(config, force=True)

        assert result.pruned_count == 1
        assert not sfs_dir.exists()

    def test_prune_skips_bookmarked(self, tmp_path):
        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir()
        conn = _setup_db(tmp_path)

        _create_session(
            sessions_dir, conn, "ses_bk", age_days=100, synced=True, bookmarked=True
        )

        pruner = SessionPruner(sessions_dir, conn)
        config = StorageConfig(
            synced_retention_days=30, preserve_bookmarked=True
        )
        result = pruner.prune(config)

        assert result.pruned_count == 0
        assert result.skipped_bookmarked == 1

    def test_prune_skips_aliased(self, tmp_path):
        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir()
        conn = _setup_db(tmp_path)

        _create_session(
            sessions_dir, conn, "ses_al", age_days=100, synced=True, alias="my-debug"
        )

        pruner = SessionPruner(sessions_dir, conn)
        config = StorageConfig(
            synced_retention_days=30, preserve_aliased=True
        )
        result = pruner.prune(config)

        assert result.pruned_count == 0
        assert result.skipped_aliased == 1

    def test_prune_force_overrides_bookmarked_and_aliased(self, tmp_path):
        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir()
        conn = _setup_db(tmp_path)

        _create_session(
            sessions_dir,
            conn,
            "ses_bk",
            age_days=100,
            synced=True,
            bookmarked=True,
            alias="protected",
        )

        pruner = SessionPruner(sessions_dir, conn)
        config = StorageConfig(synced_retention_days=30)
        result = pruner.prune(config, force=True)

        assert result.pruned_count == 1

    def test_dry_run_does_not_delete(self, tmp_path):
        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir()
        conn = _setup_db(tmp_path)

        sfs_dir = _create_session(
            sessions_dir, conn, "ses_old", age_days=35, synced=True
        )

        pruner = SessionPruner(sessions_dir, conn)
        config = StorageConfig(synced_retention_days=30)
        result = pruner.prune(config, dry_run=True)

        assert result.pruned_count == 1
        assert sfs_dir.exists()  # Not actually deleted

    def test_index_cleaned_after_prune(self, tmp_path):
        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir()
        conn = _setup_db(tmp_path)

        _create_session(sessions_dir, conn, "ses_old", age_days=35, synced=True)

        # Add a tracked session pointing to this
        conn.execute(
            """
            INSERT INTO tracked_sessions (native_session_id, tool, native_path, sfs_session_id, last_mtime)
            VALUES ('native_123', 'claude-code', '/path', 'ses_old', 0.0)
            """
        )
        conn.commit()

        pruner = SessionPruner(sessions_dir, conn)
        config = StorageConfig(synced_retention_days=30)
        pruner.prune(config)

        # Both tables should be clean
        assert conn.execute(
            "SELECT COUNT(*) FROM sessions WHERE session_id = 'ses_old'"
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM tracked_sessions WHERE sfs_session_id = 'ses_old'"
        ).fetchone()[0] == 0


# ---------------------------------------------------------------------------
# Tests: retention_days=0 disables age pruning
# ---------------------------------------------------------------------------


class TestRetentionDisabled:
    def test_zero_retention_disables_age_pruning(self, tmp_path):
        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir()
        conn = _setup_db(tmp_path)

        _create_session(sessions_dir, conn, "ses_ancient", age_days=500, synced=True)

        pruner = SessionPruner(sessions_dir, conn)
        config = StorageConfig(
            local_retention_days=0,
            max_local_bytes=10 * 1024**3,  # Very high cap
        )
        candidates = pruner.get_prunable_sessions(config)

        assert len(candidates) == 0
