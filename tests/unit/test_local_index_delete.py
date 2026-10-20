"""Every local-index delete path must work when the session has a tracked
native row: tracked_sessions references sessions and the index enforces
foreign keys, so the native ref has to be removed first."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from sessionfs.store.index import delete_session_rows
from sessionfs.watchers.base import NativeSessionRef

SID = "ses_0123456789abcdef"


def _seed(store) -> Path:
    session_dir = store.allocate_session_dir(SID)
    manifest = {
        "sfs_version": "0.1.0", "session_id": SID, "title": "t",
        "created_at": "2026-10-18T00:00:00Z", "source": {"tool": "codex"},
    }
    (session_dir / "manifest.json").write_text(json.dumps(manifest))
    (session_dir / "messages.jsonl").write_text("")
    store.upsert_session_metadata(SID, manifest, str(session_dir))
    store.upsert_tracked_session(NativeSessionRef(
        tool="codex", native_session_id="native-1", native_path="/x", sfs_session_id=SID,
    ))
    assert store.index.conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    return session_dir


def _gone(store) -> bool:
    conn = store.index.conn
    sessions = conn.execute("SELECT COUNT(*) FROM sessions WHERE session_id = ?", (SID,)).fetchone()[0]
    tracked = conn.execute("SELECT COUNT(*) FROM tracked_sessions WHERE sfs_session_id = ?", (SID,)).fetchone()[0]
    return sessions == 0 and tracked == 0


@pytest.fixture
def store():
    from sessionfs.cli.common import open_store

    s = open_store()
    yield s
    s.close()


def test_naive_order_is_what_failed(store):
    _seed(store)
    import sqlite3

    with pytest.raises(sqlite3.IntegrityError):
        store.index.conn.execute("DELETE FROM sessions WHERE session_id = ?", (SID,))
    store.index.conn.rollback()


def test_helper_deletes_tracked_rows_first(store):
    _seed(store)
    delete_session_rows(store.index.conn, SID)
    assert _gone(store)


@pytest.mark.parametrize("scope_flag", ["--local", "--everywhere"])
def test_sfs_delete_removes_index_rows(store, monkeypatch, scope_flag):
    from typer.testing import CliRunner

    from sessionfs.cli import cmd_delete
    from sessionfs.cli.main import app

    session_dir = _seed(store)
    store.close()

    class _Resp:
        status_code = 200

        @staticmethod
        def json():
            return {"purge_after": "later"}

    import httpx

    monkeypatch.setattr(cmd_delete, "_load_sync_config", lambda: {"api_url": "http://127.0.0.1:9", "api_key": "k"})
    monkeypatch.setattr(httpx, "delete", lambda *a, **k: _Resp())
    result = CliRunner().invoke(app, ["delete", SID, scope_flag, "--force"])
    assert result.exit_code == 0, result.output
    assert "Could not update the local index" not in result.output

    from sessionfs.cli.common import open_store

    s = open_store()
    try:
        assert _gone(s)
        assert not session_dir.exists()
    finally:
        s.close()


def test_server_410_cleanup_removes_index_rows(store, caplog):
    from sessionfs.sync.deleted_cleanup import cleanup_deleted_session

    session_dir = _seed(store)
    cleanup_deleted_session(SID, session_dir=session_dir, store=store, base_dir=store.store_dir)
    assert _gone(store)
    assert "Failed to clean index" not in caplog.text


def test_prune_removes_index_rows(store):
    from sessionfs.store.pruner import SessionPruner

    _seed(store)
    SessionPruner(store.sessions_dir, store.index.conn)._delete_session(SID)
    assert _gone(store)
    assert store.get_session_dir(SID) is None
