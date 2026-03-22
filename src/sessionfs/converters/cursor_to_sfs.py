"""Cursor IDE -> .sfs converter.

Reads conversations from Cursor's SQLite storage (state.vscdb) and converts
to the canonical .sfs format.

Cursor stores data in two layers:
- Bubble layer: bubbleId:{composerId}:{bubbleId} -> UI-level messages
- Agent KV layer: agentKv:blob:{hash} -> raw API messages

We read the bubble layer for simplicity and reliability, since it contains
the user-visible conversation with text content.
"""

from __future__ import annotations

import json
import logging
import shutil
import sqlite3
import tempfile
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

logger = logging.getLogger("sessionfs.converters.cursor_to_sfs")

# Platform-specific global storage path
_CURSOR_GLOBAL_STORAGE = {
    "darwin": Path.home() / "Library" / "Application Support" / "Cursor" / "User" / "globalStorage",
    "linux": Path.home() / ".config" / "Cursor" / "User" / "globalStorage",
}

_CURSOR_WORKSPACE_STORAGE = {
    "darwin": Path.home() / "Library" / "Application Support" / "Cursor" / "User" / "workspaceStorage",
    "linux": Path.home() / ".config" / "Cursor" / "User" / "workspaceStorage",
}


@dataclass
class CursorComposer:
    """Metadata about a Cursor composer (conversation)."""
    composer_id: str
    name: str = ""
    mode: str = "agent"
    created_at: int = 0  # Unix ms
    last_updated_at: int = 0
    is_archived: bool = False
    workspace_folder: str = ""


@dataclass
class CursorParsedSession:
    """Intermediate representation of a parsed Cursor session."""
    session_id: str
    name: str = ""
    workspace_folder: str = ""
    mode: str = "agent"
    created_at: str | None = None
    last_updated_at: str | None = None
    messages: list[dict[str, Any]] = field(default_factory=list)
    message_count: int = 0
    turn_count: int = 0
    parse_errors: list[str] = field(default_factory=list)


def _get_platform() -> str:
    import platform
    return platform.system().lower()


def _get_global_db_path() -> Path | None:
    plat = _get_platform()
    base = _CURSOR_GLOBAL_STORAGE.get(plat)
    if base and (base / "state.vscdb").exists():
        return base / "state.vscdb"
    return None


def _get_workspace_storage_path() -> Path | None:
    plat = _get_platform()
    return _CURSOR_WORKSPACE_STORAGE.get(plat)


def _safe_read_db(db_path: Path) -> sqlite3.Connection:
    """Open a SQLite DB safely by copying it first (avoids WAL locks)."""
    tmp = tempfile.mktemp(suffix=".vscdb")
    shutil.copy2(str(db_path), tmp)
    # Also copy WAL/SHM if they exist
    for ext in ("-wal", "-shm"):
        src = Path(str(db_path) + ext)
        if src.exists():
            shutil.copy2(str(src), tmp + ext)
    conn = sqlite3.connect(tmp)
    conn.row_factory = sqlite3.Row
    return conn


def discover_cursor_composers(
    global_db: Path | None = None,
    workspace_storage: Path | None = None,
) -> list[CursorComposer]:
    """Discover all Cursor composers (conversations) from the global DB."""
    global_db = global_db or _get_global_db_path()
    workspace_storage = workspace_storage or _get_workspace_storage_path()

    if not global_db or not global_db.exists():
        return []

    composers: list[CursorComposer] = []

    # Get composer IDs from bubble keys in global DB
    try:
        conn = _safe_read_db(global_db)
        rows = conn.execute(
            "SELECT DISTINCT key FROM cursorDiskKV WHERE key LIKE 'bubbleId:%'"
        ).fetchall()
        composer_ids: set[str] = set()
        for row in rows:
            # key format: bubbleId:{composerId}:{bubbleId}
            parts = row["key"].split(":")
            if len(parts) >= 3:
                composer_ids.add(parts[1])
        conn.close()
    except Exception as exc:
        logger.warning("Failed to read Cursor global DB: %s", exc)
        return []

    # Enrich with metadata from workspace DBs
    composer_meta: dict[str, dict] = {}
    if workspace_storage and workspace_storage.is_dir():
        for ws_dir in workspace_storage.iterdir():
            ws_db = ws_dir / "state.vscdb"
            ws_json = ws_dir / "workspace.json"
            if not ws_db.exists():
                continue

            folder = ""
            if ws_json.exists():
                try:
                    ws_data = json.loads(ws_json.read_text())
                    folder_uri = ws_data.get("folder", "")
                    if folder_uri.startswith("file:///"):
                        folder = unquote(folder_uri[7:])
                except (json.JSONDecodeError, OSError):
                    pass

            try:
                conn = _safe_read_db(ws_db)
                row = conn.execute(
                    "SELECT value FROM ItemTable WHERE key = 'composer.composerData'"
                ).fetchone()
                if row:
                    data = json.loads(row["value"])
                    for c in data.get("allComposers", []):
                        cid = c.get("composerId", "")
                        if cid in composer_ids:
                            composer_meta[cid] = {
                                "name": c.get("name", ""),
                                "mode": c.get("unifiedMode", "agent"),
                                "created_at": c.get("createdAt", 0),
                                "last_updated_at": c.get("lastUpdatedAt", 0),
                                "is_archived": c.get("isArchived", False),
                                "folder": folder,
                            }
                conn.close()
            except Exception:
                pass

    for cid in sorted(composer_ids):
        meta = composer_meta.get(cid, {})
        composers.append(CursorComposer(
            composer_id=cid,
            name=meta.get("name", ""),
            mode=meta.get("mode", "agent"),
            created_at=meta.get("created_at", 0),
            last_updated_at=meta.get("last_updated_at", 0),
            is_archived=meta.get("is_archived", False),
            workspace_folder=meta.get("folder", ""),
        ))

    return composers


def parse_cursor_composer(
    composer_id: str,
    global_db: Path | None = None,
) -> CursorParsedSession:
    """Parse a single Cursor composer's bubbles into a session."""
    global_db = global_db or _get_global_db_path()
    session = CursorParsedSession(session_id=composer_id)

    if not global_db or not global_db.exists():
        session.parse_errors.append("Global DB not found")
        return session

    try:
        conn = _safe_read_db(global_db)
        rows = conn.execute(
            "SELECT key, value FROM cursorDiskKV WHERE key LIKE ? ORDER BY rowid",
            (f"bubbleId:{composer_id}:%",),
        ).fetchall()
        conn.close()
    except Exception as exc:
        session.parse_errors.append(f"DB read error: {exc}")
        return session

    sfs_messages: list[dict[str, Any]] = []
    turn_count = 0
    prev_role = None

    for row in rows:
        try:
            bubble = json.loads(row["value"])
        except (json.JSONDecodeError, KeyError):
            continue

        bubble_type = bubble.get("type", 0)
        text = bubble.get("text", "")
        bubble_id = bubble.get("bubbleId", f"msg_{len(sfs_messages):04d}")

        if bubble_type == 1:
            # User message
            if not text:
                continue
            if prev_role != "user":
                turn_count += 1
            sfs_messages.append({
                "msg_id": bubble_id,
                "role": "user",
                "content": [{"type": "text", "text": text}],
            })
            prev_role = "user"

        elif bubble_type == 2:
            # Assistant message
            if not text:
                continue

            content: list[dict[str, Any]] = []

            # Add thinking blocks if present
            for tb in bubble.get("allThinkingBlocks", []):
                if isinstance(tb, dict) and tb.get("text"):
                    content.append({"type": "thinking", "text": tb["text"]})
                elif isinstance(tb, str) and tb:
                    content.append({"type": "thinking", "text": tb})

            content.append({"type": "text", "text": text})

            sfs_messages.append({
                "msg_id": bubble_id,
                "role": "assistant",
                "content": content,
            })
            prev_role = "assistant"

    session.messages = sfs_messages
    session.message_count = len(sfs_messages)
    session.turn_count = turn_count
    return session


def convert_cursor_to_sfs(
    cursor_session: CursorParsedSession,
    session_dir: Path,
    session_id: str | None = None,
) -> Path:
    """Convert a parsed Cursor session to .sfs format."""
    from sessionfs.session_id import session_id_from_native
    from sessionfs.utils.title_utils import extract_smart_title

    sid = session_id or session_id_from_native(cursor_session.session_id)
    session_dir.mkdir(parents=True, exist_ok=True)

    messages = cursor_session.messages
    now_iso = datetime.now(timezone.utc).isoformat()
    created_at = cursor_session.created_at or now_iso
    updated_at = cursor_session.last_updated_at or now_iso

    title = extract_smart_title(
        messages=messages or None,
        raw_title=cursor_session.name or None,
        message_count=cursor_session.message_count,
    )
    if title.startswith("Untitled session"):
        title = None

    manifest = {
        "sfs_version": "0.1.0",
        "session_id": sid,
        "title": title,
        "tags": [],
        "created_at": created_at,
        "updated_at": updated_at,
        "source": {
            "tool": "cursor",
            "tool_version": None,
            "sfs_converter_version": "0.1.0",
            "original_session_id": cursor_session.session_id,
            "interface": "ide",
        },
        "stats": {
            "message_count": cursor_session.message_count,
            "turn_count": cursor_session.turn_count,
            "tool_use_count": 0,
            "total_input_tokens": 0,
            "total_output_tokens": 0,
        },
    }

    (session_dir / "manifest.json").write_text(json.dumps(manifest, indent=2))

    with open(session_dir / "messages.jsonl", "w") as f:
        for msg in messages:
            f.write(json.dumps(msg, separators=(",", ":")) + "\n")

    if cursor_session.workspace_folder:
        workspace = {"root_path": cursor_session.workspace_folder, "git": {}}
        (session_dir / "workspace.json").write_text(json.dumps(workspace, indent=2))

    return session_dir
