"""Roo Code VS Code extension session watcher.

Thin wrapper around ClineWatcher for Roo Code. Roo Code is a Cline fork
with an identical storage format, so all parsing logic is shared.

The only differences are:
- Storage path: rooveterinaryinc.roo-cline (vs saoudrizwan.claude-dev)
- Task ID format: UUID (vs Date.now() timestamp)
- Per-task metadata: history_item.json (vs centralised taskHistory.json)
- Index: tasks/_index.json (vs state/taskHistory.json)

Capture-only — no write-back support.
"""

from __future__ import annotations

from sessionfs.daemon.config import RooCodeWatcherConfig
from sessionfs.daemon.status import WatcherStatus
from sessionfs.store.local import LocalStore
from sessionfs.watchers.cline import ClineWatcher


class RooCodeWatcher(ClineWatcher):
    """Watches Roo Code session storage. Delegates to ClineWatcher."""

    def __init__(
        self,
        config: RooCodeWatcherConfig,
        store: LocalStore,
        scan_interval: float = 5.0,
    ) -> None:
        # RooCodeWatcherConfig has the same shape as ClineWatcherConfig
        super().__init__(
            config=config,
            store=store,
            scan_interval=scan_interval,
            tool="roo-code",
        )
