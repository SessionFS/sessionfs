"""Unattended hosted-skill reconcile on the daemon's sync cadence.

When cloud sync is enabled the daemon periodically:

* pulls the marketplace revocation feed and quarantines BLOCKED content in
  user scope (with a logged notice naming the reason),
* installs REQUIRED org assignments into user scope.

It never prompts, never installs optional items, never removes a package the
user chose, and never grants an elevated capability (packages that need
scripts/hooks/... consent are skipped and logged). Failures back off
(15 min, 30 min, 1 h, then every 2 h) so an unreachable or older server is
not hammered; a server without the revocation feed is simply skipped.

The reconcile runs in ONE background thread at a time (single flight) with an
overall deadline, so it never stalls the daemon's 1-second capture loop.
Every skill-state change it makes goes through the same per-scope
interprocess operation lock and journal as `sfs skill ...`, so it never
overlaps a local CLI mutation; the hosted state file has its own lock.
"""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger("sfsd.skills")

RECONCILE_INTERVAL_S = 15 * 60
FAILURE_BACKOFF_S: tuple[int, ...] = (15 * 60, 30 * 60, 3600, 2 * 3600)
# Overall budget for one reconcile; work stops cleanly between steps after it.
RUN_DEADLINE_S = 10 * 60


class SkillReconciler:
    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        manager_factory: Callable[[], Any] | None = None,
        client_factory: Callable[[str, str], Any] | None = None,
        run_deadline_s: float = RUN_DEADLINE_S,
    ) -> None:
        self._clock = clock
        self._next_attempt = 0.0
        self.failures = 0
        self.last_error: str | None = None
        self.last_report: Any = None
        self._manager_factory = manager_factory
        self._client_factory = client_factory
        self._run_deadline_s = run_deadline_s
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        # Set on daemon shutdown: a running reconcile stops between steps.
        self._stop = threading.Event()

    def stop(self, timeout: float | None = 5.0) -> None:
        """Ask a running reconcile to stop promptly and wait briefly for it."""
        self._stop.set()
        self.wait(timeout)

    def _manager(self) -> Any:
        if self._manager_factory is not None:
            return self._manager_factory()
        from sessionfs.skills.manager import SkillManager

        # User scope only: the daemon has no project context.
        return SkillManager(project_root=Path.home())

    def _client(self, api_url: str, api_key: str) -> Any:
        if self._client_factory is not None:
            return self._client_factory(api_url, api_key)
        from sessionfs.skills.hosted import HostedClient

        return HostedClient(api_url, api_key, timeout=20.0)

    @property
    def running(self) -> bool:
        thread = self._thread
        return thread is not None and thread.is_alive()

    def maybe_run(
        self, *, enabled: bool, api_url: str, api_key: str, background: bool = True
    ) -> Any:
        """Start one reconcile when due.

        In the daemon (`background=True`) this only STARTS a worker thread and
        returns at once; at most one reconcile runs at a time. With
        `background=False` the reconcile runs inline and its report is
        returned (used by tests and one-shot callers).
        """
        if not enabled or not api_key or self._stop.is_set():
            return None
        with self._lock:
            if self.running:
                return None
            now = self._clock()
            if now < self._next_attempt:
                return None
            # Reserve the slot so a slow run is never started twice.
            self._next_attempt = now + RECONCILE_INTERVAL_S
            if background:
                self._thread = threading.Thread(
                    target=self._run,
                    args=(api_url, api_key),
                    name="sfsd-skill-reconcile",
                    daemon=True,
                )
                self._thread.start()
                return None
        return self._run(api_url, api_key)

    def wait(self, timeout: float | None = None) -> None:
        thread = self._thread
        if thread is not None:
            thread.join(timeout)

    def _run(self, api_url: str, api_key: str) -> Any:
        from sessionfs.skills import hosted

        logged: set[str] = set()

        def _recovery(message: str) -> None:
            # Logged the moment it is found; once per run.
            if message not in logged:
                logged.add(message)
                logger.warning("Skill reconcile: %s", message)

        try:
            report = hosted.daemon_reconcile(
                self._manager(),
                self._client(api_url, api_key),
                device_id=hosted.resolve_device_id(),
                deadline=time.monotonic() + self._run_deadline_s,
                cancel=self._stop.is_set,
                on_recovery=_recovery,
            )
        except Exception as exc:  # network, server, local state: back off
            # Anything recovery found that was not announced yet.
            partial = hosted.partial_pull_report(exc)
            for entry in getattr(partial, "recovery", None) or []:
                _recovery(str(entry.get("message")))
            with self._lock:
                self.failures += 1
                self.last_error = str(exc)[:300]
                delay = FAILURE_BACKOFF_S[min(self.failures, len(FAILURE_BACKOFF_S)) - 1]
                # From COMPLETION: a slow failing run must not be retried at once.
                self._next_attempt = self._clock() + delay
            logger.warning(
                "Skill reconcile failed (%d in a row, next try in %ds): %s",
                self.failures, delay, self.last_error,
            )
            return None
        with self._lock:
            self.failures = 0
            self.last_error = None
            self.last_report = report
            self._next_attempt = self._clock() + RECONCILE_INTERVAL_S
        _log_report(report, already_logged=logged)
        return report


def _log_report(report: Any, *, already_logged: set[str] | None = None) -> None:
    for note in report.notes:
        logger.info("Skill reconcile: %s", note)
    for item in report.blocked_removed:
        logger.warning(
            "Quarantined BLOCKED skill package %s (%s scope): %s%s%s",
            item.get("name"), item.get("scope"),
            item.get("reason") or "blocked by the marketplace",
            f" -> {item['quarantine_dir']}" if item.get("quarantine_dir") else "",
            f" ({item['error']})" if item.get("error") else "",
        )
    for entry in getattr(report, "recovery", []):
        if str(entry.get("message")) not in (already_logged or set()):
            logger.warning("Skill reconcile: %s", entry.get("message"))
    for item in getattr(report, "unassigned", []):
        logger.info(
            "Skill package %s (%s scope) is no longer assigned; it stays installed "
            "(run `sfs skill remove %s --scope %s` to remove it)",
            item.get("name"), item.get("scope"), item.get("package_id"), item.get("scope"),
        )
    for entry in getattr(report, "unassigned_tools", []):
        logger.info(
            "Skill package %s (%s scope): %s no longer assigned; still installed",
            entry.get("package_id"), entry.get("scope"), ", ".join(entry.get("tools") or []),
        )
    for item in report.items:
        if item.status in {"installed", "updated"}:
            logger.info("Skill %s %s (%s) %s", item.name, item.status, item.version_id, item.detail)
        elif item.status == "held":
            # Marketplace v2 D2: never auto-applied; the installed version stays.
            logger.warning(
                "Skill update %s held for approval (%s update): %s",
                item.version_id, item.update_class, item.detail,
            )
        elif item.status in {"skipped", "failed"}:
            logger.warning(
                "Required skill %s not applied (%s): %s",
                item.version_id, item.status, item.detail,
            )
