"""Crash-recoverable local application of deterministic skill plans."""

from __future__ import annotations

import contextlib
import errno
import json
import os
import shutil
import tempfile
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from sessionfs.skills.models import EmitPlan, PackageChangedConcurrently, PackageRecord, SkillError
from sessionfs.skills.path_safety import is_link_or_reparse


# ONE ceiling shared by the reader, the writer and instruction provenance:
# a state file the writer produces must always be readable back.
MAX_STATE_BYTES = 32 * 1024 * 1024

# Managed outputs above this size are treated as dirty without hashing so a
# sparse or huge file can never exhaust memory or stall a remove/sync.
MAX_HASH_BYTES = 256 * 1024 * 1024
_HASH_CHUNK_BYTES = 1024 * 1024

# Windows `msvcrt.locking(LK_LOCK)` gives up after ~10 one-second attempts; a
# sync waiting behind a long install would then fail spuriously. The lock is
# retried non-blocking every 250ms for this budget instead and fails closed
# after it (POSIX flock blocks; this is the bounded equivalent).
LOCK_WAIT_SECONDS = 120.0
_LOCK_RETRY_SECONDS = 0.25
_lock_sleep = time.sleep
_lock_clock = time.monotonic


def read_state_bytes(path: Path, *, limit: int = MAX_STATE_BYTES) -> bytes | None:
    """Bounded, no-follow read of a state/lock/journal file.

    Opens with O_NOFOLLOW (explicit link check where unavailable), refuses
    anything that is not a regular file (a symlink to /dev/zero would hang an
    unbounded read), and never pulls more than `limit` bytes.
    """
    import stat as _stat

    if is_link_or_reparse(path):
        raise SkillError(f"Skill state file cannot be a symlink: {path}")
    # O_NONBLOCK: opening a FIFO/device read-only would otherwise block until a
    # writer appears; the S_ISREG check below rejects it right after the open.
    flags = (
        os.O_RDONLY
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    try:
        fd = os.open(path, flags)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise SkillError(f"Cannot read skill state {path}: {exc}") from exc
    try:
        info = os.fstat(fd)
        if not _stat.S_ISREG(info.st_mode):
            raise SkillError(f"Skill state file is not a regular file: {path}")
        if info.st_size > limit:
            raise SkillError(f"Skill state file exceeds the {limit}-byte limit: {path}")
        chunks: list[bytes] = []
        remaining = limit + 1
        while remaining > 0:
            chunk = os.read(fd, min(65536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
    finally:
        os.close(fd)
    raw = b"".join(chunks)
    if len(raw) > limit:
        raise SkillError(f"Skill state file exceeds the {limit}-byte limit: {path}")
    return raw


def read_json(path: Path, default: Any) -> Any:
    raw = read_state_bytes(path)
    if raw is None:
        return default
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SkillError(f"Cannot read skill state {path}: {exc}") from exc


def atomic_json(path: Path, value: Any) -> None:
    if is_link_or_reparse(path.parent):
        raise SkillError(f"Skill state directory cannot be a symlink: {path.parent}")
    # Serialize BEFORE touching the filesystem: a state file larger than the
    # reader accepts would brick every later list/sync/doctor/recovery, so the
    # mutation is refused and the previous file is left untouched.
    encoded = (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")
    if len(encoded) > MAX_STATE_BYTES:
        raise SkillError(
            f"Refusing to write skill state larger than the {MAX_STATE_BYTES}-byte limit "
            f"({len(encoded)} bytes): {path}"
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    if is_link_or_reparse(path) or (path.exists() and not path.is_file()):
        raise SkillError(f"Skill state file cannot be a symlink or special file: {path}")
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        if is_link_or_reparse(path):
            raise SkillError(f"Skill state file cannot be a symlink: {path}")
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
    # The rename itself must be durable: a journal that names backups which
    # never reached the disk is worse than no journal.
    _fsync_directory(path.parent)


def _fsync_file(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _fsync_directory(path: Path) -> None:
    """fsync a directory entry after a rename (no-op where the platform cannot
    open directories, e.g. Windows - the rename is still atomic there)."""
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0)
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


_NOFOLLOW_DIR_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_DIRECTORY", 0)
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_CLOEXEC", 0)
)


class OutputNotMoved(SkillError):
    """An output could not be moved out of its tool directory, and it is
    GUARANTEED to still be at its path (nothing was changed)."""

    def __init__(self, path: str, cause: OSError) -> None:
        super().__init__(f"Cannot move managed file {path}: {cause.strerror or cause}")
        self.path = path
        self.cause = cause


_RENAME_NOREPLACE = 1  # Linux renameat2 flag
_RENAME_EXCL = 0x00000004  # macOS renamex_np / renameatx_np flag
_NATIVE_NOREPLACE: Any = None  # resolved lazily: a callable, or False


def _native_noreplace() -> Any:
    """The platform's atomic directory-fd-relative rename-without-replace,
    or False: Linux `renameat2(RENAME_NOREPLACE)`, macOS
    `renameatx_np(RENAME_EXCL)`. Called as fn(src_fd, src, dst_fd, dst)."""
    global _NATIVE_NOREPLACE
    if _NATIVE_NOREPLACE is not None:
        return _NATIVE_NOREPLACE
    found: Any = False
    try:
        import ctypes
        import ctypes.util
        import sys

        libc = ctypes.CDLL(ctypes.util.find_library("c") or None, use_errno=True)
        name = (
            "renameat2" if sys.platform.startswith("linux")
            else "renameatx_np" if sys.platform == "darwin" else None
        )
        if name is not None and hasattr(libc, name):
            fn = getattr(libc, name)
            fn.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
            fn.restype = ctypes.c_int
            flag = _RENAME_NOREPLACE if name == "renameat2" else _RENAME_EXCL

            def found(src_fd: int, src: bytes, dst_fd: int, dst: bytes) -> int:
                return fn(src_fd, src, dst_fd, dst, flag)
    except (OSError, AttributeError):
        found = False
    _NATIVE_NOREPLACE = found
    return found


# ---------------------------------------------------------------------------
# Directory-descriptor walking. Every mutation of a managed output (and of
# the transaction / trash / quarantine entries) is done RELATIVE TO A
# DESCRIPTOR of its parent directory, reached by opening a trusted anchor
# (the scope root - home or project - or the content store) once and then
# every component below it with O_NOFOLLOW|O_DIRECTORY. A directory swapped
# for a symlink after validation therefore cannot redirect a create, rename,
# link or unlink outside the adapter roots: the open of that component fails.
# Platforms without directory-descriptor support refuse these operations.
# ---------------------------------------------------------------------------

_ANCHORS: set[str] = set()
_ANCHOR_LOCK = threading.Lock()


def register_anchor(path: Path | None) -> None:
    """Record a trusted root (scope root, content store) that directory
    walks start from."""
    if path is None:
        return
    with _ANCHOR_LOCK:
        _ANCHORS.add(os.path.abspath(os.fspath(path)))


def _anchor_for(directory: str) -> str:
    best: str | None = None
    with _ANCHOR_LOCK:
        anchors = list(_ANCHORS)
    for anchor in anchors:
        if directory == anchor or directory.startswith(anchor.rstrip(os.sep) + os.sep):
            if best is None or len(anchor) > len(best):
                best = anchor
    return best if best is not None else directory


_DIR_FD_SUPPORTED = bool(
    getattr(os, "O_NOFOLLOW", 0)
    and getattr(os, "O_DIRECTORY", 0)
    and {os.open, os.rename, os.unlink, os.mkdir, os.rmdir, os.link, os.stat,
         os.readlink, os.symlink, os.chmod} <= os.supports_dir_fd
)


def dir_fd_supported() -> bool:
    return _DIR_FD_SUPPORTED


@contextlib.contextmanager
def open_dir(directory: Path, *, create: bool = False) -> Iterator[int]:
    """A descriptor of `directory`, reached from its trusted anchor without
    following any symlink below the anchor (missing components are created
    when `create`). Raises FileNotFoundError when a component is missing,
    SkillError for a symlink / non-directory on the way or an unsupported
    platform."""
    if not dir_fd_supported():
        raise SkillError(
            "This platform cannot open directories without following symlinks; "
            "refusing to change skill files"
        )
    target = os.path.abspath(os.fspath(directory))
    anchor = _anchor_for(target)
    relative = os.path.relpath(target, anchor)
    parts = [] if relative == "." else relative.split(os.sep)
    fd = os.open(anchor, os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_CLOEXEC", 0))
    try:
        walked = anchor
        for part in parts:
            if part in {"", ".", ".."}:
                raise SkillError(f"Unsafe path component in {target}")
            walked = os.path.join(walked, part)
            try:
                next_fd = os.open(part, _NOFOLLOW_DIR_FLAGS, dir_fd=fd)
            except FileNotFoundError:
                if not create:
                    raise
                with contextlib.suppress(FileExistsError):
                    os.mkdir(part, 0o777, dir_fd=fd)
                next_fd = _open_child_dir(fd, part, walked)
            except OSError as exc:
                if exc.errno in (errno.ELOOP, errno.ENOTDIR, errno.EMLINK):
                    raise SkillError(
                        f"Refusing to follow a symlink or non-directory: {walked}"
                    ) from exc
                raise
            os.close(fd)
            fd = next_fd
        yield fd
    finally:
        os.close(fd)


def _open_child_dir(fd: int, part: str, walked: str) -> int:
    try:
        return os.open(part, _NOFOLLOW_DIR_FLAGS, dir_fd=fd)
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.ENOTDIR, errno.EMLINK):
            raise SkillError(f"Refusing to follow a symlink or non-directory: {walked}") from exc
        raise


def _rename_noreplace_at(src_fd: int, src: str, dst_fd: int, dst: str) -> None:
    """No-replace rename between two directory descriptors (see
    `rename_noreplace`)."""
    import ctypes
    import stat as stat_module

    native = _native_noreplace()
    if native:
        result = native(src_fd, os.fsencode(src), dst_fd, os.fsencode(dst))
        if result == 0:
            return
        code = ctypes.get_errno()
        if code not in (errno.ENOSYS, errno.EINVAL, errno.ENOTSUP, getattr(errno, "EOPNOTSUPP", 0)):
            raise OSError(code, os.strerror(code), dst)
        # The filesystem does not support it: fall through.
    info = os.stat(src, dir_fd=src_fd, follow_symlinks=False)
    if not stat_module.S_ISREG(info.st_mode):
        raise SkillError(
            f"Cannot move {src} to {dst} without risking a replace on this filesystem; "
            "left in place"
        )
    try:
        if os.link in os.supports_follow_symlinks:
            os.link(src, dst, src_dir_fd=src_fd, dst_dir_fd=dst_fd, follow_symlinks=False)
        else:
            os.link(src, dst, src_dir_fd=src_fd, dst_dir_fd=dst_fd)
    except FileExistsError:
        raise
    except OSError as exc:
        if exc.errno == errno.EXDEV:
            raise
        raise SkillError(
            f"Cannot move {src} to {dst} without risking a replace on this filesystem "
            f"({exc.strerror}); left in place"
        ) from exc
    os.unlink(src, dir_fd=src_fd)


def rename_noreplace(source: Path, destination: Path) -> None:
    """Atomically rename the entry `source` (a file or a symlink, never
    followed) to `destination`, failing with FileExistsError instead of
    replacing anything there. Both parents are reached by `open_dir` (no
    symlink below the trusted anchor is ever followed).

    Uses the native no-replace rename where the platform has one; otherwise
    a hard link + unlink (equally atomic and no-clobber) for a regular file.
    Where neither exists (e.g. a symlink on a filesystem without native
    support, or no hard links) it REFUSES (SkillError) rather than race a
    check-then-replace. EXDEV (another filesystem) surfaces as OSError."""
    source, destination = Path(source), Path(destination)
    with open_dir(source.parent) as src_fd, open_dir(destination.parent) as dst_fd:
        _rename_noreplace_at(src_fd, source.name, dst_fd, destination.name)


def _rename_entry(source: Path, destination: Path) -> None:
    """Rename the DIRECTORY ENTRY `source` (a file or a symlink, which is
    never followed) to a FRESH `destination` (inside a transaction dir).
    Both parents are reached by `open_dir`, and the rename is relative to
    those descriptors, so a parent (or any directory above it, up to the
    trusted anchor) swapped for a symlink after validation cannot redirect
    it."""
    source, destination = Path(source), Path(destination)
    with open_dir(source.parent) as src_fd, open_dir(destination.parent) as dst_fd:
        os.rename(source.name, destination.name, src_dir_fd=src_fd, dst_dir_fd=dst_fd)


def unlink_at(path: Path) -> None:
    """Unlink the entry `path` (never following it) through `open_dir`."""
    path = Path(path)
    with open_dir(path.parent) as fd:
        os.unlink(path.name, dir_fd=fd)


def make_dirs_at(directory: Path) -> None:
    """Create `directory` (and missing parents) through `open_dir`: never
    through a symlink below the trusted anchor."""
    with open_dir(Path(directory), create=True):
        pass


def make_temp_at(directory: Path, prefix: str) -> Path:
    """Create an empty, exclusively claimed temporary file in `directory`
    (reached through `open_dir`) and return its path."""
    directory = Path(directory)
    flags = (
        os.O_WRONLY | os.O_CREAT | os.O_EXCL
        | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    )
    with open_dir(directory, create=True) as dir_fd:
        for _attempt in range(32):
            name = f"{prefix}{uuid.uuid4().hex[:12]}"
            try:
                os.close(os.open(name, flags, 0o600, dir_fd=dir_fd))
            except FileExistsError:
                continue
            return directory / name
    raise SkillError(f"Could not create a temporary file in {directory}")


def readlink_at(path: Path) -> str:
    path = Path(path)
    with open_dir(path.parent) as fd:
        return os.readlink(path.name, dir_fd=fd)


def symlink_at(target: str, path: Path) -> None:
    """Create the symlink `path` -> `target` (fails if anything is there)."""
    path = Path(path)
    with open_dir(path.parent) as fd:
        os.symlink(target, path.name, dir_fd=fd)


def _sha256_at(path: Path) -> str | None:
    """Hash of the regular file `path`, opened through `open_dir` with
    O_NOFOLLOW (a symlink or a swapped directory is never followed); None
    when it is missing or not a regular file."""
    import hashlib
    import stat as stat_module

    path = Path(path)
    try:
        with open_dir(path.parent) as dir_fd:
            fd = os.open(
                path.name,
                os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0),
                dir_fd=dir_fd,
            )
    except (OSError, SkillError):
        return None
    try:
        info = os.fstat(fd)
        if not stat_module.S_ISREG(info.st_mode):
            return None
        if info.st_size > MAX_HASH_BYTES:
            return _oversize_marker(info.st_size)
        digest = hashlib.sha256()
        while True:
            chunk = os.read(fd, _HASH_CHUNK_BYTES)
            if not chunk:
                break
            digest.update(chunk)
        return "sha256:" + digest.hexdigest()
    finally:
        os.close(fd)


def copy_mode_at(source: Path, destination: Path) -> None:
    """Copy the permission bits of `source` (never followed) onto the
    regular file `destination`, both reached through `open_dir`."""
    import stat as stat_module

    source, destination = Path(source), Path(destination)
    with open_dir(source.parent) as src_fd, open_dir(destination.parent) as dst_fd:
        mode = os.stat(source.name, dir_fd=src_fd, follow_symlinks=False).st_mode
        os.chmod(destination.name, stat_module.S_IMODE(mode), dir_fd=dst_fd)


# Lock order for every skill mutation: scope lock -> operation lock ->
# hosted-state lock. With SESSIONFS_LOCK_ORDER_CHECK=1 (tests) acquiring a
# lower-ranked lock while a higher-ranked one is held raises AssertionError.
LOCK_RANKS = {"scope": 1, "operation": 2, "hosted": 3}
_LOCK_STACK = threading.local()


@contextlib.contextmanager
def ordered_lock(kind: str) -> Iterator[None]:
    rank = LOCK_RANKS[kind]
    stack: list[int] = getattr(_LOCK_STACK, "stack", None) or []
    _LOCK_STACK.stack = stack
    if os.environ.get("SESSIONFS_LOCK_ORDER_CHECK") and stack and rank < max(stack):
        names = {value: key for key, value in LOCK_RANKS.items()}
        raise AssertionError(
            f"skill lock order violation: {kind} acquired while holding "
            f"{[names[item] for item in stack]}"
        )
    stack.append(rank)
    try:
        yield
    finally:
        stack.pop()


def make_durable_dirs(directory: Path, stop: Path | None = None) -> None:
    """Create `directory` (and any missing parents) so that it survives a
    crash: the NEAREST PRE-EXISTING ancestor is recorded before anything is
    created, then every created directory and that ancestor (which holds the
    first new entry) are fsynced.

    Below `stop` (the store root) no component may be a symlink or a
    non-directory."""
    directory = Path(directory)
    if stop is not None:
        try:
            relative = directory.relative_to(stop)
        except ValueError:
            relative = None
        if relative is not None:
            current = Path(stop)
            for part in relative.parts:
                current = current / part
                if is_link_or_reparse(current):
                    raise SkillError(f"Refusing to write through a symlink: {current}")
                if current.exists() and not current.is_dir():
                    raise SkillError(f"Not a directory: {current}")
    anchor = directory
    missing: list[Path] = []
    while not anchor.is_dir():
        if is_link_or_reparse(anchor) or anchor.exists():
            raise SkillError(f"Not a directory: {anchor}")
        missing.append(anchor)
        if anchor.parent == anchor:
            break
        anchor = anchor.parent
    for created in reversed(missing):
        try:
            if dir_fd_supported():
                # Relative to the parent's descriptor (`open_dir`): never
                # through a directory swapped for a symlink.
                with open_dir(created.parent) as parent_fd:
                    os.mkdir(created.name, 0o777, dir_fd=parent_fd)
            else:
                created.mkdir()
        except FileExistsError:
            if is_link_or_reparse(created) or not created.is_dir():
                raise SkillError(f"Not a directory: {created}") from None
    for created in missing:  # deepest first, then the pre-existing anchor
        _fsync_directory(created)
    _fsync_directory(anchor)


# Removed (clean) outputs are moved into `<store>/trash/<package>/<stamp>/`
# rather than deleted, and pruned after this long.
TRASH_RETENTION_SECONDS = 7 * 24 * 3600
_STAMP_FORMAT = "%Y%m%dT%H%M%S%fZ"


def park_stamp() -> str:
    """Unique, sortable directory name for one removal's trash/quarantine."""
    return f"{datetime.now(timezone.utc).strftime(_STAMP_FORMAT)}-{uuid.uuid4().hex[:8]}"


def safe_component(value: str) -> str:
    cleaned = "".join(char if char.isalnum() or char in "._-" else "_" for char in value)
    return cleaned.lstrip(".")[:120] or "_"


def trash_root(store_root: Path) -> Path:
    return Path(store_root) / "trash"


def _stamp_age(name: str, now: float) -> float | None:
    try:
        stamp = datetime.strptime(name.split("-", 1)[0], _STAMP_FORMAT).replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    return now - stamp.timestamp()


def prune_trash(store_root: Path, *, now: float | None = None) -> int:
    """Delete trash entries older than TRASH_RETENTION_SECONDS (by the stamp
    in their name; the mtime for anything else). Best effort; returns the
    number of entries pruned. Symlinks are never followed."""
    root = trash_root(store_root)
    if is_link_or_reparse(root) or not root.is_dir():
        return 0
    now = time.time() if now is None else now
    pruned = 0
    for package_dir in root.iterdir():
        if is_link_or_reparse(package_dir) or not package_dir.is_dir():
            continue
        for entry in package_dir.iterdir():
            age = _stamp_age(entry.name, now)
            if age is None:
                try:
                    age = now - entry.lstat().st_mtime
                except OSError:
                    continue
            if age < TRASH_RETENTION_SECONDS:
                continue
            if is_link_or_reparse(entry) or not entry.is_dir():
                with contextlib.suppress(OSError):
                    entry.unlink()
            else:
                shutil.rmtree(entry, ignore_errors=True)
            pruned += 1
        with contextlib.suppress(OSError):
            package_dir.rmdir()  # only when empty
    return pruned


def trash_usage(store_root: Path) -> tuple[int, int]:
    """(files, bytes) currently held in the trash; symlinks count as files
    and are never followed."""
    root = trash_root(store_root)
    if is_link_or_reparse(root) or not root.is_dir():
        return 0, 0
    files = size = 0
    for dirpath, _dirnames, filenames in os.walk(root, followlinks=False):
        for name in filenames:
            with contextlib.suppress(OSError):
                size += os.lstat(os.path.join(dirpath, name)).st_size
                files += 1
    return files, size


def copy_backup(source: Path, destination: Path) -> None:
    """Copy a file that is about to be overwritten or unlinked and make the copy
    durable BEFORE the (fsynced) journal that references it is written: on a
    power loss the journal must never point at a backup that does not exist.

    Both ends are opened relative to `open_dir` descriptors, the source with
    O_NOFOLLOW (a swapped directory or a final symlink is never followed)."""
    if not dir_fd_supported():
        shutil.copyfile(source, destination)
        _fsync_file(destination)
        return
    source, destination = Path(source), Path(destination)
    nofollow = getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    with open_dir(source.parent) as src_dir, open_dir(destination.parent) as dst_dir:
        src = os.open(source.name, os.O_RDONLY | nofollow, dir_fd=src_dir)
        try:
            dst = os.open(
                destination.name, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | nofollow, 0o600,
                dir_fd=dst_dir,
            )
            try:
                while True:
                    chunk = os.read(src, 1024 * 1024)
                    if not chunk:
                        break
                    view = memoryview(chunk)
                    while view:
                        written = os.write(dst, view)
                        view = view[written:]
                os.fsync(dst)
            finally:
                os.close(dst)
        finally:
            os.close(src)


def _lock_with_msvcrt(fd: int, msvcrt: Any) -> None:
    """Bounded retry around Windows byte-range locking (see LOCK_WAIT_SECONDS)."""
    deadline = _lock_clock() + LOCK_WAIT_SECONDS
    while True:
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            return
        except OSError as exc:
            if _lock_clock() >= deadline:
                raise SkillError(
                    f"Timed out after {LOCK_WAIT_SECONDS:.0f}s waiting for the skill lock; "
                    "another sfs skill operation is still running"
                ) from exc
            _lock_sleep(_LOCK_RETRY_SECONDS)


@contextlib.contextmanager
def interprocess_lock(path: Path) -> Iterator[None]:
    """Hold one scope lock on POSIX and Windows.

    The lock file is opened without following symlinks (O_NOFOLLOW where the
    platform has it, an explicit link check otherwise), its parent directory
    must not be a symlink, and no bytes are ever written to it: the lock is
    taken on the descriptor only. A pre-planted symlink at the lock path can
    therefore never make us touch an arbitrary user-writable file.
    """
    parent = path.parent
    if is_link_or_reparse(parent):
        raise SkillError(f"Lock directory cannot be a symlink: {parent}")
    parent.mkdir(parents=True, exist_ok=True)
    if is_link_or_reparse(path):
        raise SkillError(f"Lock file cannot be a symlink: {path}")
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(path, flags, 0o600)
    except OSError as exc:
        raise SkillError(f"Cannot open lock file {path}: {exc}") from exc
    try:
        if is_link_or_reparse(path) or not os.path.isfile(path):
            raise SkillError(f"Lock file cannot be a symlink: {path}")
        try:
            import fcntl
        except ImportError:  # pragma: no cover - exercised on Windows CI
            import msvcrt

            _lock_with_msvcrt(fd, msvcrt)
            try:
                yield
            finally:
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        else:
            fcntl.flock(fd, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def _emission_hash(source: Path, emission: Any) -> str | None:
    """Hash of the bytes that will actually be written: the source bytes with
    the emission's text substitutions applied (none for plain copies)."""
    if not getattr(emission, "substitutions", ()):
        return _sha256_file(source)
    import hashlib

    try:
        if is_link_or_reparse(source) or not source.is_file():
            return None
        return "sha256:" + hashlib.sha256(emission.transform(source.read_bytes())).hexdigest()
    except (OSError, UnicodeDecodeError):
        return None


def _oversize_marker(size: int) -> str:
    """Disclosed stand-in for the hash of a file too large to hash.

    Never equal to a recorded ``sha256:`` output hash, so the output is
    treated as dirty (kept, never overwritten or unlinked) and the reason is
    visible wherever the actual hash is reported.
    """
    return f"unhashed:file exceeds the {MAX_HASH_BYTES}-byte hashing limit ({size} bytes)"


def _sha256_file(path: Path) -> str | None:
    import hashlib

    try:
        if is_link_or_reparse(path) or not path.is_file():
            return None
        size = path.stat().st_size
        if size > MAX_HASH_BYTES:
            return _oversize_marker(size)
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            while True:
                chunk = handle.read(_HASH_CHUNK_BYTES)
                if not chunk:
                    break
                digest.update(chunk)
        return "sha256:" + digest.hexdigest()
    except OSError:
        return None


def _restore_backup(destination: Path, backup: Path | None) -> None:
    if backup and (is_link_or_reparse(backup) or is_link_or_reparse(destination)):
        raise SkillError(f"Refusing to restore through a symlink: {destination}")
    if backup is not None:
        # A recorded backup means the destination EXISTED before the
        # transaction. If the copy is gone, unlinking would destroy a
        # pre-existing file: stop and leave everything (journal included)
        # for manual recovery instead.
        if not backup.is_file():
            raise SkillError(
                f"Transaction backup is missing for {destination} (expected at {backup}); "
                "the file was left untouched - manual recovery required: restore it by "
                "hand, then delete skills.journal.json and the .skills-txn-* directory"
            )
        if not dir_fd_supported():
            destination.parent.mkdir(parents=True, exist_ok=True)
            tmp = destination.with_name(destination.name + f".restore-{os.getpid()}")
            shutil.copyfile(backup, tmp)
            os.replace(tmp, destination)
            return
        # Every step relative to directory descriptors (`open_dir`).
        tmp = make_temp_at(destination.parent, f".{destination.name}.restore-")
        try:
            copy_backup(backup, tmp)
            with open_dir(destination.parent) as fd:
                os.rename(tmp.name, destination.name, src_dir_fd=fd, dst_dir_fd=fd)
        except BaseException:
            with contextlib.suppress(OSError, SkillError):
                unlink_at(tmp)
            raise
        return
    try:
        if dir_fd_supported():
            unlink_at(destination)
        else:
            destination.unlink()
    except FileNotFoundError:
        pass


class SkillStateStore:
    def __init__(
        self,
        state_root: Path,
        *,
        scope_root: Path | None = None,
        allowed_output_roots: tuple[Path, ...] | None = None,
        store_root: Path | None = None,
    ) -> None:
        self.root = state_root
        # The content store: dropped/replaced outputs are parked in its
        # trash (`<store>/trash/<package>/<stamp>/`); default: the state root.
        self.store_root = store_root
        self.scope_root = scope_root or state_root.parent
        # Trusted anchors every directory walk starts from (see `open_dir`).
        register_anchor(self.scope_root)
        register_anchor(self.store_root)
        self.allowed_output_roots = allowed_output_roots or (state_root.parent,)
        self.state_path = state_root / "skills.state.json"
        self.lock_path = state_root / "skills.lock.json"
        self.journal_path = state_root / "skills.journal.json"
        self.mutex_path = state_root / "skills.lock"

    @staticmethod
    def _lexical(path: Path) -> Path:
        return Path(os.path.abspath(os.fspath(path)))

    @staticmethod
    def _assert_no_symlink_components(root: Path, path: Path) -> None:
        if is_link_or_reparse(root):
            raise SkillError(f"Managed root is a symlink: {root}")
        current = root
        try:
            relative = path.relative_to(root)
        except ValueError as exc:
            raise SkillError(f"Managed path escapes its declared root: {path}") from exc
        for part in relative.parts:
            current /= part
            if is_link_or_reparse(current):
                raise SkillError(f"Managed path traverses a symlink: {current}")
            if current.exists() and current != path and not current.is_dir():
                raise SkillError(f"Managed path parent is not a directory: {current}")

    def _validate_state_root(self) -> None:
        root = self._lexical(self.root)
        scope = self._lexical(self.scope_root)
        self._assert_no_symlink_components(scope, root)

    def _validate_output(self, path: Path, *, allow_final_link: bool = False) -> Path:
        """Validate a managed output path: inside an adapter root, no symlink
        on the way. `allow_final_link` (removal/quarantine only) accepts a
        symlink as the FINAL entry - it is then moved as a directory entry,
        never followed - while every parent must still be a real directory."""
        destination = self._lexical(path)
        scope = self._lexical(self.scope_root)
        for raw_root in self.allowed_output_roots:
            root = self._lexical(raw_root)
            try:
                destination.relative_to(root)
            except ValueError:
                continue
            self._assert_no_symlink_components(scope, root)
            if allow_final_link and destination != root:
                self._assert_no_symlink_components(root, destination.parent)
            else:
                self._assert_no_symlink_components(root, destination)
            return destination
        raise SkillError(f"Managed output is outside adapter-owned roots: {path}")

    def _output_root(self, path: Path) -> Path:
        destination = self._lexical(path)
        for raw_root in self.allowed_output_roots:
            root = self._lexical(raw_root)
            try:
                destination.relative_to(root)
            except ValueError:
                continue
            return root
        raise SkillError(f"Managed output is outside adapter-owned roots: {path}")

    @staticmethod
    def _owner_by_path(
        packages: dict[str, Any], *, exclude: set[str] | None = None
    ) -> dict[str, tuple[str, str, str]]:
        """Map every recorded output path to (owner package id, output hash, owner name)."""
        owner_by_path: dict[str, tuple[str, str, str]] = {}
        for owner_id, row in packages.items():
            if not isinstance(row, dict) or (exclude and owner_id in exclude):
                continue
            owner_name = str(row.get("name") or "")
            for output in row.get("outputs") or []:
                if isinstance(output, dict) and isinstance(output.get("path"), str):
                    owner_by_path[output["path"]] = (
                        owner_id,
                        str(output.get("output_hash", "")),
                        owner_name,
                    )
        return owner_by_path

    @staticmethod
    def _carry_provenance(
        package_row: dict[str, Any], package: PackageRecord, existing: dict[str, Any] | None
    ) -> None:
        """Row-local safety provenance: the import's own (written atomically
        with this install) or - for a re-apply of the SAME content from the
        cache (compile, sync of an existing row) - the row's existing one."""
        if package.provenance is not None:
            return
        if (
            isinstance(existing, dict)
            and isinstance(existing.get("provenance"), dict)
            and existing.get("content_hash") == package.content_hash
        ):
            package_row["provenance"] = dict(existing["provenance"])

    def clear_digest_restriction(self, package_id: str) -> bool:
        """Durably lift a digest-origin restriction (explicit widening
        approval). Atomic under the state mutex; True when a row changed."""
        self._validate_state_root()
        with interprocess_lock(self.mutex_path):
            self._recover_locked()
            state = self.state()
            row = state["packages"].get(package_id)
            if not isinstance(row, dict) or "digest_authorized_origins" not in row:
                return False
            row.pop("digest_authorized_origins", None)
            row["revision"] = self._revision(row) + 1
            atomic_json(self.state_path, state)
            return True

    @staticmethod
    def _receipts(
        package: PackageRecord,
        plans: list[EmitPlan],
        existing: dict[str, Any] | None,
        consented_capabilities: set[str] | None,
        consent_context: dict[str, Any] | None,
    ) -> list[dict[str, Any]]:
        """Local consent receipts for this exact content.

        Receipts for the SAME content are kept. An update that carries consent
        forward under the D2 rule (marketplace v2 §3.4: a text update, or the
        unchanged capabilities of a code update) writes NEW receipts for the new
        content that name the receipt they were carried from, so the audit
        trail shows the user never saw these bytes and consent was inherited
        under ``consent_rule_version``.
        """
        from sessionfs.skills.disclosure import receipt_valid_for

        same_content = (
            isinstance(existing, dict) and existing.get("content_hash") == package.content_hash
        )
        context = consent_context or {}
        if isinstance(existing, dict):
            prior = existing.get("consent_receipts", [])
        else:
            # A replacement under a new package id (locked sync of a
            # content-bound id): the selected predecessor's receipts.
            prior = context.get("predecessor_receipts") or []
        prior_receipts = [item for item in prior if isinstance(item, dict)]
        receipts = list(prior_receipts) if same_content else []
        carried = set(context.get("carried_requirements") or [])
        digest = context.get("capability_digest")
        rule = int(context.get("consent_rule_version") or 2)
        # Kept receipts stay as history, but only VALID ones (this content
        # hash, this digest, this rule) suppress minting: explicit consent on
        # legacy content mints a digest-bound receipt.
        covered = {
            (item.get("capability"), item.get("tool"), item.get("scope"))
            for item in receipts
            if not digest
            or receipt_valid_for(item, content_hash=package.content_hash, digest=digest)
        }
        # Carry sources: receipts valid for the PREVIOUS content and its
        # baseline digest, for this exact (capability, tool, scope) or an
        # unrestricted (tool None) one in the same scope.
        baseline_hash = context.get("baseline_content_hash")
        baseline_digest = context.get("baseline_digest")
        valid_sources = [
            item for item in prior_receipts
            if receipt_valid_for(item, content_hash=baseline_hash, digest=baseline_digest)
        ]

        def source_for(capability: str, tool: str, scope: str) -> dict[str, Any] | None:
            exact = [i for i in valid_sources if (i.get("capability"), i.get("tool"), i.get("scope")) == (capability, tool, scope)]
            if exact:
                return sorted(exact, key=lambda i: str(i.get("id")))[0]
            wide = [i for i in valid_sources if i.get("capability") == capability and i.get("tool") is None and i.get("scope") == scope]
            return sorted(wide, key=lambda i: str(i.get("id")))[0] if wide else None
        for capability in sorted(consented_capabilities or set()):
            for plan in plans:
                key = (capability, plan.tool, plan.scope)
                if key in covered:
                    continue
                receipt: dict[str, Any] = {
                    "id": "scr_" + uuid.uuid4().hex,
                    "content_hash": package.content_hash,
                    "capability": capability,
                    "tool": plan.tool,
                    "scope": plan.scope,
                    "actor": "local-user",
                    "created_at": datetime.now(timezone.utc).isoformat(),
                    "consent_rule_version": rule,
                }
                if digest:
                    receipt["capability_digest"] = digest
                source = (
                    source_for(capability, plan.tool, plan.scope)
                    if not same_content and capability in carried
                    else None
                )
                if source is not None:
                    receipt["carried_forward_from_receipt_id"] = source.get("id")
                    receipt["actor"] = "carried-forward"
                receipts.append(receipt)
        return receipts

    @staticmethod
    def _consent_snapshot(
        package_row: dict[str, Any],
        package: PackageRecord,
        existing: dict[str, Any] | None,
        consent_context: dict[str, Any] | None,
    ) -> None:
        """The consented capability set (access manifest + digest): the
        baseline the next update is classified against. A re-apply of the
        same content keeps the row's existing snapshot."""
        context = consent_context or {}
        manifest = context.get("capability_manifest")
        same_content = (
            isinstance(existing, dict) and existing.get("content_hash") == package.content_hash
        )
        if isinstance(manifest, dict):
            package_row["capability_manifest"] = manifest
            package_row["capability_digest"] = context.get("capability_digest")
            package_row["consent_rule_version"] = int(context.get("consent_rule_version") or 2)
            if context.get("update_class"):
                package_row["last_update_class"] = context.get("update_class")
            elif same_content and isinstance(existing, dict) and "last_update_class" in existing:
                package_row["last_update_class"] = existing["last_update_class"]
        elif same_content and isinstance(existing, dict):
            for key in ("capability_manifest", "capability_digest", "consent_rule_version", "last_update_class"):
                if key in existing:
                    package_row[key] = existing[key]
        # Origins allowed to authorize tools for a widening applied through
        # bound digests (None / absent = every origin). Set by the transition
        # that installed this content; kept across re-applies of it.
        if "digest_authorized_origins" in context:
            value = context.get("digest_authorized_origins")
            if isinstance(value, list):
                package_row["digest_authorized_origins"] = [str(item) for item in value]
            else:
                package_row.pop("digest_authorized_origins", None)
        elif same_content and isinstance(existing, dict) and isinstance(
            existing.get("digest_authorized_origins"), list
        ):
            package_row["digest_authorized_origins"] = list(existing["digest_authorized_origins"])

    @staticmethod
    def _install_id(existing: dict[str, Any] | None) -> str:
        """Immutable id of ONE installation: kept while the row lives (updates,
        compiles, migrations), new after a remove + re-add - so a decision
        made about an earlier installation can never act on a reinstall."""
        if isinstance(existing, dict):
            value = existing.get("install_id")
            if isinstance(value, str) and value:
                return value
        return "sfi_" + uuid.uuid4().hex

    @staticmethod
    def _revision(existing: dict[str, Any] | None) -> int:
        """Monotonic per-row revision (legacy rows without one are 0)."""
        if not isinstance(existing, dict):
            return 0
        value = existing.get("revision", 0)
        return int(value) if isinstance(value, int) and not isinstance(value, bool) else 0

    @staticmethod
    def _previous_package_ids(
        existing: dict[str, Any] | None, previous_id: str | None, package_id: str
    ) -> list[str]:
        """Ordered, de-duplicated ids this row has been known under (newest last),
        never including the row's current id."""
        ids: list[str] = []
        if isinstance(existing, dict):
            for item in existing.get("previous_package_ids") or []:
                if isinstance(item, str) and item and item not in ids:
                    ids.append(item)
        if previous_id and previous_id not in ids:
            ids.append(previous_id)
        return [item for item in ids if item != package_id]

    @staticmethod
    def _exceeds_hash_limit(path: Path) -> bool:
        try:
            return (
                not is_link_or_reparse(path)
                and path.is_file()
                and path.stat().st_size > MAX_HASH_BYTES
            )
        except OSError:
            return False

    @staticmethod
    def _assert_not_owned_by_other(
        owner: tuple[str, str, str] | None, package_id: str, destination: Path
    ) -> None:
        if owner is not None and owner[0] != package_id:
            label = f"{owner[0]} ({owner[2]!r})" if owner[2] else owner[0]
            raise SkillError(
                f"Output is owned by package {label}: {destination}; "
                "remove that package first or choose different targets"
            )

    def _prune_empty_parents(self, removed: list[Path]) -> list[str]:
        """Remove now-empty directories above each removed file, stopping at
        (never including) the adapter root. Only empty, non-symlink directories
        are removed, so a directory holding a user's dirty file survives."""
        pruned: list[str] = []
        for path in removed:
            root = self._output_root(path)
            current = path.parent
            while current != root and root in current.parents:
                try:
                    # rmdir relative to the parent's descriptor (`open_dir`):
                    # it only ever removes an EMPTY real directory there.
                    with open_dir(current.parent) as fd:
                        os.rmdir(current.name, dir_fd=fd)
                except (OSError, SkillError):
                    break
                pruned.append(str(current))
                current = current.parent
        return pruned

    def _validate_transaction_path(self, path: Path, tx_dir: Path) -> Path:
        candidate = self._lexical(path)
        transaction_root = self._lexical(tx_dir)
        try:
            candidate.relative_to(transaction_root)
        except ValueError as exc:
            raise SkillError(f"Recovery path escapes its transaction directory: {path}") from exc
        self._assert_no_symlink_components(transaction_root, candidate)
        return candidate

    def state(self) -> dict[str, Any]:
        self._validate_state_root()
        value = read_json(self.state_path, {"version": 1, "packages": {}})
        if not isinstance(value, dict) or not isinstance(value.get("packages"), dict):
            raise SkillError("skills.state.json must contain a packages object")
        return value

    def desired(self) -> dict[str, Any]:
        self._validate_state_root()
        value = read_json(self.lock_path, {"version": 1, "packages": {}})
        if not isinstance(value, dict) or not isinstance(value.get("packages"), dict):
            raise SkillError("skills.lock.json must contain a packages object")
        return value

    def _recover_locked(self) -> None:
        self._validate_state_root()
        if not self.journal_path.exists():
            return
        journal = read_json(self.journal_path, {})
        if not isinstance(journal, dict):
            raise SkillError("Invalid skill transaction journal")
        tx_dir_raw = journal.get("tx_dir")
        if not isinstance(tx_dir_raw, str):
            raise SkillError("Skill transaction journal has no recovery directory")
        tx_dir = self._lexical(Path(tx_dir_raw))
        expected_parent = self._lexical(self.root)
        if tx_dir.parent != expected_parent or not tx_dir.name.startswith(".skills-txn-"):
            raise SkillError("Skill transaction journal has an unsafe recovery directory")
        if journal.get("phase") != "committed":
            created: list[Path] = []
            all_actions = journal.get("actions") or []
            # Writes are undone first (they are journaled after the drops),
            # then every dropped/replaced output is put back from wherever it
            # had got to: the staged sibling, the transaction dir or its
            # parking place (trash/quarantine).
            for action in reversed(all_actions):
                if not isinstance(action, dict) or not isinstance(action.get("destination"), str):
                    continue
                if isinstance(action.get("moved"), str):
                    self._recover_drop(journal, action, tx_dir)
                    continue
                destination = self._validate_output(Path(action["destination"]))
                if "written_hash" in action:
                    # A write into an empty place (or the place of a dropped/
                    # replaced output). The file is ours only if it holds the
                    # planned bytes AND, when paired, that output had been
                    # moved away (drops precede writes).
                    pair = action.get("after_drop")
                    if pair is not None:
                        paired = (
                            all_actions[pair]
                            if isinstance(pair, int) and not isinstance(pair, bool)
                            and 0 <= pair < len(all_actions)
                            else None
                        )
                        if not isinstance(paired, dict) or not isinstance(paired.get("moved"), str):
                            raise SkillError("Skill transaction journal has an invalid write pairing")
                        if not self._drop_happened(journal, paired, tx_dir):
                            continue
                    if _sha256_at(destination) == action.get("written_hash"):
                        with contextlib.suppress(FileNotFoundError):
                            unlink_at(destination)
                        created.append(destination)
                    continue
                backup_raw = action.get("backup")
                backup = (
                    self._validate_transaction_path(Path(backup_raw), tx_dir)
                    if isinstance(backup_raw, str)
                    else None
                )
                _restore_backup(destination, backup)
                if backup is None:
                    created.append(destination)
            # Files that did not exist before the transaction are gone again;
            # drop the directory skeletons the aborted writes created.
            self._prune_empty_parents(created)
            for key, path in (("state_backup", self.state_path), ("lock_backup", self.lock_path)):
                backup_raw = journal.get(key)
                existed = bool(journal.get(key + "_existed"))
                if existed and isinstance(backup_raw, str):
                    backup = self._validate_transaction_path(Path(backup_raw), tx_dir)
                    _restore_backup(path, backup)
                elif not existed:
                    try:
                        path.unlink()
                    except FileNotFoundError:
                        pass
        try:
            self.journal_path.unlink()
        except FileNotFoundError:
            pass
        shutil.rmtree(tx_dir, ignore_errors=True)

    def ensure_install_id(self, package_id: str, candidate: str | None = None) -> str | None:
        """The installed row's immutable install id, assigning one (under
        the state lock) to an older row that has none - `candidate` when
        given (already recorded elsewhere first). Three outcomes: the id
        (the candidate, or a DIFFERENT id already there); None ONLY when a
        successful read under the lock found no such row; SkillError when
        the state could not be read or written (nothing is concluded)."""
        self._validate_state_root()
        try:
            with interprocess_lock(self.mutex_path):
                self._recover_locked()
                state = self.state()
                row = state["packages"].get(package_id)
                if not isinstance(row, dict):
                    return None
                current = row.get("install_id")
                if isinstance(current, str) and current:
                    return current
                if current is not None:
                    raise SkillError(f"Installed row {package_id} has an invalid install id")
                row["install_id"] = candidate or self._install_id(None)
                atomic_json(self.state_path, state)
                return str(row["install_id"])
        except OSError as exc:
            raise SkillError(f"Could not read or update the skill state: {exc}") from exc

    def recover(self) -> None:
        self._validate_state_root()
        with interprocess_lock(self.mutex_path):
            self._recover_locked()

    def validate_root(self) -> None:
        """Validate the state path before an operation lock file is created."""
        self._validate_state_root()

    def apply(
        self,
        package: PackageRecord,
        plans: list[EmitPlan],
        *,
        consented_capabilities: set[str] | None = None,
        expected_current_hash: str | None = None,
        allow_existing_change: bool = False,
        replace_package_id: str | None = None,
        expected_revision: int | None = None,
        consent_context: dict[str, Any] | None = None,
        rotate_install_id: bool = False,
        omit_provenance: bool = False,
    ) -> dict[str, Any]:
        """`omit_provenance` writes the row WITHOUT provenance (a recovery
        restoring an installation that had none).

        `rotate_install_id` (an explicit user install) gives the row a NEW
        install id in the same commit, so every earlier pull record (its
        authority, its optional acceptance) is void even if no cleanup runs
        afterwards."""
        self._validate_state_root()
        with interprocess_lock(self.mutex_path):
            self._recover_locked()
            state = self.state()
            desired = self.desired()
            packages = state["packages"]
            # Content-bound local packages change id when their bytes change;
            # the previous row is migrated (outputs it owns become ours).
            previous_id = (
                replace_package_id
                if replace_package_id and replace_package_id != package.package_id
                else None
            )
            if previous_id is not None and previous_id in packages:
                if isinstance(packages.get(package.package_id), dict):
                    # Two distinct local packages converging on identical bytes
                    # would collapse into one row and orphan the other's outputs.
                    other = packages[package.package_id]
                    raise SkillError(
                        f"Cannot migrate {previous_id} to {package.package_id}: that id is already "
                        f"installed as {other.get('name')!r}; remove one of them first"
                    )
                packages[package.package_id] = packages.pop(previous_id)
                desired["packages"].pop(previous_id, None)
            existing = packages.get(package.package_id)
            previous_ids = self._previous_package_ids(existing, previous_id, package.package_id)
            if isinstance(existing, dict):
                # Prepared operations are fenced by the row REVISION as well as
                # the content hash: a target-changing compile with the same
                # hash that landed after planning must not be overwritten.
                if expected_revision is not None and self._revision(existing) != expected_revision:
                    raise SkillError(
                        f"Installed state changed since planning (revision "
                        f"{self._revision(existing)} != {expected_revision}); re-run the command: "
                        f"{package.package_id}"
                    )
                current_hash = existing.get("content_hash")
                if expected_current_hash is not None and current_hash != expected_current_hash:
                    raise SkillError(
                        f"Installed package changed concurrently: {package.package_id}"
                    )
                if (
                    expected_current_hash is None
                    and not allow_existing_change
                    and current_hash != package.content_hash
                ):
                    raise SkillError(
                        f"Package is already installed at another version; use update: "
                        f"{package.package_id}"
                    )
            elif expected_current_hash is not None:
                raise SkillError(f"Installed package disappeared concurrently: {package.package_id}")
            prior_outputs = existing.get("outputs", []) if isinstance(existing, dict) else []
            owner_by_path = self._owner_by_path(packages)
            emissions = [item for plan in plans for item in plan.emissions]
            new_paths = {item.destination for item in emissions}
            stale = [
                item for item in prior_outputs
                if isinstance(item, dict) and item.get("path") not in new_paths
            ]
            for emission in emissions:
                destination = self._validate_output(Path(emission.destination))
                source = self._lexical(Path(emission.source))
                package_root = self._lexical(package.package_root)
                try:
                    source.relative_to(package_root)
                except ValueError as exc:
                    raise SkillError(f"Planned source escapes package content: {source}") from exc
                self._assert_no_symlink_components(package_root, source)
                if _emission_hash(source, emission) != emission.output_hash:
                    raise SkillError(f"Planned source integrity mismatch: {source}")
                owner = owner_by_path.get(emission.destination)
                # Ownership lives in STATE, not on disk: a managed output that
                # was deleted by hand still belongs to its package, otherwise a
                # second package could claim the path and two rows would own
                # one file.
                self._assert_not_owned_by_other(owner, package.package_id, destination)
                if destination.exists():
                    if is_link_or_reparse(destination) or not destination.is_file():
                        raise SkillError(f"Refusing non-file output collision: {destination}")
                    if owner is None:
                        raise SkillError(f"Refusing unmanaged output collision: {destination}")
                    if _sha256_file(destination) != owner[1]:
                        raise SkillError(f"Refusing to overwrite modified managed output: {destination}")
            self.root.mkdir(parents=True, exist_ok=True)
            tx_dir = self.root / f".skills-txn-{uuid.uuid4().hex}"
            (tx_dir / "moved").mkdir(parents=True)
            # Every output that leaves a tool directory - a stale one this
            # package no longer plans, or an existing one about to be
            # replaced - is DROPPED rename-first (see `_drop`); the writes are
            # journaled after the drops. No output is unlinked or copied.
            park_dir = self._trash_dir(package.package_id)
            drops: dict[str, tuple[Path, Any, str, str]] = {}
            for item in stale:
                if isinstance(item.get("path"), str):
                    stale_path = self._validate_output(Path(item["path"]), allow_final_link=True)
                    drops.setdefault(
                        str(stale_path),
                        (stale_path, item.get("output_hash"), "stale", item["path"]),
                    )
            unchanged: set[str] = set()
            for emission in emissions:
                destination = self._validate_output(Path(emission.destination))
                if str(destination) in drops:
                    continue
                if destination.exists() or is_link_or_reparse(destination):
                    owner = owner_by_path.get(emission.destination)
                    if owner is not None and owner[1] == emission.output_hash:
                        # Same bytes already recorded there: left in place
                        # (not rewritten, so never dropped or trashed).
                        unchanged.add(str(destination))
                        continue
                    drops[str(destination)] = (
                        destination,
                        owner[1] if owner is not None else None,
                        "replace",
                        emission.destination,
                    )
            actions, drop_index = self._journal_drops_and_writes(
                tx_dir,
                {key: (park_dir, value[0], value[1]) for key, value in drops.items()},
                [
                    (path, item.output_hash)
                    for item in emissions
                    for path in [self._validate_output(Path(item.destination))]
                    if str(path) not in unchanged
                ],
            )
            journal: dict[str, Any] = {
                "version": 1,
                "phase": "applying",
                "tx_dir": str(tx_dir),
                "actions": actions,
                "state_backup_existed": self.state_path.exists(),
                "lock_backup_existed": self.lock_path.exists(),
            }
            if self.state_path.exists():
                state_backup = tx_dir / "skills.state.json"
                copy_backup(self.state_path, state_backup)
                journal["state_backup"] = str(state_backup)
            if self.lock_path.exists():
                lock_backup = tx_dir / "skills.lock.json"
                copy_backup(self.lock_path, lock_backup)
                journal["lock_backup"] = str(lock_backup)
            atomic_json(self.journal_path, journal)
            try:
                dirty_stale: list[str] = []
                for key, (path, _expected, kind, raw) in drops.items():
                    outcome = self._drop(path, actions[drop_index[key]])
                    if outcome == "dirty":
                        if kind == "replace":
                            # Edited after the preflight: never overwritten.
                            raise SkillError(
                                f"Refusing to overwrite modified managed output: {path}"
                            )
                        dirty_stale.append(raw)
                    elif outcome == "trashed" and kind == "stale":
                        self._prune_empty_parents([path])
                outputs: list[dict[str, Any]] = []
                recorded_outputs: dict[str, dict[str, Any]] = {}
                for plan in plans:
                    for emission in plan.emissions:
                        source = self._lexical(Path(emission.source))
                        destination = self._validate_output(Path(emission.destination))
                        # Tools sharing one native root (cursor + amp both use
                        # .agents/skills) plan the same output; write and record
                        # it once so removal never unlinks the same path twice.
                        previous = recorded_outputs.get(str(destination))
                        if previous is not None:
                            if previous["output_hash"] != emission.output_hash:
                                raise SkillError(
                                    f"Conflicting planned content for shared output: {destination}"
                                )
                            previous["tools"].append(plan.tool)
                            continue
                        if str(destination) not in unchanged:
                            self._write_output(source, destination, emission=emission)
                        record = {
                            "path": emission.destination,
                            "output_hash": emission.output_hash,
                            "tool": plan.tool,
                            "tools": [plan.tool],
                        }
                        recorded_outputs[str(destination)] = record
                        outputs.append(record)
                package_row = package.as_dict(include_path=False)
                if omit_provenance:
                    package_row.pop("provenance", None)
                else:
                    self._carry_provenance(package_row, package, existing)
                receipts = self._receipts(
                    package, plans, existing, consented_capabilities, consent_context
                )
                self._consent_snapshot(package_row, package, existing, consent_context)
                package_row.update(
                    {
                        "scope": plans[0].scope if plans else "project",
                        "targets": [item.tool for item in plans],
                        "plan_hashes": {item.tool: item.plan_hash for item in plans},
                        "degradations": [
                            value.as_dict() for plan in plans for value in plan.degradations
                        ],
                        "omissions": [value.as_dict() for plan in plans for value in plan.omissions],
                        "outputs": outputs,
                        "dirty_stale_outputs": dirty_stale,
                        "consent_receipts": receipts,
                        "consent_receipt_ids": [item["id"] for item in receipts],
                        # Ids this row was migrated from: cached versions imported
                        # under them are still this package's (rollback/doctor).
                        "previous_package_ids": previous_ids,
                        "revision": self._revision(existing) + 1,
                        # A NEW installation made by a pull operation takes
                        # the operation's token as its install id: proof for
                        # its crash recovery that this installation is its.
                        "install_id": (
                            package.pull_op
                            if package.pull_op and not isinstance(existing, dict)
                            else self._install_id(None if rotate_install_id else existing)
                        ),
                    }
                )
                packages[package.package_id] = package_row
                desired["packages"][package.package_id] = {
                    "package_id": package.package_id,
                    "name": package.name,
                    "content_hash": package.content_hash,
                    "source": package.source.as_dict(),
                    "scope": package_row["scope"],
                    "targets": package_row["targets"],
                    "required_capabilities": sorted(
                        {
                            requirement
                            for plan in plans
                            for requirement in plan.consent_requirements
                        }
                    ),
                }
                atomic_json(self.state_path, state)
                atomic_json(self.lock_path, desired)
                journal["phase"] = "committed"
                atomic_json(self.journal_path, journal)
            except Exception:
                self._recover_locked()
                raise
            self._recover_locked()
            return package_row

    def remove(
        self,
        package_id: str,
        *,
        expected_current_hash: str | None = None,
        quarantine_dir: Path | None = None,
        expected_revision: int | None = None,
        trash_dir: Path | None = None,
        park_root: Path | None = None,
    ) -> dict[str, Any]:
        """Remove a package's managed outputs.

        Every output is first RENAMED into the transaction directory and only
        then hashed, so an edit made after a check can never be unlinked on
        the strength of an earlier hash: the hashed bytes are exactly the
        ones that left the tool directory.

        Nothing that left a tool directory is ever deleted or copied by the
        removal itself: the moved inode is RENAMED onward ("parked") into a
        durable directory before the commit. Only when that directory is on
        another filesystem is it copied (fsynced), the moved entry staying in
        the transaction directory until the commit.

        Normal removal (`quarantine_dir=None`): clean outputs are parked in
        `trash_dir` (default `<root>/trash/<package>/<stamp>/`; pruned after
        TRASH_RETENTION_SECONDS) and a modified output is put back in place
        and reported as dirty.

        Quarantine (`quarantine_dir` set; used for BLOCKED content): every
        output, clean or modified, leaves the adapter roots and is parked in
        `quarantine_dir` (recoverable). The state row is dropped only when no
        output remains active; otherwise it is kept (with just the outputs
        still in place) so the removal is reported and retried.

        `park_root` (the store root) bounds the symlink check on the parking
        directories.
        """
        self._validate_state_root()
        with interprocess_lock(self.mutex_path):
            self._recover_locked()
            state = self.state()
            desired = self.desired()
            row = state["packages"].get(package_id)
            if not isinstance(row, dict):
                raise SkillError(f"Installed package not found: {package_id}")
            if (
                expected_current_hash is not None
                and row.get("content_hash") != expected_current_hash
            ) or (
                expected_revision is not None and self._revision(row) != expected_revision
            ):
                raise PackageChangedConcurrently(
                    f"Installed package changed concurrently: {package_id}"
                )
            if quarantine_dir is None and row.get("quarantine_pending") is True:
                # A normal removal would put a modified BLOCKED output back
                # and drop the row tracking it: only quarantine finishes it.
                raise SkillError(
                    f"{row.get('name') or package_id} is blocked and its quarantine is not "
                    "finished; run `sfs skill pull` to move the remaining files out"
                )
            outputs: list[tuple[Path, Any]] = []
            for output in self._unique_outputs(row):
                path = self._validate_output(Path(output["path"]), allow_final_link=True)
                if path.exists() or is_link_or_reparse(path):
                    outputs.append((path, output.get("output_hash")))
            self.root.mkdir(parents=True, exist_ok=True)
            tx_dir = self.root / f".skills-txn-{uuid.uuid4().hex}"
            moved_dir = tx_dir / "moved"
            moved_dir.mkdir(parents=True)
            park_dir = Path(
                quarantine_dir
                if quarantine_dir is not None
                else trash_dir
                if trash_dir is not None
                else self.root / "trash" / safe_component(package_id) / park_stamp()
            )
            park_bound = park_root if park_root is not None else self.root
            # `staged` is a hidden sibling on the output's own filesystem, used
            # only when the output cannot be renamed straight into the
            # transaction dir (another filesystem); `parked` is where the
            # moved entry goes next (trash or quarantine). Both are journaled
            # up front so recovery can always put an output back from
            # wherever it is.
            actions: list[dict[str, Any]] = [
                {
                    "destination": str(path),
                    "moved": str(moved_dir / str(index)),
                    "staged": str(path.with_name(f".{path.name}.sfs-removing-{tx_dir.name[-12:]}")),
                    "parked": str(self._quarantine_target(park_dir, path)),
                }
                for index, (path, _) in enumerate(outputs)
            ]
            journal: dict[str, Any] = {
                "version": 1,
                "phase": "removing",
                "tx_dir": str(tx_dir),
                "park_dir": str(park_dir),
                "actions": actions,
                "state_backup_existed": self.state_path.exists(),
                "lock_backup_existed": self.lock_path.exists(),
            }
            if self.state_path.exists():
                state_backup = tx_dir / "skills.state.json"
                copy_backup(self.state_path, state_backup)
                journal["state_backup"] = str(state_backup)
            if self.lock_path.exists():
                lock_backup = tx_dir / "skills.lock.json"
                copy_backup(self.lock_path, lock_backup)
                journal["lock_backup"] = str(lock_backup)
            atomic_json(self.journal_path, journal)
            removed: list[Path] = []
            dirty: list[str] = []
            quarantined: list[dict[str, str]] = []
            trashed: list[dict[str, str]] = []
            failed: list[str] = []
            try:
                for (path, expected), action in zip(outputs, actions):
                    moved = Path(action["moved"])
                    try:
                        if not self._move_out(path, action):
                            continue
                    except OutputNotMoved:
                        # Only when the output is GUARANTEED back at its path
                        # may quarantine record it as failed and go on; any
                        # other failure aborts the whole transaction (and
                        # recovery restores every staged entry).
                        if quarantine_dir is not None:
                            failed.append(str(path))
                            continue
                        raise
                    clean = not is_link_or_reparse(moved) and _sha256_file(moved) == expected
                    if quarantine_dir is not None:
                        target = Path(action["parked"])
                        self._park(moved, target, park_bound)
                        quarantined.append(
                            {"from": str(path), "to": str(target), "modified": str(not clean).lower()}
                        )
                        if not clean:
                            dirty.append(str(path))
                        removed.append(path)
                    elif clean:
                        self._park(moved, Path(action["parked"]), park_bound)
                        trashed.append({"from": str(path), "to": action["parked"]})
                        removed.append(path)
                    else:
                        # Modified by the user: put it back untouched.
                        self._put_back(moved, path)
                        dirty.append(str(path))
                self._prune_empty_parents(removed)
                if not failed:
                    desired["packages"].pop(package_id, None)
                    del state["packages"][package_id]
                else:
                    # Blocked outputs are still active: keep a DURABLE
                    # quarantine-pending lock row (sync neither reinstalls nor
                    # "removes" it - see `SkillManager.sync`) and the
                    # installed row with just the outputs still in place, so
                    # warnings, doctor and the next quarantine pass see it.
                    lock_row = desired["packages"].get(package_id)
                    desired["packages"][package_id] = {
                        **(lock_row if isinstance(lock_row, dict) else {
                            "package_id": package_id, "name": row.get("name"),
                            "content_hash": row.get("content_hash"),
                        }),
                        "quarantine_pending": True,
                    }
                    row["quarantine_pending"] = True
                    # Not a pull operation's change: its recovery leaves it.
                    row.pop("pull_op", None)
                    remaining = set(failed)
                    row["outputs"] = [
                        output for output in row.get("outputs") or []
                        if isinstance(output, dict)
                        and isinstance(output.get("path"), str)
                        and str(
                            self._validate_output(Path(output["path"]), allow_final_link=True)
                        )
                        in remaining
                    ]
                    row["revision"] = self._revision(row) + 1
                atomic_json(self.state_path, state)
                atomic_json(self.lock_path, desired)
                journal["phase"] = "committed"
                atomic_json(self.journal_path, journal)
            except Exception:
                self._recover_locked()
                raise
            self._recover_locked()
            result: dict[str, Any] = {"removed": [str(path) for path in removed], "dirty": dirty}
            if trashed:
                result.update(trashed=trashed, trash_dir=str(park_dir))
            if quarantine_dir is not None:
                result.update(
                    quarantined=quarantined, failed=failed, quarantine_dir=str(quarantine_dir)
                )
            return result

    def _journal_parked_path(
        self, journal: dict[str, Any], action: dict[str, Any]
    ) -> Path | None:
        """The journaled trash/quarantine path of one removal action, only if
        it lies lexically inside the journaled parking directory."""
        parked_raw = action.get("parked")
        park_raw = action.get("park_dir", journal.get("park_dir"))
        if not isinstance(parked_raw, str) or not isinstance(park_raw, str):
            return None
        parked = self._lexical(Path(parked_raw))
        try:
            parked.relative_to(self._lexical(Path(park_raw)))
        except ValueError:
            return None
        return parked

    @staticmethod
    def _park(moved: Path, target: Path, bound: Path) -> None:
        """Move the removed entry `moved` (in the transaction dir) to `target`
        durably, before the commit.

        Same filesystem: the inode is RENAMED (never copied or deleted).
        Another filesystem (EXDEV): the entry is copied and the copy fsynced;
        `moved` stays in the transaction dir, which is only discarded after
        the commit, so an interruption at any point leaves a complete copy.
        Every directory created for `target` and its nearest pre-existing
        ancestor are fsynced first."""
        make_durable_dirs(target.parent, bound)
        try:
            rename_noreplace(moved, target)  # never replaces an entry there
        except FileExistsError:
            raise SkillError(f"Refusing to overwrite an existing entry: {target}") from None
        except OSError as exc:
            if exc.errno != errno.EXDEV:
                raise
            if is_link_or_reparse(moved):
                symlink_at(readlink_at(moved), target)  # fails if anything is there
            else:
                tmp = make_temp_at(target.parent, f".{target.name}.")
                try:
                    copy_backup(moved, tmp)  # fsyncs the file
                    copy_mode_at(moved, tmp)
                    rename_noreplace(tmp, target)
                except BaseException:
                    with contextlib.suppress(OSError, SkillError):
                        unlink_at(tmp)
                    raise
            _fsync_directory(target.parent)
            return
        _fsync_directory(target.parent)
        _fsync_directory(moved.parent)

    def _park_bound(self) -> Path:
        return self.store_root if self.store_root is not None else self.root

    def _trash_dir(self, package_id: str) -> Path:
        return trash_root(self._park_bound()) / safe_component(package_id) / park_stamp()

    def _drop_action(
        self, tx_dir: Path, index: int, path: Path, park_dir: Path, expected: Any
    ) -> dict[str, Any]:
        """Journal entry for an output that leaves its tool directory (dropped,
        or replaced by a new version): where it is moved first (`moved`, or the
        `staged` sibling on another filesystem) and where a clean copy is
        parked (`parked`, inside `park_dir`). Recovery puts it back from
        whichever of them holds it."""
        return {
            "destination": str(path),
            "moved": str(tx_dir / "moved" / str(index)),
            "staged": str(path.with_name(f".{path.name}.sfs-removing-{tx_dir.name[-12:]}")),
            "parked": str(self._quarantine_target(park_dir, path)),
            "park_dir": str(park_dir),
            "expected": expected if isinstance(expected, str) else None,
        }

    def _move_out(self, path: Path, action: dict[str, Any]) -> bool:
        """Rename the entry `path` into the transaction dir (never following a
        final symlink). On another filesystem it is first renamed to the
        journaled hidden sibling on its own filesystem, and that frozen entry
        is copied into the transaction dir (the only copy made). Returns False
        when the entry is already gone; raises OSError when it cannot move."""
        moved = Path(action["moved"])
        make_dirs_at(moved.parent)
        try:
            _rename_entry(path, moved)
        except FileNotFoundError:
            return False
        except OSError as exc:
            staged = self._validate_output(Path(action["staged"]), allow_final_link=True)
            try:
                _rename_entry(path, staged)
            except FileNotFoundError:
                return False
            except OSError as stage_exc:
                # Nothing moved: the original is untouched at its path.
                raise OutputNotMoved(str(path), stage_exc) from exc
            _fsync_directory(path.parent)
            try:
                if is_link_or_reparse(staged):
                    symlink_at(readlink_at(staged), moved)
                else:
                    copy_backup(staged, moved)
                    copy_mode_at(staged, moved)
                _fsync_directory(moved.parent)
            except OSError as copy_exc:
                # E.g. ENOSPC: drop the partial copy and put the original
                # back at its path before anyone may continue.
                self._unstage(staged, path, moved, copy_exc)
                raise OutputNotMoved(str(path), copy_exc) from copy_exc
            # The sibling's bytes now exist durably in the transaction dir.
            unlink_at(staged)
            _fsync_directory(path.parent)
        # The rename IS the backup: make both directory entries durable.
        _fsync_directory(moved.parent)
        _fsync_directory(path.parent)
        return True

    @staticmethod
    def _unstage(staged: Path, path: Path, moved: Path, cause: OSError) -> None:
        """Undo a staging whose copy into the transaction dir failed: remove
        the partial copy and rename the staged entry back to `path` (same
        directory, no-replace). If the original cannot be guaranteed back,
        raise SkillError so the WHOLE transaction aborts and recovery puts
        every staged entry back from the journal."""
        with contextlib.suppress(OSError, SkillError):
            unlink_at(moved)
        try:
            rename_noreplace(staged, path)
            _fsync_directory(path.parent)
        except (OSError, SkillError) as exc:
            raise SkillError(
                f"Could not move {path} out ({cause.strerror or cause}) nor put it back "
                f"({exc}); aborting so it is restored from {staged}"
            ) from exc

    def _journal_drops_and_writes(
        self,
        tx_dir: Path,
        drops: dict[str, tuple[Path, Path, Any]],
        writes: list[tuple[Path, str]],
    ) -> tuple[list[dict[str, Any]], dict[str, int]]:
        """Journal actions: every drop (key -> (park dir, path, expected
        hash)) first, then every write (path, planned output hash). A write
        into a dropped path is paired with that drop (`after_drop`). Recovery
        removes a written file only when it holds exactly the planned bytes
        (`written_hash`), so an entry that appeared meanwhile is never
        removed; a paired write only once its drop had moved the original."""
        actions: list[dict[str, Any]] = []
        drop_index: dict[str, int] = {}
        for key, (park_dir, path, expected) in drops.items():
            drop_index[key] = len(actions)
            actions.append(self._drop_action(tx_dir, len(actions), path, park_dir, expected))
        seen: set[str] = set()
        for path, written_hash in writes:
            key = str(path)
            if key in seen:
                continue
            seen.add(key)
            action: dict[str, Any] = {"destination": key, "written_hash": written_hash}
            if key in drop_index:
                action["after_drop"] = drop_index[key]
            actions.append(action)
        return actions, drop_index

    def _drop(self, path: Path, action: dict[str, Any]) -> str:
        """Take one output out of its tool directory, rename-first: the entry
        is moved, THEN hashed, so an edit made after any earlier check is
        never lost. Clean ("trashed"): parked in the trash. Modified or a
        symlink ("dirty"): put back untouched. "gone": nothing was there."""
        if not self._move_out(path, action):
            return "gone"
        moved = Path(action["moved"])
        expected = action.get("expected")
        clean = (
            isinstance(expected, str)
            and not is_link_or_reparse(moved)
            and _sha256_file(moved) == expected
        )
        if clean:
            self._park(moved, Path(action["parked"]), self._park_bound())
            return "trashed"
        self._put_back(moved, path)
        return "dirty"

    def _recover_drop(self, journal: dict[str, Any], action: dict[str, Any], tx_dir: Path) -> None:
        destination = self._validate_output(Path(action["destination"]), allow_final_link=True)
        moved = self._validate_transaction_path(Path(action["moved"]), tx_dir)
        staged_raw = action.get("staged")
        staged = (
            self._validate_output(Path(staged_raw), allow_final_link=True)
            if isinstance(staged_raw, str)
            else None
        )
        parked = self._journal_parked_path(journal, action)
        parked_present = parked is not None and (parked.exists() or is_link_or_reparse(parked))
        if staged is not None and (staged.exists() or is_link_or_reparse(staged)):
            # Interrupted cross-filesystem staging: the sibling is the
            # authoritative copy.
            self._put_back(staged, destination)
        elif moved.exists() or is_link_or_reparse(moved):
            self._put_back(moved, destination)
            if parked_present and parked is not None:
                # A cross-filesystem COPY made before the interruption; the
                # original just went back.
                with contextlib.suppress(OSError, SkillError):
                    unlink_at(parked)
        elif parked_present and parked is not None:
            # Already renamed onward into trash/quarantine.
            self._put_back(parked, destination)

    def _drop_happened(
        self, journal: dict[str, Any], action: dict[str, Any], tx_dir: Path
    ) -> bool:
        """Whether the journaled drop `action` moved its entry away (it is in
        the staged sibling, the transaction dir or its parking place)."""
        candidates: list[Path | None] = [
            self._validate_transaction_path(Path(action["moved"]), tx_dir),
            self._journal_parked_path(journal, action),
        ]
        if isinstance(action.get("staged"), str):
            candidates.append(
                self._validate_output(Path(action["staged"]), allow_final_link=True)
            )
        return any(
            item is not None and (item.exists() or is_link_or_reparse(item)) for item in candidates
        )

    def _quarantine_target(self, quarantine_dir: Path, path: Path) -> Path:
        lexical = self._lexical(path)
        try:
            relative = lexical.relative_to(self._lexical(self.scope_root))
        except ValueError:
            relative = Path(*lexical.parts[1:])
        return quarantine_dir / relative

    @staticmethod
    def _put_back(moved: Path, destination: Path) -> None:
        """Return a moved output to its place without clobbering a file that
        appeared there meanwhile (that one wins; ours is kept beside it).

        Every placement is an atomic NO-REPLACE rename (`rename_noreplace`):
        if the destination is occupied - also if it became occupied an
        instant before - the entry goes to an exclusively claimed
        `.sfs-restored-*` sidecar instead. Nothing is ever replaced."""
        make_dirs_at(destination.parent)
        try:
            SkillStateStore._place_noreplace(moved, destination)
        except OSError as exc:
            if exc.errno != errno.EXDEV or is_link_or_reparse(moved):
                raise
        else:
            # Both directory entries must be durable before the caller commits
            # (and later deletes the transaction directory).
            _fsync_directory(destination.parent)
            _fsync_directory(moved.parent)
            return
        # Another filesystem: copy to an exclusively created sibling of the
        # destination (file fsynced), place it with a no-replace rename and
        # fsync that directory - only then drop the source and fsync its
        # directory. The data always exists durably in at least one place.
        tmp = make_temp_at(destination.parent, f".{destination.name}.sfs-restore-")
        try:
            copy_backup(moved, tmp)
            copy_mode_at(moved, tmp)
            SkillStateStore._place_noreplace(tmp, destination)
        except BaseException:
            with contextlib.suppress(OSError, SkillError):
                unlink_at(tmp)
            raise
        _fsync_directory(destination.parent)
        unlink_at(moved)
        _fsync_directory(moved.parent)

    @staticmethod
    def _write_exclusive_at(dir_fd: int, name: str, payload: bytes, shown: Path) -> None:
        """Create `name` in the directory `dir_fd` with O_CREAT|O_EXCL (never
        an existing entry, never through a symlink) and write `payload`."""
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        flags |= getattr(os, "O_BINARY", 0) | getattr(os, "O_CLOEXEC", 0)
        try:
            fd = os.open(name, flags, 0o600, dir_fd=dir_fd)
        except FileExistsError:
            raise SkillError(
                f"A file appeared at {shown} while installing; it was not overwritten"
            ) from None
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())

    @staticmethod
    def _place_noreplace(entry: Path, destination: Path) -> Path:
        """No-replace rename of `entry` to `destination`, or - when that is
        occupied - to a fresh `.sfs-restored-<random>` sidecar claimed by
        the same no-replace rename. Returns where it landed."""
        target = destination
        for _attempt in range(16):
            try:
                rename_noreplace(entry, target)
                return target
            except FileExistsError:
                target = destination.with_name(
                    destination.name + f".sfs-restored-{uuid.uuid4().hex[:8]}"
                )
        raise SkillError(f"Could not find a free name next to {destination}")

    def _unique_outputs(self, row: dict[str, Any]) -> list[dict[str, Any]]:
        """Managed outputs deduplicated by resolved path (legacy state may repeat)."""
        unique: dict[str, dict[str, Any]] = {}
        for output in row.get("outputs") or []:
            if not isinstance(output, dict) or not isinstance(output.get("path"), str):
                continue
            key = str(self._lexical(Path(output["path"])))
            unique.setdefault(key, output)
        return list(unique.values())

    def _write_output(self, source: Path, destination: Path, *, emission: Any = None) -> None:
        """Write a planned output into a place that is EMPTY (any previous
        entry was dropped rename-first). Everything happens relative to the
        destination directory's descriptor (`open_dir`, created if needed,
        never through a symlink): an exclusively created temporary file is
        placed with a no-replace rename (or, with no atomic primitive, the
        final name is created exclusively and written), so an entry that
        appeared there meanwhile is never overwritten - the transaction is
        aborted instead."""
        raw = source.read_bytes()
        payload = emission.transform(raw) if emission is not None else raw
        flags = (
            os.O_WRONLY | os.O_CREAT | os.O_EXCL
            | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
        )
        with open_dir(destination.parent, create=True) as dir_fd:
            tmp_name = ""
            for _attempt in range(32):
                candidate = f".{destination.name}.{uuid.uuid4().hex[:12]}"
                try:
                    fd = os.open(candidate, flags, 0o600, dir_fd=dir_fd)
                except FileExistsError:
                    continue
                tmp_name = candidate
                break
            if not tmp_name:
                raise SkillError(f"Could not create a temporary file next to {destination}")
            try:
                with os.fdopen(fd, "wb") as handle:
                    handle.write(payload)
                    handle.flush()
                    os.fsync(handle.fileno())
                try:
                    _rename_noreplace_at(dir_fd, tmp_name, dir_fd, destination.name)
                    tmp_name = ""
                except FileExistsError:
                    raise SkillError(
                        f"A file appeared at {destination} while installing; it was not "
                        "overwritten"
                    ) from None
                except SkillError:
                    # Neither a native no-replace rename nor hard links here:
                    # create the final name exclusively and write into it.
                    self._write_exclusive_at(dir_fd, destination.name, payload, destination)
                os.fsync(dir_fd)
            finally:
                if tmp_name:
                    with contextlib.suppress(OSError):
                        os.unlink(tmp_name, dir_fd=dir_fd)

    def _package_rows(
        self,
        package: PackageRecord,
        plans: list[EmitPlan],
        outputs: list[dict[str, Any]],
        dirty_stale: list[str],
        existing: dict[str, Any] | None,
        consented_capabilities: set[str] | None,
        consent_context: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        package_row = package.as_dict(include_path=False)
        self._carry_provenance(package_row, package, existing)
        receipts = self._receipts(package, plans, existing, consented_capabilities, consent_context)
        self._consent_snapshot(package_row, package, existing, consent_context)
        package_row.update(
            {
                "scope": plans[0].scope if plans else "project",
                "targets": [item.tool for item in plans],
                "plan_hashes": {item.tool: item.plan_hash for item in plans},
                "degradations": [value.as_dict() for plan in plans for value in plan.degradations],
                "omissions": [value.as_dict() for plan in plans for value in plan.omissions],
                "outputs": outputs,
                "dirty_stale_outputs": dirty_stale,
                "consent_receipts": receipts,
                "consent_receipt_ids": [item["id"] for item in receipts],
                "previous_package_ids": self._previous_package_ids(
                    existing, None, package.package_id
                ),
                "revision": self._revision(existing) + 1,
                "install_id": self._install_id(existing),
            }
        )
        lock_row = {
            "package_id": package.package_id,
            "name": package.name,
            "content_hash": package.content_hash,
            "source": package.source.as_dict(),
            "scope": package_row["scope"],
            "targets": package_row["targets"],
            "required_capabilities": sorted(
                {requirement for plan in plans for requirement in plan.consent_requirements}
            ),
        }
        return package_row, lock_row

    def reconcile(
        self,
        installs: list[tuple[PackageRecord, list[EmitPlan], set[str]]],
        removals: list[str],
        *,
        consent_contexts: dict[str, dict[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        """Apply many removals and installs as ONE journaled transaction.

        Phase 1 (no writes): every planned output across the whole desired set
        is preflighted — against each other (two packages may not plan the
        same path), against unmanaged files, and against dirty managed files.
        Phase 2 performs every unlink and write under a single journal entry
        so a failure or crash anywhere rolls the entire reconcile back.
        """
        self._validate_state_root()
        with interprocess_lock(self.mutex_path):
            self._recover_locked()
            state = self.state()
            desired = self.desired()
            packages = state["packages"]
            removal_set = set(removals)
            for package_id in removals:
                if not isinstance(packages.get(package_id), dict):
                    raise SkillError(f"Installed package not found: {package_id}")
            # ---- phase 1: preflight ----
            # Ownership is computed against the POST-removal set: a package
            # that stops planning an output (dropped target) releases it in
            # phase 2 BEFORE any write, so another package may adopt the path
            # in the same reconcile.
            install_rows: list[tuple[PackageRecord, list[EmitPlan], set[str], dict[str, Any] | None, list[Any], list[dict[str, Any]]]] = []
            dropped_by_owner: dict[str, set[str]] = {}
            for package, plans, consented in installs:
                existing_raw = packages.get(package.package_id)
                existing = existing_raw if isinstance(existing_raw, dict) else None
                prior_outputs = existing.get("outputs", []) if existing else []
                emissions = [item for plan in plans for item in plan.emissions]
                new_paths = {item.destination for item in emissions}
                stale = [
                    item for item in prior_outputs
                    if isinstance(item, dict) and item.get("path") not in new_paths
                ]
                dropped_by_owner[package.package_id] = {
                    item["path"] for item in stale if isinstance(item.get("path"), str)
                }
                install_rows.append((package, plans, consented, existing, emissions, stale))
            owner_by_path = self._owner_by_path(packages, exclude=removal_set)
            for owner_id, dropped in dropped_by_owner.items():
                for raw in dropped:
                    owner = owner_by_path.get(raw)
                    if owner is not None and owner[0] == owner_id:
                        del owner_by_path[raw]
            # Preflight only: phase 2 re-decides every drop on the MOVED entry
            # (an edit made after this check is never lost).
            freed: set[str] = set()
            kept_dirty: set[str] = set()
            for package_id in removals:
                row = packages[package_id]
                for output in self._unique_outputs(row):
                    path = self._validate_output(Path(output["path"]))
                    if not path.exists():
                        continue
                    if _sha256_file(path) == output.get("output_hash"):
                        freed.add(str(path))
                    else:
                        kept_dirty.add(str(path))
            # Stale outputs of updated packages are released the same way:
            # unmodified ones are dropped before any write, modified ones are
            # kept (and may not be overwritten by anyone).
            for _package, _plans, _consented, _existing, _emissions, stale in install_rows:
                for item in stale:
                    if not isinstance(item.get("path"), str):
                        continue
                    path = self._validate_output(Path(item["path"]))
                    if not path.exists():
                        continue
                    if _sha256_file(path) == item.get("output_hash"):
                        freed.add(str(path))
                    else:
                        kept_dirty.add(str(path))
            planned_by: dict[str, str] = {}
            prepared: list[dict[str, Any]] = []
            for package, plans, consented, existing, emissions, stale in install_rows:
                package_root = self._lexical(package.package_root)
                for emission in emissions:
                    destination = self._validate_output(Path(emission.destination))
                    key = str(destination)
                    other = planned_by.get(key)
                    if other is not None and other != package.package_id:
                        raise SkillError(
                            f"Output is planned by both {other} and {package.package_id}: {destination}"
                        )
                    planned_by[key] = package.package_id
                    source = self._lexical(Path(emission.source))
                    try:
                        source.relative_to(package_root)
                    except ValueError as exc:
                        raise SkillError(f"Planned source escapes package content: {source}") from exc
                    self._assert_no_symlink_components(package_root, source)
                    if _emission_hash(source, emission) != emission.output_hash:
                        raise SkillError(f"Planned source integrity mismatch: {source}")
                    if key in kept_dirty:
                        raise SkillError(f"Refusing to overwrite modified managed output: {destination}")
                    if key in freed:
                        continue  # unlinked by a removal before this write
                    owner = owner_by_path.get(emission.destination)
                    self._assert_not_owned_by_other(owner, package.package_id, destination)
                    if destination.exists():
                        if is_link_or_reparse(destination) or not destination.is_file():
                            raise SkillError(f"Refusing non-file output collision: {destination}")
                        if owner is None:
                            raise SkillError(f"Refusing unmanaged output collision: {destination}")
                        if _sha256_file(destination) != owner[1]:
                            raise SkillError(
                                f"Refusing to overwrite modified managed output: {destination}"
                            )
                prepared.append(
                    {
                        "package": package,
                        "plans": plans,
                        "consented": consented,
                        "existing": existing,
                        "emissions": emissions,
                        "stale": stale,
                    }
                )
            # ---- phase 2: one journal for everything ----
            # Every output that leaves a tool directory - outputs of removed
            # packages, stale outputs of updated ones and existing outputs
            # about to be replaced - is DROPPED rename-first (`_drop`): moved,
            # THEN hashed; clean ones are parked in the trash, modified ones
            # put back. Nothing is unlinked or copied. All drops precede all
            # writes, so a path one package drops and another adopts is
            # released first.
            self.root.mkdir(parents=True, exist_ok=True)
            tx_dir = self.root / f".skills-txn-{uuid.uuid4().hex}"
            (tx_dir / "moved").mkdir(parents=True)
            park_dirs: dict[str, Path] = {}

            def _park_dir(owner_id: str) -> Path:
                if owner_id not in park_dirs:
                    park_dirs[owner_id] = self._trash_dir(owner_id)
                return park_dirs[owner_id]

            # key -> (path, expected hash, kind, owner package id, raw path)
            drops: dict[str, tuple[Path, Any, str, str, str]] = {}
            for package_id in removals:
                for output in self._unique_outputs(packages[package_id]):
                    path = self._validate_output(Path(output["path"]), allow_final_link=True)
                    if path.exists() or is_link_or_reparse(path):
                        drops.setdefault(
                            str(path),
                            (path, output.get("output_hash"), "remove", package_id, output["path"]),
                        )
            for item in prepared:
                package_id = item["package"].package_id
                for stale_item in item["stale"]:
                    if isinstance(stale_item.get("path"), str):
                        path = self._validate_output(
                            Path(stale_item["path"]), allow_final_link=True
                        )
                        drops.setdefault(
                            str(path),
                            (path, stale_item.get("output_hash"), "stale", package_id,
                             stale_item["path"]),
                        )
            unchanged: set[str] = set()
            writes: list[tuple[Path, str]] = []
            for item in prepared:
                package_id = item["package"].package_id
                for emission in item["emissions"]:
                    destination = self._validate_output(Path(emission.destination))
                    key = str(destination)
                    writes.append((destination, emission.output_hash))
                    if key in drops or not (
                        destination.exists() or is_link_or_reparse(destination)
                    ):
                        continue
                    owner = owner_by_path.get(emission.destination)
                    if owner is not None and owner[1] == emission.output_hash:
                        unchanged.add(key)  # same bytes already there: left in place
                        continue
                    drops[key] = (
                        destination,
                        owner[1] if owner is not None else None,
                        "replace",
                        package_id,
                        emission.destination,
                    )
            actions, drop_index = self._journal_drops_and_writes(
                tx_dir,
                {key: (_park_dir(value[3]), value[0], value[1]) for key, value in drops.items()},
                [entry for entry in writes if str(entry[0]) not in unchanged],
            )
            journal: dict[str, Any] = {
                "version": 1,
                "phase": "reconciling",
                "tx_dir": str(tx_dir),
                "actions": actions,
                "state_backup_existed": self.state_path.exists(),
                "lock_backup_existed": self.lock_path.exists(),
            }
            if self.state_path.exists():
                state_backup = tx_dir / "skills.state.json"
                copy_backup(self.state_path, state_backup)
                journal["state_backup"] = str(state_backup)
            if self.lock_path.exists():
                lock_backup = tx_dir / "skills.lock.json"
                copy_backup(self.lock_path, lock_backup)
                journal["lock_backup"] = str(lock_backup)
            atomic_json(self.journal_path, journal)
            results: list[dict[str, Any]] = []
            try:
                planned = {str(entry[0]) for entry in writes}
                actual: dict[str, dict[str, list[str]]] = {
                    package_id: {"removed": [], "dirty": []} for package_id in removals
                }
                dirty_by_package: dict[str, list[str]] = {}
                for key, (path, _expected, kind, owner_id, raw) in drops.items():
                    outcome = self._drop(path, actions[drop_index[key]])
                    if outcome == "dirty" and (kind == "replace" or key in planned):
                        # Modified after the preflight, and something would be
                        # written there: abort, never overwrite it.
                        raise SkillError(f"Refusing to overwrite modified managed output: {path}")
                    if kind == "remove":
                        if outcome == "trashed":
                            actual[owner_id]["removed"].append(key)
                        elif outcome == "dirty":
                            actual[owner_id]["dirty"].append(key)
                    elif kind == "stale" and outcome == "dirty":
                        dirty_by_package.setdefault(owner_id, []).append(raw)
                    if outcome == "trashed" and kind != "replace":
                        self._prune_empty_parents([path])
                for package_id in removals:
                    row = packages[package_id]
                    del packages[package_id]
                    desired["packages"].pop(package_id, None)
                    results.append(
                        {
                            "action": "removed",
                            "package_id": package_id,
                            "name": row.get("name"),
                            **actual[package_id],
                        }
                    )
                for item in prepared:
                    item["dirty_stale"] = dirty_by_package.get(item["package"].package_id, [])
                for item in prepared:
                    package = item["package"]
                    plans = item["plans"]
                    dirty_stale = item["dirty_stale"]
                    outputs: list[dict[str, Any]] = []
                    recorded: dict[str, dict[str, Any]] = {}
                    for plan in plans:
                        for emission in plan.emissions:
                            destination = self._validate_output(Path(emission.destination))
                            previous = recorded.get(str(destination))
                            if previous is not None:
                                previous["tools"].append(plan.tool)
                                continue
                            if str(destination) in unchanged:
                                pass
                            elif emission.substitutions:
                                self._write_output(
                                    self._lexical(Path(emission.source)), destination,
                                    emission=emission,
                                )
                            else:
                                self._write_output(self._lexical(Path(emission.source)), destination)
                            record = {
                                "path": emission.destination,
                                "output_hash": emission.output_hash,
                                "tool": plan.tool,
                                "tools": [plan.tool],
                            }
                            recorded[str(destination)] = record
                            outputs.append(record)
                    package_row, lock_row = self._package_rows(
                        package, plans, outputs, dirty_stale, item["existing"], item["consented"],
                        (consent_contexts or {}).get(package.package_id),
                    )
                    packages[package.package_id] = package_row
                    desired["packages"][package.package_id] = lock_row
                    results.append(package_row)
                atomic_json(self.state_path, state)
                atomic_json(self.lock_path, desired)
                journal["phase"] = "committed"
                atomic_json(self.journal_path, journal)
            except Exception:
                self._recover_locked()
                raise
            self._recover_locked()
            return results

    def drift(self) -> list[dict[str, str]]:
        result: list[dict[str, str]] = []
        for package_id, row in self.state()["packages"].items():
            if not isinstance(row, dict):
                continue
            for output in self._unique_outputs(row):
                path = self._validate_output(Path(output["path"]))
                actual = _sha256_file(path)
                expected = str(output.get("output_hash", ""))
                if actual != expected:
                    result.append(
                        {
                            "package_id": package_id,
                            "path": output["path"],
                            "expected": expected,
                            "actual": actual or "missing",
                        }
                    )
        result.extend(self.stranded_staging())
        return result

    def stranded_staging(self) -> list[dict[str, str]]:
        """Hidden `.<name>.sfs-removing-<tx>` siblings left in a tool
        directory with NO transaction pending to restore them (should never
        happen; reported so the file is never silently lost). Symlinks are
        never followed."""
        if self.journal_path.exists():
            return []  # the next operation's recovery puts them back
        found: list[dict[str, str]] = []
        for root in self.allowed_output_roots:
            if is_link_or_reparse(root) or not root.is_dir():
                continue
            for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
                for name in [*filenames, *dirnames]:
                    if name.startswith(".") and ".sfs-removing-" in name:
                        stranded = Path(dirpath) / name
                        original = Path(dirpath) / name[1:].split(".sfs-removing-", 1)[0]
                        found.append(
                            {
                                "package_id": "",
                                "path": str(stranded),
                                "expected": f"no stranded removal copy (original: {original})",
                                "actual": "stranded-removal-copy",
                            }
                        )
        return found
