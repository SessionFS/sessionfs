"""Amp session watcher.

Watches ~/.local/share/amp/threads/ for session changes, discovers sessions,
parses them, and stores .sfs captures.

Amp threads are single JSON files with a flat message array.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from watchdog.events import FileSystemEventHandler, FileSystemEvent
from watchdog.observers import Observer

from sessionfs.daemon.config import AmpWatcherConfig
from sessionfs.daemon.status import WatcherStatus
from sessionfs.session_id import session_id_from_native
from sessionfs.store.local import LocalStore
from sessionfs.watchers.base import NativeSessionRef, WatcherHealth

logger = logging.getLogger("sfsd.watcher.amp")


class _AmpEventHandler(FileSystemEventHandler):
    def __init__(self, queue: list[str], lock: threading.Lock):
        self._queue = queue
        self._lock = lock

    def on_modified(self, event: FileSystemEvent) -> None:
        if event.is_directory:
            return
        if event.src_path.endswith(".json"):
            with self._lock:
                if event.src_path not in self._queue:
                    self._queue.append(event.src_path)

    def on_created(self, event: FileSystemEvent) -> None:
        self.on_modified(event)


class AmpWatcher:
    """Watches Amp thread storage and captures to .sfs."""

    def __init__(
        self,
        config: AmpWatcherConfig,
        store: LocalStore,
        scan_interval: float = 5.0,
    ) -> None:
        self._data_dir = config.data_dir
        self._store = store
        self._scan_interval = scan_interval
        self._threads_dir = config.data_dir / "threads"

        self._tracked: dict[str, NativeSessionRef] = {}
        self._health = WatcherHealth.HEALTHY
        self._last_scan_at: str | None = None
        self._last_error: str | None = None
        self._last_event_time = 0.0

        self._observer: Observer | None = None
        self._event_queue: list[str] = []
        self._event_lock = threading.Lock()

    def full_scan(self) -> None:
        if not self._data_dir.is_dir():
            self._health = WatcherHealth.DEGRADED
            self._last_error = f"Amp data dir not found: {self._data_dir}"
            return

        try:
            from sessionfs.converters.amp_to_sfs import discover_amp_sessions

            sessions = discover_amp_sessions(self._data_dir)
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

                self._capture_session(
                    native_id, native_path, current_mtime, current_size,
                )
                captured += 1

            self._health = WatcherHealth.HEALTHY
            self._last_scan_at = datetime.now(timezone.utc).isoformat()
            logger.info("Amp scan: %d found, %d captured", len(sessions), captured)

        except Exception as e:
            logger.error("Amp full scan failed: %s", e, exc_info=True)
            self._health = WatcherHealth.DEGRADED
            self._last_error = str(e)

    def _capture_session(
        self,
        native_id: str,
        native_path: Path,
        mtime: float,
        size: int,
    ) -> None:
        logger.info("Capturing Amp session %s (%d bytes)", native_id[:8], size)
        try:
            from sessionfs.converters.amp_to_sfs import (
                parse_amp_session,
                convert_amp_to_sfs,
            )

            amp_session = parse_amp_session(native_path)

            sfs_id = session_id_from_native(native_id)
            session_dir = self._store.allocate_session_dir(sfs_id)
            convert_amp_to_sfs(native_path, session_dir, session_id=sfs_id)

            manifest_path = session_dir / "manifest.json"
            if manifest_path.exists():
                manifest = json.loads(manifest_path.read_text())
                self._store.upsert_session_metadata(sfs_id, manifest, str(session_dir))

            ref = NativeSessionRef(
                tool="amp",
                native_session_id=native_id,
                native_path=str(native_path),
                sfs_session_id=sfs_id,
                last_mtime=mtime,
                last_size=size,
                last_captured_at=datetime.now(timezone.utc).isoformat(),
            )
            self._tracked[native_id] = ref
            self._store.upsert_tracked_session(ref)

        except Exception as e:
            logger.error("Failed to capture Amp session %s: %s", native_id[:8], e, exc_info=True)
            self._last_error = f"Capture failed: {e}"

    def start_watching(self) -> None:
        if not self._threads_dir.is_dir():
            return
        handler = _AmpEventHandler(self._event_queue, self._event_lock)
        self._observer = Observer()
        self._observer.schedule(handler, str(self._threads_dir), recursive=False)
        self._observer.daemon = True
        self._observer.start()
        logger.info("Watching %s for Amp session changes", self._threads_dir)

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
            if not path.exists() or not path.suffix == ".json":
                continue
            native_id = path.stem
            stat = path.stat()
            self._capture_session(native_id, path, stat.st_mtime, stat.st_size)

    def get_status(self) -> WatcherStatus:
        return WatcherStatus(
            name="amp",
            enabled=True,
            health=self._health.value,
            sessions_tracked=len(self._tracked),
            last_scan_at=self._last_scan_at,
            last_error=self._last_error,
            watch_paths=[str(self._threads_dir)],
        )
