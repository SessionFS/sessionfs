"""Shared helper for cleaning up locally when the server says a session is deleted.

Called from three sync paths:
- `sfs sync` (bulk CLI sync)
- `sfs push <id>` (explicit CLI push)
- daemon autosync loop

Each path catches `SyncDeletedError` and calls `cleanup_deleted_session()`.
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path

logger = logging.getLogger("sessionfs.sync.deleted_cleanup")


def cleanup_deleted_session(
    session_id: str,
    session_dir: Path | None = None,
    store: object | None = None,
    base_dir: Path | None = None,
) -> None:
    """Remove a server-deleted session from local state.

    1. Add to the active profile's deleted.json (scope=everywhere)
    2. Remove the .sfs directory if present
    3. Remove from SQLite index (sessions + tracked_sessions)

    `base_dir` scopes the exclusion file to the active profile's store
    (tk_457d060822bc48c0 R1 MED #3). When None, falls back to the global
    ~/.sessionfs/deleted.json default — callers in the profile-aware
    sync/delete paths and the daemon should pass their resolved /
    pinned store dir so two accounts don't share one exclusion file.

    Non-fatal: logs warnings on failure but never raises.
    Recovery: `sfs restore <id>` + `sfs pull <id>` (30-day window).
    """
    from sessionfs.store.deleted import is_excluded, mark_deleted

    # 1. Exclusion list
    try:
        if not is_excluded(session_id, base_dir=base_dir):
            mark_deleted(session_id, "everywhere", base_dir=base_dir)
            logger.info("Auto-excluded %s (server 410)", session_id)
    except Exception as exc:
        logger.warning("Failed to mark %s as deleted: %s", session_id, exc)

    # 2. Remove .sfs directory
    if session_dir and session_dir.is_dir():
        try:
            shutil.rmtree(session_dir)
            logger.info("Removed local dir %s", session_dir)
        except OSError as exc:
            logger.warning("Failed to remove %s: %s", session_dir, exc)

    # 3. Remove from SQLite index (sessions + tracked_sessions)
    if store is not None:
        try:
            conn = getattr(getattr(store, "index", None), "_conn", None)
            if conn is not None:
                conn.execute(
                    "DELETE FROM sessions WHERE session_id = ?", (session_id,)
                )
                # tracked_sessions maps native tool sessions → .sfs IDs
                conn.execute(
                    "DELETE FROM tracked_sessions WHERE sfs_session_id = ?",
                    (session_id,),
                )
                conn.commit()
        except Exception as exc:
            logger.warning("Failed to clean index for %s: %s", session_id, exc)
