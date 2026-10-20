"""Symlink-safe, bounded file access under an Antigravity storage root.

Every path the Antigravity converter reads is built from names that come
out of the app's own files (conversation ids from the summaries DB, step
numbers, chunk names). Those names are validated before use, and every
component below the storage root is opened with ``O_NOFOLLOW`` relative to
its parent's descriptor, so neither a crafted id nor a planted symlink can
make SessionFS read a file outside the root.

The root itself (``~/.gemini/antigravity`` and friends) is the trust anchor:
it may be a symlink the user set up; nothing below it may be.
"""

from __future__ import annotations

import contextlib
import errno
import hashlib
import os
import re
import stat
from collections import OrderedDict
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Canonical lowercase UUID — the only shape a conversation id has (and one
# that maps onto a valid ses_ id: ses_ + 16 lowercase hex characters).
_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)
# File and directory names we read below a conversation: plain names only.
_SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9_.][A-Za-z0-9_.\-]{0,127}$")

_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
_DIR_FLAGS = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | _NOFOLLOW | _CLOEXEC
_FILE_FLAGS = os.O_RDONLY | _NOFOLLOW | _CLOEXEC

MAX_LINE_BYTES = 32 * 1024 * 1024  # one transcript step; larger lines are skipped
SKIP_CHUNK_BYTES = 1024 * 1024  # read size while discarding an oversized line


class UnsafePath(ValueError):
    """A name or path component that must not be opened."""


def is_conversation_id(value: Any) -> bool:
    return isinstance(value, str) and bool(_UUID_RE.match(value))


def _check_name(name: str) -> None:
    if (
        not isinstance(name, str)
        or name in ("", ".", "..")
        or "/" in name
        or "\\" in name
        or "\x00" in name
        or not _SAFE_NAME_RE.match(name)
    ):
        raise UnsafePath(f"unsafe path component: {name!r}")


def dir_fd_supported() -> bool:
    return os.open in os.supports_dir_fd and bool(_NOFOLLOW)


@contextlib.contextmanager
def open_dir(root: Path, parts: tuple[str, ...] | list[str]) -> Iterator[int]:
    """Descriptor for ``root/parts...`` reached without following symlinks
    below ``root``. Raises FileNotFoundError / UnsafePath."""
    for part in parts:
        _check_name(part)
    if not dir_fd_supported():
        raise UnsafePath("this platform cannot open files without following symlinks")
    fd = os.open(str(root), os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | _CLOEXEC)
    try:
        for part in parts:
            try:
                nxt = os.open(part, _DIR_FLAGS, dir_fd=fd)
            except OSError as exc:
                if exc.errno in (errno.ELOOP, errno.ENOTDIR, errno.EMLINK):
                    raise UnsafePath(f"refusing to follow a symlink: {part}") from exc
                raise
            os.close(fd)
            fd = nxt
        yield fd
    finally:
        os.close(fd)


def _open_file(dir_fd: int, name: str) -> int:
    _check_name(name)
    try:
        fd = os.open(name, _FILE_FLAGS, dir_fd=dir_fd)
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.EMLINK):
            raise UnsafePath(f"refusing to follow a symlink: {name}") from exc
        raise
    st = os.fstat(fd)
    if not stat.S_ISREG(st.st_mode):
        os.close(fd)
        raise UnsafePath(f"not a regular file: {name}")
    return fd


def stat_file(root: Path, parts: tuple[str, ...] | list[str]) -> os.stat_result | None:
    """lstat of a regular file below root (None if missing, a link, or unsafe)."""
    try:
        with open_dir(root, tuple(parts[:-1])) as dfd:
            _check_name(parts[-1])
            st = os.stat(parts[-1], dir_fd=dfd, follow_symlinks=False)
    except (OSError, UnsafePath):
        return None
    return st if stat.S_ISREG(st.st_mode) else None


def list_dir(root: Path, parts: tuple[str, ...] | list[str]) -> list[tuple[str, bool]]:
    """(name, is_dir) entries of a directory below root; links are omitted."""
    out: list[tuple[str, bool]] = []
    try:
        with open_dir(root, tuple(parts)) as dfd:
            for name in sorted(os.listdir(dfd)):
                try:
                    st = os.stat(name, dir_fd=dfd, follow_symlinks=False)
                except OSError:
                    continue
                if stat.S_ISLNK(st.st_mode):
                    continue
                if stat.S_ISDIR(st.st_mode) or stat.S_ISREG(st.st_mode):
                    out.append((name, stat.S_ISDIR(st.st_mode)))
    except (OSError, UnsafePath):
        return []
    return out


def read_bytes(
    root: Path, parts: tuple[str, ...] | list[str], max_bytes: int | None = None,
) -> tuple[bytes, bool] | None:
    """(data, truncated) of a regular file below root, at most ``max_bytes``."""
    try:
        with open_dir(root, tuple(parts[:-1])) as dfd:
            fd = _open_file(dfd, parts[-1])
            with os.fdopen(fd, "rb") as f:
                if max_bytes is None:
                    return f.read(), False
                data = f.read(max_bytes + 1)
                return data[:max_bytes], len(data) > max_bytes
    except (OSError, UnsafePath):
        return None


# ---------------------------------------------------------------------------
# Incremental JSONL reading
# ---------------------------------------------------------------------------


@dataclass
class _FileState:
    dev: int
    ino: int
    offset: int = 0  # bytes consumed through the last complete line
    mtime_ns: int = -1
    size: int = -1
    prefix_sha: str = ""  # SHA-256 of bytes [0, offset)
    records: list[Any] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


class JsonlCache:
    """Parses append-only JSONL files incrementally.

    An unchanged file (same inode, size and mtime) is not read at all. A
    file that only grew is resumed after its last complete line — but only
    after the bytes already consumed are re-hashed and still match, so a
    rewrite anywhere in the old part is caught. A file whose mtime changed
    without growing, that shrank, or that was replaced is parsed again from
    the start. A trailing partial line is left for the next read. Bounded
    LRU so a long-running daemon does not grow without limit.
    """

    def __init__(self, max_files: int = 512) -> None:
        self._max = max_files
        self._files: OrderedDict[tuple[str, tuple[str, ...]], _FileState] = OrderedDict()

    def clear(self) -> None:
        self._files.clear()

    def read(
        self, root: Path, parts: tuple[str, ...],
    ) -> tuple[list[Any], list[str]] | None:
        """(records, errors) for a JSONL file below root, or None if unreadable."""
        key = (str(root), tuple(parts))
        try:
            with open_dir(root, parts[:-1]) as dfd:
                fd = _open_file(dfd, parts[-1])
                with os.fdopen(fd, "rb") as f:
                    st = os.fstat(f.fileno())
                    state = self._files.get(key)
                    if state is not None and (st.st_size, st.st_mtime_ns) == (state.size, state.mtime_ns) \
                            and (state.dev, state.ino) == (st.st_dev, st.st_ino):
                        self._touch(key, state)
                        return list(state.records), list(state.errors)
                    hasher = None
                    if state is not None:
                        hasher = self._resume_hasher(f, st, state)
                    if hasher is None:
                        state = _FileState(st.st_dev, st.st_ino)
                        hasher = hashlib.sha256()
                    assert state is not None
                    partial, hasher = self._consume(f, state, parts[-1], hasher)
                    state.prefix_sha = hasher.hexdigest()
                    state.size = st.st_size
                    state.mtime_ns = st.st_mtime_ns
                    if partial:
                        state.size = -1  # re-check once the line is complete
                    self._touch(key, state)
                    errors = list(state.errors)
                    if partial:
                        errors.append(f"{parts[-1]}: incomplete final line")
                    return list(state.records), errors
        except (OSError, UnsafePath):
            self._files.pop(key, None)
            return None

    @staticmethod
    def _resume_hasher(f: Any, st: os.stat_result, state: _FileState) -> Any:
        """A hasher primed with the consumed prefix if the file can be
        resumed, else None (parse from scratch)."""
        if (state.dev, state.ino) != (st.st_dev, st.st_ino):
            return None
        if st.st_size <= state.size and st.st_mtime_ns != state.mtime_ns:
            return None  # rewritten without growing
        if st.st_size < state.offset:
            return None
        hasher = hashlib.sha256()
        f.seek(0)
        remaining = state.offset
        while remaining > 0:
            chunk = f.read(min(1 << 20, remaining))
            if not chunk:
                return None
            hasher.update(chunk)
            remaining -= len(chunk)
        if hasher.hexdigest() != state.prefix_sha:
            return None  # something already consumed was rewritten
        return hasher

    @staticmethod
    def _consume(f: Any, state: _FileState, name: str, hasher: Any) -> tuple[bool, Any]:
        """Parse complete lines from state.offset. Returns (a partial line
        remains, the hasher of everything consumed)."""
        import json

        f.seek(state.offset)
        lineno = len(state.records) + len(state.errors)
        while True:
            start = f.tell()
            line = f.readline(MAX_LINE_BYTES + 1)
            if not line:
                return False, hasher
            if not line.endswith(b"\n"):
                if len(line) <= MAX_LINE_BYTES:
                    return True, hasher  # partial final line — the app is mid-append
                # Oversized step: discard it chunk by chunk up to its newline,
                # hashing on a copy that is kept only once the newline is found
                # (memory stays at about one chunk).
                pending = hasher.copy()
                pending.update(line)
                del line
                while True:
                    chunk = f.readline(SKIP_CHUNK_BYTES)
                    if not chunk:
                        f.seek(start)
                        return True, hasher  # no newline yet: retry later
                    pending.update(chunk)
                    if chunk.endswith(b"\n"):
                        break
                hasher = pending
                state.offset = f.tell()
                lineno += 1
                state.errors.append(f"{name}:{lineno}: line too large, skipped")
                continue
            hasher.update(line)
            state.offset = f.tell()
            lineno += 1
            text = line.strip()
            if not text:
                continue
            try:
                state.records.append(json.loads(text))
            except ValueError:
                state.errors.append(f"{name}:{lineno}: invalid JSON")
    def _touch(self, key: tuple[str, tuple[str, ...]], state: _FileState) -> None:
        self._files[key] = state
        self._files.move_to_end(key)
        while len(self._files) > self._max:
            self._files.popitem(last=False)
