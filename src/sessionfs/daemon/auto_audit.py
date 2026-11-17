"""Auto-audit on sync, off the daemon's main loop (tk_e3eecad8c3874d92).

The server runs every audit inside the request, so one audit call can take
minutes. The daemon's main loop is single-threaded (capture, sync and the
status heartbeat all run on it), so audits are handed to ONE background
worker thread instead:

* at most one audit request is in flight at a time;
* the queue holds at most ``MAX_PENDING`` sessions, one entry per session
  (re-queuing a session that is already waiting keeps its place; the newest
  credentials win), and the oldest waiting entry is dropped with a log line
  when it is full;
* ``submit`` never blocks; ``stop`` asks the worker to finish and joins it
  with a timeout, so shutdown never waits on an LLM call.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field

logger = logging.getLogger("sfsd.auto_audit")

# Sessions this size or larger are never audited automatically.
AUTO_AUDIT_MAX_MESSAGES = 500
# Client timeout for one audit call (the server's per-call judge bound).
AUTO_AUDIT_TIMEOUT_SECONDS = 120
MAX_PENDING = 50


@dataclass(frozen=True)
class AuditTarget:
    api_url: str
    # Kept out of repr so a logged target never prints the key.
    api_key: str = field(repr=False)


def _post_audit(session_id: str, target: AuditTarget) -> None:
    """Send one audit request and log the outcome (runs on the worker)."""
    import httpx

    async def _go() -> None:
        async with httpx.AsyncClient(timeout=AUTO_AUDIT_TIMEOUT_SECONDS) as http:
            resp = await http.post(
                f"{target.api_url}/api/v1/sessions/{session_id}/audit",
                headers={"Authorization": f"Bearer {target.api_key}"},
                json={},
            )
            if resp.status_code in (200, 201, 202):
                logger.info("Auto-audit completed for %s", session_id[:12])
            else:
                logger.info(
                    "Auto-audit not run for %s: HTTP %d", session_id[:12], resp.status_code
                )

    try:
        asyncio.run(_go())
    except httpx.TimeoutException:
        logger.warning(
            "Auto-audit for %s gave no answer within %ds; the server may still "
            "finish it. Check the dashboard or run `sfs audit`",
            session_id[:12],
            AUTO_AUDIT_TIMEOUT_SECONDS,
        )
    except Exception as exc:  # noqa: BLE001 — never kill the worker
        logger.warning("Auto-audit failed for %s: %s", session_id[:12], type(exc).__name__)


class AutoAuditWorker:
    """One background thread draining a bounded, per-session-deduplicated queue."""

    def __init__(
        self,
        send: Callable[[str, AuditTarget], None] = _post_audit,
        max_pending: int = MAX_PENDING,
    ) -> None:
        self._send = send
        self._max_pending = max_pending
        self._pending: OrderedDict[str, AuditTarget] = OrderedDict()
        self._cond = threading.Condition()
        self._stopping = False
        self._thread: threading.Thread | None = None
        self.in_flight: str | None = None

    def submit(self, session_id: str, target: AuditTarget) -> None:
        """Queue an audit; never blocks the caller."""
        with self._cond:
            if self._stopping:
                return
            if session_id in self._pending:
                self._pending[session_id] = target  # keep its place, newest key
            else:
                if len(self._pending) >= self._max_pending:
                    dropped, _ = self._pending.popitem(last=False)
                    logger.info("Auto-audit queue full; dropped %s", dropped[:12])
                self._pending[session_id] = target
            self._ensure_thread()
            self._cond.notify()

    def pending(self) -> list[str]:
        with self._cond:
            return list(self._pending)

    def _ensure_thread(self) -> None:
        if self._thread is None or not self._thread.is_alive():
            self._thread = threading.Thread(
                target=self._loop, name="sfsd-auto-audit", daemon=True
            )
            self._thread.start()

    def _loop(self) -> None:
        while True:
            with self._cond:
                while not self._pending and not self._stopping:
                    self._cond.wait()
                if self._stopping:
                    return
                session_id, target = self._pending.popitem(last=False)
                self.in_flight = session_id
            try:
                self._send(session_id, target)
            finally:
                with self._cond:
                    self.in_flight = None
                    self._cond.notify_all()

    def wait_idle(self, timeout: float) -> bool:
        """Wait until nothing is queued or running (tests)."""
        with self._cond:
            return self._cond.wait_for(
                lambda: not self._pending and self.in_flight is None, timeout=timeout
            )

    def stop(self, timeout: float = 5.0) -> None:
        """Drop queued audits and join the worker (a call in flight is left to
        its own timeout; the thread is a daemon thread)."""
        with self._cond:
            self._stopping = True
            self._pending.clear()
            self._cond.notify_all()
            thread = self._thread
        if thread is not None:
            thread.join(timeout)
