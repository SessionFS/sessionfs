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
        self._debounce_timestamps: dict[str, float] = {}
        self._watchlist: set[str] = set()
        self._last_settings_check = 0.0

    @property
    def is_enabled(self) -> bool:
        return self.config.sync.enabled and bool(self.config.sync.api_key)

    @property
    def auto_mode(self) -> str:
        return self.config.sync.auto

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
        """Mark a session as needing sync, respecting autosync mode."""
        if not self.is_enabled:
            return

        mode = self.auto_mode
        if mode == "off":
            # Still allow explicit pushes via _pending_sessions
            return
        elif mode == "selective":
            if session_id not in self._watchlist:
                return
        # mode == "all" or watchlisted in selective — debounce and queue
        self._debounce_timestamps[session_id] = time.monotonic()

    def add_to_watchlist(self, session_id: str) -> None:
        """Add a session to the local autosync watchlist."""
        self._watchlist.add(session_id)

    def remove_from_watchlist(self, session_id: str) -> None:
        """Remove a session from the local autosync watchlist."""
        self._watchlist.discard(session_id)
        self._debounce_timestamps.pop(session_id, None)

    def maybe_sync(self) -> None:
        """Run sync if enough time has elapsed since last push. Non-blocking."""
        if not self.is_enabled:
            return

        # Check for settings changes from API (every 60s)
        self._maybe_check_remote_settings()

        # Promote debounced sessions to pending
        now = time.monotonic()
        debounce = self.config.sync.debounce
        ready = [
            sid for sid, ts in list(self._debounce_timestamps.items())
            if now - ts >= debounce
        ]
        for sid in ready:
            self._pending_sessions.add(sid)
            del self._debounce_timestamps[sid]

        if not self._pending_sessions:
            return

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

        # Mark all as queued before starting
        for session_id in session_ids:
            await self._update_watch_status(session_id, "queued", client)

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

                # Update watchlist status on server
                await self._update_watch_status(session_id, "synced", client)

                # Auto-audit after sync if configured
                await self._maybe_auto_audit(session_id, client)

            except SyncConflictError as exc:
                logger.warning(
                    "Sync conflict for %s: remote etag=%s. Will retry next cycle.",
                    session_id[:12],
                    exc.current_etag[:12],
                )
                # Update local etag to remote's so next push uses correct If-Match,
                # but keep dirty=true so _collect_dirty_sessions re-queues it.
                self._store_local_etag(session_id, exc.current_etag)
                self._mark_session_dirty_flag(session_id)

            except SyncError as exc:
                self._consecutive_failures += 1
                await self._update_watch_status(session_id, "failed", client)
                logger.warning(
                    "Sync failed for %s (failures=%d/%d): %s",
                    session_id[:12],
                    self._consecutive_failures,
                    self.config.sync.retry_max,
                    exc,
                )

            except Exception:
                self._consecutive_failures += 1
                await self._update_watch_status(session_id, "failed", client)
                logger.exception("Unexpected sync error for %s", session_id[:12])

        try:
            await client.close()
        except Exception:
            pass

    async def _update_watch_status(self, session_id: str, status: str, client) -> None:
        """Update watchlist status on the server (non-critical)."""
        try:
            import httpx
            async with httpx.AsyncClient(timeout=5) as http:
                await http.put(
                    f"{client.api_url}/api/v1/sync/watch/{session_id}/{status}",
                    headers={"Authorization": f"Bearer {client.api_key}"},
                )
        except Exception:
            pass  # Non-critical — status is cosmetic

    async def _maybe_auto_audit(self, session_id: str, client) -> None:
        """Trigger audit after sync if user has audit_trigger=on_sync."""
        if not hasattr(self, '_audit_trigger'):
            self._audit_trigger = "manual"
        if self._audit_trigger != "on_sync":
            return
        try:
            import httpx

            async with httpx.AsyncClient(timeout=30) as http:
                resp = await http.post(
                    f"{client.api_url}/api/v1/sessions/{session_id}/audit",
                    headers={"Authorization": f"Bearer {client.api_key}"},
                    json={},
                )
                if resp.status_code in (200, 201, 202):
                    logger.info("Auto-audit triggered for %s", session_id[:12])
                else:
                    logger.debug("Auto-audit skipped for %s: %d", session_id[:12], resp.status_code)
        except Exception as e:
            logger.debug("Auto-audit failed for %s: %s", session_id[:12], e)

    def _maybe_check_remote_settings(self) -> None:
        """Poll API for settings changes every 60 seconds. Non-blocking."""
        now = time.monotonic()
        if now - self._last_settings_check < 60:
            return
        self._last_settings_check = now

        try:
            asyncio.run(self._fetch_remote_settings())
        except Exception:
            pass  # Offline — keep using local config

    async def _fetch_remote_settings(self) -> None:
        """Async fetch of remote settings."""
        import httpx

        client = self._get_client()
        async with httpx.AsyncClient(timeout=10) as http:
            resp = await http.get(
                f"{client.api_url}/api/v1/sync/settings",
                headers={"Authorization": f"Bearer {client.api_key}"},
            )
            if resp.status_code == 200:
                data = resp.json()
                new_mode = data.get("mode", "off")
                if new_mode != self.config.sync.auto:
                    logger.info("Autosync mode changed: %s -> %s", self.config.sync.auto, new_mode)
                    self.config.sync.auto = new_mode
                self.config.sync.debounce = data.get("debounce_seconds", 30)

            # Fetch remote watchlist for selective autosync — replace local
            # set entirely so unwatches on other clients propagate.
            if self.auto_mode == "selective":
                try:
                    wl_resp = await http.get(
                        f"{client.api_url}/api/v1/sync/watchlist",
                        headers={"Authorization": f"Bearer {client.api_key}"},
                    )
                    if wl_resp.status_code == 200:
                        wl_data = wl_resp.json()
                        # Server returns {"sessions": [{"session_id": ...}, ...]}
                        entries = wl_data.get("sessions", [])
                        remote_ids: set[str] = set()
                        for entry in entries:
                            sid = entry.get("session_id", "") if isinstance(entry, dict) else ""
                            if sid:
                                remote_ids.add(sid)
                        # Purge unwatched sessions from debounce and pending
                        removed = (self._watchlist - remote_ids)
                        for sid in removed:
                            self._debounce_timestamps.pop(sid, None)
                            self._pending_sessions.discard(sid)
                        # Detect newly added watches and queue dirty ones
                        added = remote_ids - self._watchlist
                        self._watchlist = remote_ids
                        if added:
                            self._enqueue_dirty_watched(added)
                except Exception:
                    pass  # Non-critical — keep using local watchlist

            resp2 = await http.get(
                f"{client.api_url}/api/v1/settings/audit-trigger",
                headers={"Authorization": f"Bearer {client.api_key}"},
            )
            if resp2.status_code == 200:
                self._audit_trigger = resp2.json().get("trigger", "manual")

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

    def _enqueue_dirty_watched(self, session_ids: set[str]) -> None:
        """Queue newly watchlisted sessions that are already dirty locally."""
        for session_id in session_ids:
            session_dir = self.store.get_session_dir(session_id)
            if not session_dir:
                continue
            manifest_path = session_dir / "manifest.json"
            if not manifest_path.exists():
                continue
            manifest = json.loads(manifest_path.read_text())
            sync_state = manifest.get("sync", {})
            if not sync_state.get("etag") or sync_state.get("dirty", True):
                self._debounce_timestamps[session_id] = time.monotonic()

    def _mark_session_dirty_flag(self, session_id: str) -> None:
        """Set dirty=true in manifest so _collect_dirty_sessions re-queues it."""
        session_dir = self.store.get_session_dir(session_id)
        if not session_dir:
            return
        manifest_path = session_dir / "manifest.json"
        if not manifest_path.exists():
            return
        manifest = json.loads(manifest_path.read_text())
        if "sync" not in manifest:
            manifest["sync"] = {}
        manifest["sync"]["dirty"] = True
        manifest_path.write_text(json.dumps(manifest, indent=2))

    async def close(self) -> None:
        """Clean up the sync client."""
        if self._sync_client:
            await self._sync_client.close()


class Daemon:
    """Main daemon controller."""

    _PRUNE_INTERVAL = 3600  # Check every hour

    def __init__(self, config: DaemonConfig) -> None:
        self.config = config
        self.store = LocalStore(config.store_dir)
        self.watchers: list[Watcher] = []
        self._running = False
        self._status_path = config.store_dir / "daemon.json"
        self._pid_path = config.store_dir / "sfsd.pid"
        self._syncer = DaemonSyncer(config, self.store)
        self._reload_requested = False
        self._last_prune_check = 0.0
        self._capture_paused = False

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

    def _maybe_prune(self) -> None:
        """Run pruning check if enough time has elapsed."""
        now = time.monotonic()
        if now - self._last_prune_check < self._PRUNE_INTERVAL:
            return
        self._last_prune_check = now

        try:
            from sessionfs.store.pruner import (
                SessionPruner,
                StorageConfig,
                _human_bytes,
                parse_size,
            )

            sc = StorageConfig()
            storage_cfg = self.config.storage
            sc.max_local_bytes = parse_size(storage_cfg.max_local_storage)
            sc.local_retention_days = storage_cfg.local_retention_days
            sc.synced_retention_days = storage_cfg.synced_retention_days
            sc.preserve_bookmarked = storage_cfg.preserve_bookmarked
            sc.preserve_aliased = storage_cfg.preserve_aliased

            pruner = SessionPruner(self.store.sessions_dir, self.store.index.conn)
            usage = pruner.calculate_usage()

            # Disk space warnings
            pct = (
                usage.total_bytes / sc.max_local_bytes * 100
                if sc.max_local_bytes > 0
                else 0
            )

            if pct >= 95:
                logger.error(
                    "Local storage full (%s / %s). "
                    "Session capture paused. Run 'sfs storage prune' to free space.",
                    _human_bytes(usage.total_bytes),
                    _human_bytes(sc.max_local_bytes),
                )
                self._capture_paused = True
            elif pct >= 80:
                logger.warning(
                    "Local storage at %s / %s (%.0f%%). "
                    "Run 'sfs storage prune' or increase limit.",
                    _human_bytes(usage.total_bytes),
                    _human_bytes(sc.max_local_bytes),
                    pct,
                )
                self._capture_paused = False
            else:
                self._capture_paused = False

            # Auto-prune synced sessions past retention
            result = pruner.prune(sc, dry_run=False, force=False)
            if result.pruned_count > 0:
                logger.info(
                    "Pruned %d sessions, freed %s",
                    result.pruned_count,
                    result.freed_bytes_human,
                )
        except Exception:
            logger.exception("Prune check failed (non-critical)")

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

        # Fetch remote settings + watchlist before collecting dirty sessions
        # so selective mode has the watchlist populated at startup.
        if self._syncer.is_enabled:
            try:
                asyncio.run(self._syncer._fetch_remote_settings())
            except Exception:
                pass  # Offline — proceed with local state

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

                    # Prune check (hourly)
                    self._maybe_prune()

                    if not self._capture_paused:
                        for watcher in self.watchers:
                            watcher.process_events()
                    else:
                        # Still update status even when paused
                        pass

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
