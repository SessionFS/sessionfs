"""Codex CLI session watcher.

Watches ~/.codex/sessions/ for session changes, discovers sessions via
the SQLite index or filesystem scan, parses them, and stores .sfs captures.

Codex sessions are JSONL rollout files with 5 top-level types:
session_meta, response_item, event_msg, turn_context, compacted.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from watchdog.events import FileSystemEventHandler, FileSystemEvent
from watchdog.observers import Observer

from sessionfs.daemon.config import CodexWatcherConfig
from sessionfs.daemon.status import WatcherStatus
from sessionfs.session_id import session_id_from_native
from sessionfs.spec.version import SFS_FORMAT_VERSION, SFS_CONVERTER_VERSION
from sessionfs.store.local import LocalStore
from sessionfs.watchers.base import NativeSessionRef, WatcherHealth

logger = logging.getLogger("sfsd.watcher.codex")

_STATE_DB = "state_5.sqlite"


# ---------------------------------------------------------------------------
# Parsed session dataclass
# ---------------------------------------------------------------------------


@dataclass
class CodexParsedSession:
    """Intermediate representation of a parsed Codex session."""

    session_id: str
    source_path: str | None = None
    cwd: str | None = None
    cli_version: str | None = None
    model: str | None = None
    model_provider: str | None = None
    git_branch: str | None = None
    git_commit: str | None = None
    git_remote: str | None = None
    first_prompt: str | None = None
    messages: list[dict[str, Any]] = field(default_factory=list)
    message_count: int = 0
    turn_count: int = 0
    tool_use_count: int = 0
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    parse_errors: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


def parse_codex_session(jsonl_path: Path) -> CodexParsedSession:
    """Parse a Codex JSONL rollout file into a CodexParsedSession."""
    session = CodexParsedSession(
        session_id=_extract_session_id_from_path(jsonl_path),
        source_path=str(jsonl_path),
    )

    sfs_messages: list[dict[str, Any]] = []
    current_turn_id: str | None = None

    with open(jsonl_path) as f:
        for line_num, raw_line in enumerate(f, 1):
            raw_line = raw_line.strip()
            if not raw_line:
                continue

            try:
                entry = json.loads(raw_line)
            except json.JSONDecodeError as e:
                session.parse_errors.append(f"Line {line_num}: {e}")
                continue

            ts = entry.get("timestamp", "")
            entry_type = entry.get("type", "")
            payload = entry.get("payload", {})

            if entry_type == "session_meta":
                session.session_id = payload.get("id", session.session_id)
                session.cwd = payload.get("cwd")
                session.cli_version = payload.get("cli_version")
                session.model_provider = payload.get("model_provider")
                git = payload.get("git") or {}
                session.git_branch = git.get("branch")
                session.git_commit = git.get("commit_hash")
                session.git_remote = git.get("repository_url")

            elif entry_type == "turn_context":
                current_turn_id = payload.get("turn_id")
                session.model = payload.get("model") or session.model

            elif entry_type == "event_msg":
                evt_type = payload.get("type", "")

                if evt_type == "user_message":
                    text = payload.get("message", "")
                    if not session.first_prompt:
                        session.first_prompt = text[:200]
                    sfs_messages.append({
                        "msg_id": f"msg_{len(sfs_messages):04d}",
                        "role": "user",
                        "content": [{"type": "text", "text": text}],
                        "timestamp": ts,
                        "turn_id": current_turn_id,
                    })

                elif evt_type == "task_started":
                    current_turn_id = payload.get("turn_id", current_turn_id)
                    session.turn_count += 1

                elif evt_type == "token_count":
                    info = payload.get("info", {})
                    total_usage = info.get("total_token_usage", {})
                    session.total_input_tokens = total_usage.get("input_tokens", 0)
                    session.total_output_tokens = total_usage.get("output_tokens", 0)

                elif evt_type == "agent_message":
                    text = payload.get("message", "")
                    phase = payload.get("phase", "final_answer")
                    sfs_messages.append({
                        "msg_id": f"msg_{len(sfs_messages):04d}",
                        "role": "assistant",
                        "content": [{"type": "text", "text": text}],
                        "timestamp": ts,
                        "model": session.model,
                        "turn_id": current_turn_id,
                    })

            elif entry_type == "response_item":
                ri_type = payload.get("type", "")

                if ri_type == "message":
                    role = payload.get("role", "assistant")
                    sfs_role = "developer" if role == "developer" else role
                    codex_content = payload.get("content", [])
                    sfs_content = []
                    for item in codex_content:
                        if not isinstance(item, dict):
                            continue
                        ct = item.get("type", "")
                        if ct in ("input_text", "output_text"):
                            sfs_content.append({"type": "text", "text": item.get("text", "")})
                        elif ct == "input_image":
                            sfs_content.append({"type": "image", "source": {"url": item.get("image_url", "")}})

                    if sfs_content:
                        sfs_messages.append({
                            "msg_id": f"msg_{len(sfs_messages):04d}",
                            "role": sfs_role,
                            "content": sfs_content,
                            "timestamp": ts,
                            "model": session.model if sfs_role == "assistant" else None,
                            "turn_id": current_turn_id,
                        })

                elif ri_type == "reasoning":
                    text = ""
                    for item in payload.get("content", []):
                        if isinstance(item, dict):
                            text += item.get("text", "")
                    summary_parts = payload.get("summary", [])
                    summary = " ".join(
                        s.get("text", "") for s in summary_parts if isinstance(s, dict)
                    )
                    sfs_messages.append({
                        "msg_id": f"msg_{len(sfs_messages):04d}",
                        "role": "assistant",
                        "content": [{"type": "thinking", "text": text or summary}],
                        "timestamp": ts,
                        "model": session.model,
                        "turn_id": current_turn_id,
                    })

                elif ri_type == "local_shell_call":
                    action = payload.get("action", {})
                    cmd = action.get("command", [])
                    cmd_str = cmd[-1] if cmd else ""
                    call_id = payload.get("call_id", "")
                    sfs_messages.append({
                        "msg_id": f"msg_{len(sfs_messages):04d}",
                        "role": "assistant",
                        "content": [{
                            "type": "tool_use",
                            "tool_use_id": call_id,
                            "name": "Bash",
                            "input": {"command": cmd_str},
                        }],
                        "timestamp": ts,
                        "model": session.model,
                        "turn_id": current_turn_id,
                    })
                    session.tool_use_count += 1

                elif ri_type == "function_call":
                    sfs_messages.append({
                        "msg_id": f"msg_{len(sfs_messages):04d}",
                        "role": "assistant",
                        "content": [{
                            "type": "tool_use",
                            "tool_use_id": payload.get("call_id", ""),
                            "name": payload.get("name", "unknown"),
                            "input": json.loads(payload.get("arguments", "{}"))
                            if isinstance(payload.get("arguments"), str)
                            else payload.get("arguments", {}),
                        }],
                        "timestamp": ts,
                        "model": session.model,
                        "turn_id": current_turn_id,
                    })
                    session.tool_use_count += 1

                elif ri_type == "function_call_output":
                    output = payload.get("output", {})
                    text = output.get("text", "") if isinstance(output, dict) else str(output)
                    sfs_messages.append({
                        "msg_id": f"msg_{len(sfs_messages):04d}",
                        "role": "tool",
                        "content": [{
                            "type": "tool_result",
                            "tool_use_id": payload.get("call_id", ""),
                            "content": text,
                        }],
                        "timestamp": ts,
                        "turn_id": current_turn_id,
                    })

    session.messages = sfs_messages
    session.message_count = len(sfs_messages)
    return session


def _extract_session_id_from_path(path: Path) -> str:
    """Extract session UUID from a Codex rollout filename."""
    # Format: rollout-YYYY-MM-DDThh-mm-ss-{UUID}.jsonl
    stem = path.stem  # rollout-2026-03-20T09-12-00-019d0a84-...
    parts = stem.split("-")
    # UUID starts after the timestamp (6 dash-separated parts)
    if len(parts) > 6:
        return "-".join(parts[6:])
    return stem


# ---------------------------------------------------------------------------
# .sfs converter (Codex → .sfs)
# ---------------------------------------------------------------------------


def convert_codex_to_sfs(
    codex_session: CodexParsedSession,
    session_dir: Path,
    session_id: str | None = None,
) -> Path:
    """Convert a parsed Codex session to .sfs format."""
    from sessionfs.utils.title_utils import extract_smart_title

    sid = session_id or session_id_from_native(codex_session.session_id)
    session_dir.mkdir(parents=True, exist_ok=True)

    messages = codex_session.messages
    timestamps = [m["timestamp"] for m in messages if m.get("timestamp")]
    created_at = min(timestamps) if timestamps else datetime.now(timezone.utc).isoformat()
    updated_at = max(timestamps) if timestamps else created_at

    # Duration
    duration_ms = None
    if len(timestamps) >= 2:
        try:
            first = datetime.fromisoformat(min(timestamps).replace("Z", "+00:00"))
            last = datetime.fromisoformat(max(timestamps).replace("Z", "+00:00"))
            duration_ms = int((last - first).total_seconds() * 1000)
        except (ValueError, TypeError):
            pass

    title = extract_smart_title(
        messages=messages or None,
        raw_title=codex_session.first_prompt,
        message_count=codex_session.message_count,
    )
    if title.startswith("Untitled session"):
        title = None

    manifest = {
        "sfs_version": SFS_FORMAT_VERSION,
        "session_id": sid,
        "title": title,
        "tags": [],
        "created_at": created_at,
        "updated_at": updated_at,
        "source": {
            "tool": "codex",
            "tool_version": codex_session.cli_version,
            "sfs_converter_version": SFS_CONVERTER_VERSION,
            "original_session_id": codex_session.session_id,
            "original_path": codex_session.source_path,
            "interface": "cli",
        },
        "stats": {
            "message_count": codex_session.message_count,
            "turn_count": codex_session.turn_count,
            "tool_use_count": codex_session.tool_use_count,
            "total_input_tokens": codex_session.total_input_tokens,
            "total_output_tokens": codex_session.total_output_tokens,
            "duration_ms": duration_ms,
        },
    }

    if codex_session.model:
        manifest["model"] = {
            "provider": codex_session.model_provider or "openai",
            "model_id": codex_session.model,
        }

    (session_dir / "manifest.json").write_text(json.dumps(manifest, indent=2))

    with open(session_dir / "messages.jsonl", "w") as f:
        for msg in messages:
            f.write(json.dumps(msg, separators=(",", ":")) + "\n")

    # Workspace
    if codex_session.cwd:
        workspace = {
            "root_path": codex_session.cwd,
            "git": {},
        }
        if codex_session.git_branch:
            workspace["git"]["branch"] = codex_session.git_branch
        if codex_session.git_commit:
            workspace["git"]["commit_sha"] = codex_session.git_commit
        if codex_session.git_remote:
            workspace["git"]["remote_url"] = codex_session.git_remote
        (session_dir / "workspace.json").write_text(json.dumps(workspace, indent=2))

    return session_dir


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


def discover_codex_sessions(home_dir: Path) -> list[dict[str, Any]]:
    """Discover Codex sessions via SQLite index or filesystem scan."""
    sessions: list[dict[str, Any]] = []

    # Try SQLite first
    db_path = home_dir / _STATE_DB
    if db_path.exists():
        try:
            conn = sqlite3.connect(str(db_path))
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM threads WHERE archived = 0 ORDER BY updated_at DESC"
            ).fetchall()
            for row in rows:
                path = Path(row["rollout_path"])
                if path.exists():
                    sessions.append({
                        "session_id": row["id"],
                        "path": str(path),
                        "cwd": row["cwd"],
                        "title": row["title"],
                        "model": row["model"],
                        "mtime": path.stat().st_mtime,
                        "size_bytes": path.stat().st_size,
                    })
            conn.close()
            return sessions
        except Exception as exc:
            logger.warning("Failed to read Codex SQLite index, falling back to scan: %s", exc)

    # Fallback: filesystem scan
    sessions_dir = home_dir / "sessions"
    if not sessions_dir.is_dir():
        return sessions

    for jsonl in sorted(sessions_dir.rglob("rollout-*.jsonl"), reverse=True):
        stat = jsonl.stat()
        sessions.append({
            "session_id": _extract_session_id_from_path(jsonl),
            "path": str(jsonl),
            "cwd": "",
            "title": "",
            "model": None,
            "mtime": stat.st_mtime,
            "size_bytes": stat.st_size,
        })
        if len(sessions) >= 1000:
            break

    return sessions


# ---------------------------------------------------------------------------
# Watcher
# ---------------------------------------------------------------------------


class _CodexEventHandler(FileSystemEventHandler):
    def __init__(self, queue: list[str], lock: threading.Lock):
        self._queue = queue
        self._lock = lock

    def on_modified(self, event: FileSystemEvent) -> None:
        if event.is_directory:
            return
        if event.src_path.endswith(".jsonl"):
            with self._lock:
                if event.src_path not in self._queue:
                    self._queue.append(event.src_path)

    def on_created(self, event: FileSystemEvent) -> None:
        self.on_modified(event)


class CodexWatcher:
    """Watches Codex CLI session storage and captures to .sfs."""

    def __init__(
        self,
        config: CodexWatcherConfig,
        store: LocalStore,
        scan_interval: float = 5.0,
    ) -> None:
        self._home_dir = config.home_dir
        self._store = store
        self._scan_interval = scan_interval
        self._sessions_dir = config.home_dir / "sessions"

        self._tracked: dict[str, NativeSessionRef] = {}
        self._health = WatcherHealth.HEALTHY
        self._last_scan_at: str | None = None
        self._last_error: str | None = None
        self._last_event_time = 0.0

        self._observer: Observer | None = None
        self._event_queue: list[str] = []
        self._event_lock = threading.Lock()

    def full_scan(self) -> None:
        if not self._home_dir.is_dir():
            self._health = WatcherHealth.DEGRADED
            self._last_error = f"Codex home not found: {self._home_dir}"
            return

        try:
            sessions = discover_codex_sessions(self._home_dir)
            captured = 0
            for s_info in sessions:
                native_id = s_info["session_id"]
                native_path = Path(s_info["path"])
                if not native_path.exists():
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

                self._capture_session(native_id, native_path, current_mtime, current_size)
                captured += 1

            self._health = WatcherHealth.HEALTHY
            self._last_scan_at = datetime.now(timezone.utc).isoformat()
            logger.info("Codex scan: %d found, %d captured", len(sessions), captured)

        except Exception as e:
            logger.error("Codex full scan failed: %s", e, exc_info=True)
            self._health = WatcherHealth.DEGRADED
            self._last_error = str(e)

    def _capture_session(
        self, native_id: str, native_path: Path, mtime: float, size: int,
    ) -> None:
        logger.info("Capturing Codex session %s (%d bytes)", native_id[:12], size)
        try:
            codex_session = parse_codex_session(native_path)
            sfs_id = session_id_from_native(native_id)
            session_dir = self._store.allocate_session_dir(sfs_id)
            convert_codex_to_sfs(codex_session, session_dir, session_id=sfs_id)

            manifest_path = session_dir / "manifest.json"
            if manifest_path.exists():
                manifest = json.loads(manifest_path.read_text())
                self._store.upsert_session_metadata(sfs_id, manifest, str(session_dir))

            ref = NativeSessionRef(
                tool="codex",
                native_session_id=native_id,
                native_path=str(native_path),
                sfs_session_id=sfs_id,
                last_mtime=mtime,
                last_size=size,
                last_captured_at=datetime.now(timezone.utc).isoformat(),
                project_path=codex_session.cwd,
            )
            self._tracked[native_id] = ref
            self._store.upsert_tracked_session(ref)

        except Exception as e:
            logger.error("Failed to capture Codex session %s: %s", native_id[:12], e, exc_info=True)
            self._last_error = f"Capture failed: {e}"

    def start_watching(self) -> None:
        if not self._sessions_dir.is_dir():
            return
        handler = _CodexEventHandler(self._event_queue, self._event_lock)
        self._observer = Observer()
        self._observer.schedule(handler, str(self._sessions_dir), recursive=True)
        self._observer.daemon = True
        self._observer.start()
        logger.info("Watching %s for Codex session changes", self._sessions_dir)

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

        for path_str in paths:
            path = Path(path_str)
            if not path.exists() or not path.name.startswith("rollout-"):
                continue
            native_id = _extract_session_id_from_path(path)
            stat = path.stat()
            self._capture_session(native_id, path, stat.st_mtime, stat.st_size)

    def get_status(self) -> WatcherStatus:
        return WatcherStatus(
            name="codex",
            enabled=True,
            health=self._health.value,
            sessions_tracked=len(self._tracked),
            last_scan_at=self._last_scan_at,
            last_error=self._last_error,
            watch_paths=[str(self._sessions_dir)],
        )
