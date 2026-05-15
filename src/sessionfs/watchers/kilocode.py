"""Kilo Code VS Code extension session watcher.

Thin wrapper around ClineWatcher for Kilo Code. Kilo Code is a fork of
Roo Code (which is itself a fork of Cline). Storage format is identical:
each task is a UUID-named subdirectory under tasks/ containing
api_conversation_history.json and cline_messages.json. The current
extension also writes a legacy uiMessages.json copy.

Differences from Roo:
- Storage path: kilocode.kilo-code (vs rooveterinaryinc.roo-cline)
  Note: marketplace ID is kilocode.Kilo-Code (case-sensitive) but the
  on-disk globalStorage directory is lowercased.
- Atomic writes via safeWriteJson() (temp + rename)

Capture-only — no write-back support.
"""

from __future__ import annotations

from sessionfs.daemon.config import KiloCodeWatcherConfig
from sessionfs.store.local import LocalStore
from sessionfs.watchers.cline import ClineWatcher


class KiloCodeWatcher(ClineWatcher):
    """Watches Kilo Code session storage. Delegates to ClineWatcher."""

    def __init__(
        self,
        config: KiloCodeWatcherConfig,
        store: LocalStore,
        scan_interval: float = 5.0,
    ) -> None:
        super().__init__(
            config=config,
            store=store,
            scan_interval=scan_interval,
            tool="kilo-code",
        )
