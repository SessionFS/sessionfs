"""Compression-safe capture guard.

When AI tools compress/compact their session files (rewriting them shorter),
the watcher would otherwise re-capture the compressed version and overwrite
the fuller .sfs capture, losing messages. This guard prevents that.
"""

from __future__ import annotations

import json
import logging

from sessionfs.store.local import LocalStore

logger = logging.getLogger("sfsd.capture_guard")


def should_recapture(
    store: LocalStore,
    sfs_id: str,
    new_message_count: int,
    tool: str,
) -> bool:
    """Return True if the session should be re-captured.

    Returns False (skip) when the new source has fewer messages than
    the existing capture — this indicates compression/compaction, not
    new content. Logs the decision at INFO level.

    Always returns True for first-time captures (no existing .sfs).
    """
    existing_dir = store.get_session_dir(sfs_id)
    if not existing_dir or not existing_dir.is_dir():
        return True  # First capture

    manifest_path = existing_dir / "manifest.json"
    if not manifest_path.exists():
        return True  # No manifest to compare against

    try:
        manifest = json.loads(manifest_path.read_text())
        existing_count = manifest.get("stats", {}).get("message_count", 0)
    except (json.JSONDecodeError, OSError):
        return True  # Can't read existing manifest — proceed with capture

    if new_message_count < existing_count:
        logger.info(
            "Skipping re-capture of %s: %s compressed (%d → %d messages)",
            sfs_id,
            tool,
            existing_count,
            new_message_count,
        )
        return False

    return True
