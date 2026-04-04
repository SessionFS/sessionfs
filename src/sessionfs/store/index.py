"""SQLite session metadata index.

Caches session metadata for fast listing and lookup. Also tracks
native-to-sfs session mappings for change detection.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sessionfs.watchers.base import NativeSessionRef

logger = logging.getLogger("sessionfs.store.index")

_SCHEMA_SQL = """\
CREATE TABLE IF NOT EXISTS sessions (
    session_id            TEXT PRIMARY KEY,
    title                 TEXT,
    source_tool           TEXT NOT NULL,
    source_tool_version   TEXT,
    original_session_id   TEXT,
    project_path          TEXT,
    model_provider        TEXT,
    model_id              TEXT,
    created_at            TEXT NOT NULL,
    updated_at            TEXT,
    message_count         INTEGER DEFAULT 0,
    turn_count            INTEGER DEFAULT 0,
    tool_use_count        INTEGER DEFAULT 0,
    total_input_tokens    INTEGER DEFAULT 0,
    total_output_tokens   INTEGER DEFAULT 0,
    duration_ms           INTEGER,
    tags                  TEXT DEFAULT '[]',
    sfs_dir_path          TEXT NOT NULL,
    indexed_at            TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_sessions_source_tool
    ON sessions(source_tool);
CREATE INDEX IF NOT EXISTS idx_sessions_project_path
    ON sessions(project_path);
CREATE INDEX IF NOT EXISTS idx_sessions_created_at
    ON sessions(created_at);

CREATE TABLE IF NOT EXISTS tracked_sessions (
    native_session_id   TEXT PRIMARY KEY,
    tool                TEXT NOT NULL,
    native_path         TEXT NOT NULL,
    sfs_session_id      TEXT,
    last_mtime          REAL NOT NULL DEFAULT 0.0,
    last_size           INTEGER NOT NULL DEFAULT 0,
    last_captured_at    TEXT,
    project_path        TEXT,
    FOREIGN KEY (sfs_session_id) REFERENCES sessions(session_id)
);

CREATE INDEX IF NOT EXISTS idx_tracked_tool
    ON tracked_sessions(tool);

CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER PRIMARY KEY
);

INSERT OR IGNORE INTO schema_version (version) VALUES (1);
"""


class SessionIndex:
    """SQLite-backed session metadata index."""

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._conn: sqlite3.Connection | None = None
        self._needs_reindex: bool = False

    def initialize(self) -> None:
        """Create the database and tables.

        If the database is corrupted, automatically deletes the index
        files and recreates the schema from scratch.
        """
        try:
            self._conn = sqlite3.connect(str(self._db_path))
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA busy_timeout=5000")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.executescript(_SCHEMA_SQL)
            self._conn.commit()
        except (sqlite3.DatabaseError, sqlite3.OperationalError) as exc:
            logger.warning(
                "Index was corrupted. Rebuilding automatically... (%s)", exc
            )
            # Close the broken connection
            if self._conn:
                try:
                    self._conn.close()
                except Exception:
                    pass
                self._conn = None

            # Delete index files
            for suffix in ("", "-wal", "-shm"):
                p = self._db_path.parent / (self._db_path.name + suffix)
                if p.exists():
                    p.unlink()

            # Recreate from scratch
            self._conn = sqlite3.connect(str(self._db_path))
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA busy_timeout=5000")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.executescript(_SCHEMA_SQL)
            self._conn.commit()

            self._needs_reindex = True

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError("Index not initialized")
        return self._conn

    def upsert_session(
        self,
        session_id: str,
        manifest: dict[str, Any],
        sfs_dir_path: str,
    ) -> None:
        """Insert or update a session record from its manifest."""
        source = manifest.get("source", {})
        model = manifest.get("model") or {}
        stats = manifest.get("stats") or {}

        self.conn.execute(
            """
            INSERT INTO sessions (
                session_id, title, source_tool, source_tool_version,
                original_session_id, project_path, model_provider, model_id,
                created_at, updated_at, message_count, turn_count,
                tool_use_count, total_input_tokens, total_output_tokens,
                duration_ms, tags, sfs_dir_path, indexed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(session_id) DO UPDATE SET
                title=excluded.title,
                updated_at=excluded.updated_at,
                message_count=excluded.message_count,
                turn_count=excluded.turn_count,
                tool_use_count=excluded.tool_use_count,
                total_input_tokens=excluded.total_input_tokens,
                total_output_tokens=excluded.total_output_tokens,
                duration_ms=excluded.duration_ms,
                tags=excluded.tags,
                indexed_at=excluded.indexed_at
            """,
            (
                session_id,
                manifest.get("title"),
                source.get("tool", "unknown"),
                source.get("tool_version"),
                source.get("original_session_id"),
                None,
                model.get("provider"),
                model.get("model_id"),
                manifest.get("created_at", ""),
                manifest.get("updated_at"),
                stats.get("message_count", 0),
                stats.get("turn_count", 0),
                stats.get("tool_use_count", 0),
                stats.get("total_input_tokens", 0),
                stats.get("total_output_tokens", 0),
                stats.get("duration_ms"),
                json.dumps(manifest.get("tags", [])),
                sfs_dir_path,
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        self.conn.commit()

    def get_tracked_session(self, native_session_id: str) -> NativeSessionRef | None:
        """Look up a tracked session by native ID."""
        row = self.conn.execute(
            "SELECT * FROM tracked_sessions WHERE native_session_id = ?",
            (native_session_id,),
        ).fetchone()

        if not row:
            return None

        return NativeSessionRef(
            tool=row["tool"],
            native_session_id=row["native_session_id"],
            native_path=row["native_path"],
            sfs_session_id=row["sfs_session_id"],
            last_mtime=row["last_mtime"],
            last_size=row["last_size"],
            last_captured_at=row["last_captured_at"],
            project_path=row["project_path"],
        )

    def upsert_tracked_session(self, ref: NativeSessionRef) -> None:
        """Insert or update a tracked session mapping."""
        self.conn.execute(
            """
            INSERT INTO tracked_sessions (
                native_session_id, tool, native_path, sfs_session_id,
                last_mtime, last_size, last_captured_at, project_path
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(native_session_id) DO UPDATE SET
                native_path=excluded.native_path,
                sfs_session_id=excluded.sfs_session_id,
                last_mtime=excluded.last_mtime,
                last_size=excluded.last_size,
                last_captured_at=excluded.last_captured_at,
                project_path=excluded.project_path
            """,
            (
                ref.native_session_id,
                ref.tool,
                ref.native_path,
                ref.sfs_session_id,
                ref.last_mtime,
                ref.last_size,
                ref.last_captured_at,
                ref.project_path,
            ),
        )
        self.conn.commit()

    def get_session(self, session_id: str) -> dict[str, Any] | None:
        """Get a single session by exact ID."""
        row = self.conn.execute(
            "SELECT * FROM sessions WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        return dict(row) if row else None

    def find_sessions_by_prefix(self, prefix: str) -> list[dict[str, Any]]:
        """Find sessions whose ID starts with given prefix."""
        rows = self.conn.execute(
            "SELECT * FROM sessions WHERE session_id LIKE ? ORDER BY created_at DESC",
            (prefix + "%",),
        ).fetchall()
        return [dict(r) for r in rows]

    def list_sessions(self) -> list[dict[str, Any]]:
        """List all sessions, newest first."""
        rows = self.conn.execute(
            "SELECT * FROM sessions ORDER BY created_at DESC"
        ).fetchall()
        return [dict(r) for r in rows]

    def session_count(self) -> int:
        """Return total number of sessions."""
        row = self.conn.execute("SELECT COUNT(*) FROM sessions").fetchone()
        return row[0] if row else 0

    def close(self) -> None:
        """Close the database connection."""
        if self._conn:
            self._conn.close()
            self._conn = None
