"""Tests for Cursor IDE converter and watcher."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from sessionfs.converters.cursor_to_sfs import (
    CursorParsedSession,
    parse_cursor_composer,
    convert_cursor_to_sfs,
    discover_cursor_composers,
)


@pytest.fixture
def cursor_global_db(tmp_path: Path) -> Path:
    """Create a fake Cursor global state.vscdb with test data."""
    db_path = tmp_path / "globalStorage" / "state.vscdb"
    db_path.parent.mkdir(parents=True)

    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE ItemTable (key TEXT UNIQUE ON CONFLICT REPLACE, value BLOB)")
    conn.execute("CREATE TABLE cursorDiskKV (key TEXT UNIQUE ON CONFLICT REPLACE, value BLOB)")

    composer_id = "test-comp-1234-abcd-efgh"

    # Insert bubbles (user=type1, assistant=type2)
    bubbles = [
        ("001", {
            "_v": 2, "type": 1, "bubbleId": "b001",
            "text": "Why is my API returning 500?",
            "checkpointId": "cp1",
        }),
        ("002", {
            "_v": 2, "type": 2, "bubbleId": "b002",
            "text": "I'll check the error logs. The issue is in the middleware.",
            "allThinkingBlocks": [{"text": "Let me analyze the stack trace"}],
        }),
        ("003", {
            "_v": 2, "type": 1, "bubbleId": "b003",
            "text": "Can you fix it?",
        }),
        ("004", {
            "_v": 2, "type": 2, "bubbleId": "b004",
            "text": "Done! The null check was missing in the auth middleware.",
        }),
    ]

    for bid, data in bubbles:
        key = f"bubbleId:{composer_id}:{bid}"
        conn.execute("INSERT INTO cursorDiskKV (key, value) VALUES (?, ?)",
                      (key, json.dumps(data)))

    conn.commit()
    conn.close()
    return db_path


@pytest.fixture
def cursor_workspace_db(tmp_path: Path, cursor_global_db: Path) -> Path:
    """Create a workspace state.vscdb with composer metadata."""
    ws_dir = tmp_path / "workspaceStorage" / "abc123"
    ws_dir.mkdir(parents=True)

    # workspace.json
    (ws_dir / "workspace.json").write_text(json.dumps({
        "folder": "file:///Users/test/myproject"
    }))

    # state.vscdb with composer metadata
    db_path = ws_dir / "state.vscdb"
    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE ItemTable (key TEXT UNIQUE ON CONFLICT REPLACE, value BLOB)")
    conn.execute("CREATE TABLE cursorDiskKV (key TEXT UNIQUE ON CONFLICT REPLACE, value BLOB)")

    composer_data = {
        "allComposers": [{
            "type": "head",
            "composerId": "test-comp-1234-abcd-efgh",
            "unifiedMode": "agent",
            "createdAt": 1772495485573,
            "lastUpdatedAt": 1772499085573,
            "name": "Fix API 500 error",
            "isArchived": False,
        }]
    }
    conn.execute("INSERT INTO ItemTable (key, value) VALUES (?, ?)",
                  ("composer.composerData", json.dumps(composer_data)))
    conn.commit()
    conn.close()
    return ws_dir


class TestParseCursorComposer:
    def test_basic_parse(self, cursor_global_db: Path):
        session = parse_cursor_composer("test-comp-1234-abcd-efgh", global_db=cursor_global_db)
        assert session.message_count == 4
        assert session.turn_count == 2

    def test_user_messages(self, cursor_global_db: Path):
        session = parse_cursor_composer("test-comp-1234-abcd-efgh", global_db=cursor_global_db)
        user_msgs = [m for m in session.messages if m["role"] == "user"]
        assert len(user_msgs) == 2
        assert "500" in user_msgs[0]["content"][0]["text"]

    def test_assistant_messages(self, cursor_global_db: Path):
        session = parse_cursor_composer("test-comp-1234-abcd-efgh", global_db=cursor_global_db)
        asst_msgs = [m for m in session.messages if m["role"] == "assistant"]
        assert len(asst_msgs) == 2

    def test_thinking_blocks(self, cursor_global_db: Path):
        session = parse_cursor_composer("test-comp-1234-abcd-efgh", global_db=cursor_global_db)
        asst_msgs = [m for m in session.messages if m["role"] == "assistant"]
        # First assistant message should have a thinking block
        first_asst = asst_msgs[0]
        thinking = [c for c in first_asst["content"] if c.get("type") == "thinking"]
        assert len(thinking) == 1
        assert "stack trace" in thinking[0]["text"]

    def test_missing_db(self, tmp_path: Path):
        session = parse_cursor_composer("nonexistent", global_db=tmp_path / "missing.vscdb")
        assert session.message_count == 0
        assert len(session.parse_errors) > 0


class TestDiscoverComposers:
    def test_discover(self, cursor_global_db: Path, cursor_workspace_db: Path, tmp_path: Path):
        composers = discover_cursor_composers(
            global_db=cursor_global_db,
            workspace_storage=tmp_path / "workspaceStorage",
        )
        assert len(composers) >= 1
        comp = composers[0]
        assert comp.composer_id == "test-comp-1234-abcd-efgh"
        assert comp.name == "Fix API 500 error"
        assert comp.workspace_folder == "/Users/test/myproject"

    def test_discover_no_db(self, tmp_path: Path):
        composers = discover_cursor_composers(global_db=tmp_path / "missing.vscdb")
        assert composers == []


class TestConvertCursorToSfs:
    def test_produces_valid_sfs(self, cursor_global_db: Path, tmp_path: Path):
        session = parse_cursor_composer("test-comp-1234-abcd-efgh", global_db=cursor_global_db)
        session.name = "Fix API error"
        sfs_dir = tmp_path / "output.sfs"
        convert_cursor_to_sfs(session, sfs_dir)

        assert (sfs_dir / "manifest.json").exists()
        assert (sfs_dir / "messages.jsonl").exists()

        manifest = json.loads((sfs_dir / "manifest.json").read_text())
        assert manifest["source"]["tool"] == "cursor"
        assert manifest["source"]["interface"] == "ide"
        assert manifest["stats"]["message_count"] == 4
        assert manifest["stats"]["turn_count"] == 2

    def test_title_from_name(self, cursor_global_db: Path, tmp_path: Path):
        session = parse_cursor_composer("test-comp-1234-abcd-efgh", global_db=cursor_global_db)
        session.name = "Fix API error"
        sfs_dir = tmp_path / "output.sfs"
        convert_cursor_to_sfs(session, sfs_dir)

        manifest = json.loads((sfs_dir / "manifest.json").read_text())
        assert manifest["title"] == "Fix API error"

    def test_title_from_messages(self, cursor_global_db: Path, tmp_path: Path):
        session = parse_cursor_composer("test-comp-1234-abcd-efgh", global_db=cursor_global_db)
        # No name — should extract from first user message
        sfs_dir = tmp_path / "output.sfs"
        convert_cursor_to_sfs(session, sfs_dir)

        manifest = json.loads((sfs_dir / "manifest.json").read_text())
        assert "500" in (manifest["title"] or "")


class TestSqliteSafety:
    def test_read_only_access(self, cursor_global_db: Path):
        """Verify we don't modify the source database."""
        import os
        mtime_before = os.path.getmtime(cursor_global_db)
        parse_cursor_composer("test-comp-1234-abcd-efgh", global_db=cursor_global_db)
        mtime_after = os.path.getmtime(cursor_global_db)
        assert mtime_before == mtime_after
