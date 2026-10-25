"""Filesystem durability + locking helpers for the local session store.

Shared by ``store.local``, ``store.deleted`` and the daemon status writer so
crash-safety is implemented once and used everywhere:

- ``atomic_write`` / ``atomic_write_text``: write a temp file in the target's
  directory, fsync it, atomically ``os.replace`` it into place, then fsync the
  parent directory (best-effort). A crash can therefore never leave a
  truncated manifest, index or state file behind — either the old file or the
  complete new file survives.
- ``file_lock``: a cross-process exclusive advisory lock (``fcntl.flock``) on a
  sidecar lock file, taken on the descriptor only and never followed through a
  symlink. Degrades to a no-op on Windows where ``fcntl`` is unavailable.
- ``fsync_dir``: best-effort directory fsync so renames/unlinks survive a
  crash on POSIX filesystems that support it.
"""

from __future__ import annotations

import contextlib
import errno
import logging
import os
import tempfile
from collections.abc import Iterator
from pathlib import Path

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows has no fcntl
    fcntl = None

logger = logging.getLogger("sessionfs.store.fsutil")

_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_O_CLOEXEC = getattr(os, "O_CLOEXEC", 0)

# flock() errnos that mean "this filesystem doesn't support POSIX locks" rather
# than "something went wrong". On those we proceed without mutual exclusion
# rather than failing the whole store, matching the no-fcntl Windows fallback.
_LOCK_UNSUPPORTED = (errno.ENOTSUP, errno.ENOLCK, errno.EOPNOTSUPP)


_lock_support: dict[str, bool] = {}


def locks_available(lock_dir: Path | None = None) -> bool:
    """Whether ``file_lock`` provides real cross-process mutual exclusion.

    False on Windows (no ``fcntl``) and, when ``lock_dir`` is given, on a
    filesystem that rejects ``flock`` there (some network mounts), where
    ``file_lock`` degrades to a no-op. The filesystem answer is probed once
    per directory with a non-blocking flock and cached. Callers that must not
    remove a directory another process may be mid-commit on (startup recovery
    of stale staging dirs) use this to fall back to an mtime-age heuristic
    instead of trusting a lock that never excludes.
    """
    if fcntl is None:
        return False
    if lock_dir is None:
        return True
    key = str(lock_dir)
    cached = _lock_support.get(key)
    if cached is not None:
        return cached
    supported = True
    try:
        if lock_dir.is_symlink():
            raise OSError(f"Lock directory cannot be a symlink: {lock_dir}")
        lock_dir.mkdir(parents=True, exist_ok=True)
        fd = os.open(
            lock_dir / ".probe.lock",
            os.O_RDWR | os.O_CREAT | _O_NOFOLLOW | _O_CLOEXEC,
            0o600,
        )
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError as exc:
            # EWOULDBLOCK/EAGAIN just means another process holds the probe:
            # locking works. Only "unsupported" errnos mean it doesn't.
            if exc.errno in _LOCK_UNSUPPORTED:
                supported = False
        finally:
            os.close(fd)
    except OSError:
        # Can't even probe: be conservative and assume no mutual exclusion.
        supported = False
    _lock_support[key] = supported
    return supported


def fsync_dir(path: Path) -> None:
    """fsync a directory so a rename/unlink inside it is durable.

    Best-effort: some platforms/filesystems refuse to open a directory as a
    file, in which case this silently does nothing.
    """
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        fd = os.open(path, flags)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def atomic_write(
    path: Path,
    data: bytes,
    *,
    mode: int = 0o600,
    create_parent: bool = True,
) -> None:
    """Durably replace ``path`` with ``data``.

    The temp file lives in the same directory (so the final rename is a same
    filesystem, atomic ``os.replace``) and is fsynced before the swap. A
    planted symlink at ``path`` is replaced, not followed — ``os.replace``
    renames over the link itself, never its target.

    ``create_parent=False`` is for session-dir paths (``{id}.sfs/manifest.json``
    and friends): the parent is a live session directory that must not be
    recreated if it is missing — recreating it would resurrect a session a
    concurrent delete just removed. In that mode a missing parent raises
    ``FileNotFoundError`` instead of being created.
    """
    if create_parent:
        path.parent.mkdir(parents=True, exist_ok=True)
    elif not path.parent.is_dir():
        raise FileNotFoundError(
            f"Refusing to create parent directory for {path}: it does not exist"
        )
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        with contextlib.suppress(OSError):
            os.chmod(tmp_name, mode)
        os.replace(tmp_name, path)
        fsync_dir(path.parent)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise


def atomic_write_text(
    path: Path,
    text: str,
    *,
    mode: int = 0o600,
    create_parent: bool = True,
) -> None:
    """``atomic_write`` for UTF-8 text."""
    atomic_write(path, text.encode("utf-8"), mode=mode, create_parent=create_parent)


@contextlib.contextmanager
def file_lock(lock_path: Path) -> Iterator[None]:
    """Hold an exclusive cross-process advisory lock on ``lock_path``.

    Mirrors ``skills.state.interprocess_lock``: the lock file is opened without
    following symlinks (``O_NOFOLLOW`` where the platform has it) and the lock
    is taken on the descriptor only — no bytes are ever written, so a
    pre-planted symlink at the lock path can never make us touch an arbitrary
    file. On Windows ``fcntl`` is missing and this degrades to a no-op: the
    store still works, only the cross-process mutual exclusion is lost there.

    The lock file is deliberately never unlinked. Unlinking a flock file while
    another process is blocked on it lets a third process open a *different*
    inode at the same path and acquire a second lock on it, so two writers
    end up "holding" the same session lock at once. The empty lock file is
    left in place for the lifetime of the store so every process locks the
    same inode.
    """
    parent = lock_path.parent
    if parent.is_symlink():
        raise OSError(f"Lock directory cannot be a symlink: {parent}")
    parent.mkdir(parents=True, exist_ok=True)
    if lock_path.is_symlink():
        raise OSError(f"Lock file cannot be a symlink: {lock_path}")
    if fcntl is None:  # pragma: no cover - Windows
        yield
        return
    flags = os.O_RDWR | os.O_CREAT | _O_NOFOLLOW | _O_CLOEXEC
    try:
        fd = os.open(lock_path, flags, 0o600)
    except OSError as exc:
        # O_NOFOLLOW refuses to open a symlink planted after the is_symlink
        # check above; surface that as a clear refusal rather than touching
        # the link's target.
        if lock_path.is_symlink():
            raise OSError(f"Lock file cannot be a symlink: {lock_path}") from exc
        raise
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
        except OSError as exc:
            # Some filesystems (network mounts, some virtual filesystems)
            # don't support flock. That is not a reason to refuse to run the
            # store — proceed without the cross-process mutual exclusion,
            # matching the no-fcntl fallback above.
            if exc.errno in _LOCK_UNSUPPORTED:
                logger.warning(
                    "flock not supported on %s (%s); proceeding without "
                    "the per-session lock",
                    lock_path,
                    exc,
                )
                yield
                return
            raise
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)
