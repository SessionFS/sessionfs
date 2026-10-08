"""Tests for the local session store."""

from __future__ import annotations

from pathlib import Path

from sessionfs.store.local import LocalStore


def test_initialize_creates_dirs(tmp_path: Path):
    """initialize() creates store and sessions directories."""
    store_dir = tmp_path / ".sessionfs"
    store = LocalStore(store_dir)
    store.initialize()

    assert store_dir.is_dir()
    assert (store_dir / "sessions").is_dir()
    assert (store_dir / "index.db").exists()
    store.close()


def test_allocate_session_dir(tmp_path: Path):
    """allocate_session_dir creates a .sfs directory."""
    store = LocalStore(tmp_path)
    store.initialize()

    session_dir = store.allocate_session_dir("test-session-123")
    assert session_dir.is_dir()
    assert session_dir.name == "test-session-123.sfs"
    store.close()


def test_get_session_dir_exists(tmp_path: Path):
    """get_session_dir returns the directory if it exists."""
    store = LocalStore(tmp_path)
    store.initialize()

    store.allocate_session_dir("abc")
    assert store.get_session_dir("abc") is not None
    store.close()


def test_get_session_dir_missing(tmp_path: Path):
    """get_session_dir returns None for missing sessions."""
    store = LocalStore(tmp_path)
    store.initialize()

    assert store.get_session_dir("nonexistent") is None
    store.close()


def test_session_manifest_read(tmp_path: Path):
    """get_session_manifest reads manifest.json from session dir."""
    store = LocalStore(tmp_path)
    store.initialize()

    session_dir = store.allocate_session_dir("test-manifest")
    import json
    (session_dir / "manifest.json").write_text(json.dumps({"title": "Test"}))

    manifest = store.get_session_manifest("test-manifest")
    assert manifest is not None
    assert manifest["title"] == "Test"
    store.close()


# ---------------------------------------------------------------------------
# Sync state survives a session rewrite (re-capture / re-import)
# ---------------------------------------------------------------------------

import json  # noqa: E402
import stat as _stat  # noqa: E402


def _write_session(store: LocalStore, session_id: str, manifest: dict) -> Path:
    """Mimic a watcher capture: allocate, write a fresh manifest, upsert."""
    session_dir = store.allocate_session_dir(session_id)
    (session_dir / "manifest.json").write_text(json.dumps(manifest))
    store.upsert_session_metadata(session_id, manifest, str(session_dir))
    return session_dir


_BASE_MANIFEST = {
    "sfs_version": "0.1.0",
    "session_id": "ses_aaaa1111bbbb2222",
    "title": "t",
    "created_at": "2026-10-08T00:00:00Z",
    "source": {"tool": "claude-code"},
}


def test_recapture_keeps_etag_and_marks_dirty(tmp_path: Path):
    store = LocalStore(tmp_path)
    store.initialize()
    sid = "ses_aaaa1111bbbb2222"
    synced = {**_BASE_MANIFEST, "sync": {"etag": "abc123", "last_sync_at": "x", "dirty": False}}
    _write_session(store, sid, synced)

    # The capture pipeline writes a brand-new manifest with no sync block. A
    # manifest-only change (e.g. a renamed chat) still counts as a change.
    session_dir = _write_session(store, sid, {**_BASE_MANIFEST, "title": "renamed"})

    on_disk = json.loads((session_dir / "manifest.json").read_text())
    assert on_disk["sync"]["etag"] == "abc123"
    assert on_disk["sync"]["dirty"] is True
    mode = _stat.S_IMODE((session_dir / "manifest.json").stat().st_mode)
    assert mode == 0o600
    store.close()


def test_first_capture_adds_no_sync_block(tmp_path: Path):
    store = LocalStore(tmp_path)
    store.initialize()
    session_dir = _write_session(store, "ses_aaaa1111bbbb2222", dict(_BASE_MANIFEST))
    assert "sync" not in json.loads((session_dir / "manifest.json").read_text())
    store.close()


def test_rewrite_that_brings_its_own_sync_block_is_left_alone(tmp_path: Path):
    store = LocalStore(tmp_path)
    store.initialize()
    sid = "ses_aaaa1111bbbb2222"
    _write_session(store, sid, {**_BASE_MANIFEST, "sync": {"etag": "old", "dirty": False}})
    session_dir = _write_session(
        store, sid, {**_BASE_MANIFEST, "sync": {"etag": "new", "dirty": False}}
    )
    on_disk = json.loads((session_dir / "manifest.json").read_text())
    assert on_disk["sync"] == {"etag": "new", "dirty": False}
    store.close()


def test_pull_still_ends_clean_with_the_server_etag(tmp_path: Path):
    """cloud pull: allocate → unpack → upsert → write the fresh sync state."""
    from sessionfs.cli.cmd_cloud import _update_manifest_sync

    store = LocalStore(tmp_path)
    store.initialize()
    sid = "ses_aaaa1111bbbb2222"
    _write_session(store, sid, {**_BASE_MANIFEST, "sync": {"etag": "old", "dirty": False}})
    session_dir = _write_session(store, sid, dict(_BASE_MANIFEST))  # unpacked archive
    _update_manifest_sync(session_dir, "server-etag")

    sync = json.loads((session_dir / "manifest.json").read_text())["sync"]
    assert sync["etag"] == "server-etag"
    assert sync["dirty"] is False
    store.close()


def test_write_listener_fires_for_writes_only(tmp_path: Path):
    store = LocalStore(tmp_path)
    store.initialize()
    seen: list[str] = []
    store.add_write_listener(seen.append)

    session_dir = _write_session(store, "ses_aaaa1111bbbb2222", dict(_BASE_MANIFEST))
    assert seen == ["ses_aaaa1111bbbb2222"]

    # A metadata-only upsert (index rebuild) is not a new write.
    store.upsert_session_metadata("ses_aaaa1111bbbb2222", dict(_BASE_MANIFEST), str(session_dir))
    assert seen == ["ses_aaaa1111bbbb2222"]
    store.close()


def test_failing_write_listener_does_not_break_the_write(tmp_path: Path):
    store = LocalStore(tmp_path)
    store.initialize()

    def boom(session_id: str) -> None:
        raise RuntimeError("listener failed")

    store.add_write_listener(boom)
    _write_session(store, "ses_aaaa1111bbbb2222", dict(_BASE_MANIFEST))
    assert store.get_session_metadata("ses_aaaa1111bbbb2222") is not None
    store.close()


def _write_content(session_dir: Path, text: str) -> None:
    (session_dir / "messages.jsonl").write_text(json.dumps({"role": "user", "content": text}) + "\n")


def _capture(store: LocalStore, sid: str, text: str, manifest: dict | None = None) -> Path:
    session_dir = store.allocate_session_dir(sid)
    _write_content(session_dir, text)
    m = manifest if manifest is not None else dict(_BASE_MANIFEST)
    (session_dir / "manifest.json").write_text(json.dumps(m))
    store.upsert_session_metadata(sid, m, str(session_dir))
    return session_dir


def test_unchanged_rewrite_stays_clean_and_quiet(tmp_path: Path):
    """Cursor re-captures every composer when its shared DB changes."""
    store = LocalStore(tmp_path)
    store.initialize()
    sid = "ses_aaaa1111bbbb2222"
    _capture(store, sid, "hello", {**_BASE_MANIFEST, "sync": {"etag": "e1", "dirty": False}})
    seen: list[str] = []
    store.add_write_listener(seen.append)

    session_dir = _capture(store, sid, "hello")  # same content, fresh manifest

    assert json.loads((session_dir / "manifest.json").read_text())["sync"] == {
        "etag": "e1", "dirty": False,
    }
    assert seen == []
    store.close()


def test_changed_rewrite_notifies(tmp_path: Path):
    store = LocalStore(tmp_path)
    store.initialize()
    sid = "ses_aaaa1111bbbb2222"
    _capture(store, sid, "hello", {**_BASE_MANIFEST, "sync": {"etag": "e1", "dirty": False}})
    seen: list[str] = []
    store.add_write_listener(seen.append)

    session_dir = _capture(store, sid, "hello again")

    sync = json.loads((session_dir / "manifest.json").read_text())["sync"]
    assert sync == {"etag": "e1", "dirty": True}
    assert seen == [sid]
    store.close()


def test_retry_after_failed_capture_keeps_original_etag(tmp_path: Path):
    store = LocalStore(tmp_path)
    store.initialize()
    sid = "ses_aaaa1111bbbb2222"
    _capture(store, sid, "hello", {**_BASE_MANIFEST, "sync": {"etag": "e1", "dirty": False}})

    # First attempt writes a fresh manifest, then fails before upsert.
    session_dir = store.allocate_session_dir(sid)
    (session_dir / "manifest.json").write_text(json.dumps(_BASE_MANIFEST))

    # The retry succeeds.
    session_dir = _capture(store, sid, "hello again")
    assert json.loads((session_dir / "manifest.json").read_text())["sync"]["etag"] == "e1"
    store.close()


def test_index_rebuild_does_not_notify(tmp_path: Path):
    store = LocalStore(tmp_path)
    store.initialize()
    _capture(store, "ses_aaaa1111bbbb2222", "hello")
    seen: list[str] = []
    store.add_write_listener(seen.append)
    store._rebuild_index_from_disk()
    assert seen == []
    store.close()


def test_restore_failure_keeps_stash_for_the_retry(tmp_path: Path, monkeypatch):
    import sessionfs.store.local as local_mod

    store = LocalStore(tmp_path)
    store.initialize()
    sid = "ses_aaaa1111bbbb2222"
    _capture(store, sid, "hello", {**_BASE_MANIFEST, "sync": {"etag": "e1", "dirty": False}})

    real = local_mod._write_json_atomic

    def disk_full(path, data):
        raise OSError("No space left on device")

    monkeypatch.setattr(local_mod, "_write_json_atomic", disk_full)
    try:
        _capture(store, sid, "hello again")
    except OSError:
        pass
    monkeypatch.setattr(local_mod, "_write_json_atomic", real)

    session_dir = _capture(store, sid, "hello again")
    sync = json.loads((session_dir / "manifest.json").read_text())["sync"]
    assert sync == {"etag": "e1", "dirty": True}
    store.close()
