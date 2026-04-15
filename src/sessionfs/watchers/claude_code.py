"""Claude Code session watcher.

Watches ~/.claude/projects/ for session changes using fsevents/inotify
(via watchdog), discovers sessions, parses them, and stores .sfs captures.

The parser is extracted from spike_1a_cc_read.py. Data classes here
(ContentBlock, Message, SubAgent, ParsedSession) are Claude Code-specific
intermediate representations, not .sfs models.
"""

from __future__ import annotations

import json
import logging
import shutil
import tempfile
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from watchdog.events import FileSystemEventHandler, FileSystemEvent
from watchdog.observers import Observer

from sessionfs.daemon.config import ClaudeCodeWatcherConfig
from sessionfs.daemon.status import WatcherStatus
from sessionfs.store.local import LocalStore
from sessionfs.watchers.base import NativeSessionRef, WatchEvent, WatcherHealth

logger = logging.getLogger("sfsd.watcher.claude_code")


# ---------------------------------------------------------------------------
# Data model (from spike_1a_cc_read.py)
# ---------------------------------------------------------------------------


@dataclass
class ContentBlock:
    """A single content block within a CC message."""

    block_type: str
    text: str | None = None
    thinking: str | None = None
    tool_use_id: str | None = None
    tool_name: str | None = None
    tool_input: dict[str, Any] | None = None
    tool_result_content: str | None = None
    signature: str | None = None


@dataclass
class Message:
    """A parsed CC conversation message."""

    uuid: str
    parent_uuid: str | None
    role: str
    content_blocks: list[ContentBlock]
    timestamp: str | None = None
    model: str | None = None
    stop_reason: str | None = None
    is_sidechain: bool = False
    is_meta: bool = False
    cwd: str | None = None
    git_branch: str | None = None
    request_id: str | None = None
    usage: dict[str, Any] | None = None


@dataclass
class SubAgent:
    """A sub-agent session."""

    agent_id: str
    messages: list[Message] = field(default_factory=list)
    model: str | None = None


@dataclass
class ParsedSession:
    """A fully parsed Claude Code session."""

    session_id: str
    project_path: str | None = None
    source_path: str | None = None
    claude_code_version: str | None = None
    slug: str | None = None
    git_branch: str | None = None
    first_prompt: str | None = None
    messages: list[Message] = field(default_factory=list)
    sub_agents: list[SubAgent] = field(default_factory=list)
    file_snapshots: list[dict[str, Any]] = field(default_factory=list)
    message_count: int = 0
    parse_errors: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Discovery (parameterized on home_dir)
# ---------------------------------------------------------------------------


def discover_projects(home_dir: Path) -> list[dict[str, Any]]:
    """Find all Claude Code project directories."""
    projects_dir = home_dir / "projects"
    if not projects_dir.is_dir():
        return []

    projects = []
    for entry in sorted(projects_dir.iterdir()):
        if not entry.is_dir() or entry.name.startswith("."):
            continue
        decoded_path = "/" + entry.name.replace("-", "/")
        sessions = list(entry.glob("*.jsonl"))
        projects.append({
            "encoded_name": entry.name,
            "decoded_path": decoded_path,
            "directory": str(entry),
            "session_count": len(sessions),
        })
    return projects


def discover_sessions(
    home_dir: Path, project_dir: Path | None = None
) -> list[dict[str, Any]]:
    """Find all session JSONL files, optionally filtered to one project."""
    projects_dir = home_dir / "projects"
    if not projects_dir.is_dir():
        return []

    dirs = [project_dir] if project_dir else [
        d for d in sorted(projects_dir.iterdir())
        if d.is_dir() and not d.name.startswith(".")
    ]

    sessions = []
    for d in dirs:
        index_path = d / "sessions-index.json"
        index_entries: dict[str, dict] = {}
        if index_path.exists():
            try:
                idx = json.loads(index_path.read_text())
                for entry in idx.get("entries", []):
                    index_entries[entry["sessionId"]] = entry
            except (json.JSONDecodeError, KeyError):
                pass

        for jsonl in sorted(d.glob("*.jsonl")):
            session_id = jsonl.stem
            idx_entry = index_entries.get(session_id, {})
            stat = jsonl.stat()
            sessions.append({
                "session_id": session_id,
                "path": str(jsonl),
                "project_dir": str(d),
                "size_bytes": stat.st_size,
                "mtime": stat.st_mtime,
                "first_prompt": idx_entry.get("firstPrompt", ""),
                "created": idx_entry.get("created", ""),
                "modified": idx_entry.get("modified", ""),
                "git_branch": idx_entry.get("gitBranch", ""),
                "project_path": idx_entry.get("projectPath", ""),
                "message_count": idx_entry.get("messageCount", 0),
            })
    return sessions


def find_session_path(home_dir: Path, identifier: str) -> Path | None:
    """Resolve a session UUID or file path to a JSONL path."""
    p = Path(identifier)
    if p.exists() and p.suffix == ".jsonl":
        return p

    projects_dir = home_dir / "projects"
    for jsonl in projects_dir.rglob(f"{identifier}.jsonl"):
        if "subagents" not in str(jsonl):
            return jsonl

    return None


# ---------------------------------------------------------------------------
# Parsing (from spike_1a_cc_read.py)
# ---------------------------------------------------------------------------


def _copy_to_temp(source: Path) -> Path:
    """Copy a file to a temp location for safe reading (copy-on-read)."""
    # M5: Reject symlinks to prevent reading sensitive files outside session dir
    if source.is_symlink():
        raise ValueError(f"Refusing to read symlink: {source}")
    tmp = Path(tempfile.mkdtemp(prefix="sfs_", dir=None))
    dest = tmp / source.name
    shutil.copy2(source, dest)
    return dest


def _parse_content_blocks(content: Any) -> list[ContentBlock]:
    """Parse message content into ContentBlock list."""
    if isinstance(content, str):
        return [ContentBlock(block_type="text", text=content)]

    if not isinstance(content, list):
        return [ContentBlock(block_type="text", text=str(content))]

    blocks = []
    for item in content:
        if not isinstance(item, dict):
            blocks.append(ContentBlock(block_type="text", text=str(item)))
            continue

        btype = item.get("type", "unknown")

        if btype == "text":
            blocks.append(ContentBlock(block_type="text", text=item.get("text", "")))
        elif btype == "thinking":
            blocks.append(ContentBlock(
                block_type="thinking",
                thinking=item.get("thinking", ""),
                signature=item.get("signature"),
            ))
        elif btype == "tool_use":
            blocks.append(ContentBlock(
                block_type="tool_use",
                tool_use_id=item.get("id"),
                tool_name=item.get("name"),
                tool_input=item.get("input"),
            ))
        elif btype == "tool_result":
            result_content = item.get("content", "")
            if isinstance(result_content, list):
                parts = []
                for sub in result_content:
                    if isinstance(sub, dict):
                        parts.append(sub.get("text", str(sub)))
                    else:
                        parts.append(str(sub))
                result_content = "\n".join(parts)
            blocks.append(ContentBlock(
                block_type="tool_result",
                tool_use_id=item.get("tool_use_id"),
                tool_result_content=result_content,
            ))
        else:
            blocks.append(ContentBlock(block_type=btype, text=json.dumps(item)))

    return blocks


def _parse_message(raw: dict[str, Any]) -> Message | None:
    """Parse a raw JSONL line into a Message, or None if not a message type."""
    msg_type = raw.get("type")
    if msg_type not in ("user", "assistant", "summary"):
        return None

    if msg_type == "summary":
        return Message(
            uuid=raw.get("leafUuid", ""),
            parent_uuid=None,
            role="summary",
            content_blocks=[ContentBlock(
                block_type="text",
                text=raw.get("summary", ""),
            )],
            timestamp=raw.get("timestamp"),
        )

    msg = raw.get("message", {})
    role = msg.get("role", msg_type)
    content = msg.get("content", "")
    blocks = _parse_content_blocks(content)

    return Message(
        uuid=raw.get("uuid", ""),
        parent_uuid=raw.get("parentUuid"),
        role=role,
        content_blocks=blocks,
        timestamp=raw.get("timestamp"),
        model=msg.get("model"),
        stop_reason=msg.get("stop_reason"),
        is_sidechain=raw.get("isSidechain", False),
        is_meta=raw.get("isMeta", False),
        cwd=raw.get("cwd"),
        git_branch=raw.get("gitBranch"),
        request_id=raw.get("requestId"),
        usage=msg.get("usage"),
    )


def parse_session(jsonl_path: Path, *, copy_on_read: bool = True) -> ParsedSession:
    """Parse a Claude Code session JSONL file into a structured representation."""
    session_id = jsonl_path.stem
    session = ParsedSession(
        session_id=session_id,
        source_path=str(jsonl_path),
    )

    read_path = jsonl_path
    tmp_dir: Path | None = None
    if copy_on_read:
        read_path = _copy_to_temp(jsonl_path)
        tmp_dir = read_path.parent

    try:
        with open(read_path, "r", encoding="utf-8") as f:
            for line_num, line in enumerate(f, start=1):
                line = line.strip()
                if not line:
                    continue
                try:
                    raw = json.loads(line)
                except json.JSONDecodeError as e:
                    session.parse_errors.append(f"Line {line_num}: JSON decode error: {e}")
                    continue

                raw_type = raw.get("type")

                # Extract session metadata from first substantive message
                if raw_type in ("user", "assistant") and not session.claude_code_version:
                    session.claude_code_version = raw.get("version")
                    session.slug = raw.get("slug")
                    session.git_branch = raw.get("gitBranch")
                    session.project_path = raw.get("cwd")

                if raw_type == "file-history-snapshot":
                    snap = raw.get("snapshot", {})
                    backups = snap.get("trackedFileBackups", {})
                    if backups:
                        session.file_snapshots.append({
                            "message_id": raw.get("messageId"),
                            "timestamp": snap.get("timestamp"),
                            "files": list(backups.keys()),
                            "is_update": raw.get("isSnapshotUpdate", False),
                        })
                    continue

                if raw_type in ("progress", "system"):
                    continue

                message = _parse_message(raw)
                if message:
                    session.messages.append(message)

    finally:
        if tmp_dir and tmp_dir.exists():
            shutil.rmtree(tmp_dir, ignore_errors=True)

    session.message_count = len(session.messages)

    # Extract first user prompt
    for msg in session.messages:
        if msg.role == "user" and not msg.is_meta:
            for block in msg.content_blocks:
                if block.block_type == "text" and block.text:
                    session.first_prompt = block.text[:200]
                    break
            if session.first_prompt:
                break

    # Parse sub-agents
    session_dir = jsonl_path.parent / session_id
    subagents_dir = session_dir / "subagents"
    if subagents_dir.is_dir():
        for agent_file in sorted(subagents_dir.glob("*.jsonl")):
            agent_id = agent_file.stem
            sub = SubAgent(agent_id=agent_id)

            sub_read = agent_file
            sub_tmp: Path | None = None
            if copy_on_read:
                sub_read = _copy_to_temp(agent_file)
                sub_tmp = sub_read.parent

            try:
                with open(sub_read, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            raw = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        msg = _parse_message(raw)
                        if msg:
                            sub.messages.append(msg)
                            if not sub.model and msg.model:
                                sub.model = msg.model
            finally:
                if sub_tmp and sub_tmp.exists():
                    shutil.rmtree(sub_tmp, ignore_errors=True)

            session.sub_agents.append(sub)

    return session


# ---------------------------------------------------------------------------
# Watchdog event handler
# ---------------------------------------------------------------------------


class _CCEventHandler(FileSystemEventHandler):
    """Queues .jsonl file change events for the watcher."""

    def __init__(self, event_queue: list[WatchEvent], lock: threading.Lock) -> None:
        self._queue = event_queue
        self._lock = lock

    def on_modified(self, event: FileSystemEvent) -> None:
        if event.is_directory or not event.src_path.endswith(".jsonl"):
            return
        with self._lock:
            self._queue.append(WatchEvent(event_type="modified", path=event.src_path))

    def on_created(self, event: FileSystemEvent) -> None:
        if event.is_directory or not event.src_path.endswith(".jsonl"):
            return
        with self._lock:
            self._queue.append(WatchEvent(event_type="created", path=event.src_path))


# ---------------------------------------------------------------------------
# Watcher
# ---------------------------------------------------------------------------


class ClaudeCodeWatcher:
    """Watches Claude Code session storage and captures changes."""

    def __init__(
        self,
        config: ClaudeCodeWatcherConfig,
        store: LocalStore,
        scan_interval: float = 5.0,
    ) -> None:
        self._config = config
        self._store = store
        self._scan_interval = scan_interval
        self._home_dir = config.home_dir
        self._projects_dir = config.home_dir / "projects"
        self._observer: Observer | None = None
        self._event_queue: list[WatchEvent] = []
        self._event_lock = threading.Lock()
        self._last_event_time: float = 0.0
        self._health = WatcherHealth.HEALTHY
        self._tracked: dict[str, NativeSessionRef] = {}
        self._last_error: str | None = None
        self._last_scan_at: str | None = None

    def full_scan(self) -> None:
        """Discover all existing CC sessions, capture new/changed ones."""
        if not self._projects_dir.is_dir():
            logger.warning("Claude Code projects dir not found: %s", self._projects_dir)
            self._health = WatcherHealth.DEGRADED
            self._last_error = f"Projects dir not found: {self._projects_dir}"
            return

        try:
            sessions = discover_sessions(self._home_dir)
            captured = 0
            for s_info in sessions:
                native_id = s_info["session_id"]
                native_path = Path(s_info["path"])

                if not native_path.exists():
                    continue

                # M5: Skip symlinks
                if native_path.is_symlink():
                    logger.warning("Skipping symlink during scan: %s", native_path)
                    continue

                stat = native_path.stat()
                current_mtime = stat.st_mtime
                current_size = stat.st_size

                # Check if already captured and unchanged
                existing = self._store.get_tracked_session(native_id)
                if (
                    existing
                    and existing.last_mtime >= current_mtime
                    and existing.last_size == current_size
                ):
                    self._tracked[native_id] = existing
                    continue

                self._capture_session(
                    native_id, native_path, current_mtime, current_size
                )
                captured += 1

            self._health = WatcherHealth.HEALTHY
            self._last_scan_at = datetime.now(timezone.utc).isoformat()
            logger.info(
                "Full scan complete: %d sessions found, %d captured",
                len(sessions),
                captured,
            )
        except Exception as e:
            logger.error("Full scan failed: %s", e, exc_info=True)
            self._health = WatcherHealth.DEGRADED
            self._last_error = str(e)

    def _capture_session(
        self,
        native_id: str,
        native_path: Path,
        mtime: float,
        size: int,
    ) -> None:
        """Parse a CC session, convert to .sfs, write to store."""
        from sessionfs.session_id import session_id_from_native
        from sessionfs.spec.convert_cc import convert_session

        logger.info("Capturing session %s (%d bytes)", native_id, size)

        try:
            cc_session = parse_session(native_path, copy_on_read=True)

            # Generate a spec-compliant ses_ prefixed ID from the native UUID
            sfs_id = session_id_from_native(native_id)
            session_dir = self._store.allocate_session_dir(sfs_id)
            convert_session(
                cc_session, session_dir.parent,
                session_id=sfs_id, session_dir=session_dir,
            )

            # Migration 028: annotate manifest with instruction provenance.
            from sessionfs.watchers.provenance import annotate_manifest_with_provenance
            annotate_manifest_with_provenance(
                session_dir, "claude-code", cc_session.project_path
            )

            # Read back manifest and index it (must happen before tracked
            # session upsert due to FK constraint)
            manifest_path = session_dir / "manifest.json"
            if manifest_path.exists():
                manifest = json.loads(manifest_path.read_text())
                self._store.upsert_session_metadata(
                    sfs_id, manifest, str(session_dir)
                )

            ref = NativeSessionRef(
                tool="claude-code",
                native_session_id=native_id,
                native_path=str(native_path),
                sfs_session_id=sfs_id,
                last_mtime=mtime,
                last_size=size,
                last_captured_at=datetime.now(timezone.utc).isoformat(),
                project_path=cc_session.project_path,
            )
            self._tracked[native_id] = ref
            self._store.upsert_tracked_session(ref)
            logger.info("Captured session %s -> %s", native_id, session_dir)

        except Exception as e:
            logger.error("Failed to capture session %s: %s", native_id, e, exc_info=True)
            self._last_error = f"Capture failed for {native_id}: {e}"

    def start_watching(self) -> None:
        """Start the watchdog filesystem observer."""
        if not self._projects_dir.is_dir():
            return

        handler = _CCEventHandler(self._event_queue, self._event_lock)
        self._observer = Observer()
        self._observer.schedule(handler, str(self._projects_dir), recursive=True)
        self._observer.daemon = True
        self._observer.start()
        logger.info("Watching %s for CC session changes", self._projects_dir)

    def stop_watching(self) -> None:
        """Stop the watchdog observer."""
        if self._observer:
            self._observer.stop()
            self._observer.join(timeout=5.0)
            self._observer = None

    def process_events(self) -> None:
        """Process queued filesystem events with debouncing."""
        with self._event_lock:
            if not self._event_queue:
                return
            events = list(self._event_queue)
            self._event_queue.clear()

        now = time.monotonic()
        if now - self._last_event_time < self._scan_interval:
            # Re-queue: too soon since last scan (debounce)
            with self._event_lock:
                self._event_queue.extend(events)
            return

        self._last_event_time = now

        # Deduplicate: get unique session files that changed
        changed_paths: set[str] = set()
        for event in events:
            changed_paths.add(event.path)

        for path_str in changed_paths:
            native_path = Path(path_str)
            if not native_path.exists() or native_path.suffix != ".jsonl":
                continue

            # M5: Skip symlinks
            if native_path.is_symlink():
                logger.warning("Skipping symlink during event processing: %s", native_path)
                continue

            # Sub-agent changes trigger re-capture of parent
            if "subagents" in str(native_path):
                parent_dir = native_path.parent.parent
                native_id = parent_dir.name
                parent_jsonl = parent_dir.parent / f"{native_id}.jsonl"
                if parent_jsonl.exists():
                    native_path = parent_jsonl
                else:
                    continue

            native_id = native_path.stem
            try:
                stat = native_path.stat()
                self._capture_session(
                    native_id, native_path, stat.st_mtime, stat.st_size
                )
            except OSError as e:
                logger.warning("Cannot stat %s: %s", native_path, e)

        self._last_scan_at = datetime.now(timezone.utc).isoformat()

    def get_status(self) -> WatcherStatus:
        """Return current watcher status."""
        return WatcherStatus(
            name="claude-code",
            enabled=self._config.enabled,
            health=self._health.value,
            sessions_tracked=len(self._tracked),
            last_scan_at=self._last_scan_at,
            last_error=self._last_error,
            watch_paths=[str(self._projects_dir)],
        )
