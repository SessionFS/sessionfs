"""Per-session backoff for repeated transient sync failures.

A push that fails with a transient error (5xx, 429, network) used to be
retried every push interval forever — one session that the server could not
accept was retried 15,670 times. Each failing session now waits longer between
attempts (1 min, 5 min, 15 min, then hourly), and after a long run of
failures it is paused: the daemon stops retrying it until its files change,
the daemon restarts, or the user runs ``sfs daemon retry-sync``.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

# Seconds to wait after the 1st, 2nd, 3rd and later consecutive failure.
BACKOFF_SCHEDULE_S: tuple[int, ...] = (60, 300, 900, 3600)

# Pause a session once it has failed this many times in a row AND has been
# failing for at least this long (about nine hours at the hourly cap).
PAUSE_AFTER_FAILURES = 12
PAUSE_AFTER_SECONDS = 6 * 3600

# Written by `sfs daemon retry-sync` (one file per request, so concurrent
# requests never overwrite each other), claimed and removed by the daemon.
RETRY_REQUEST_DIR = "sync_retry.d"


def backoff_delay(failures: int) -> int:
    """Seconds to wait before the next attempt after ``failures`` in a row."""
    if failures <= 0:
        return 0
    return BACKOFF_SCHEDULE_S[min(failures, len(BACKOFF_SCHEDULE_S)) - 1]


def session_fingerprint(session_dir: Path | None) -> str:
    """A cheap fingerprint of a session's files (names, sizes, mtimes).

    Changes whenever the session is captured again, which re-arms a paused
    session without packing the archive just to find out.
    """
    if session_dir is None or not session_dir.is_dir():
        return ""
    digest = hashlib.sha256()
    try:
        for path in sorted(session_dir.rglob("*")):
            if path.is_file():
                st = path.stat()
                digest.update(
                    f"{path.relative_to(session_dir)}|{st.st_size}|{st.st_mtime_ns}\n".encode()
                )
    except OSError:
        return ""
    return digest.hexdigest()


@dataclass
class _Entry:
    failures: int = 0
    first_failed_wall: float = 0.0
    next_retry_mono: float = 0.0
    next_retry_wall: float = 0.0
    paused: bool = False
    fingerprint: str = ""
    last_error: str = ""


@dataclass
class SyncBackoff:
    """In-memory backoff state for the daemon's sync loop."""

    entries: dict[str, _Entry] = field(default_factory=dict)

    def should_attempt(self, session_id: str, fingerprint: str, now_mono: float) -> bool:
        """Whether the session may be pushed now.

        A paused session is re-armed (attempted, with a fresh count) as soon
        as its fingerprint differs from the one recorded when it failed.
        """
        entry = self.entries.get(session_id)
        if entry is None:
            return True
        if entry.paused:
            if fingerprint and fingerprint != entry.fingerprint:
                del self.entries[session_id]
                return True
            return False
        return now_mono >= entry.next_retry_mono

    def record_failure(
        self,
        session_id: str,
        fingerprint: str,
        error: str,
        now_mono: float,
        now_wall: float,
    ) -> _Entry:
        entry = self.entries.get(session_id)
        if entry is None or entry.fingerprint != fingerprint:
            # New failure streak (or the content changed since the last one).
            entry = _Entry(first_failed_wall=now_wall)
            self.entries[session_id] = entry
        entry.failures += 1
        entry.fingerprint = fingerprint
        entry.last_error = error[:300]
        delay = backoff_delay(entry.failures)
        entry.next_retry_mono = now_mono + delay
        entry.next_retry_wall = now_wall + delay
        if (
            entry.failures >= PAUSE_AFTER_FAILURES
            and now_wall - entry.first_failed_wall >= PAUSE_AFTER_SECONDS
        ):
            entry.paused = True
        return entry

    def record_success(self, session_id: str) -> None:
        self.entries.pop(session_id, None)

    def clear(self, session_ids: list[str] | None = None) -> list[str]:
        """Forget backoff state (all sessions when ``session_ids`` is None)."""
        if session_ids is None:
            everything = list(self.entries)
            self.entries.clear()
            return everything
        cleared: list[str] = []
        for wanted in session_ids:
            if wanted in self.entries:
                match = [wanted]
            else:
                # Accept a unique prefix, as shown in shortened listings.
                match = [sid for sid in self.entries if sid.startswith(wanted)]
                if len(match) != 1:
                    continue
            for sid in match:
                if sid not in cleared:
                    del self.entries[sid]
                    cleared.append(sid)
        return cleared

    def snapshot(self) -> list[dict]:
        """Status rows for daemon.json, most-failed first."""
        rows = []
        for sid, entry in self.entries.items():
            rows.append(
                {
                    "session_id": sid,
                    "failures": entry.failures,
                    "paused": entry.paused,
                    "failing_since": _iso(entry.first_failed_wall),
                    "next_retry_at": None if entry.paused else _iso(entry.next_retry_wall),
                    "last_error": entry.last_error,
                }
            )
        rows.sort(key=lambda r: r["failures"], reverse=True)
        return rows


def _iso(wall: float) -> str | None:
    if not wall:
        return None
    return datetime.fromtimestamp(wall, tz=timezone.utc).isoformat()


def write_retry_request(store_dir: Path, session_ids: list[str] | None) -> Path:
    """Queue a request for the running daemon to retry now (all when None)."""
    queue = store_dir / RETRY_REQUEST_DIR
    queue.mkdir(parents=True, exist_ok=True)
    name = f"{time.time_ns()}-{uuid.uuid4().hex}"
    payload = {"all": session_ids is None, "sessions": session_ids or []}
    tmp = queue / f".{name}.tmp"
    tmp.write_text(json.dumps(payload))
    os.chmod(tmp, 0o600)
    final = queue / f"{name}.json"
    tmp.replace(final)
    return final


def take_retry_request(store_dir: Path) -> tuple[bool, list[str]] | None:
    """Claim and merge every queued retry request: ``(all, session_ids)``.

    Each file is claimed by renaming it first, so a request written while
    the daemon is reading is never deleted unread; it is picked up next time.
    """
    queue = store_dir / RETRY_REQUEST_DIR
    try:
        pending = sorted(p for p in queue.iterdir() if p.suffix == ".json")
    except OSError:
        return None
    if not pending:
        return None
    retry_all = False
    sessions: list[str] = []
    for path in pending:
        claimed = path.with_suffix(".claimed")
        try:
            path.rename(claimed)
        except OSError:
            continue  # claimed elsewhere or already gone
        try:
            raw = json.loads(claimed.read_text())
        except (OSError, ValueError):
            raw = {}
        finally:
            try:
                claimed.unlink()
            except OSError:
                pass
        if not isinstance(raw, dict):
            continue
        retry_all = retry_all or bool(raw.get("all"))
        for sid in raw.get("sessions") or []:
            if isinstance(sid, str) and sid not in sessions:
                sessions.append(sid)
    return retry_all, sessions


def now_pair() -> tuple[float, float]:
    """(monotonic, wall-clock) now."""
    return time.monotonic(), time.time()
