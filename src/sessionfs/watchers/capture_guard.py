"""Compression-safe capture guard + exclusion-list guard.

Two ways a re-capture should be skipped:

1. **Compression**: when AI tools compress/compact their session files
   (rewriting them shorter), the watcher would otherwise overwrite the
   fuller .sfs capture, losing messages.

2. **Intentional delete**: when a session was deleted via the dashboard,
   CLI, or sync 410, ``deleted.json`` records the intent. Without an
   exclusion check, native watchers would re-discover the session from
   the still-present native source (Claude Code JSONL, Codex rollout,
   Gemini session, Cursor composer, etc.) and resurrect it — undoing
   the user's delete and producing orphaned local copies on every
   watcher-managed machine.
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

    Returns False (skip) when:
    - The session is in the local exclusion list (deleted.json) — the
      user intentionally deleted this session, watcher must not resurrect.
    - The new source has fewer messages than the existing capture —
      compression/compaction, not new content.

    Always returns True for first-time captures (no existing .sfs) UNLESS
    the session is excluded.
    """
    # Exclusion-list check first — applies to all captures, including
    # first-time discoveries. A session in deleted.json must not be
    # captured at all until the user explicitly clears the exclusion
    # (via `sfs restore` or `sfs pull <id>`).
    try:
        from sessionfs.store.deleted import is_excluded
        if is_excluded(sfs_id):
            logger.info(
                "Skipping capture of %s: %s session is in local exclusion list "
                "(deleted.json) — use 'sfs restore' or 'sfs pull' to clear",
                sfs_id,
                tool,
            )
            return False
    except Exception as exc:  # pragma: no cover — defensive
        logger.warning("Exclusion check failed for %s: %s", sfs_id, exc)

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
