"""Regression tests for the crash-safe, race-free local session store.

Covers the store-level behaviour of the crash-safe commit redesign and the
shared durability helper (the helper itself is tested in
``test_store_fsutil.py``):

- a delete racing a capture no longer resurrects the session
  (``commit_session`` rechecks the tombstone at commit time);
- a sync ack only clears ``dirty`` when the content still matches what was
  packed (a capture landing mid-upload keeps the session dirty);
- the commit step never swaps the session directory away: files the writer
  did not produce (``audit_report.json``, ``checkpoints/``) survive a
  re-capture, the final directory still exists after every per-file move, and
  the move path never copies tree entries;
- stale ``.staging.``/``.old.`` leftovers are recovered at startup, and a
  stranded ``.old.`` is renamed back into place;
- ``pack_and_hash`` records the content hash before packing under one lock;
- a symlink planted at ``{id}.sfs`` is refused, and its target is untouched.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import threading
import time
from pathlib import Path

import pytest

from sessionfs.store.deleted import mark_deleted
from sessionfs.store.index import delete_session_rows
from sessionfs.store.local import (
    LocalStore,
    WriterResult,
    apply_sync_ack,
    mark_session_synced,
    record_packed_content_hash,
)
from sessionfs.watchers.base import NativeSessionRef

SID = "ses_aaaa1111bbbb2222"
SID2 = "ses_bbbb2222cccc3333"
SID3 = "ses_cccc3333dddd4444"

_BASE_MANIFEST = {
    "sfs_version": "0.1.0",
    "session_id": SID,
    "title": "t",
    "created_at": "2026-10-08T00:00:00Z",
    "source": {"tool": "claude-code"},
}


def _write_capture(staging: Path, text: str) -> dict:
    """Fill a staging dir the way a converter would, returning the manifest."""
    (staging / "manifest.json").write_text(json.dumps(_BASE_MANIFEST))
    (staging / "messages.jsonl").write_text(
        json.dumps({"role": "user", "content": text}) + "\n"
    )
    return _BASE_MANIFEST


def _capture(store: LocalStore, text: str) -> Path:
    outcome = store.commit_session(SID, lambda s: _write_capture(s, text))
    assert not outcome.skipped
    return outcome.session_dir


# ---------------------------------------------------------------------------
# Fix 1: a delete racing a capture must not resurrect the session
# ---------------------------------------------------------------------------


def test_commit_session_skips_when_deleted_between_check_and_commit(tmp_path: Path):
    store = LocalStore(tmp_path)
    store.initialize()

    def _write(staging: Path):
        # A delete lands after the initial exclusion check, before commit.
        mark_deleted(SID, "local", base_dir=store.store_dir)
        return _write_capture(staging, "hello")

    outcome = store.commit_session(SID, _write)

    assert outcome.skipped is True
    assert outcome.session_dir is None
    # No resurrection: the directory was never written into place.
    assert not (store.sessions_dir / f"{SID}.sfs").exists()
    assert store.index.session_count() == 0
    store.close()


def test_commit_session_writes_when_not_deleted(tmp_path: Path):
    store = LocalStore(tmp_path)
    store.initialize()

    outcome = store.commit_session(SID, lambda s: _write_capture(s, "hello"))

    assert outcome.skipped is False
    assert (store.sessions_dir / f"{SID}.sfs").is_dir()
    assert store.index.session_count() == 1
    store.close()


# ---------------------------------------------------------------------------
# Fix 2: a sync ack must not clear a session that changed mid-upload
# ---------------------------------------------------------------------------


def test_ack_keeps_dirty_when_content_changed_during_upload(tmp_path: Path):
    store = LocalStore(tmp_path)
    store.initialize()
    session_dir = _capture(store, "hello")

    packed = record_packed_content_hash(session_dir)
    assert packed is not None

    # A capture lands while the upload is in flight.
    _capture(store, "hello again")

    # The server acks the (stale) upload.
    apply_sync_ack(session_dir, "new-etag")

    sync = json.loads((session_dir / "manifest.json").read_text())["sync"]
    assert sync["etag"] == "new-etag"
    assert sync["dirty"] is True  # content moved on, so it stays dirty
    store.close()


def test_ack_clears_dirty_when_content_unchanged(tmp_path: Path):
    store = LocalStore(tmp_path)
    store.initialize()
    session_dir = _capture(store, "hello")

    record_packed_content_hash(session_dir)
    apply_sync_ack(session_dir, "new-etag")

    sync = json.loads((session_dir / "manifest.json").read_text())["sync"]
    assert sync["etag"] == "new-etag"
    assert sync["dirty"] is False
    store.close()


# ---------------------------------------------------------------------------
# Index delete paths roll back atomically on failure (no open transaction)
# ---------------------------------------------------------------------------


def test_delete_rows_rolls_back_on_failure(tmp_path: Path):
    store = LocalStore(tmp_path)
    store.initialize()
    _capture(store, "hello")
    store.upsert_tracked_session(
        NativeSessionRef(
            tool="claude-code",
            native_session_id="native-1",
            native_path="/x",
            sfs_session_id=SID,
        )
    )
    conn = store.index.conn
    assert conn.execute(
        "SELECT COUNT(*) FROM tracked_sessions WHERE sfs_session_id = ?", (SID,)
    ).fetchone()[0] == 1

    # A trigger makes the sessions DELETE fail after the tracked DELETE ran,
    # so the transaction has to roll both back.
    conn.execute(
        "CREATE TRIGGER fail_session_delete BEFORE DELETE ON sessions "
        "BEGIN SELECT RAISE(ABORT, 'boom'); END;"
    )
    try:
        with pytest.raises(sqlite3.DatabaseError):
            delete_session_rows(conn, SID)
    finally:
        conn.execute("DROP TRIGGER fail_session_delete")

    # Both deletes were rolled back and no transaction was left open.
    assert conn.execute(
        "SELECT COUNT(*) FROM tracked_sessions WHERE sfs_session_id = ?", (SID,)
    ).fetchone()[0] == 1
    assert conn.execute(
        "SELECT COUNT(*) FROM sessions WHERE session_id = ?", (SID,)
    ).fetchone()[0] == 1
    assert conn.in_transaction is False
    store.close()


# ---------------------------------------------------------------------------
# A symlink planted at {id}.sfs is refused, target untouched
# ---------------------------------------------------------------------------


def test_commit_session_refuses_symlinked_dir(tmp_path: Path):
    store = LocalStore(tmp_path)
    store.initialize()

    victim = tmp_path / "victim"
    victim.mkdir()
    (victim / "precious.txt").write_text("keep")

    final = store.sessions_dir / f"{SID}.sfs"
    final.symlink_to(victim, target_is_directory=True)

    with pytest.raises(OSError):
        store.commit_session(SID, lambda s: _write_capture(s, "hello"))

    assert (victim / "precious.txt").read_text() == "keep"
    assert final.is_symlink()
    assert store.index.session_count() == 0
    store.close()


def test_allocate_session_dir_refuses_symlink(tmp_path: Path):
    store = LocalStore(tmp_path)
    store.initialize()

    victim = tmp_path / "victim"
    victim.mkdir()
    (victim / "precious.txt").write_text("keep")

    final = store.sessions_dir / f"{SID}.sfs"
    final.symlink_to(victim, target_is_directory=True)

    with pytest.raises(OSError):
        store.allocate_session_dir(SID)

    assert (victim / "precious.txt").read_text() == "keep"
    assert final.is_symlink()
    store.close()


# ---------------------------------------------------------------------------
# A crash mid-commit leaves neither a partial dir nor a stale backup
# ---------------------------------------------------------------------------


def test_commit_swap_leaves_no_partial_dir_on_failure(tmp_path: Path, monkeypatch):
    store = LocalStore(tmp_path)
    store.initialize()
    session_dir = _capture(store, "hello")

    import sessionfs.store.local as local_mod

    def boom(final_dir: Path, staging: Path, *, mode: str = "merge", retire=None) -> None:
        raise OSError("rename failed")

    monkeypatch.setattr(local_mod, "_commit_staging", boom)

    with pytest.raises(OSError):
        store.commit_session(SID, lambda s: _write_capture(s, "hello again"))

    # The original directory is untouched and no .old. backup lingers.
    assert (session_dir / "messages.jsonl").is_file()
    backups = [p for p in store.sessions_dir.iterdir() if ".old." in p.name]
    assert backups == []
    store.close()


# ---------------------------------------------------------------------------
# HIGH: files the writer did not produce survive a re-capture untouched
# ---------------------------------------------------------------------------


def test_recapture_leaves_audit_report_and_checkpoints(tmp_path: Path):
    store = LocalStore(tmp_path)
    store.initialize()
    session_dir = _capture(store, "v1")

    # audit_report.json and checkpoints/ are written by other paths (audit,
    # checkpoint), never by the converter's writer_fn — a re-capture must leave
    # them exactly as they are.
    (session_dir / "audit_report.json").write_text("report-bytes")
    checkpoint = session_dir / "checkpoints" / "cp1"
    checkpoint.mkdir(parents=True)
    (checkpoint / "manifest.json").write_text("{}")

    _capture(store, "v2")

    assert (session_dir / "audit_report.json").read_text() == "report-bytes"
    assert (checkpoint / "manifest.json").read_text() == "{}"
    # The re-captured content landed on top of the converter-produced files.
    assert json.loads((session_dir / "messages.jsonl").read_text())["content"] == "v2"
    store.close()


def test_recapture_does_not_copy_checkpoint_tree(tmp_path: Path, monkeypatch):
    store = LocalStore(tmp_path)
    store.initialize()
    session_dir = _capture(store, "v1")

    checkpoint = session_dir / "checkpoints" / "cp1"
    checkpoint.mkdir(parents=True)
    (checkpoint / "manifest.json").write_text("{}")

    # The commit path moves only what the writer produced with os.replace; it
    # must never copytree/copy2 the pre-existing checkpoints/ subtree. Patch
    # both to fail so any attempt is caught.
    def _no_copy(*_a, **_k):
        raise AssertionError("commit must not copy tree entries")

    monkeypatch.setattr(shutil, "copytree", _no_copy)
    monkeypatch.setattr(shutil, "copy2", _no_copy)

    _capture(store, "v2")

    assert (checkpoint / "manifest.json").read_text() == "{}"
    store.close()


def test_final_dir_exists_after_every_replace_failure(tmp_path: Path, monkeypatch):
    """The session dir is never swapped away: it exists after each move fails.

    The old directory-swap design renamed ``{id}.sfs`` aside before moving the
    staging dir in, so a failure part-way left the session missing or stranded
    under a ``.old.`` name. The new commit moves files one at a time with
    ``os.replace`` and never touches the final directory itself, so it must
    still be there (and keep its untouched files) after a failure at any move.
    """
    import sessionfs.store.local as local_mod

    produced = ["messages.jsonl", "workspace.json", "tools.json", "manifest.json"]
    real_replace = local_mod.os.replace  # the true original, captured once

    for fail_after in range(len(produced)):
        store = LocalStore(tmp_path / f"run-{fail_after}")
        store.initialize()
        session_dir = _capture(store, "v1")
        sentinel = session_dir / "audit_report.json"
        sentinel.write_text("keep me")

        def _write(staging: Path, _files=produced):
            for name in _files:
                (staging / name).write_text("new-" + name)
            return None  # no metadata upsert → no other os.replace in play

        moves = {"n": 0}

        def _failing_replace(src, dst, _moves=moves, _fail=fail_after):
            if _moves["n"] == _fail:
                raise OSError("injected replace failure")
            _moves["n"] += 1
            return real_replace(src, dst)

        # Scope the patch to just the commit under test so the initial capture
        # of the next iteration still uses the real os.replace.
        with monkeypatch.context() as m:
            m.setattr(local_mod.os, "replace", _failing_replace)
            with pytest.raises(OSError):
                store.commit_session(SID, _write)

        # The final dir was never removed or renamed aside.
        assert session_dir.is_dir()
        assert sentinel.read_text() == "keep me"
        # Only the session dir remains: no staging dir, no .old. backup.
        assert [p for p in store.sessions_dir.iterdir() if p != session_dir] == []
        store.close()


# ---------------------------------------------------------------------------
# HIGH: pull must replace, not merge
# ---------------------------------------------------------------------------


def _remote_archive(tmp_path: Path) -> tuple[bytes, dict]:
    """Build a tar.gz archive that is authoritative for a pull."""
    from sessionfs.sync.archive import pack_session

    remote = tmp_path / "remote"
    remote.mkdir()
    remote_manifest = {**_BASE_MANIFEST, "title": "remote"}
    (remote / "manifest.json").write_text(json.dumps(remote_manifest))
    (remote / "messages.jsonl").write_text(
        json.dumps({"role": "user", "content": "remote"}) + "\n"
    )
    (remote / "checkpoints" / "cp1").mkdir(parents=True)
    (remote / "checkpoints" / "cp1" / "manifest.json").write_text('{"remote": true}')
    return pack_session(remote), remote_manifest


def test_replace_commit_pull_over_checkpoints_succeeds(tmp_path: Path):
    """A pull over an existing session with checkpoints/ replaces, never merges.

    POSIX rename can't replace a non-empty directory, so the old merge commit
    raised ``Directory not empty`` after some files had already moved. Replace
    mode moves the live entries aside first, so the pull succeeds and the live
    dir ends up exactly equal to the archive — local-only files removed,
    checkpoints/ from the archive present, the remote manifest indexed.
    """
    from sessionfs.sync.archive import unpack_session

    store = LocalStore(tmp_path)
    store.initialize()
    live = _capture(store, "local")
    (live / "checkpoints" / "cp1").mkdir(parents=True)
    (live / "checkpoints" / "cp1" / "manifest.json").write_text('{"local": true}')
    (live / "local_only.txt").write_text("local-only bytes")

    archive, remote_manifest = _remote_archive(tmp_path)

    def _writer(staging: Path):
        unpack_session(archive, staging)
        return json.loads((staging / "manifest.json").read_text())

    outcome = store.commit_session(SID, _writer, skip_excluded=False, mode="replace")

    assert outcome.skipped is False
    assert outcome.session_dir == live
    # The archive is authoritative: checkpoints/ from the archive replaced the
    # local one, the local-only file is gone, and the remote manifest landed.
    assert (live / "checkpoints" / "cp1" / "manifest.json").read_text() == '{"remote": true}'
    assert not (live / "local_only.txt").exists()
    assert json.loads((live / "messages.jsonl").read_text())["content"] == "remote"
    assert json.loads((live / "manifest.json").read_text()) == remote_manifest
    # The index was updated to the remote manifest.
    indexed = store.index.get_session(SID)
    assert indexed is not None and indexed.get("title") == "remote"
    store.close()


def test_merge_mode_dir_collision_succeeds(tmp_path: Path):
    """A staged directory colliding with a live non-empty dir moves it aside.

    The old merge commit os.replace'd the staged dir over the live one, which
    POSIX refuses for a non-empty target. Merge mode now moves the live dir
    aside first, so the staged directory lands in its place.
    """
    store = LocalStore(tmp_path)
    store.initialize()
    live = _capture(store, "v1")
    (live / "checkpoints" / "cp1").mkdir(parents=True)
    (live / "checkpoints" / "cp1" / "manifest.json").write_text("old")

    def _writer(staging: Path):
        (staging / "manifest.json").write_text(json.dumps(_BASE_MANIFEST))
        (staging / "messages.jsonl").write_text(
            json.dumps({"role": "user", "content": "v2"}) + "\n"
        )
        (staging / "checkpoints" / "cp1").mkdir(parents=True)
        (staging / "checkpoints" / "cp1" / "manifest.json").write_text("new")
        return _BASE_MANIFEST

    outcome = store.commit_session(SID, _writer)

    assert outcome.skipped is False
    assert (live / "checkpoints" / "cp1" / "manifest.json").read_text() == "new"
    assert json.loads((live / "messages.jsonl").read_text())["content"] == "v2"
    store.close()


def test_replace_commit_final_dir_present_after_failure(tmp_path: Path, monkeypatch):
    """A replace-mode commit never swaps the session dir away on failure."""
    import sessionfs.store.local as local_mod
    from sessionfs.sync.archive import unpack_session

    store = LocalStore(tmp_path)
    store.initialize()
    live = _capture(store, "v1")
    (live / "checkpoints" / "cp1").mkdir(parents=True)
    (live / "checkpoints" / "cp1" / "manifest.json").write_text("{}")
    (live / "local_only.txt").write_text("local")

    archive, _ = _remote_archive(tmp_path)

    def _writer(staging: Path):
        unpack_session(archive, staging)
        return json.loads((staging / "manifest.json").read_text())

    real_replace = local_mod.os.replace
    moves = {"n": 0}

    def _failing_replace(src, dst, _moves=moves, _fail=3):
        if _moves["n"] == _fail:
            raise OSError("injected replace failure")
        _moves["n"] += 1
        return real_replace(src, dst)

    with monkeypatch.context() as m:
        m.setattr(local_mod.os, "replace", _failing_replace)
        with pytest.raises(OSError):
            store.commit_session(SID, _writer, skip_excluded=False, mode="replace")

    # The final dir was never removed or renamed aside; only it remains.
    assert live.is_dir()
    assert (live / "manifest.json").is_file()
    assert [p for p in store.sessions_dir.iterdir() if p != live] == []
    store.close()


def test_replace_commit_never_leaves_session_without_manifest(tmp_path: Path, monkeypatch):
    """A failure while swapping in the pulled manifest keeps the old one in place."""
    import sessionfs.store.local as local_mod
    from sessionfs.sync.archive import unpack_session

    store = LocalStore(tmp_path)
    store.initialize()
    live = _capture(store, "v1")
    old_manifest = (live / "manifest.json").read_text()

    archive, _ = _remote_archive(tmp_path)

    def _writer(staging: Path):
        unpack_session(archive, staging)
        return json.loads((staging / "manifest.json").read_text())

    real_replace = local_mod.os.replace

    def _fail_on_manifest(src, dst):
        if Path(dst).name == "manifest.json" and Path(dst).parent == live:
            raise OSError("injected manifest replace failure")
        return real_replace(src, dst)

    with monkeypatch.context() as m:
        m.setattr(local_mod.os, "replace", _fail_on_manifest)
        with pytest.raises(OSError):
            store.commit_session(SID, _writer, skip_excluded=False, mode="replace")

    assert (live / "manifest.json").read_text() == old_manifest
    store.close()


def test_locks_available_detects_filesystem_without_flock(tmp_path: Path, monkeypatch):
    import errno

    import sessionfs.store.fsutil as fsutil_mod

    if fsutil_mod.fcntl is None:  # pragma: no cover - Windows
        pytest.skip("no fcntl")
    lock_dir = tmp_path / "locks"
    calls = {"n": 0}

    def _unsupported(fd, op):
        calls["n"] += 1
        raise OSError(errno.ENOLCK, "No locks available")

    monkeypatch.setattr(fsutil_mod, "_lock_support", {})
    monkeypatch.setattr(fsutil_mod.fcntl, "flock", _unsupported)
    assert fsutil_mod.locks_available(lock_dir) is False
    assert fsutil_mod.locks_available(lock_dir) is False
    assert calls["n"] == 1  # probed once, then cached
    monkeypatch.undo()
    monkeypatch.setattr(fsutil_mod, "_lock_support", {})
    assert fsutil_mod.locks_available(tmp_path / "other") is True


# ---------------------------------------------------------------------------
# LOW N2: a skipped commit must not retire the previous capture's files
# ---------------------------------------------------------------------------


def test_skipped_commit_leaves_retire_targets_intact(tmp_path: Path):
    """Exclusion landing mid-capture skips the commit without retiring files."""
    store = LocalStore(tmp_path)
    store.initialize()
    live = _capture(store, "v1")
    (live / "workspace.json").write_text("ws")
    (live / "tools.json").write_text("tools")

    def _writer(staging: Path):
        # A delete lands after the initial exclusion check, before the commit.
        mark_deleted(SID, "local", base_dir=store.store_dir)
        (staging / "manifest.json").write_text(json.dumps(_BASE_MANIFEST))
        (staging / "messages.jsonl").write_text(
            json.dumps({"role": "user", "content": "v2"}) + "\n"
        )
        return WriterResult(_BASE_MANIFEST, frozenset({"workspace.json", "tools.json"}))

    outcome = store.commit_session(SID, _writer)

    assert outcome.skipped is True
    # The retire step runs only after the commit-time recheck, so a skipped
    # commit leaves the previous capture's workspace.json/tools.json intact.
    assert (live / "workspace.json").read_text() == "ws"
    assert (live / "tools.json").read_text() == "tools"
    store.close()


# ---------------------------------------------------------------------------
# Startup recovery: stale staging/.old leftovers cleaned, stranded .old rescued
# ---------------------------------------------------------------------------


def test_initialize_recovers_stale_dirs(tmp_path: Path):
    store = LocalStore(tmp_path)

    # Lay leftovers down before initialize() runs so _recover_stale_dirs sees
    # them exactly as a previous interrupted commit left them.
    sessions = store._sessions_dir
    sessions.mkdir(parents=True)

    stranded_old = sessions / f".{SID}.sfs.old.stranded"
    stranded_old.mkdir()
    (stranded_old / "manifest.json").write_text("rescue me")

    redundant_old = sessions / f".{SID2}.sfs.old.redundant"
    redundant_old.mkdir()
    (redundant_old / "manifest.json").write_text("drop me")
    final2 = sessions / f"{SID2}.sfs"
    final2.mkdir()
    (final2 / "manifest.json").write_text("real")

    stale_staging = sessions / f".{SID3}.sfs.staging.abc123"
    stale_staging.mkdir()
    (stale_staging / "messages.jsonl").write_text("partial")

    store.initialize()

    # A stranded .old. with no final dir is renamed back into place.
    assert not stranded_old.exists()
    assert (sessions / f"{SID}.sfs").is_dir()
    assert ((sessions / f"{SID}.sfs") / "manifest.json").read_text() == "rescue me"

    # A .old. whose final dir still exists is redundant and removed.
    assert not redundant_old.exists()
    assert (final2 / "manifest.json").read_text() == "real"

    # A stale staging dir is removed.
    assert not stale_staging.exists()

    store.close()


def test_initialize_leaves_fresh_staging_when_lock_unavailable(tmp_path: Path, monkeypatch):
    """Without a working lock, a fresh staging dir is not reclaimed.

    On Windows / flock-less filesystems the per-session lock degrades to a
    no-op, so a CLI's initialize() could otherwise delete a staging dir the
    daemon is mid-commit on. A staging dir younger than an hour is left alone;
    an older one is still cleaned up.
    """
    import os as _os

    import sessionfs.store.fsutil as fsutil_mod

    store = LocalStore(tmp_path)
    sessions = store._sessions_dir
    sessions.mkdir(parents=True)

    fresh = sessions / f".{SID}.sfs.staging.fresh"
    fresh.mkdir()
    (fresh / "messages.jsonl").write_text("partial")
    _os.utime(fresh, (time.time(), time.time()))

    old = sessions / f".{SID2}.sfs.staging.old"
    old.mkdir()
    (old / "messages.jsonl").write_text("stale")
    _os.utime(old, (time.time() - 7200, time.time() - 7200))

    monkeypatch.setattr(fsutil_mod, "locks_available", lambda *a, **k: False)

    store.initialize()

    assert fresh.exists()  # too fresh to risk deleting
    assert not old.exists()  # clearly abandoned: reclaimed
    store.close()


# ---------------------------------------------------------------------------
# M1: hash-then-pack under one lock
# ---------------------------------------------------------------------------


def test_pack_and_hash_records_hash_before_packing(tmp_path: Path, monkeypatch):
    import sessionfs.sync.archive as archive_mod
    import sessionfs.store.local as local_mod

    store = LocalStore(tmp_path)
    store.initialize()
    session_dir = _capture(store, "v1")

    expected = local_mod._content_hash(session_dir)  # hash of v1

    real_pack = archive_mod.pack_session

    def _pack_then_mutate(sd):
        data = real_pack(sd)  # packs v1
        # A capture lands mid-upload after the bytes were packed. On the old
        # pack-then-hash ordering this would be recorded as the packed hash and
        # the ack would wrongly clear dirty; hash-first records v1 instead.
        (sd / "messages.jsonl").write_text(
            json.dumps({"role": "user", "content": "v2"}) + "\n"
        )
        return data

    monkeypatch.setattr(archive_mod, "pack_session", _pack_then_mutate)

    local_mod.pack_and_hash(session_dir)

    recorded = json.loads((session_dir / "manifest.json").read_text())["sync"][
        "_content_hash"
    ]
    assert recorded == expected  # matches what was packed (v1), not v2

    # The ack sees v2 != v1 and keeps the session dirty.
    local_mod.apply_sync_ack(session_dir, "etag-1")
    sync = json.loads((session_dir / "manifest.json").read_text())["sync"]
    assert sync["etag"] == "etag-1"
    assert sync["dirty"] is True
    store.close()


# ---------------------------------------------------------------------------
# M2: manifest writers never resurrect a deleted session dir
# ---------------------------------------------------------------------------


def test_manifest_writers_do_not_recreate_missing_session_dir(tmp_path: Path):
    missing = tmp_path / "sessions" / f"{SID}.sfs"

    assert record_packed_content_hash(missing) is None
    apply_sync_ack(missing, "etag-1")
    mark_session_synced(missing, "etag-2")

    # None of the read-modify-write manifest helpers may recreate the directory
    # of a session a concurrent delete just removed.
    assert not missing.exists()


# ---------------------------------------------------------------------------
# L4: delete/cleanup serialize with commit_session via the per-session lock
# ---------------------------------------------------------------------------


def test_cleanup_waits_for_commit_lock(tmp_path: Path):
    from sessionfs.store.deleted import is_excluded
    from sessionfs.store.local import lock_session_dir
    from sessionfs.sync.deleted_cleanup import cleanup_deleted_session

    store = LocalStore(tmp_path)
    store.initialize()
    session_dir = _capture(store, "hello")

    done = threading.Event()

    def _delete() -> None:
        cleanup_deleted_session(SID, session_dir, store, base_dir=store.store_dir)
        done.set()

    # Hold the same per-session lock a capture's commit_session takes. The
    # cleanup must block behind it rather than removing the dir mid-commit.
    with lock_session_dir(store.sessions_dir, SID):
        worker = threading.Thread(target=_delete)
        worker.start()
        time.sleep(0.5)
        assert not done.is_set(), "cleanup must wait for the commit lock"
        assert session_dir.is_dir(), "cleanup must not remove the dir while locked"

    worker.join(timeout=10)
    assert done.is_set()
    assert not session_dir.exists()
    # The exclusion marker was written before the lock was taken (independent
    # of the SQLite index, which is thread-affine and cleaned by the daemon).
    assert is_excluded(SID, base_dir=store.store_dir)
    store.close()


@pytest.mark.parametrize("sid", ["ses_T3f2a9c1d", "ses_thr_amp1", "ses_minimal"])
def test_commit_accepts_ids_derived_from_native_ids(tmp_path: Path, sid: str):
    """Watcher ids come from native ids and need not match the strict server format."""
    store = LocalStore(tmp_path)
    store.initialize()

    def _writer(staging: Path):
        (staging / "manifest.json").write_text(json.dumps({"session_id": sid}))
        return {"session_id": sid}

    outcome = store.commit_session(sid, _writer)
    assert not outcome.skipped
    assert (store.sessions_dir / f"{sid}.sfs" / "manifest.json").is_file()
    store.close()


@pytest.mark.parametrize("sid", ["", "../escape", "a/b", ".hidden", "ses_x\\y", "x" * 129, "ses_abc\n"])
def test_commit_refuses_unsafe_session_ids(tmp_path: Path, sid: str):
    store = LocalStore(tmp_path)
    store.initialize()
    with pytest.raises(ValueError):
        store.commit_session(sid, lambda staging: None)
    store.close()


def test_lock_path_refuses_unsafe_session_ids(tmp_path: Path):
    from sessionfs.store.local import session_lock_path

    with pytest.raises(ValueError):
        session_lock_path(tmp_path / "sessions", "../outside")
    assert session_lock_path(tmp_path / "sessions", "ses_T1").name == "ses_T1.lock"
