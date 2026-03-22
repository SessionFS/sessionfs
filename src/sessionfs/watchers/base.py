"""Base watcher protocol and shared types.

Every tool-specific watcher (Claude Code, Codex, Gemini CLI, Cursor) implements
the Watcher protocol. The daemon delegates to watchers without knowing
the specifics of each tool's storage format.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

from sessionfs.daemon.status import WatcherStatus


class WatcherHealth(enum.Enum):
    """Watcher health state."""

    HEALTHY = "healthy"
    DEGRADED = "degraded"
    BROKEN = "broken"
    DISABLED = "disabled"


@dataclass
class NativeSessionRef:
    """Tracks a native tool session for change detection."""

    tool: str
    native_session_id: str
    native_path: str
    sfs_session_id: str | None = None
    last_mtime: float = 0.0
    last_size: int = 0
    last_captured_at: str | None = None
    project_path: str | None = None


@dataclass
class WatchEvent:
    """A filesystem change event."""

    event_type: str  # "modified", "created", "deleted"
    path: str
    timestamp: datetime = field(default_factory=datetime.now)


class Watcher(Protocol):
    """Protocol that all tool-specific watchers must implement."""

    def full_scan(self) -> None:
        """Discover all existing sessions and capture any that are new/changed."""
        ...

    def start_watching(self) -> None:
        """Start the filesystem observer for real-time change detection."""
        ...

    def stop_watching(self) -> None:
        """Stop the filesystem observer."""
        ...

    def process_events(self) -> None:
        """Process any queued filesystem events (called from main loop)."""
        ...

    def get_status(self) -> WatcherStatus:
        """Return current watcher status for daemon.json."""
        ...
