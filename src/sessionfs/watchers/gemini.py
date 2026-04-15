"""Gemini CLI session watcher.

Watches ~/.gemini/tmp/*/chats/ for session changes, discovers sessions,
parses them, and stores .sfs captures.

Gemini sessions are single JSON files (not JSONL) with a flat message array.
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

from sessionfs.daemon.config import GeminiWatcherConfig
from sessionfs.daemon.status import WatcherStatus
from sessionfs.session_id import session_id_from_native
from sessionfs.store.local import LocalStore
from sessionfs.watchers.base import NativeSessionRef, WatcherHealth

logger = logging.getLogger("sfsd.watcher.gemini")


class _GeminiEventHandler(FileSystemEventHandler):
    def __init__(self, queue: list[str], lock: threading.Lock):
        self._queue = queue
        self._lock = lock

    def on_modified(self, event: FileSystemEvent) -> None:
        if event.is_directory:
            return
        if event.src_path.endswith(".json") and "session-" in event.src_path:
            with self._lock:
                if event.src_path not in self._queue:
                    self._queue.append(event.src_path)

    def on_created(self, event: FileSystemEvent) -> None:
        self.on_modified(event)


class GeminiWatcher:
    """Watches Gemini CLI session storage and captures to .sfs."""

    def __init__(
        self,
        config: GeminiWatcherConfig,
        store: LocalStore,
        scan_interval: float = 5.0,
    ) -> None:
        self._home_dir = config.home_dir
        self._store = store
        self._scan_interval = scan_interval
        self._tmp_dir = config.home_dir / "tmp"

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
            self._last_error = f"Gemini home not found: {self._home_dir}"
            return

        try:
            from sessionfs.converters.gemini_to_sfs import discover_gemini_sessions

            sessions = discover_gemini_sessions(self._home_dir)
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
                    project_path=s_info.get("project_path"),
                )
                captured += 1

            self._health = WatcherHealth.HEALTHY
            self._last_scan_at = datetime.now(timezone.utc).isoformat()
            logger.info("Gemini scan: %d found, %d captured", len(sessions), captured)

        except Exception as e:
            logger.error("Gemini full scan failed: %s", e, exc_info=True)
            self._health = WatcherHealth.DEGRADED
            self._last_error = str(e)

    def _capture_session(
        self,
        native_id: str,
        native_path: Path,
        mtime: float,
        size: int,
        project_path: str | None = None,
    ) -> None:
        logger.info("Capturing Gemini session %s (%d bytes)", native_id[:8], size)
        try:
            from sessionfs.converters.gemini_to_sfs import (
                parse_gemini_session,
                convert_gemini_to_sfs,
            )

            from sessionfs.converters.gemini_to_sfs import _extract_model_from_logs

            gemini_session = parse_gemini_session(native_path)
            gemini_session.project_path = project_path

            # Extract model from logs.json (parent of chats/)
            project_dir = native_path.parent.parent
            model_id = _extract_model_from_logs(project_dir, native_id)
            if model_id:
                gemini_session.model_id = model_id

            sfs_id = session_id_from_native(native_id)
            session_dir = self._store.allocate_session_dir(sfs_id)
            convert_gemini_to_sfs(gemini_session, session_dir, session_id=sfs_id)

            # Migration 028: annotate with instruction provenance.
            from sessionfs.watchers.provenance import annotate_manifest_with_provenance
            annotate_manifest_with_provenance(session_dir, "gemini", project_path)

            manifest_path = session_dir / "manifest.json"
            if manifest_path.exists():
                manifest = json.loads(manifest_path.read_text())
                self._store.upsert_session_metadata(sfs_id, manifest, str(session_dir))

            ref = NativeSessionRef(
                tool="gemini-cli",
                native_session_id=native_id,
                native_path=str(native_path),
                sfs_session_id=sfs_id,
                last_mtime=mtime,
                last_size=size,
                last_captured_at=datetime.now(timezone.utc).isoformat(),
                project_path=project_path,
            )
            self._tracked[native_id] = ref
            self._store.upsert_tracked_session(ref)

        except Exception as e:
            logger.error("Failed to capture Gemini session %s: %s", native_id[:8], e, exc_info=True)
            self._last_error = f"Capture failed: {e}"

    def start_watching(self) -> None:
        if not self._tmp_dir.is_dir():
            return
        handler = _GeminiEventHandler(self._event_queue, self._event_lock)
        self._observer = Observer()
        self._observer.schedule(handler, str(self._tmp_dir), recursive=True)
        self._observer.daemon = True
        self._observer.start()
        logger.info("Watching %s for Gemini session changes", self._tmp_dir)

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
            if not path.exists() or not path.name.startswith("session-"):
                continue
            native_id = path.stem.split("-")[-1]  # uuid8 suffix
            stat = path.stat()
            self._capture_session(native_id, path, stat.st_mtime, stat.st_size)

    def get_status(self) -> WatcherStatus:
        return WatcherStatus(
            name="gemini-cli",
            enabled=True,
            health=self._health.value,
            sessions_tracked=len(self._tracked),
            last_scan_at=self._last_scan_at,
            last_error=self._last_error,
            watch_paths=[str(self._tmp_dir)],
        )
