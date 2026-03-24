"""SessionFS daemon entry point.

Foreground process that watches AI tool session directories and captures
sessions into the local .sfs store.

Usage:
    sfsd                    # Start with default config
    sfsd --log-level DEBUG  # Start with debug logging
    sfsd -c /path/to/config.toml
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import signal
import stat
import sys
import time
from pathlib import Path
from typing import Any

from sessionfs.daemon.config import DaemonConfig, ensure_config, load_config, DEFAULT_CONFIG_PATH
from sessionfs.daemon.status import (
    DaemonStatus,
    clear_status,
    write_status,
)
from sessionfs.store.local import LocalStore
from sessionfs.watchers.base import Watcher
from sessionfs.watchers.claude_code import ClaudeCodeWatcher

logger = logging.getLogger("sfsd")


class DaemonSyncer:
    """Handles background sync of captured sessions to the server."""

    def __init__(self, config: DaemonConfig, store: LocalStore) -> None:
        self.config = config
        self.store = store
        self._sync_client = None
        self._consecutive_failures = 0
        self._last_sync_time = 0.0
        self._pending_sessions: set[str] = set()

    @property
    def is_enabled(self) -> bool:
        return self.config.sync.enabled and bool(self.config.sync.api_key)

    @property
    def health(self) -> str:
        if not self.is_enabled:
            return "disabled"
        if self._consecutive_failures >= self.config.sync.retry_max:
            return "degraded"
        return "healthy"

    def _get_client(self):
        """Lazily create the sync client."""
        if self._sync_client is None:
            from sessionfs.sync.client import SyncClient
            self._sync_client = SyncClient(
                api_url=self.config.sync.api_url,
                api_key=self.config.sync.api_key,
            )
        return self._sync_client

    def mark_session_dirty(self, session_id: str) -> None:
        """Mark a session as needing sync."""
        if self.is_enabled:
            self._pending_sessions.add(session_id)

    def maybe_sync(self) -> None:
        """Run sync if enough time has elapsed since last push. Non-blocking."""
        if not self.is_enabled:
            return
        if not self._pending_sessions:
            return

        now = time.monotonic()
        if now - self._last_sync_time < self.config.sync.push_interval:
            return

        self._last_sync_time = now
        # Run sync in a one-shot event loop (daemon is synchronous)
        sessions_to_sync = set(self._pending_sessions)
        try:
            asyncio.run(self._sync_sessions(sessions_to_sync))
        except Exception:
            logger.exception("Sync cycle failed")

    async def _sync_sessions(self, session_ids: set[str]) -> None:
        """Push pending sessions to the server."""
        from sessionfs.sync.archive import pack_session
        from sessionfs.sync.client import SyncConflictError, SyncError

        client = self._get_client()

        for session_id in session_ids:
            session_dir = self.store.get_session_dir(session_id)
            if not session_dir:
                self._pending_sessions.discard(session_id)
                continue

            try:
                archive_data = pack_session(session_dir)
                # Read current etag from manifest sync state
                etag = self._get_local_etag(session_id)

                result = await client.push_session(session_id, archive_data, etag=etag)

                # Store the new etag
                self._store_local_etag(session_id, result.etag)
                self._pending_sessions.discard(session_id)
                self._consecutive_failures = 0

                logger.info(
                    "Synced session %s (etag=%s, size=%d)",
                    session_id[:12],
                    result.etag[:12],
                    result.blob_size_bytes,
                )

            except SyncConflictError as exc:
                logger.warning(
                    "Sync conflict for %s: remote etag=%s. Will retry next cycle.",
                    session_id[:12],
                    exc.current_etag[:12],
                )
                # Update local etag to remote's so next push uses correct If-Match
                self._store_local_etag(session_id, exc.current_etag)

            except SyncError as exc:
                self._consecutive_failures += 1
                logger.warning(
                    "Sync failed for %s (failures=%d/%d): %s",
                    session_id[:12],
                    self._consecutive_failures,
                    self.config.sync.retry_max,
                    exc,
                )

            except Exception:
                self._consecutive_failures += 1
                logger.exception("Unexpected sync error for %s", session_id[:12])

        try:
            await client.close()
        except Exception:
            pass

    def _get_local_etag(self, session_id: str) -> str | None:
        """Read the locally stored etag for a session."""
        manifest = self.store.get_session_manifest(session_id)
        if not manifest:
            return None
        sync_state = manifest.get("sync", {})
        return sync_state.get("etag")

    def _store_local_etag(self, session_id: str, etag: str) -> None:
        """Store the etag in the session's manifest.json sync state."""
        session_dir = self.store.get_session_dir(session_id)
        if not session_dir:
            return
        manifest_path = session_dir / "manifest.json"
        if not manifest_path.exists():
            return

        manifest = json.loads(manifest_path.read_text())
        if "sync" not in manifest:
            manifest["sync"] = {}
        manifest["sync"]["etag"] = etag
        manifest["sync"]["last_sync_at"] = (
            __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat()
        )
        manifest["sync"]["dirty"] = False
        manifest_path.write_text(json.dumps(manifest, indent=2))

    async def close(self) -> None:
        """Clean up the sync client."""
        if self._sync_client:
            await self._sync_client.close()


class Daemon:
    """Main daemon controller."""

    def __init__(self, config: DaemonConfig) -> None:
        self.config = config
        self.store = LocalStore(config.store_dir)
        self.watchers: list[Watcher] = []
        self._running = False
        self._status_path = config.store_dir / "daemon.json"
        self._pid_path = config.store_dir / "sfsd.pid"
        self._syncer = DaemonSyncer(config, self.store)
        self._reload_requested = False

    def _setup_signals(self) -> None:
        """Register signal handlers for graceful shutdown and config reload."""
        signal.signal(signal.SIGTERM, self._handle_shutdown)
        signal.signal(signal.SIGINT, self._handle_shutdown)
        signal.signal(signal.SIGHUP, self._handle_reload)

    def _handle_shutdown(self, signum: int, frame: Any) -> None:
        sig_name = signal.Signals(signum).name
        logger.info("Received %s, shutting down...", sig_name)
        self._running = False

    def _handle_reload(self, signum: int, frame: Any) -> None:
        logger.info("Received SIGHUP, will reload config on next cycle")
        self._reload_requested = True

    def _reload_config(self) -> None:
        """Reload config from disk (for sync settings changes without restart)."""
        try:
            new_config = load_config(DEFAULT_CONFIG_PATH)
            self.config.sync = new_config.sync
            self._syncer.config = self.config
            logger.info("Config reloaded (sync.enabled=%s)", self.config.sync.enabled)
        except Exception:
            logger.exception("Failed to reload config")

    def _init_watchers(self) -> None:
        """Create watcher instances based on config."""
        if self.config.claude_code.enabled:
            watcher = ClaudeCodeWatcher(
                config=self.config.claude_code,
                store=self.store,
                scan_interval=self.config.scan_interval_s,
            )
            self.watchers.append(watcher)

        if self.config.codex.enabled:
            from sessionfs.watchers.codex import CodexWatcher
            codex_watcher = CodexWatcher(
                config=self.config.codex,
                store=self.store,
                scan_interval=self.config.scan_interval_s,
            )
            self.watchers.append(codex_watcher)

        if self.config.gemini.enabled:
            from sessionfs.watchers.gemini import GeminiWatcher
            gemini_watcher = GeminiWatcher(
                config=self.config.gemini,
                store=self.store,
                scan_interval=self.config.scan_interval_s,
            )
            self.watchers.append(gemini_watcher)

        if self.config.copilot.enabled:
            from sessionfs.watchers.copilot import CopilotWatcher
            copilot_watcher = CopilotWatcher(
                config=self.config.copilot,
                store=self.store,
                scan_interval=self.config.scan_interval_s,
            )
            self.watchers.append(copilot_watcher)

        if self.config.cursor.enabled:
            from sessionfs.watchers.cursor import CursorWatcher
            cursor_watcher = CursorWatcher(
                config=self.config.cursor,
                store=self.store,
                scan_interval=self.config.scan_interval_s,
            )
            self.watchers.append(cursor_watcher)

        if self.config.cline.enabled:
            from sessionfs.watchers.cline import ClineWatcher
            cline_watcher = ClineWatcher(
                config=self.config.cline,
                store=self.store,
                scan_interval=self.config.scan_interval_s,
                tool="cline",
            )
            self.watchers.append(cline_watcher)

        if self.config.roo_code.enabled:
            from sessionfs.watchers.roo import RooCodeWatcher
            roo_watcher = RooCodeWatcher(
                config=self.config.roo_code,
                store=self.store,
                scan_interval=self.config.scan_interval_s,
            )
            self.watchers.append(roo_watcher)

        if self.config.amp.enabled:
            from sessionfs.watchers.amp import AmpWatcher
            amp_watcher = AmpWatcher(
                config=self.config.amp,
                store=self.store,
                scan_interval=self.config.scan_interval_s,
            )
            self.watchers.append(amp_watcher)

    def _check_permissions(self) -> None:
        """M8: Check store directory permissions on startup."""
        warnings = self.store.check_permissions()
        for warning in warnings:
            logger.warning("Permission issue: %s", warning)

    def _write_pid(self) -> None:
        self._pid_path.write_text(str(os.getpid()))
        os.chmod(self._pid_path, stat.S_IRUSR | stat.S_IWUSR)  # 0o600

    def _clear_pid(self) -> None:
        self._pid_path.unlink(missing_ok=True)

    def _update_status(self) -> None:
        """Write current daemon status to daemon.json."""
        watcher_statuses = [w.get_status() for w in self.watchers]
        total = sum(ws.sessions_tracked for ws in watcher_statuses)
        status = DaemonStatus(
            store_dir=str(self.config.store_dir),
            watchers=watcher_statuses,
            sessions_total=total,
        )
        write_status(status, self._status_path)

    def _collect_dirty_sessions(self) -> None:
        """Mark all sessions with local changes as needing sync."""
        if not self._syncer.is_enabled:
            return
        for session in self.store.list_sessions():
            session_id = session["session_id"]
            session_dir = self.store.get_session_dir(session_id)
            if not session_dir:
                continue
            manifest_path = session_dir / "manifest.json"
            if not manifest_path.exists():
                continue
            manifest = json.loads(manifest_path.read_text())
            sync_state = manifest.get("sync", {})
            # If no etag stored or marked dirty, it needs sync
            if not sync_state.get("etag") or sync_state.get("dirty", True):
                self._syncer.mark_session_dirty(session_id)

    def run(self) -> None:
        """Main daemon loop."""
        self._setup_signals()
        self.store.initialize()
        self._check_permissions()
        self._init_watchers()
        self._write_pid()

        if self._syncer.is_enabled:
            logger.info("Cloud sync enabled → %s", self.config.sync.api_url)

        logger.info("sfsd starting with %d watcher(s)", len(self.watchers))

        # Initial full scan
        for watcher in self.watchers:
            watcher.full_scan()

        # Start filesystem observers
        for watcher in self.watchers:
            watcher.start_watching()

        # Collect sessions that need initial sync
        self._collect_dirty_sessions()

        self._running = True
        self._update_status()
        logger.info("sfsd running (PID %d)", os.getpid())

        try:
            while self._running:
                try:
                    # Handle config reload
                    if self._reload_requested:
                        self._reload_requested = False
                        self._reload_config()

                    for watcher in self.watchers:
                        watcher.process_events()

                    # Sync any pending sessions
                    self._syncer.maybe_sync()

                    self._update_status()
                except Exception as exc:
                    logger.error("Error in daemon loop (continuing): %s", exc)
                time.sleep(1.0)
        finally:
            logger.info("sfsd shutting down...")
            for watcher in self.watchers:
                watcher.stop_watching()
            self.store.close()
            clear_status(self._status_path)
            self._clear_pid()
            logger.info("sfsd stopped")


def cli_main() -> None:
    """CLI entry point for the daemon."""
    parser = argparse.ArgumentParser(description="SessionFS daemon")
    parser.add_argument(
        "--config", "-c",
        type=Path,
        default=DEFAULT_CONFIG_PATH,
        help="Path to config.toml",
    )
    parser.add_argument(
        "--log-level",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        default=None,
        help="Override log level from config",
    )
    args = parser.parse_args()

    ensure_config(args.config)
    config = load_config(args.config)
    if args.log_level:
        config.log_level = args.log_level

    logging.basicConfig(
        level=getattr(logging, config.log_level),
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        stream=sys.stderr,
    )

    daemon = Daemon(config)
    daemon.run()


if __name__ == "__main__":
    cli_main()
