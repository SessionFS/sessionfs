"""SQLite FTS5 search index for session content.

Stores full-text content from messages, file paths, and error messages
in a SQLite FTS5 virtual table for fast keyword search. The index lives
at ~/.sessionfs/search.db. ``reindex_all`` is incremental: each indexed
session records a fingerprint of its files, and unchanged sessions are
skipped on later passes.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger("sessionfs.mcp.search")

_SCHEMA_SQL = """\
CREATE VIRTUAL TABLE IF NOT EXISTS session_search
USING fts5(
    session_id UNINDEXED,
    title,
    source_tool UNINDEXED,
    model_id UNINDEXED,
    project_path,
    messages_text,
    file_paths,
    error_messages,
    created_at UNINDEXED,
    message_count UNINDEXED
);

CREATE TABLE IF NOT EXISTS search_meta (
    session_id TEXT PRIMARY KEY,
    indexed_at TEXT NOT NULL,
    message_count INTEGER DEFAULT 0,
    fingerprint TEXT
);
"""

def _manifest_signature(manifest_path: Path) -> str:
    """Hash of only the manifest fields the index stores.

    The manifest is rewritten for reasons that don't affect search (sync
    bookkeeping such as etag and last_sync_at changes on every sync), so its
    size and mtime would mark sessions dirty far more often than needed.
    """
    try:
        manifest = json.loads(manifest_path.read_text())
    except (OSError, json.JSONDecodeError):
        return "-"
    if not isinstance(manifest, dict):
        return "-"

    def sub(key: str, field: str) -> Any:
        value = manifest.get(key)
        return value.get(field) if isinstance(value, dict) else None

    fields = [
        manifest.get("title"),
        manifest.get("created_at"),
        sub("source", "tool"),
        sub("model", "model_id"),
        sub("stats", "message_count"),
    ]
    digest = hashlib.sha256(json.dumps(fields, default=str).encode()).hexdigest()
    return digest[:16]


def _session_fingerprint(sfs_dir: Path) -> str:
    """Cheap change detector for a session.

    The message and workspace files are only stat'ed (size and mtime), never
    read, so checking every session costs a few syscalls each regardless of
    session size. The small manifest is read so that sync-only rewrites of it
    don't count as changes.
    """
    parts = [f"manifest.json:{_manifest_signature(sfs_dir / 'manifest.json')}"]
    for name in ("messages.jsonl", "workspace.json"):
        try:
            st = (sfs_dir / name).stat()
        except OSError:
            parts.append(f"{name}:-")
        else:
            parts.append(f"{name}:{st.st_size}:{st.st_mtime_ns}")
    return "|".join(parts)

# Patterns for extracting file paths and errors from message text
_FILE_PATH_RE = re.compile(r"(?:^|[\s\"'`(])(/[\w./-]{2,})(?:[\s\"'`):,]|$)", re.MULTILINE)
_ERROR_RE = re.compile(
    r"(?:Error|Exception|FAILED|FATAL|error\[)[\s:].{10,200}",
    re.IGNORECASE,
)


class SessionSearchIndex:
    """SQLite FTS5 search index for session content."""

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._conn: sqlite3.Connection | None = None

    def initialize(self) -> None:
        self._conn = sqlite3.connect(str(self._db_path))
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.executescript(_SCHEMA_SQL)
        # Indexes created before fingerprints existed lack the column; add it
        # so those sessions get fingerprinted on the next pass.
        columns = {
            row["name"] for row in self._conn.execute("PRAGMA table_info(search_meta)")
        }
        if "fingerprint" not in columns:
            self._conn.execute("ALTER TABLE search_meta ADD COLUMN fingerprint TEXT")
        self._conn.commit()

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError("Search index not initialized")
        return self._conn

    def index_session(
        self, session_id: str, sfs_dir: Path, fingerprint: str | None = None
    ) -> bool:
        """Index a .sfs session directory for full-text search.

        Returns False when the session has no readable manifest; any earlier
        entry for it is removed so search doesn't keep serving stale content.
        """
        # Fingerprint before reading: if the session changes mid-read, the
        # stored fingerprint is stale and the next pass re-indexes it.
        if fingerprint is None:
            fingerprint = _session_fingerprint(sfs_dir)
        manifest_path = sfs_dir / "manifest.json"
        messages_path = sfs_dir / "messages.jsonl"

        try:
            manifest = json.loads(manifest_path.read_text())
        except (json.JSONDecodeError, OSError):
            manifest = None
        if not isinstance(manifest, dict):
            self._remove(session_id)
            return False

        source = manifest.get("source", {})
        model = manifest.get("model") or {}
        stats = manifest.get("stats") or {}

        # Extract project path from workspace.json
        project_path = ""
        workspace_path = sfs_dir / "workspace.json"
        if workspace_path.exists():
            try:
                workspace = json.loads(workspace_path.read_text())
                project_path = workspace.get("root_path", "")
            except (json.JSONDecodeError, OSError):
                pass

        # Extract text from messages
        all_text: list[str] = []
        file_paths: set[str] = set()
        errors: list[str] = []

        if messages_path.exists():
            try:
                with open(messages_path) as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        msg = json.loads(line)
                        text = _extract_message_text(msg)
                        if text:
                            all_text.append(text)
                            # Extract file paths
                            for match in _FILE_PATH_RE.finditer(text):
                                file_paths.add(match.group(1))
                            # Extract error patterns
                            for match in _ERROR_RE.finditer(text):
                                errors.append(match.group(0)[:200])
            except (json.JSONDecodeError, OSError) as exc:
                logger.warning("Failed to read messages for %s: %s", session_id, exc)

        messages_text = "\n".join(all_text)
        file_paths_text = "\n".join(sorted(file_paths))
        errors_text = "\n".join(errors[:50])  # Cap errors

        # Delete existing entry then insert (FTS5 doesn't support UPDATE well)
        self.conn.execute(
            "DELETE FROM session_search WHERE session_id = ?", (session_id,)
        )
        self.conn.execute(
            "DELETE FROM search_meta WHERE session_id = ?", (session_id,)
        )

        self.conn.execute(
            """INSERT INTO session_search (
                session_id, title, source_tool, model_id, project_path,
                messages_text, file_paths, error_messages,
                created_at, message_count
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                session_id,
                manifest.get("title") or "",
                source.get("tool", ""),
                model.get("model_id", ""),
                project_path,
                messages_text,
                file_paths_text,
                errors_text,
                manifest.get("created_at", ""),
                str(stats.get("message_count", 0)),
            ),
        )

        self.conn.execute(
            "INSERT INTO search_meta (session_id, indexed_at, message_count, fingerprint)"
            " VALUES (?, ?, ?, ?)",
            (
                session_id,
                datetime.now(timezone.utc).isoformat(),
                stats.get("message_count", 0),
                fingerprint,
            ),
        )
        self.conn.commit()
        return True

    def _remove(self, session_id: str) -> None:
        self.conn.execute("DELETE FROM session_search WHERE session_id = ?", (session_id,))
        self.conn.execute("DELETE FROM search_meta WHERE session_id = ?", (session_id,))
        self.conn.commit()

    def is_indexed(self, session_id: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM search_meta WHERE session_id = ?", (session_id,)
        ).fetchone()
        return row is not None

    def search(
        self,
        query: str,
        tool_filter: str | None = None,
        limit: int = 5,
    ) -> list[dict[str, Any]]:
        """Full-text search across all indexed sessions."""
        # Escape FTS5 special chars for safety
        safe_query = _fts5_escape(query)
        if not safe_query:
            return []

        sql = """
            SELECT session_id, title, source_tool, model_id, created_at,
                   message_count,
                   snippet(session_search, 5, '>>>', '<<<', '...', 40) AS excerpt
            FROM session_search
            WHERE session_search MATCH ?
            ORDER BY rank
            LIMIT ?
        """
        rows = self.conn.execute(sql, (safe_query, limit)).fetchall()

        results = []
        for row in rows:
            if tool_filter and row["source_tool"] != tool_filter:
                continue
            results.append({
                "session_id": row["session_id"],
                "title": row["title"],
                "source_tool": row["source_tool"],
                "model_id": row["model_id"],
                "created_at": row["created_at"],
                "message_count": int(row["message_count"]) if row["message_count"] else 0,
                "excerpt": row["excerpt"],
            })

        return results[:limit]

    def find_by_file(self, file_path: str, limit: int = 5) -> list[dict[str, Any]]:
        """Find sessions that touched a specific file."""
        basename = Path(file_path).name
        safe = _fts5_escape(basename)
        if not safe:
            return []

        # Search across file_paths and messages_text (tool calls reference files)
        sql = """
            SELECT session_id, title, source_tool, model_id, created_at,
                   message_count,
                   snippet(session_search, 5, '>>>', '<<<', '...', 40) AS excerpt
            FROM session_search
            WHERE session_search MATCH ?
            ORDER BY rank
            LIMIT ?
        """
        rows = self.conn.execute(sql, (safe, limit)).fetchall()
        return [
            {
                "session_id": row["session_id"],
                "title": row["title"],
                "source_tool": row["source_tool"],
                "created_at": row["created_at"],
                "message_count": int(row["message_count"]) if row["message_count"] else 0,
                "excerpt": row["excerpt"],
            }
            for row in rows
        ]

    def find_by_error(self, error_text: str, limit: int = 5) -> list[dict[str, Any]]:
        """Find sessions that encountered similar errors."""
        safe = _fts5_escape(error_text)
        if not safe:
            return []

        # Search across all text fields for error patterns
        sql = """
            SELECT session_id, title, source_tool, model_id, created_at,
                   message_count,
                   snippet(session_search, 5, '>>>', '<<<', '...', 40) AS excerpt
            FROM session_search
            WHERE session_search MATCH ?
            ORDER BY rank
            LIMIT ?
        """
        rows = self.conn.execute(sql, (safe, limit)).fetchall()
        return [
            {
                "session_id": row["session_id"],
                "title": row["title"],
                "source_tool": row["source_tool"],
                "created_at": row["created_at"],
                "message_count": int(row["message_count"]) if row["message_count"] else 0,
                "excerpt": row["excerpt"],
            }
            for row in rows
        ]

    def reindex_all(self, store_dir: Path, *, force: bool = False) -> int:
        """Bring the index up to date with the store. Returns count indexed.

        Sessions whose files are unchanged since they were last indexed are
        skipped unless ``force`` is set, and entries for sessions that no
        longer exist on disk are removed.
        """
        sessions_dir = store_dir / "sessions"
        if not sessions_dir.is_dir():
            return 0

        known = {
            row["session_id"]: row["fingerprint"]
            for row in self.conn.execute("SELECT session_id, fingerprint FROM search_meta")
        }

        present: set[str] = set()
        count = 0
        for sfs_dir in sessions_dir.iterdir():
            if sfs_dir.is_dir() and sfs_dir.name.endswith(".sfs"):
                session_id = sfs_dir.name[:-4]  # Strip .sfs
                present.add(session_id)
                fingerprint = _session_fingerprint(sfs_dir)
                if not force and known.get(session_id) == fingerprint:
                    continue
                if self.index_session(session_id, sfs_dir, fingerprint=fingerprint):
                    count += 1

        for session_id in known:
            if session_id not in present:
                self._remove(session_id)

        return count

    def close(self) -> None:
        if self._conn:
            self._conn.close()
            self._conn = None


def _extract_message_text(msg: dict[str, Any]) -> str:
    """Extract plain text from a message for indexing."""
    content = msg.get("content", [])
    if isinstance(content, str):
        return content

    parts = []
    for block in content:
        if isinstance(block, str):
            parts.append(block)
        elif isinstance(block, dict):
            btype = block.get("type", "")
            if btype == "text":
                parts.append(block.get("text", ""))
            elif btype == "tool_use":
                name = block.get("name", "")
                inp = block.get("input", {})
                if isinstance(inp, dict):
                    cmd = inp.get("command", "")
                    if cmd:
                        parts.append(f"[{name}] {cmd}")
                    else:
                        parts.append(f"[{name}]")
            elif btype == "tool_result":
                result = block.get("content", "")
                if isinstance(result, str):
                    parts.append(result[:500])
    return "\n".join(parts)


def _fts5_escape(query: str) -> str:
    """Escape a user query for safe FTS5 MATCH usage."""
    # Remove FTS5 operators and special chars, keep words
    words = re.findall(r'[\w./-]+', query)
    if not words:
        return ""
    # Join with implicit AND (FTS5 default)
    return " ".join(f'"{w}"' for w in words[:20])
