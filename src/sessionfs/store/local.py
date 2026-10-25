"""Local session store at ~/.sessionfs/.

Directory layout:
    ~/.sessionfs/
    ├── config.toml
    ├── daemon.json
    ├── sfsd.pid
    ├── index.db
    └── sessions/
        └── {session_id}.sfs/
            ├── manifest.json
            ├── messages.jsonl
            ├── workspace.json
            └── tools.json
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import sqlite3
import stat
import tempfile
import time
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any, NamedTuple

from sessionfs.store.index import SessionIndex
from sessionfs.watchers.base import NativeSessionRef

logger = logging.getLogger("sessionfs.store")

# M2: Session ID validation at store layer — imported from canonical module
from sessionfs.session_id import validate_session_id

# Stale directories left behind by an interrupted commit (``{id}.sfs.staging.*``)
# or by the previous directory-swap design (``{id}.sfs.old.*``). Both are
# cleaned up at startup; a stranded ``.old.`` directory is renamed back into
# place first so a session abandoned by the old swap code isn't lost.
_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
_STAGING_RE = re.compile(r"^\.([A-Za-z0-9][A-Za-z0-9_-]*)\.sfs\.staging\.")
_OLD_RE = re.compile(r"^\.([A-Za-z0-9][A-Za-z0-9_-]*)\.sfs\.old\.")


def _validate_session_path_component(session_id: str) -> None:
    """Refuse a session id that is unsafe as a directory/lock-file name.

    Deliberately looser than ``validate_session_id``: watchers derive ids from
    native ids (``session_id_from_native``), and some tools' native ids carry
    uppercase letters or short names (Amp's ``T-…``), which the local store
    has always accepted. Only path traversal and hidden-name tricks matter here.
    """
    if (
        not session_id
        or len(session_id) > 128
        or not _SAFE_ID_RE.fullmatch(session_id)
    ):
        raise ValueError(f"Invalid session ID format: {session_id!r}")


def _validate_session_id(session_id: str) -> None:
    """Validate session ID format at the store layer."""
    if not validate_session_id(session_id):
        raise ValueError(f"Invalid session ID format: {session_id!r}")


def _set_dir_permissions(path: Path) -> None:
    """Set directory to 0700 (owner rwx only)."""
    os.chmod(path, stat.S_IRWXU)


def _set_file_permissions(path: Path) -> None:
    """Set file to 0600 (owner rw only)."""
    os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)


class LocalStore:
    """Manages the local ~/.sessionfs/ directory and SQLite index."""

    def __init__(self, store_dir: Path) -> None:
        self._store_dir = store_dir
        self._sessions_dir = store_dir / "sessions"
        self._index: SessionIndex | None = None
        # Sessions allocated for writing whose metadata hasn't been upserted
        # yet, mapped to what they looked like before: their manifest's sync
        # block (or None) and a hash of their content files (or None if new).
        # Capture rewrites manifest.json from scratch, which would otherwise
        # erase the record that the session was synced and with which etag.
        self._pending_writes: dict[str, _PriorState] = {}
        self._write_listeners: list[Callable[[str], None]] = []

    @property
    def store_dir(self) -> Path:
        """The root store directory (e.g. ~/.sessionfs, or a named
        profile's store). Public accessor for callers that must scope
        the active profile's deleted.json — tk_457d060822bc48c0 R2:
        capture_guard checks is_excluded(base_dir=store.store_dir) so a
        named-profile deletion is honored by the watcher, not just the
        sync path."""
        return self._store_dir

    def initialize(self) -> None:
        """Create directory structure and open the index database."""
        self._store_dir.mkdir(parents=True, exist_ok=True)
        _set_dir_permissions(self._store_dir)
        self._sessions_dir.mkdir(parents=True, exist_ok=True)
        _set_dir_permissions(self._sessions_dir)
        self._recover_stale_dirs()
        self._index = SessionIndex(self._store_dir / "index.db")
        self._index.initialize()
        # M8: Restrict index.db permissions
        index_path = self._store_dir / "index.db"
        if index_path.exists():
            _set_file_permissions(index_path)
        # Auto-rebuild index if corruption was detected
        if self._index._needs_reindex:
            logger.warning("Reindexing sessions after index corruption recovery...")
            self._rebuild_index_from_disk()
            self._index._needs_reindex = False

    def _recover_stale_dirs(self) -> None:
        """Remove interrupted-commit leftovers and rescue stranded sessions.

        Runs at startup (``initialize``) under each session's per-session lock.
        Two kinds of leftovers are handled:

        - ``.{id}.sfs.staging.*`` — a staging directory whose commit did not
          finish. It is deleted: the final session directory is never missing
          or partial (the new commit moves files in one at a time). When the
          per-session lock is unavailable (Windows / flock-less filesystems) a
          staging dir younger than an hour is left alone, so a CLI can't delete
          a staging dir the daemon is mid-commit on.
        - ``.{id}.sfs.old.*`` — a backup the previous directory-swap design
          left behind. If the real ``{id}.sfs`` is missing the backup is
          renamed back into place first (rescuing the session), otherwise it
          is deleted.
        """
        from sessionfs.store.fsutil import fsync_dir, locks_available

        lock_dir = session_lock_path(self._sessions_dir, "probe").parent

        for entry in list(self._sessions_dir.iterdir()):
            if entry.is_symlink() or not entry.is_dir():
                continue
            old_match = _OLD_RE.match(entry.name)
            staging_match = _STAGING_RE.match(entry.name)
            if not old_match and not staging_match:
                continue
            session_id = (old_match or staging_match).group(1)  # type: ignore[union-attr]
            # A staging dir may belong to a commit that is happening right now.
            # With a working per-session lock the lock below serializes us
            # against it; without one (Windows, flock-less filesystems) a CLI's
            # initialize() must not delete a staging dir the daemon is still
            # moving files out of, so only a staging dir older than an hour is
            # reclaimed.
            if staging_match and not locks_available(lock_dir):
                try:
                    age_s = time.time() - entry.stat().st_mtime
                except OSError:
                    age_s = float("inf")
                if age_s < 3600:
                    logger.info(
                        "Leaving fresh staging dir %s (lock unavailable)", entry.name
                    )
                    continue
            with self._session_lock(session_id):
                if old_match:
                    final_dir = self._sessions_dir / f"{session_id}.sfs"
                    if not final_dir.exists():
                        os.rename(entry, final_dir)
                        fsync_dir(self._sessions_dir)
                        logger.warning(
                            "Restored stranded session %s from %s",
                            session_id,
                            entry.name,
                        )
                        continue
                shutil.rmtree(entry, ignore_errors=True)
                if old_match:
                    logger.info("Removed stale backup directory %s", entry.name)

    def _rebuild_index_from_disk(self) -> None:
        """Rebuild the session index by scanning .sfs directories on disk.

        Each session is reindexed in isolation: a single malformed
        manifest cannot abort the loop. Pre-v0.9.9.12, the except
        clause caught only `(json.JSONDecodeError, OSError)`, so any
        unexpected exception (AttributeError from a null `source`
        field, sqlite IntegrityError from a missing required field,
        TypeError from non-serializable tags, etc.) bubbled up and
        aborted the rebuild after the offending session — every
        sorted-later session was silently dropped from the index.
        Skips are logged at WARNING so they appear in normal logs.
        """
        if not self._sessions_dir.is_dir():
            return
        count = 0
        skipped = 0
        for sfs_dir in sorted(self._sessions_dir.iterdir()):
            if not sfs_dir.is_dir() or not sfs_dir.name.endswith(".sfs"):
                continue
            manifest_path = sfs_dir / "manifest.json"
            if not manifest_path.exists():
                continue
            try:
                manifest = json.loads(manifest_path.read_text())
                session_id = manifest.get(
                    "session_id", sfs_dir.name.replace(".sfs", "")
                )
                # A rebuild re-reads what is on disk; it is not a new write.
                self.upsert_session_metadata(
                    session_id, manifest, str(sfs_dir), notify=False
                )
                count += 1
            except Exception as exc:  # noqa: BLE001 — isolate per-session failure
                skipped += 1
                logger.warning(
                    "Skipped %s during reindex (%s: %s)",
                    sfs_dir.name,
                    type(exc).__name__,
                    exc,
                )
        if skipped:
            logger.warning(
                "Rebuilt index from disk: %d sessions indexed, %d skipped",
                count,
                skipped,
            )
        else:
            logger.info("Rebuilt index from disk: %d sessions", count)

    def check_permissions(self) -> list[str]:
        """Check store directory permissions and return warnings."""
        warnings: list[str] = []
        if self._store_dir.exists():
            mode = self._store_dir.stat().st_mode
            if mode & (stat.S_IRGRP | stat.S_IWGRP | stat.S_IXGRP |
                       stat.S_IROTH | stat.S_IWOTH | stat.S_IXOTH):
                warnings.append(
                    f"Store directory {self._store_dir} has permissions "
                    f"{oct(mode & 0o777)} (expected 0o700)"
                )
        return warnings

    @property
    def sessions_dir(self) -> Path:
        return self._sessions_dir

    @property
    def index(self) -> SessionIndex:
        if self._index is None:
            raise RuntimeError("Store not initialized. Call initialize() first.")
        return self._index

    def allocate_session_dir(self, session_id: str) -> Path:
        """Get or create the .sfs directory for a session.

        Callers then write the session's files and finish with
        ``upsert_session_metadata``; the session's previous sync state is
        carried across that rewrite (see ``upsert_session_metadata``).

        New writers should prefer ``commit_session``, which routes the write
        through a staging directory + per-session lock and rechecks the
        exclusion list at commit time (closing the delete-resurrection race).
        This method stays for tests and the metadata-only reindex repair.
        """
        session_dir = self._sessions_dir / f"{session_id}.sfs"
        if session_dir.is_symlink():
            raise OSError(
                f"Refusing to write through symlinked session dir {session_dir}"
            )
        # If an earlier write of this session failed part-way (it never reached
        # upsert), what is on disk now is a partial rewrite; keep the state
        # recorded before that first attempt instead.
        if session_id not in self._pending_writes:
            self._pending_writes[session_id] = _PriorState(
                sync=_read_sync_block(session_dir / "manifest.json"),
                content=_content_hash(session_dir),
            )
        session_dir.mkdir(parents=True, exist_ok=True)
        _set_dir_permissions(session_dir)
        return session_dir

    def _session_lock(self, session_id: str):
        """A per-session cross-process lock (the same lock the delete path
        takes) so a capture and a delete of the same session serialize."""
        return lock_session_dir(self._sessions_dir, session_id)

    def commit_session(
        self,
        session_id: str,
        writer_fn: Callable[[Path], Any],
        *,
        skip_excluded: bool = True,
        mode: str = "merge",
        retire: Iterable[str] | None = None,
    ) -> CommitResult:
        """Commit a session directory by writing to staging and renaming in.

        ``writer_fn`` fills ``staging`` — a fresh, owner-only directory on the
        sessions dir — and returns the manifest dict to index, or None to
        index nothing (matching "no manifest → no index row"). A writer that
        also makes a previous capture's file obsolete may instead return a
        :class:`WriterResult` carrying the names to retire (see below). The
        write only lands if, rechecked at commit time under the per-session
        lock, the session is still not excluded and ``{id}.sfs`` is still not
        a symlink.

        ``mode`` selects how staged entries land over an existing live dir:

        - ``"merge"`` (default, captures): files the writer did not produce
          (``audit_report.json``, ``checkpoints/``, …) survive untouched; a
          staged directory colliding with a live directory moves the live one
          aside first instead of raising.
        - ``"replace"`` (pulls): the archive is authoritative. Every live entry
          the archive did not produce is removed, and every colliding live
          entry is replaced by the staged one.

        ``retire`` names files present in a previous capture but not produced
        by this one (e.g. ``workspace.json`` / ``tools.json``). They are
        unlinked under the lock, after the commit-time recheck, right before
        ``manifest.json`` moves — so a commit skipped by a mid-capture delete
        leaves them intact. Only plain files inside the session dir are
        retired, never symlinks or subdirectories.

        This replaces the old ``allocate_session_dir`` + write + ``upsert``
        dance for every real writer. The old dance had three races:

        - a delete between allocation and upsert resurrected the session,
          because ``mkdir`` recreated the just-removed directory;
        - a symlink planted at ``{id}.sfs`` was followed by ``mkdir``;
        - a partial write left a half-updated directory visible to packers.
        """
        _validate_session_path_component(session_id)
        from sessionfs.store.deleted import is_excluded

        if mode not in ("merge", "replace"):
            raise ValueError(f"Unknown commit mode: {mode!r}")
        caller_retire = frozenset(retire) if retire else frozenset()

        final_dir = self._sessions_dir / f"{session_id}.sfs"
        with self._session_lock(session_id):
            if final_dir.is_symlink():
                raise OSError(
                    f"Refusing to write through symlinked session dir {final_dir}"
                )
            if skip_excluded and is_excluded(session_id, base_dir=self._store_dir):
                return CommitResult(True, None, None)
            if session_id not in self._pending_writes:
                self._pending_writes[session_id] = _PriorState(
                    sync=_read_sync_block(final_dir / "manifest.json"),
                    content=_content_hash(final_dir),
                )
            staging = Path(
                tempfile.mkdtemp(
                    prefix=f".{session_id}.sfs.staging.", dir=self._sessions_dir
                )
            )
            _set_dir_permissions(staging)
            try:
                value = writer_fn(staging)
                writer_retire = frozenset()
                if isinstance(value, WriterResult):
                    writer_retire = value.retire
                    value = value.value
                # Recheck the tombstone and symlink at commit time — a delete
                # or a symlink plant may have raced the capture.
                if final_dir.is_symlink():
                    raise OSError(
                        f"Refusing to write through symlinked session dir {final_dir}"
                    )
                if skip_excluded and is_excluded(session_id, base_dir=self._store_dir):
                    self._pending_writes.pop(session_id, None)
                    return CommitResult(True, None, None)
                _commit_staging(
                    final_dir,
                    staging,
                    mode=mode,
                    retire=caller_retire | writer_retire,
                )
                if value is not None:
                    self.upsert_session_metadata(session_id, value, str(final_dir))
                else:
                    self._pending_writes.pop(session_id, None)
                return CommitResult(False, final_dir, value)
            finally:
                if staging.is_dir():
                    shutil.rmtree(staging, ignore_errors=True)

    def add_write_listener(self, listener: Callable[[str], None]) -> None:
        """Call ``listener(session_id)`` when a write changes a session's content.

        The daemon uses this to queue freshly captured sessions for autosync.
        Rewrites that leave the content unchanged (some tools re-capture every
        session whenever a shared database changes), index rebuilds and other
        metadata-only upserts don't notify.
        """
        self._write_listeners.append(listener)

    def get_session_dir(self, session_id: str) -> Path | None:
        """Get an existing session directory, or None."""
        session_dir = self._sessions_dir / f"{session_id}.sfs"
        return session_dir if session_dir.is_dir() else None

    def list_sessions(self) -> list[dict[str, Any]]:
        """List all sessions from the index."""
        return self.index.list_sessions()

    def get_tracked_session(self, native_session_id: str) -> NativeSessionRef | None:
        """Look up a tracked session by native ID."""
        return self.index.get_tracked_session(native_session_id)

    def get_tracked_session_by_sfs_id(self, sfs_session_id: str) -> NativeSessionRef | None:
        """Look up a tracked session by .sfs session ID."""
        return self.index.get_tracked_session_by_sfs_id(sfs_session_id)

    def upsert_tracked_session(self, ref: NativeSessionRef) -> None:
        """Insert or update a tracked session record.

        IntegrityError is propagated (data problem with this ref —
        not the index). Other DatabaseError subclasses (genuine
        index corruption) trigger a rebuild + retry. Same shape as
        upsert_session_metadata — see the longer docstring there.
        """
        try:
            self.index.upsert_tracked_session(ref)
        except sqlite3.IntegrityError:
            raise
        except sqlite3.DatabaseError as exc:
            logger.warning(
                "Index corrupted during tracked session write. Rebuilding... (%s)", exc
            )
            self._index = SessionIndex(self._store_dir / "index.db")
            self._index.initialize()
            if self._index._needs_reindex:
                self._rebuild_index_from_disk()
                self._index._needs_reindex = False
            # Retry the write
            self.index.upsert_tracked_session(ref)

    def upsert_session_metadata(
        self,
        session_id: str,
        manifest: dict[str, Any],
        sfs_dir_path: str,
        *,
        notify: bool = True,
    ) -> None:
        """Insert or update session metadata in the index.

        Distinguishes two failure modes that share the same parent
        exception class (`sqlite3.DatabaseError`):

        - `sqlite3.IntegrityError` (NOT NULL / UNIQUE / FK / CHECK
          violation) is a DATA problem with this specific manifest.
          The index itself is fine. Propagate so the caller — usually
          `_rebuild_index_from_disk` — can isolate this one session
          via its broad per-session except and continue with the rest.
          Pre-v0.9.9.12, IntegrityError was caught by the
          `sqlite3.DatabaseError` branch below and misinterpreted as
          index corruption, triggering a destructive recreate of the
          DB handle + recursive reindex per bad session.

        - Other `sqlite3.DatabaseError` subclasses (OperationalError
          for "database disk image is malformed", etc.) ARE genuine
          index corruption. Recreate the index handle, reindex from
          disk, retry the write once.
        """
        # Consumed only once the whole upsert succeeds, so a failure part-way
        # leaves the pre-rewrite state in place for the retry.
        prior = self._pending_writes.get(session_id)
        changed = False
        if prior is not None:
            changed = prior.content is None or prior.content != _content_hash(Path(sfs_dir_path))
            if prior.sync is not None and "sync" not in manifest:
                # The session was rewritten (re-captured or re-imported) and
                # the new manifest dropped its sync state. Keep the etag so the
                # next push is conditional on what the server has; mark it
                # dirty only if the content actually changed.
                sync = dict(prior.sync)
                if changed:
                    sync["dirty"] = True
                manifest = {**manifest, "sync": sync}
                _write_json_atomic(Path(sfs_dir_path) / "manifest.json", manifest)

        try:
            self.index.upsert_session(session_id, manifest, sfs_dir_path)
        except sqlite3.IntegrityError:
            # Data-level constraint failure for this manifest. Don't
            # touch the index — let the caller skip this session.
            raise
        except sqlite3.DatabaseError as exc:
            logger.warning(
                "Index corrupted during write. Rebuilding... (%s)", exc
            )
            self._index = SessionIndex(self._store_dir / "index.db")
            self._index.initialize()
            if self._index._needs_reindex:
                self._rebuild_index_from_disk()
                self._index._needs_reindex = False
            # Retry the write
            self.index.upsert_session(session_id, manifest, sfs_dir_path)

        self._pending_writes.pop(session_id, None)
        if changed and notify:
            for listener in self._write_listeners:
                try:
                    listener(session_id)
                except Exception:
                    logger.warning("Session write listener failed for %s", session_id,
                                   exc_info=True)

    def get_session_metadata(self, session_id: str) -> dict[str, Any] | None:
        """Get a single session's index data by ID."""
        return self.index.get_session(session_id)

    def find_sessions_by_prefix(self, prefix: str) -> list[dict[str, Any]]:
        """Find sessions whose ID starts with given prefix."""
        return self.index.find_sessions_by_prefix(prefix)

    def get_session_manifest(self, session_id: str) -> dict[str, Any] | None:
        """Read a session's manifest.json."""
        session_dir = self.get_session_dir(session_id)
        if not session_dir:
            return None
        manifest_path = session_dir / "manifest.json"
        if not manifest_path.exists():
            return None
        return json.loads(manifest_path.read_text())

    def close(self) -> None:
        """Close the index database."""
        if self._index:
            self._index.close()


class _PriorState(NamedTuple):
    sync: dict[str, Any] | None
    content: str | None


class CommitResult(NamedTuple):
    """Outcome of ``LocalStore.commit_session``.

    ``skipped`` is True when the session was excluded at commit time (the
    writer's staging directory was discarded and nothing was indexed).
    ``session_dir`` is the final on-disk directory on a committed write.
    ``value`` is whatever ``writer_fn`` returned (the manifest, or None).
    """

    skipped: bool
    session_dir: Path | None
    value: Any


class WriterResult(NamedTuple):
    """A ``commit_session`` writer return that also retires stale files.

    ``value`` is the manifest dict to index (or None, matching the plain
    return contract). ``retire`` names files a previous capture produced but
    this one did not; ``commit_session`` unlinks them under the per-session
    lock, after the commit-time exclusion recheck, so a skipped commit leaves
    them intact.
    """

    value: Any
    retire: frozenset[str] = frozenset()


def session_lock_path(sessions_dir: Path, session_id: str) -> Path:
    """The sidecar lock file guarding a session's directory, under the store.

    ``sessions_dir`` is the store's ``sessions/`` dir, so the lock lives in
    ``<store>/locks/<session_id>.lock`` — the same path every writer and every
    delete path uses, so they serialize with one another across processes.
    The id is checked here too, since some callers pass ids read from the
    index or the command line straight through.
    """
    _validate_session_path_component(session_id)
    return sessions_dir.parent / "locks" / f"{session_id}.lock"


def lock_session_dir(sessions_dir: Path, session_id: str):
    """Hold the per-session cross-process lock (the same lock ``commit_session``
    and the delete paths take)."""
    from sessionfs.store.fsutil import file_lock

    return file_lock(session_lock_path(sessions_dir, session_id))


def _commit_staging(
    final_dir: Path,
    staging: Path,
    *,
    mode: str = "merge",
    retire: frozenset[str] | None = None,
) -> None:
    """Move ``staging`` into ``final_dir`` file by file, never deleting it.

    The session directory itself is never renamed away or removed: staged
    entries are moved into it one at a time with ``os.replace`` (atomic per
    entry), and ``manifest.json`` is moved last so a reader can never observe
    a directory without a complete manifest. Subdirectories the writer did
    produce are moved with ``os.replace`` too, never ``copytree``.

    ``mode`` is ``"merge"`` (default) or ``"replace"``:

    - merge keeps files the writer did not produce (``audit_report.json``,
      ``checkpoints/``, anything else) exactly as they are; a staged directory
      colliding with a live directory moves the live one aside first.
    - replace treats the staged archive as authoritative: every live entry the
      archive did not produce is moved aside (and later removed with the
      staging dir), so a pull can't leave local-only files behind.

    ``retire`` names files a previous capture produced but this one did not
    (``workspace.json`` / ``tools.json``). They are unlinked under the lock,
    right before ``manifest.json`` moves, and only when they are plain files.

    Live entries a staged entry replaces are moved into a ``.trash`` dir inside
    ``staging``, so the caller's final ``rmtree(staging)`` — outside these
    critical moves — removes them together. On failure part-way the moves
    already done stay in place (each is atomic), so the final directory always
    exists and stays readable; the caller deletes only the staging directory,
    never the final one.
    """
    from sessionfs.store.fsutil import fsync_dir

    # fsync every staged file first so a crash after a move can't leave a
    # truncated file where a complete one was expected.
    for path in sorted(staging.rglob("*")):
        if path.is_file():
            with open(path, "rb") as fh:
                os.fsync(fh.fileno())

    final_dir.mkdir(parents=True, exist_ok=True)
    _set_dir_permissions(final_dir)

    trash = staging / ".trash"
    trash.mkdir()

    def _move_entry(entry: Path, *, replace: bool) -> None:
        """Move one staged entry into the live dir, resolving any collision."""
        live = final_dir / entry.name
        if replace:
            # The archive is authoritative. A file over a plain file needs no
            # trash step: os.replace overwrites it atomically, so readers never
            # see the name missing (manifest.json in particular). Only a
            # directory, or a type change, is retired to the trash first.
            live_is_plain_file = live.is_file() and not live.is_symlink()
            if (live.exists() or live.is_symlink()) and not (
                live_is_plain_file and entry.is_file()
            ):
                os.replace(live, trash / entry.name)
        elif live.is_dir() and not live.is_symlink() and entry.is_dir():
            # POSIX rename can't replace a non-empty directory; move the live
            # one aside so the staged directory can land in its place.
            os.replace(live, trash / entry.name)
        os.replace(entry, live)

    # manifest.json last: its presence is what makes the directory readable as
    # a complete session to the index and packers.
    entries = sorted(
        (p for p in staging.iterdir() if p.name != ".trash"),
        key=lambda p: (p.name == "manifest.json", p.name),
    )
    staged_names = {p.name for p in entries}
    replace = mode == "replace"

    for entry in entries:
        if entry.name == "manifest.json":
            continue
        _move_entry(entry, replace=replace)

    if replace:
        # The archive is authoritative: move every live entry it did not
        # produce into the trash too, so local-only files don't survive a pull
        # that is then marked clean.
        for live in sorted(final_dir.iterdir(), key=lambda p: p.name):
            if live.name not in staged_names:
                os.replace(live, trash / live.name)

    if retire:
        _retire_names(final_dir, retire)

    manifest_entry = staging / "manifest.json"
    if manifest_entry.exists():
        _move_entry(manifest_entry, replace=replace)

    fsync_dir(final_dir)


def _retire_names(final_dir: Path, retire: frozenset[str]) -> None:
    """Unlink obsolete files from a live session dir — plain files only.

    Runs under the per-session lock after the commit-time exclusion recheck,
    so a skipped commit never retires a file. Only a name that resolves to a
    plain file directly inside ``final_dir`` is unlinked; symlinks and
    subdirectories are left alone and ``manifest.json`` is never a target.
    """
    for name in sorted(retire):
        if not name or name in (".", "..") or "/" in name or "\\" in name:
            continue
        if name == "manifest.json":
            continue
        target = final_dir / name
        try:
            if target.is_symlink() or not target.is_file():
                continue
            target.unlink()
        except OSError:
            continue


def _content_hash(session_dir: Path) -> str | None:
    """Hash of what a sync would upload for a session, or None if it has none.

    Covers every file ``pack_session`` packs. The manifest is hashed without
    its ``sync`` block, which records upload state rather than content; two
    captures of an unchanged session otherwise produce identical files.
    Files are streamed, so large transcripts aren't loaded into memory.
    """
    if not session_dir.is_dir():
        return None
    digest = hashlib.sha256()
    found = False
    for path in sorted(session_dir.rglob("*")):
        # Skip our own in-flight atomic-write temp files.
        if not path.is_file() or (path.name.startswith(".") and path.name.endswith(".tmp")):
            continue
        rel = path.relative_to(session_dir).as_posix()
        found = True
        digest.update(f"\0{rel}\0".encode())
        try:
            if rel == "manifest.json":
                digest.update(_manifest_without_sync(path))
                continue
            with open(path, "rb") as f:
                for chunk in iter(lambda: f.read(1 << 20), b""):
                    digest.update(chunk)
        except OSError:
            digest.update(b"<unreadable>")
    return digest.hexdigest() if found else None


def _manifest_without_sync(path: Path) -> bytes:
    raw = path.read_bytes()
    try:
        manifest = json.loads(raw)
    except (ValueError, RecursionError):
        return raw
    if not isinstance(manifest, dict):
        return raw
    manifest.pop("sync", None)
    return json.dumps(manifest, sort_keys=True).encode()


def _read_sync_block(manifest_path: Path) -> dict[str, Any] | None:
    """Return a manifest's ``sync`` block, or None if absent or unreadable."""
    try:
        manifest = json.loads(manifest_path.read_text())
    except (OSError, ValueError, RecursionError):
        return None
    sync = manifest.get("sync") if isinstance(manifest, dict) else None
    return dict(sync) if isinstance(sync, dict) else None


def _write_json_atomic(path: Path, data: dict[str, Any]) -> None:
    """Replace ``path`` atomically with an owner-only (0600) JSON file.

    Delegates to the shared fsutil helper so the temp-file write is fsynced
    before the rename and the parent directory is fsynced after it — a crash
    mid-write can therefore never leave a truncated manifest behind.
    """
    from sessionfs.store.fsutil import atomic_write_text

    atomic_write_text(path, json.dumps(data, indent=2))


def _session_lock_for_dir(session_dir: Path):
    """Derive and hold the per-session lock for a session directory path.

    ``session_dir`` is ``<sessions>/<id>.sfs``, so the session id and the
    sessions dir are recovered from the path itself. The manifest
    read-modify-write helpers use this so a caller that already holds the lock
    (``pack_and_hash``) can call an inner unlocked variant instead of
    re-entering the lock (which would self-deadlock on flock).
    """
    name = session_dir.name
    session_id = name[:-4] if name.endswith(".sfs") else name
    return lock_session_dir(session_dir.parent, session_id)


def pack_and_hash(session_dir: Path) -> bytes:
    """Record the content hash, then pack the session, under its lock.

    Hash-first-then-pack inside a single lock acquisition means the returned
    archive bytes always correspond to the recorded ``sync._content_hash``: a
    concurrent capture cannot land between the two. Previously the ack sites
    packed first and hashed second, so a capture between them was hashed as if
    it were the uploaded content and the ack wrongly cleared ``dirty``.
    """
    from sessionfs.sync.archive import pack_session

    with _session_lock_for_dir(session_dir):
        _record_packed_content_hash(session_dir)
        return pack_session(session_dir)


def record_packed_content_hash(session_dir: Path) -> str | None:
    """Record the content hash that is about to be packed for upload.

    Stored as ``sync._content_hash`` in the manifest; ``_content_hash`` strips
    the whole ``sync`` block, so writing this field never changes the hash
    itself. The upload ack then compares the on-disk hash against this value:
    if a capture landed while the upload was in flight they differ and the
    session stays dirty (see ``apply_sync_ack``). Runs under the per-session
    lock so it serializes with a concurrent capture/delete.
    """
    with _session_lock_for_dir(session_dir):
        return _record_packed_content_hash(session_dir)


def _record_packed_content_hash(session_dir: Path) -> str | None:
    """Unlocked inner variant of ``record_packed_content_hash``.

    Callers already holding the per-session lock (``pack_and_hash``) call this
    directly; everyone else goes through the locking wrapper.
    """
    from sessionfs.store.fsutil import atomic_write_text

    try:
        packed = _content_hash(session_dir)
    except Exception:
        # An unreadable session must not abort the upload; leave no packed
        # hash so the ack keeps the session dirty rather than marking it clean.
        return None
    if packed is None:
        return None
    manifest_path = session_dir / "manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text())
    except (OSError, ValueError, RecursionError):
        return None
    if not isinstance(manifest, dict):
        return None
    sync = manifest.get("sync")
    sync = dict(sync) if isinstance(sync, dict) else {}
    sync["_content_hash"] = packed
    manifest["sync"] = sync
    try:
        atomic_write_text(
            manifest_path, json.dumps(manifest, indent=2), create_parent=False
        )
    except OSError:
        # The session dir disappeared mid-write; leave no recorded hash so the
        # ack keeps the session dirty rather than marking it clean.
        return None
    return packed


def apply_sync_ack(session_dir: Path, etag: str) -> None:
    """Record a successful upload ack (etag), clearing ``dirty`` only if the
    content hasn't changed since it was packed.

    If a capture landed during the upload, the current content hash no longer
    matches the one recorded at pack time, so the session stays dirty (and is
    re-queued) while still recording the new etag. If no hash was recorded at
    pack time the session is left dirty rather than wrongly marked clean.
    Runs under the per-session lock so it serializes with a capture/delete.
    """
    with _session_lock_for_dir(session_dir):
        _apply_sync_ack(session_dir, etag)


def _apply_sync_ack(session_dir: Path, etag: str) -> None:
    """Unlocked inner variant of ``apply_sync_ack``."""
    from datetime import datetime, timezone

    from sessionfs.store.fsutil import atomic_write_text

    manifest_path = session_dir / "manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text())
    except (OSError, ValueError, RecursionError):
        return
    if not isinstance(manifest, dict):
        return
    sync = manifest.get("sync")
    sync = dict(sync) if isinstance(sync, dict) else {}
    packed = sync.pop("_content_hash", None)
    current = _content_hash(session_dir)
    sync["etag"] = etag
    sync["last_sync_at"] = datetime.now(timezone.utc).isoformat()
    sync["dirty"] = packed != current
    manifest["sync"] = sync
    try:
        atomic_write_text(
            manifest_path, json.dumps(manifest, indent=2), create_parent=False
        )
    except OSError:
        return


def mark_session_synced(session_dir: Path, etag: str) -> None:
    """Unconditionally record a session as clean at ``etag`` (pull path).

    Unlike ``apply_sync_ack`` this doesn't consult a packed-content hash: the
    content was just received from the server, so it is clean by definition.
    Runs under the per-session lock so it serializes with a capture/delete.
    """
    with _session_lock_for_dir(session_dir):
        _mark_session_synced(session_dir, etag)


def _mark_session_synced(session_dir: Path, etag: str) -> None:
    """Unlocked inner variant of ``mark_session_synced``."""
    from datetime import datetime, timezone

    from sessionfs.store.fsutil import atomic_write_text

    manifest_path = session_dir / "manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text())
    except (OSError, ValueError, RecursionError):
        return
    if not isinstance(manifest, dict):
        return
    sync = manifest.get("sync")
    sync = dict(sync) if isinstance(sync, dict) else {}
    sync.pop("_content_hash", None)
    sync["etag"] = etag
    sync["last_sync_at"] = datetime.now(timezone.utc).isoformat()
    sync["dirty"] = False
    manifest["sync"] = sync
    try:
        atomic_write_text(
            manifest_path, json.dumps(manifest, indent=2), create_parent=False
        )
    except OSError:
        return
