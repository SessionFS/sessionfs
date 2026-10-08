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

import contextlib
import hashlib
import json
import logging
import os
import sqlite3
import stat
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any, NamedTuple

from sessionfs.store.index import SessionIndex
from sessionfs.watchers.base import NativeSessionRef

logger = logging.getLogger("sessionfs.store")

# M2: Session ID validation at store layer — imported from canonical module
from sessionfs.session_id import validate_session_id


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
        """
        session_dir = self._sessions_dir / f"{session_id}.sfs"
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
    """Replace ``path`` atomically with an owner-only (0600) JSON file."""
    # mkstemp creates a uniquely named file with O_EXCL and mode 0600, so two
    # writers can't share a temp file and a planted symlink isn't followed.
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as f:
            f.write(json.dumps(data, indent=2))
        os.replace(tmp_name, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise
