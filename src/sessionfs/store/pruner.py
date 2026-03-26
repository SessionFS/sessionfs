"""Local storage pruning engine.

Manages disk usage by pruning old sessions based on configurable
retention policies. Respects bookmarks, aliases, and sync status.
"""

from __future__ import annotations

import json
import logging
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger("sessionfs.pruner")


@dataclass
class StorageUsage:
    """Current local storage statistics."""

    total_bytes: int = 0
    session_count: int = 0
    oldest_session_at: str | None = None
    newest_session_at: str | None = None
    synced_count: int = 0
    unsynced_count: int = 0


@dataclass
class PrunableSession:
    """A session that is eligible for pruning."""

    session_id: str
    size_bytes: int
    age_days: float
    is_synced: bool
    is_bookmarked: bool
    is_aliased: bool
    reason: str  # "age", "storage_cap"
    created_at: str = ""

    @property
    def size_human(self) -> str:
        return _human_bytes(self.size_bytes)


@dataclass
class PruneResult:
    """Result of a pruning operation."""

    pruned_count: int = 0
    freed_bytes: int = 0
    skipped_unsynced: int = 0
    skipped_bookmarked: int = 0
    skipped_aliased: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def freed_bytes_human(self) -> str:
        return _human_bytes(self.freed_bytes)


@dataclass
class StorageConfig:
    """Pruning configuration, read from [storage] section of config.toml."""

    max_local_bytes: int = 2 * 1024 * 1024 * 1024  # 2 GB
    local_retention_days: int = 90
    synced_retention_days: int = 30
    preserve_bookmarked: bool = True
    preserve_aliased: bool = True


def parse_size(value: str) -> int:
    """Parse a human-readable size string like '2GB', '500MB' into bytes."""
    value = value.strip().upper()
    multipliers = {
        "B": 1,
        "KB": 1024,
        "MB": 1024 ** 2,
        "GB": 1024 ** 3,
        "TB": 1024 ** 4,
    }
    for suffix, mult in sorted(multipliers.items(), key=lambda x: -len(x[0])):
        if value.endswith(suffix):
            num_str = value[: -len(suffix)].strip()
            return int(float(num_str) * mult)
    return int(value)


def _human_bytes(b: int) -> str:
    """Format bytes as human-readable string."""
    if b < 1024:
        return f"{b} B"
    elif b < 1024 ** 2:
        return f"{b / 1024:.1f} KB"
    elif b < 1024 ** 3:
        return f"{b / 1024 ** 2:.1f} MB"
    else:
        return f"{b / 1024 ** 3:.2f} GB"


def _dir_size(path: Path) -> int:
    """Calculate total size of a directory in bytes."""
    total = 0
    try:
        for f in path.rglob("*"):
            if f.is_file():
                try:
                    total += f.stat().st_size
                except OSError:
                    pass
    except OSError:
        pass
    return total


class SessionPruner:
    """Manages local disk usage by pruning old sessions."""

    def __init__(self, sessions_dir: Path, index_conn: Any) -> None:
        """
        Args:
            sessions_dir: Path to ~/.sessionfs/sessions/
            index_conn: SQLite connection from SessionIndex.conn
        """
        self._sessions_dir = sessions_dir
        self._conn = index_conn

    def calculate_usage(self) -> StorageUsage:
        """Calculate current local storage usage."""
        usage = StorageUsage()

        if not self._sessions_dir.exists():
            return usage

        rows = self._conn.execute(
            "SELECT session_id, created_at FROM sessions ORDER BY created_at ASC"
        ).fetchall()

        synced = 0
        unsynced = 0
        total_bytes = 0

        for row in rows:
            sid = row["session_id"]
            sfs_dir = self._sessions_dir / f"{sid}.sfs"
            if sfs_dir.is_dir():
                total_bytes += _dir_size(sfs_dir)
                usage.session_count += 1

                # Check sync status from manifest
                manifest = self._read_manifest(sfs_dir)
                if manifest and manifest.get("sync", {}).get("etag"):
                    synced += 1
                else:
                    unsynced += 1

        usage.total_bytes = total_bytes
        usage.synced_count = synced
        usage.unsynced_count = unsynced

        if rows:
            usage.oldest_session_at = rows[0]["created_at"]
            usage.newest_session_at = rows[-1]["created_at"]

        return usage

    def get_prunable_sessions(
        self, config: StorageConfig, usage: StorageUsage | None = None
    ) -> list[PrunableSession]:
        """List sessions eligible for pruning under current policy."""
        if usage is None:
            usage = self.calculate_usage()

        now = datetime.now(timezone.utc)
        candidates: list[PrunableSession] = []

        rows = self._conn.execute(
            "SELECT session_id, created_at, sfs_dir_path FROM sessions ORDER BY created_at ASC"
        ).fetchall()

        for row in rows:
            sid = row["session_id"]
            sfs_dir = self._sessions_dir / f"{sid}.sfs"
            if not sfs_dir.is_dir():
                continue

            created_at_str = row["created_at"] or ""
            try:
                created_at = datetime.fromisoformat(
                    created_at_str.replace("Z", "+00:00")
                )
            except (ValueError, TypeError):
                created_at = now  # Can't parse → treat as new

            age_days = (now - created_at).total_seconds() / 86400.0
            size_bytes = _dir_size(sfs_dir)
            manifest = self._read_manifest(sfs_dir)

            is_synced = bool(
                manifest and manifest.get("sync", {}).get("etag")
            )
            is_bookmarked = bool(
                manifest and manifest.get("bookmarked")
            )
            is_aliased = bool(manifest and manifest.get("alias"))

            # Check age-based retention
            retention = (
                config.synced_retention_days
                if is_synced
                else config.local_retention_days
            )
            if config.local_retention_days > 0 and age_days > retention:
                candidates.append(
                    PrunableSession(
                        session_id=sid,
                        size_bytes=size_bytes,
                        age_days=age_days,
                        is_synced=is_synced,
                        is_bookmarked=is_bookmarked,
                        is_aliased=is_aliased,
                        reason="age",
                        created_at=created_at_str,
                    )
                )

        # If still over cap after age-based candidates, add largest sessions
        age_candidate_ids = {c.session_id for c in candidates}
        remaining_after_age = usage.total_bytes - sum(
            c.size_bytes for c in candidates
        )

        if remaining_after_age > config.max_local_bytes:
            # Need to prune more — add non-candidate sessions, largest first
            all_sessions = []
            for row in rows:
                sid = row["session_id"]
                if sid in age_candidate_ids:
                    continue
                sfs_dir = self._sessions_dir / f"{sid}.sfs"
                if not sfs_dir.is_dir():
                    continue

                created_at_str = row["created_at"] or ""
                try:
                    created_at = datetime.fromisoformat(
                        created_at_str.replace("Z", "+00:00")
                    )
                except (ValueError, TypeError):
                    created_at = now

                age_days = (now - created_at).total_seconds() / 86400.0
                size_bytes = _dir_size(sfs_dir)
                manifest = self._read_manifest(sfs_dir)

                is_synced = bool(
                    manifest and manifest.get("sync", {}).get("etag")
                )
                is_bookmarked = bool(
                    manifest and manifest.get("bookmarked")
                )
                is_aliased = bool(manifest and manifest.get("alias"))

                all_sessions.append(
                    PrunableSession(
                        session_id=sid,
                        size_bytes=size_bytes,
                        age_days=age_days,
                        is_synced=is_synced,
                        is_bookmarked=is_bookmarked,
                        is_aliased=is_aliased,
                        reason="storage_cap",
                        created_at=created_at_str,
                    )
                )

            # Sort: synced first (safer to prune), then oldest, then largest
            all_sessions.sort(
                key=lambda s: (not s.is_synced, -s.age_days, -s.size_bytes)
            )

            still_over = remaining_after_age - config.max_local_bytes
            for s in all_sessions:
                if still_over <= 0:
                    break
                candidates.append(s)
                still_over -= s.size_bytes

        # Sort final list: synced before unsynced, oldest first
        candidates.sort(key=lambda c: (not c.is_synced, -c.age_days))

        return candidates

    def prune(
        self,
        config: StorageConfig,
        dry_run: bool = False,
        force: bool = False,
    ) -> PruneResult:
        """Run pruning based on configured retention policy.

        Args:
            config: Storage configuration with retention settings.
            dry_run: If True, calculate but don't delete.
            force: If True, delete even unsynced/bookmarked/aliased sessions.
        """
        usage = self.calculate_usage()
        candidates = self.get_prunable_sessions(config, usage)
        result = PruneResult()

        for session in candidates:
            # Respect bookmarks
            if session.is_bookmarked and config.preserve_bookmarked and not force:
                result.skipped_bookmarked += 1
                continue

            # Respect aliases
            if session.is_aliased and config.preserve_aliased and not force:
                result.skipped_aliased += 1
                continue

            # Warn on unsynced
            if not session.is_synced and not force:
                result.skipped_unsynced += 1
                continue

            if not dry_run:
                try:
                    self._delete_session(session.session_id)
                    result.pruned_count += 1
                    result.freed_bytes += session.size_bytes
                except Exception as exc:
                    result.errors.append(f"{session.session_id}: {exc}")
                    logger.error("Failed to prune %s: %s", session.session_id, exc)
            else:
                result.pruned_count += 1
                result.freed_bytes += session.size_bytes

        # Vacuum after pruning
        if not dry_run and result.pruned_count > 0:
            try:
                self._conn.execute("VACUUM")
            except Exception:
                logger.debug("VACUUM failed (non-critical)")

        return result

    def _delete_session(self, session_id: str) -> None:
        """Delete a session from disk and SQLite index."""
        sfs_dir = self._sessions_dir / f"{session_id}.sfs"

        # Remove from index
        self._conn.execute(
            "DELETE FROM sessions WHERE session_id = ?", (session_id,)
        )
        self._conn.execute(
            "DELETE FROM tracked_sessions WHERE sfs_session_id = ?",
            (session_id,),
        )
        self._conn.commit()

        # Remove from disk
        if sfs_dir.is_dir():
            shutil.rmtree(sfs_dir)

        logger.info("Deleted session %s", session_id)

    def _read_manifest(self, sfs_dir: Path) -> dict[str, Any] | None:
        """Read a session's manifest.json."""
        manifest_path = sfs_dir / "manifest.json"
        if not manifest_path.exists():
            return None
        try:
            return json.loads(manifest_path.read_text())
        except (json.JSONDecodeError, OSError):
            return None
