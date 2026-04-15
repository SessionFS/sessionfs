"""Copilot CLI session watcher.

Watches ~/.copilot/session-state/ for session changes, discovers sessions
via filesystem scan, parses them, and stores .sfs captures.

Copilot sessions are directories containing events.jsonl (event stream)
and workspace.yaml (session metadata).
"""

from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from watchdog.events import FileSystemEventHandler, FileSystemEvent
from watchdog.observers import Observer

from sessionfs.daemon.config import CopilotWatcherConfig
from sessionfs.daemon.status import WatcherStatus
from sessionfs.session_id import session_id_from_native
from sessionfs.store.local import LocalStore
from sessionfs.watchers.base import NativeSessionRef, WatcherHealth

logger = logging.getLogger("sfsd.watcher.copilot")


# ---------------------------------------------------------------------------
# Watcher
# ---------------------------------------------------------------------------


class _CopilotEventHandler(FileSystemEventHandler):
    def __init__(self, queue: list[str], lock: threading.Lock):
        self._queue = queue
        self._lock = lock

    def on_modified(self, event: FileSystemEvent) -> None:
        if event.is_directory:
            return
        if event.src_path.endswith("events.jsonl"):
            with self._lock:
                if event.src_path not in self._queue:
                    self._queue.append(event.src_path)

    def on_created(self, event: FileSystemEvent) -> None:
        self.on_modified(event)


class CopilotWatcher:
    """Watches Copilot CLI session storage and captures to .sfs."""

    def __init__(
        self,
        config: CopilotWatcherConfig,
        store: LocalStore,
        scan_interval: float = 5.0,
    ) -> None:
        self._home_dir = config.home_dir
        self._store = store
        self._scan_interval = scan_interval
        self._session_state_dir = config.home_dir / "session-state"

        self._tracked: dict[str, NativeSessionRef] = {}
        self._health = WatcherHealth.HEALTHY
        self._last_scan_at: str | None = None
        self._last_error: str | None = None
        self._last_event_time = 0.0

        self._observer: Observer | None = None
        self._event_queue: list[str] = []
        self._event_lock = threading.Lock()

    def full_scan(self) -> None:
        if not self._home_dir.is_dir():
            self._health = WatcherHealth.DEGRADED
            self._last_error = f"Copilot home not found: {self._home_dir}"
            return

        try:
            from sessionfs.converters.copilot_to_sfs import discover_copilot_sessions

            sessions = discover_copilot_sessions(self._home_dir)
            captured = 0
            for s_info in sessions:
                native_id = s_info["session_id"]
                native_path = Path(s_info["path"])
                if not native_path.exists():
                    continue

                current_mtime = s_info["mtime"]
                current_size = s_info["size_bytes"]

                existing = self._store.get_tracked_session(native_id)
                if (
                    existing
                    and existing.last_mtime >= current_mtime
                    and existing.last_size == current_size
                ):
                    self._tracked[native_id] = existing
                    continue

                self._capture_session(native_id, native_path, current_mtime, current_size)
                captured += 1

            self._health = WatcherHealth.HEALTHY
            self._last_scan_at = datetime.now(timezone.utc).isoformat()
            logger.info("Copilot scan: %d found, %d captured", len(sessions), captured)

        except Exception as e:
            logger.error("Copilot full scan failed: %s", e, exc_info=True)
            self._health = WatcherHealth.DEGRADED
            self._last_error = str(e)

    def _capture_session(
        self, native_id: str, native_path: Path, mtime: float, size: int,
    ) -> None:
        logger.info("Capturing Copilot session %s (%d bytes)", native_id[:12], size)
        try:
            from sessionfs.converters.copilot_to_sfs import (
                convert_copilot_to_sfs,
            )

            sfs_id = session_id_from_native(native_id)
            session_dir = self._store.allocate_session_dir(sfs_id)
            convert_copilot_to_sfs(native_path, session_dir, session_id=sfs_id)

            # Read cwd from workspace.yaml via the parsed session
            cwd = None
            workspace_path = session_dir / "workspace.json"
            if workspace_path.exists():
                ws = json.loads(workspace_path.read_text())
                cwd = ws.get("root_path")

            # Migration 028: annotate with instruction provenance.
            from sessionfs.watchers.provenance import annotate_manifest_with_provenance
            annotate_manifest_with_provenance(session_dir, "copilot", cwd)

            manifest_path = session_dir / "manifest.json"
            if manifest_path.exists():
                manifest = json.loads(manifest_path.read_text())
                self._store.upsert_session_metadata(sfs_id, manifest, str(session_dir))

            ref = NativeSessionRef(
                tool="copilot-cli",
                native_session_id=native_id,
                native_path=str(native_path),
                sfs_session_id=sfs_id,
                last_mtime=mtime,
                last_size=size,
                last_captured_at=datetime.now(timezone.utc).isoformat(),
                project_path=cwd,
            )
            self._tracked[native_id] = ref
            self._store.upsert_tracked_session(ref)

        except Exception as e:
            logger.error("Failed to capture Copilot session %s: %s", native_id[:12], e, exc_info=True)
            self._last_error = f"Capture failed: {e}"

    def start_watching(self) -> None:
        if not self._session_state_dir.is_dir():
            return
        handler = _CopilotEventHandler(self._event_queue, self._event_lock)
        self._observer = Observer()
        self._observer.schedule(handler, str(self._session_state_dir), recursive=True)
        self._observer.daemon = True
        self._observer.start()
        logger.info("Watching %s for Copilot session changes", self._session_state_dir)

    def stop_watching(self) -> None:
        if self._observer:
            self._observer.stop()
            self._observer.join(timeout=5.0)
            self._observer = None

    def process_events(self) -> None:
        now = time.monotonic()
        if now - self._last_event_time < self._scan_interval:
            return

        with self._event_lock:
            if not self._event_queue:
                return
            paths = list(set(self._event_queue))
            self._event_queue.clear()

        self._last_event_time = now

        for path_str in paths:
            path = Path(path_str)
            if not path.exists() or path.name != "events.jsonl":
                continue
            # The session directory is the parent of events.jsonl
            session_dir = path.parent
            native_id = session_dir.name
            stat = path.stat()
            self._capture_session(native_id, session_dir, stat.st_mtime, stat.st_size)

    def get_status(self) -> WatcherStatus:
        return WatcherStatus(
            name="copilot",
            enabled=True,
            health=self._health.value,
            sessions_tracked=len(self._tracked),
            last_scan_at=self._last_scan_at,
            last_error=self._last_error,
            watch_paths=[str(self._session_state_dir)],
        )
