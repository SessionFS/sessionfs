"""Durability + locking helpers: crash-safe atomic writes and the per-session
cross-process lock (fixes #3 and the shared plumbing for #1/#5)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

import sessionfs.store.fsutil as fsutil


def test_atomic_write_replaces_content(tmp_path: Path):
    target = tmp_path / "manifest.json"
    target.write_text("old")
    fsutil.atomic_write(target, b"new")
    assert target.read_bytes() == b"new"
    mode = target.stat().st_mode & 0o777
    assert mode == 0o600


def test_atomic_write_failure_keeps_old_file(tmp_path: Path, monkeypatch):
    target = tmp_path / "manifest.json"
    target.write_text("old")

    def boom(_fd: int) -> None:
        raise OSError("No space left on device")

    monkeypatch.setattr(os, "fsync", boom)
    with pytest.raises(OSError):
        fsutil.atomic_write(target, b"new")
    # The temp file was never renamed over the target.
    assert target.read_text() == "old"
    assert list(tmp_path.glob(".manifest.json.*.tmp")) == []


def test_atomic_write_text(tmp_path: Path):
    target = tmp_path / "state.json"
    fsutil.atomic_write_text(target, '{"ok": true}')
    assert target.read_text() == '{"ok": true}'


def test_atomic_write_create_parent_false_raises_when_parent_missing(tmp_path: Path):
    """create_parent=False must not resurrect a missing session dir."""
    missing = tmp_path / "sessions" / "ses_abc.sfs" / "manifest.json"
    with pytest.raises(FileNotFoundError):
        fsutil.atomic_write(missing, b"new", create_parent=False)
    assert not (tmp_path / "sessions").exists()


def test_atomic_write_create_parent_false_ok_when_parent_exists(tmp_path: Path):
    parent = tmp_path / "sessions" / "ses_abc.sfs"
    parent.mkdir(parents=True)
    target = parent / "manifest.json"
    fsutil.atomic_write(target, b"new", create_parent=False)
    assert target.read_bytes() == b"new"


def test_atomic_write_replaces_symlink_not_target(tmp_path: Path):
    """A planted symlink at the write path is replaced, never followed."""
    target = tmp_path / "victim.txt"
    target.write_text("precious")
    link = tmp_path / "manifest.json"
    link.symlink_to(target)

    fsutil.atomic_write(link, b"new")

    assert target.read_text() == "precious"  # target untouched
    assert not link.is_symlink()
    assert link.read_text() == "new"


def test_file_lock_refuses_symlink(tmp_path: Path):
    target = tmp_path / "victim"
    target.write_text("data")
    lock = tmp_path / "locks" / "ses_0123456789abcdef.lock"
    lock.parent.mkdir()
    lock.symlink_to(target)

    with pytest.raises(OSError):
        with fsutil.file_lock(lock):
            pass
    assert target.read_text() == "data"


def test_file_lock_creates_lock_file_and_writes_nothing(tmp_path: Path):
    lock = tmp_path / "locks" / "ses_0123456789abcdef.lock"
    with fsutil.file_lock(lock):
        assert lock.is_file()
        assert lock.read_bytes() == b""
    # Lock is released on exit and the file is left in place for reuse.
    assert lock.is_file()


def test_file_lock_proceeds_without_lock_when_flock_unsupported(
    tmp_path: Path, monkeypatch, caplog
):
    """flock on ENOTSUP/ENOLCK/EOPNOTSUPP warns once and proceeds unlocked."""
    import errno
    import fcntl as fcntl_mod

    lock = tmp_path / "locks" / "ses_0123456789abcdef.lock"

    def _unsupported(_fd: int, _op: int) -> None:
        raise OSError(errno.ENOTSUP, "Operation not supported")

    monkeypatch.setattr(fcntl_mod, "flock", _unsupported)

    with caplog.at_level("WARNING", logger="sessionfs.store.fsutil"):
        with fsutil.file_lock(lock):
            assert lock.is_file()

    assert "flock not supported" in caplog.text
