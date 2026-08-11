"""Base watcher protocol and shared types.

Every tool-specific watcher (Claude Code, Codex, Gemini CLI, Cursor) implements
the Watcher protocol. The daemon delegates to watchers without knowing
the specifics of each tool's storage format.
"""

from __future__ import annotations

import enum
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Protocol

from sessionfs.daemon.status import WatcherStatus

logger = logging.getLogger("sessionfs.watchers.base")


class WatcherHealth(enum.Enum):
    """Watcher health state."""

    HEALTHY = "healthy"
    DEGRADED = "degraded"
    BROKEN = "broken"
    DISABLED = "disabled"


class CaptureHealthTracker:
    """Shared per-watcher capture-health state machine.

    Tracks consecutive parse/convert failures and successes to detect when a
    tool has shipped a format change that silently breaks capture.  A watcher
    that sees native file activity but fails to parse 3 sessions in a row is
    degraded; a watcher with no native activity is NEVER degraded (the idle
    guard — an unused tool is not a broken tool).

    Degradation emits ``capture_degraded`` telemetry EXACTLY once per tool per
    install (marker-file gated via ``emit_once``).  Recovery logs one INFO line
    and clears the error.
    """

    FAILURE_THRESHOLD = 3

    def __init__(self, tool_name: str) -> None:
        self._tool_name = tool_name
        self._consecutive_failures: int = 0
        self._failing_keys: set[str] = set()
        self._health: WatcherHealth = WatcherHealth.HEALTHY
        self._last_error: str | None = None
        self._degraded_since: str | None = None

    # -- read-only properties ------------------------------------------------

    @property
    def health(self) -> WatcherHealth:
        return self._health

    @property
    def last_error(self) -> str | None:
        return self._last_error

    @property
    def degraded_since(self) -> str | None:
        return self._degraded_since

    # -- structural state (dir not found, full-scan crash, etc.) ------------

    def set_degraded(self, error_msg: str) -> None:
        """Immediate degradation for structural failures (missing dir, etc.).

        Does NOT count toward the parse-failure threshold — structural
        failures are always a hard degrade.
        """
        was_healthy = self._health == WatcherHealth.HEALTHY
        self._health = WatcherHealth.DEGRADED
        self._last_error = error_msg
        if self._degraded_since is None:
            self._degraded_since = datetime.now(timezone.utc).isoformat()
        if was_healthy:
            self._emit_degraded()

    def set_healthy(self) -> None:
        """Clear STRUCTURAL degradation only (e.g. a watched dir reappears).

        Watchers call this when a scan completes structurally — which says
        nothing about whether captures inside it parsed. Per-capture
        degradation is cleared ONLY by record_success(); without this guard,
        the end-of-scan call would wipe a format-break degradation before
        daemon.json / sfs doctor ever saw it.
        """
        if self._consecutive_failures >= self.FAILURE_THRESHOLD:
            return  # capture-degraded — only a successful capture recovers
        self._health = WatcherHealth.HEALTHY
        self._last_error = None
        self._degraded_since = None

    # -- per-capture counters ------------------------------------------------

    def record_success(self) -> None:
        """Call after a successful parse + convert.

        Resets the failure counter.  If the watcher was degraded it recovers
        to healthy and logs one INFO line.
        """
        self._consecutive_failures = 0
        self._failing_keys.clear()
        # A success always clears the stale error string — not only on the
        # degraded→healthy transition (a sub-threshold failure would otherwise
        # leave its message in the status forever).
        self._last_error = None
        if self._health == WatcherHealth.DEGRADED:
            self._health = WatcherHealth.HEALTHY
            self._degraded_since = None
            logger.info(
                "Watcher %s recovered — capture is healthy again",
                self._tool_name,
            )

    def record_failure(self, error_msg: str, session_key: str | None = None) -> None:
        """Call after a failed parse/convert of a SINGLE session.

        Increments the consecutive-failure counter.  When the counter reaches
        FAILURE_THRESHOLD the watcher is marked degraded (transition only —
        repeat ticks do not re-emit).

        ``session_key`` (the native session id/path) distinguishes a FORMAT
        BREAK (many sessions failing) from ONE corrupt file retried every scan:
        degradation additionally requires failures from at least 2 distinct
        sessions when keys are provided. Callers that pass no key keep the
        plain consecutive-count behavior.
        """
        self._consecutive_failures += 1
        self._last_error = error_msg
        if session_key is not None:
            self._failing_keys.add(session_key)

        distinct_ok = session_key is None or len(self._failing_keys) >= 2
        if self._consecutive_failures >= self.FAILURE_THRESHOLD and distinct_ok:
            if self._health != WatcherHealth.DEGRADED:
                self._health = WatcherHealth.DEGRADED
                self._degraded_since = datetime.now(timezone.utc).isoformat()
                logger.warning(
                    "Watcher %s degraded after %d consecutive capture failures: %s",
                    self._tool_name,
                    self._consecutive_failures,
                    error_msg,
                )
                self._emit_degraded()

    # -- telemetry -----------------------------------------------------------

    def _emit_degraded(self) -> None:
        """Emit capture_degraded once per tool per install (marker-gated)."""
        try:
            from sessionfs.telemetry import emit_once

            emit_once(
                "capture_degraded",
                f"capture-degraded-{self._tool_name}",
                tool=self._tool_name,
            )
        except Exception:
            pass  # telemetry is fire-and-forget


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
