"""Daemon status reporting via daemon.json.

The daemon writes its status to ~/.sessionfs/daemon.json so the CLI
can query whether the daemon is running and the health of each watcher.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from pydantic import BaseModel, Field

from sessionfs import __version__


class WatcherStatus(BaseModel):
    """Status of a single watcher."""

    name: str
    enabled: bool = False
    health: str = "unknown"
    sessions_tracked: int = 0
    last_scan_at: str | None = None
    last_error: str | None = None
    watch_paths: list[str] = Field(default_factory=list)


class DaemonStatus(BaseModel):
    """Full daemon status written to daemon.json."""

    pid: int = Field(default_factory=os.getpid)
    started_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    version: str = __version__
    status: str = "running"
    store_dir: str = ""
    watchers: list[WatcherStatus] = Field(default_factory=list)
    last_updated_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    sessions_total: int = 0


def write_status(status: DaemonStatus, status_path: Path) -> None:
    """Write daemon status atomically (write to temp, rename)."""
    try:
        status.last_updated_at = datetime.now(timezone.utc).isoformat()
        status_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = status_path.with_suffix(".tmp")
        tmp_path.write_text(json.dumps(status.model_dump(), indent=2))
        import os as _os
        import stat as _stat
        _os.chmod(tmp_path, _stat.S_IRUSR | _stat.S_IWUSR)  # 0o600
        tmp_path.rename(status_path)
    except OSError:
        pass  # Non-fatal — status is informational, don't crash the daemon


def read_status(status_path: Path) -> DaemonStatus | None:
    """Read daemon status, or None if not found/corrupt."""
    if not status_path.exists():
        return None
    try:
        return DaemonStatus.model_validate_json(status_path.read_text())
    except Exception:
        return None


def clear_status(status_path: Path) -> None:
    """Remove the daemon status file."""
    status_path.unlink(missing_ok=True)
