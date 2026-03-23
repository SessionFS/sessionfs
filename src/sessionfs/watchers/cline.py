"""Cline VS Code extension session watcher.

Watches Cline's globalStorage/tasks/ directory for new or modified task
subdirectories and captures conversations to .sfs format.

Also serves as the base watcher for Roo Code (a Cline fork with identical
storage format). The `tool` parameter controls which tool name is used
in manifests and status reporting.

Both are capture-only — write-back is not supported due to the complexity
of reconstructing Cline's task history state for automated injection.
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

from sessionfs.daemon.config import ClineWatcherConfig
from sessionfs.daemon.status import WatcherStatus
from sessionfs.session_id import session_id_from_native
from sessionfs.store.local import LocalStore
from sessionfs.watchers.base import NativeSessionRef, WatcherHealth

logger = logging.getLogger("sfsd.watcher.cline")


class _ClineEventHandler(FileSystemEventHandler):
    """Watches for changes inside tasks/ subdirectories."""

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


class ClineWatcher:
    """Watches Cline/Roo Code globalStorage and captures sessions to .sfs."""

    def __init__(
        self,
        config: ClineWatcherConfig,
        store: LocalStore,
        scan_interval: float = 5.0,
        tool: str = "cline",
    ) -> None:
        self._config = config
        self._store = store
        self._scan_interval = scan_interval
        self._tool = tool
        self._storage_dir = config.storage_dir
        self._tasks_dir = config.storage_dir / "tasks"

        self._tracked: dict[str, NativeSessionRef] = {}
        self._health = WatcherHealth.HEALTHY
        self._last_scan_at: str | None = None
        self._last_error: str | None = None
        self._last_event_time = 0.0

        self._observer: Observer | None = None
        self._event_queue: list[str] = []
        self._event_lock = threading.Lock()

    def full_scan(self) -> None:
        if not self._storage_dir.is_dir():
            self._health = WatcherHealth.DEGRADED
            self._last_error = f"{self._tool} storage not found: {self._storage_dir}"
            return

        try:
            from sessionfs.converters.cline_to_sfs import (
                discover_cline_sessions,
                parse_cline_session,
                convert_cline_to_sfs,
            )

            sessions = discover_cline_sessions(self._storage_dir, tool=self._tool)
            captured = 0

            for s_info in sessions:
                native_id = s_info["session_id"]
                task_path = Path(s_info["path"])
                if not task_path.is_dir():
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
                    native_id, task_path, current_mtime, current_size,
                )
                captured += 1

            self._health = WatcherHealth.HEALTHY
            self._last_scan_at = datetime.now(timezone.utc).isoformat()
            logger.info(
                "%s scan: %d found, %d captured",
                self._tool, len(sessions), captured,
            )

        except Exception as e:
            logger.error("%s full scan failed: %s", self._tool, e, exc_info=True)
            self._health = WatcherHealth.DEGRADED
            self._last_error = str(e)

    def _capture_session(
        self,
        native_id: str,
        task_path: Path,
        mtime: float,
        size: int,
    ) -> None:
        logger.info(
            "Capturing %s session %s (%d bytes)",
            self._tool, native_id[:12], size,
        )
        try:
            from sessionfs.converters.cline_to_sfs import (
                parse_cline_session,
                convert_cline_to_sfs,
            )

            cline_session = parse_cline_session(task_path, tool=self._tool)

            if cline_session.message_count < 2:
                return  # Skip empty/trivial sessions

            sfs_id = session_id_from_native(native_id)
            session_dir = self._store.allocate_session_dir(sfs_id)
            convert_cline_to_sfs(cline_session, session_dir, session_id=sfs_id)

            manifest_path = session_dir / "manifest.json"
            if manifest_path.exists():
                manifest = json.loads(manifest_path.read_text())
                self._store.upsert_session_metadata(sfs_id, manifest, str(session_dir))

            ref = NativeSessionRef(
                tool=self._tool,
                native_session_id=native_id,
                native_path=str(task_path),
                sfs_session_id=sfs_id,
                last_mtime=mtime,
                last_size=size,
                last_captured_at=datetime.now(timezone.utc).isoformat(),
                project_path=cline_session.workspace_folder,
            )
            self._tracked[native_id] = ref
            self._store.upsert_tracked_session(ref)

        except Exception as e:
            logger.error(
                "Failed to capture %s session %s: %s",
                self._tool, native_id[:12], e, exc_info=True,
            )
            self._last_error = f"Capture failed: {e}"

    def start_watching(self) -> None:
        if not self._tasks_dir.is_dir():
            return
        handler = _ClineEventHandler(self._event_queue, self._event_lock)
        self._observer = Observer()
        self._observer.schedule(handler, str(self._tasks_dir), recursive=True)
        self._observer.daemon = True
        self._observer.start()
        logger.info(
            "Watching %s for %s session changes", self._tasks_dir, self._tool,
        )

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

        # Deduplicate by task directory
        task_dirs_seen: set[str] = set()
        for path_str in paths:
            path = Path(path_str)
            # Resolve task dir: tasks/{task-id}/some_file.json
            task_dir = path.parent
            if task_dir.name == "tasks":
                continue  # Not inside a task subdir
            if str(task_dir) in task_dirs_seen:
                continue
            task_dirs_seen.add(str(task_dir))

            if not task_dir.is_dir():
                continue
            native_id = task_dir.name
            api_file = task_dir / "api_conversation_history.json"
            ref_file = api_file if api_file.exists() else task_dir / "ui_messages.json"
            if not ref_file.exists():
                continue
            stat = ref_file.stat()
            self._capture_session(native_id, task_dir, stat.st_mtime, stat.st_size)

    def get_status(self) -> WatcherStatus:
        return WatcherStatus(
            name=self._tool,
            enabled=True,
            health=self._health.value,
            sessions_tracked=len(self._tracked),
            last_scan_at=self._last_scan_at,
            last_error=self._last_error,
            watch_paths=[str(self._tasks_dir)],
        )
