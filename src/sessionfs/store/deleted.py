"""Local exclusion list for deleted sessions.

Manages ~/.sessionfs/deleted.json to track which sessions have been
intentionally deleted. The daemon and CLI check this before sync
operations to prevent re-pushing or re-pulling deleted sessions.

Race-safe: all mutating operations acquire a file lock (.deleted.lock)
before reading, so two concurrent writers cannot clobber each other's
entries. Read-only operations (is_excluded, list_deleted, get_entry)
skip the lock for performance.
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger("sessionfs.store.deleted")

_DEFAULT_DIR = Path.home() / ".sessionfs"
_DEFAULT_PATH = _DEFAULT_DIR / "deleted.json"


def _deleted_path(base_dir: Path | None = None) -> Path:
    """Return the path to deleted.json."""
    if base_dir is not None:
        return base_dir / "deleted.json"
    return _DEFAULT_PATH


def _read_deleted(path: Path) -> dict[str, Any]:
    """Read the deleted.json file. Returns empty dict if missing/corrupt."""
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return {}
        return data
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Could not read %s: %s", path, exc)
        return {}


def _write_deleted(path: Path, data: dict[str, Any]) -> None:
    """Atomically write the deleted.json file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    # Write to temp file in same directory, then rename (atomic on POSIX)
    fd, tmp_path = tempfile.mkstemp(
        dir=str(path.parent), suffix=".tmp", prefix="deleted_"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, default=str)
        os.replace(tmp_path, str(path))
    except Exception:
        # Clean up temp file on failure
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _lock_path(path: Path) -> Path:
    """Return the lock file companion for a deleted.json path."""
    return path.with_suffix(".lock")


def mark_deleted(
    session_id: str,
    scope: str,
    base_dir: Path | None = None,
) -> None:
    """Add a session to the local exclusion list.

    Args:
        session_id: The session ID to mark as deleted.
        scope: One of 'cloud', 'local', 'everywhere'.
        base_dir: Override base directory (for testing).
    """
    path = _deleted_path(base_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = _lock_path(path)
    with open(lock, "a") as lf:
        fcntl.flock(lf.fileno(), fcntl.LOCK_EX)
        try:
            data = _read_deleted(path)
            data[session_id] = {
                "deleted_at": datetime.now(timezone.utc).isoformat(),
                "scope": scope,
            }
            _write_deleted(path, data)
        finally:
            fcntl.flock(lf.fileno(), fcntl.LOCK_UN)
    logger.info("Marked session %s as deleted (scope=%s)", session_id, scope)


def is_excluded(session_id: str, base_dir: Path | None = None) -> bool:
    """Check if a session is in the local exclusion list."""
    path = _deleted_path(base_dir)
    data = _read_deleted(path)
    return session_id in data


def remove_exclusion(session_id: str, base_dir: Path | None = None) -> None:
    """Remove a session from the local exclusion list."""
    path = _deleted_path(base_dir)
    lock = _lock_path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock, "a") as lf:
        fcntl.flock(lf.fileno(), fcntl.LOCK_EX)
        try:
            data = _read_deleted(path)
            if session_id in data:
                del data[session_id]
                _write_deleted(path, data)
        finally:
            fcntl.flock(lf.fileno(), fcntl.LOCK_UN)
    logger.info("Removed exclusion for session %s", session_id)


def list_deleted(base_dir: Path | None = None) -> dict[str, Any]:
    """Return all entries from the local exclusion list."""
    path = _deleted_path(base_dir)
    return _read_deleted(path)


def get_entry(session_id: str, base_dir: Path | None = None) -> dict[str, Any] | None:
    """Get the exclusion entry for a session, or None."""
    path = _deleted_path(base_dir)
    data = _read_deleted(path)
    return data.get(session_id)
