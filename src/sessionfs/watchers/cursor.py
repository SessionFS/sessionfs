"""Cursor IDE session watcher.

Watches Cursor's global state.vscdb for changes and captures conversations
to .sfs format. Uses the bubble layer (bubbleId:* keys) for reliability.

Cursor is capture-only — write-back is not supported due to the complexity
of Cursor's content-addressed storage and extensive UI state.
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

from sessionfs.daemon.config import CursorWatcherConfig
from sessionfs.daemon.status import WatcherStatus
from sessionfs.session_id import session_id_from_native
from sessionfs.store.local import LocalStore
from sessionfs.watchers.base import NativeSessionRef, WatcherHealth

logger = logging.getLogger("sfsd.watcher.cursor")


class _CursorEventHandler(FileSystemEventHandler):
    def __init__(self, queue: list[str], lock: threading.Lock):
        self._queue = queue
        self._lock = lock

    def on_modified(self, event: FileSystemEvent) -> None:
        if event.is_directory:
            return
        if event.src_path.endswith(".vscdb") or event.src_path.endswith(".vscdb-wal"):
            with self._lock:
                if event.src_path not in self._queue:
                    self._queue.append(event.src_path)


class CursorWatcher:
    """Watches Cursor IDE storage and captures conversations to .sfs."""

    def __init__(
        self,
        config: CursorWatcherConfig,
        store: LocalStore,
        scan_interval: float = 10.0,
    ) -> None:
        self._config = config
        self._store = store
        self._scan_interval = scan_interval
        self._global_db = config.global_db_path
        self._workspace_storage = config.workspace_storage_path

        self._tracked: dict[str, NativeSessionRef] = {}
        self._health = WatcherHealth.HEALTHY
        self._last_scan_at: str | None = None
        self._last_error: str | None = None
        self._last_event_time = 0.0
        self._last_db_mtime = 0.0

        self._observer: Observer | None = None
        self._event_queue: list[str] = []
        self._event_lock = threading.Lock()

    def full_scan(self) -> None:
        if not self._global_db.exists():
            self._health = WatcherHealth.DEGRADED
            self._last_error = f"Cursor global DB not found: {self._global_db}"
            return

        try:
            from sessionfs.converters.cursor_to_sfs import (
                discover_cursor_composers,
                parse_cursor_composer,
                convert_cursor_to_sfs,
            )

            composers = discover_cursor_composers(
                global_db=self._global_db,
                workspace_storage=self._workspace_storage,
            )
            captured = 0

            for comp in composers:
                if comp.is_archived:
                    continue

                native_id = comp.composer_id

                # Check if already captured with same DB mtime
                existing = self._store.get_tracked_session(native_id)
                db_mtime = self._global_db.stat().st_mtime
                if existing and existing.last_mtime >= db_mtime:
                    self._tracked[native_id] = existing
                    continue

                try:
                    session = parse_cursor_composer(native_id, global_db=self._global_db)
                    session.name = comp.name
                    session.workspace_folder = comp.workspace_folder
                    session.mode = comp.mode

                    if session.message_count < 2:
                        continue  # Skip empty/trivial sessions

                    if comp.created_at:
                        session.created_at = datetime.fromtimestamp(
                            comp.created_at / 1000, tz=timezone.utc
                        ).isoformat()
                    if comp.last_updated_at:
                        session.last_updated_at = datetime.fromtimestamp(
                            comp.last_updated_at / 1000, tz=timezone.utc
                        ).isoformat()

                    sfs_id = session_id_from_native(native_id)
                    session_dir = self._store.allocate_session_dir(sfs_id)
                    convert_cursor_to_sfs(session, session_dir, session_id=sfs_id)

                    manifest_path = session_dir / "manifest.json"
                    if manifest_path.exists():
                        manifest = json.loads(manifest_path.read_text())
                        self._store.upsert_session_metadata(sfs_id, manifest, str(session_dir))

                    ref = NativeSessionRef(
                        tool="cursor",
                        native_session_id=native_id,
                        native_path=str(self._global_db),
                        sfs_session_id=sfs_id,
                        last_mtime=db_mtime,
                        last_size=0,
                        last_captured_at=datetime.now(timezone.utc).isoformat(),
                        project_path=comp.workspace_folder,
                    )
                    self._tracked[native_id] = ref
                    self._store.upsert_tracked_session(ref)
                    captured += 1

                except Exception as e:
                    logger.error("Failed to capture Cursor composer %s: %s", native_id[:12], e)

            self._health = WatcherHealth.HEALTHY
            self._last_scan_at = datetime.now(timezone.utc).isoformat()
            self._last_db_mtime = self._global_db.stat().st_mtime
            logger.info("Cursor scan: %d composers, %d captured", len(composers), captured)

        except Exception as e:
            logger.error("Cursor full scan failed: %s", e, exc_info=True)
            self._health = WatcherHealth.DEGRADED
            self._last_error = str(e)

    def start_watching(self) -> None:
        watch_dir = self._global_db.parent
        if not watch_dir.is_dir():
            return
        handler = _CursorEventHandler(self._event_queue, self._event_lock)
        self._observer = Observer()
        self._observer.schedule(handler, str(watch_dir), recursive=False)
        self._observer.daemon = True
        self._observer.start()
        logger.info("Watching %s for Cursor DB changes", watch_dir)

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
            self._event_queue.clear()

        self._last_event_time = now

        # Re-scan on any DB change
        if self._global_db.exists():
            current_mtime = self._global_db.stat().st_mtime
            if current_mtime > self._last_db_mtime:
                self.full_scan()

    def get_status(self) -> WatcherStatus:
        return WatcherStatus(
            name="cursor",
            enabled=True,
            health=self._health.value,
            sessions_tracked=len(self._tracked),
            last_scan_at=self._last_scan_at,
            last_error=self._last_error,
            watch_paths=[str(self._global_db.parent)],
        )
