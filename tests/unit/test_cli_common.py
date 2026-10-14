"""Tests for CLI common utilities."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from sessionfs.cli import common
from sessionfs.cli.common import read_sfs_messages, resolve_session_id
from sessionfs.store.local import LocalStore


@pytest.fixture
def store_with_sessions(tmp_path: Path) -> LocalStore:
    """Create a store with some test sessions indexed."""
    store = LocalStore(tmp_path / "store")
    store.initialize()

    for i, sid in enumerate([
        "ses_aaaa11110000ab",
        "ses_aaaa11110000cd",
        "ses_bbbb22220000ab",
    ]):
        session_dir = store.allocate_session_dir(sid)
        manifest = {
            "sfs_version": "0.1.0",
            "session_id": sid,
            "title": f"Session {i}",
            "created_at": f"2026-03-20T10:00:0{i}Z",
            "source": {"tool": "claude-code"},
            "stats": {"message_count": 1},
        }
        (session_dir / "manifest.json").write_text(json.dumps(manifest))
        store.upsert_session_metadata(sid, manifest, str(session_dir))

    return store


def test_resolve_exact_match(store_with_sessions: LocalStore):
    result = resolve_session_id(
        store_with_sessions, "ses_bbbb22220000ab"
    )
    assert result == "ses_bbbb22220000ab"


def test_resolve_prefix_unique(store_with_sessions: LocalStore):
    result = resolve_session_id(store_with_sessions, "ses_bbbb")
    assert result == "ses_bbbb22220000ab"


def test_resolve_prefix_ambiguous(store_with_sessions: LocalStore):
    with pytest.raises(SystemExit):
        resolve_session_id(store_with_sessions, "ses_aaaa")


def test_resolve_not_found(store_with_sessions: LocalStore):
    with pytest.raises(SystemExit):
        resolve_session_id(store_with_sessions, "ses_cccc")


def test_resolve_too_short(store_with_sessions: LocalStore):
    with pytest.raises(SystemExit):
        resolve_session_id(store_with_sessions, "aa")


def test_read_sfs_messages(tmp_path: Path):
    session_dir = tmp_path / "session.sfs"
    session_dir.mkdir()

    messages = [
        {"msg_id": "1", "role": "user", "content": [{"type": "text", "text": "Hi"}]},
        {"msg_id": "2", "role": "assistant", "content": [{"type": "text", "text": "Hello"}]},
    ]
    with open(session_dir / "messages.jsonl", "w") as f:
        for msg in messages:
            f.write(json.dumps(msg) + "\n")

    result = read_sfs_messages(session_dir)
    assert len(result) == 2
    assert result[0]["msg_id"] == "1"


def test_read_sfs_messages_missing(tmp_path: Path):
    result = read_sfs_messages(tmp_path / "nonexistent")
    assert result == []


def test_read_sfs_messages_skips_malformed_line_with_warning(tmp_path: Path):
    """GH #65 — a line left unbalanced by the raw-text DLP redaction path
    (the backslash escaping the closing quote was swallowed) must be skipped
    with a warning, not crash resume/display."""
    session_dir = tmp_path / "session.sfs"
    session_dir.mkdir()

    valid = [
        {"msg_id": "1", "role": "user", "content": [{"type": "text", "text": "Hi"}]},
        {"msg_id": "3", "role": "assistant", "content": [{"type": "text", "text": "Bye"}]},
    ]
    # Corrupted serialization: the `\\"` ends the value string, then a bare
    # `"` dangles — invalid JSON, but must not raise.
    corrupt = '{"msg_id": "2", "content": [{"type": "text", "text": "database_url = \\"postgres://u:p@h/db""}]}'

    with open(session_dir / "messages.jsonl", "w") as f:
        f.write(json.dumps(valid[0]) + "\n")
        f.write(corrupt + "\n")
        f.write(json.dumps(valid[1]) + "\n")

    captured: list[str] = []

    def capture(*args, **_kwargs):
        captured.append(str(args[0]) if args else "")

    with patch.object(common.err_console, "print", side_effect=capture):
        result = read_sfs_messages(session_dir, session_id="ses_test123")

    assert [m["msg_id"] for m in result] == ["1", "3"]
    assert captured == [
        "[yellow]Skipped 1 unreadable message line in ses_test123[/yellow]"
    ]
