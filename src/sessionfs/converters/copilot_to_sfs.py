"""Copilot CLI session parser and converter.

Watches ~/.copilot/session-state/{session-id}/ for session data.
Each session directory contains:
  - events.jsonl: Event stream with user.message, assistant.message,
    tool.execution_start events
  - workspace.yaml: Session metadata (cwd, model, etc.)

Each JSONL line:
  {"type": "event.type", "data": {...}, "id": "uuid", "timestamp": "ISO-8601", "parentId": "uuid|null"}
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sessionfs.spec.version import SFS_FORMAT_VERSION, SFS_CONVERTER_VERSION

logger = logging.getLogger("sessionfs.converters.copilot_to_sfs")


# ---------------------------------------------------------------------------
# Parsed session dataclass
# ---------------------------------------------------------------------------


@dataclass
class CopilotParsedSession:
    """Intermediate representation of a parsed Copilot CLI session."""

    session_id: str
    source_path: str | None = None
    cwd: str | None = None
    cli_version: str | None = None
    model: str | None = None
    model_provider: str | None = None
    first_prompt: str | None = None
    messages: list[dict[str, Any]] = field(default_factory=list)
    message_count: int = 0
    turn_count: int = 0
    tool_use_count: int = 0
    parse_errors: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


def parse_copilot_session(session_dir: Path) -> CopilotParsedSession:
    """Parse a Copilot CLI session directory into a CopilotParsedSession.

    Args:
        session_dir: Path to ~/.copilot/session-state/{session-id}/

    Returns:
        CopilotParsedSession with parsed messages and metadata.
    """
    session_id = session_dir.name
    session = CopilotParsedSession(
        session_id=session_id,
        source_path=str(session_dir),
    )

    # Read workspace.yaml for metadata
    workspace_yaml = session_dir / "workspace.yaml"
    if workspace_yaml.exists():
        _parse_workspace_yaml(workspace_yaml, session)

    # Parse events.jsonl
    events_path = session_dir / "events.jsonl"
    if not events_path.exists():
        session.parse_errors.append("events.jsonl not found")
        return session

    sfs_messages: list[dict[str, Any]] = []
    current_turn_id: str | None = None

    with open(events_path) as f:
        for line_num, raw_line in enumerate(f, 1):
            raw_line = raw_line.strip()
            if not raw_line:
                continue

            try:
                entry = json.loads(raw_line)
            except json.JSONDecodeError as e:
                session.parse_errors.append(f"Line {line_num}: {e}")
                continue

            event_type = entry.get("type", "")
            data = entry.get("data", {})
            ts = entry.get("timestamp", "")
            event_id = entry.get("id", "")
            parent_id = entry.get("parentId")

            if event_type == "user.message":
                text = data.get("content", "")
                if not session.first_prompt and text:
                    session.first_prompt = text[:200]

                # Each user message starts a new turn
                session.turn_count += 1
                current_turn_id = event_id

                sfs_messages.append({
                    "msg_id": f"msg_{len(sfs_messages):04d}",
                    "role": "user",
                    "content": [{"type": "text", "text": text}],
                    "timestamp": ts,
                    "turn_id": current_turn_id,
                })

            elif event_type == "assistant.message":
                text = data.get("content", "")
                model = data.get("model") or session.model

                if model and not session.model:
                    session.model = model

                sfs_messages.append({
                    "msg_id": f"msg_{len(sfs_messages):04d}",
                    "role": "assistant",
                    "content": [{"type": "text", "text": text}],
                    "timestamp": ts,
                    "model": model or session.model,
                    "turn_id": current_turn_id or parent_id,
                })

            elif event_type == "tool.execution_start":
                tool_name = data.get("tool", "unknown")
                tool_input = data.get("input", {})
                call_id = event_id or f"call_{len(sfs_messages):04d}"

                sfs_messages.append({
                    "msg_id": f"msg_{len(sfs_messages):04d}",
                    "role": "assistant",
                    "content": [{
                        "type": "tool_use",
                        "tool_use_id": call_id,
                        "name": tool_name,
                        "input": tool_input if isinstance(tool_input, dict) else {"command": str(tool_input)},
                    }],
                    "timestamp": ts,
                    "model": session.model,
                    "turn_id": current_turn_id or parent_id,
                })
                session.tool_use_count += 1

            elif event_type == "tool.execution_result":
                output = data.get("output", "")
                call_id = parent_id or event_id

                sfs_messages.append({
                    "msg_id": f"msg_{len(sfs_messages):04d}",
                    "role": "tool",
                    "content": [{
                        "type": "tool_result",
                        "tool_use_id": call_id,
                        "content": str(output),
                    }],
                    "timestamp": ts,
                    "turn_id": current_turn_id,
                })

    session.messages = sfs_messages
    session.message_count = len(sfs_messages)
    return session


def _parse_workspace_yaml(path: Path, session: CopilotParsedSession) -> None:
    """Parse workspace.yaml for session metadata."""
    try:
        # Use simple key: value parsing to avoid PyYAML dependency
        text = path.read_text()
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if ":" not in line:
                continue
            key, _, value = line.partition(":")
            key = key.strip()
            value = value.strip().strip("'\"")

            if key == "cwd":
                session.cwd = value
            elif key == "model":
                session.model = value
            elif key == "model_provider":
                session.model_provider = value
            elif key == "cli_version":
                session.cli_version = value
    except Exception as e:
        session.parse_errors.append(f"workspace.yaml: {e}")


# ---------------------------------------------------------------------------
# .sfs converter (Copilot → .sfs)
# ---------------------------------------------------------------------------


def convert_copilot_to_sfs(
    session_dir: Path,
    output_dir: Path,
    session_id: str | None = None,
) -> Path:
    """Convert a Copilot CLI session directory to .sfs format.

    Args:
        session_dir: Path to the Copilot session directory.
        output_dir: Where to write the .sfs session.
        session_id: Override for the sfs session ID.

    Returns:
        Path to the created .sfs session directory.
    """
    from sessionfs.session_id import session_id_from_native
    from sessionfs.utils.title_utils import extract_smart_title

    copilot_session = parse_copilot_session(session_dir)
    sid = session_id or session_id_from_native(copilot_session.session_id)
    output_dir.mkdir(parents=True, exist_ok=True)

    messages = copilot_session.messages
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
        raw_title=copilot_session.first_prompt,
        message_count=copilot_session.message_count,
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
            "tool": "copilot-cli",
            "tool_version": copilot_session.cli_version,
            "sfs_converter_version": SFS_CONVERTER_VERSION,
            "original_session_id": copilot_session.session_id,
            "original_path": copilot_session.source_path,
            "interface": "cli",
        },
        "stats": {
            "message_count": copilot_session.message_count,
            "turn_count": copilot_session.turn_count,
            "tool_use_count": copilot_session.tool_use_count,
            "duration_ms": duration_ms,
        },
    }

    if copilot_session.model and copilot_session.model not in ("<synthetic>", "synthetic", ""):
        manifest["model"] = {
            "provider": copilot_session.model_provider or "github",
            "model_id": copilot_session.model,
        }

    (output_dir / "manifest.json").write_text(json.dumps(manifest, indent=2))

    with open(output_dir / "messages.jsonl", "w") as f:
        for msg in messages:
            f.write(json.dumps(msg, separators=(",", ":")) + "\n")

    # Workspace
    if copilot_session.cwd:
        workspace = {
            "root_path": copilot_session.cwd,
        }
        (output_dir / "workspace.json").write_text(json.dumps(workspace, indent=2))

    # Detect slash-command skills in user messages
    from sessionfs.converters.skill_detector import detect_skills, skills_to_tools_json

    detected = detect_skills(messages, "copilot-cli")
    if detected:
        tools_data = {"custom_tools": skills_to_tools_json(detected)}
        (output_dir / "tools.json").write_text(json.dumps(tools_data, indent=2))

    return output_dir


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


def discover_copilot_sessions(home_dir: Path) -> list[dict[str, Any]]:
    """Discover Copilot CLI sessions via filesystem scan.

    Args:
        home_dir: Path to ~/.copilot (or override).

    Returns:
        List of dicts with session_id, path, cwd, mtime, size_bytes.
    """
    sessions: list[dict[str, Any]] = []
    session_state_dir = home_dir / "session-state"

    if not session_state_dir.is_dir():
        return sessions

    for session_dir in sorted(session_state_dir.iterdir(), reverse=True):
        if not session_dir.is_dir():
            continue

        events_path = session_dir / "events.jsonl"
        if not events_path.exists():
            continue

        stat = events_path.stat()

        # Try to read cwd from workspace.yaml
        cwd = ""
        workspace_yaml = session_dir / "workspace.yaml"
        if workspace_yaml.exists():
            try:
                text = workspace_yaml.read_text()
                for line in text.splitlines():
                    if line.strip().startswith("cwd:"):
                        cwd = line.split(":", 1)[1].strip().strip("'\"")
                        break
            except Exception:
                pass

        sessions.append({
            "session_id": session_dir.name,
            "path": str(session_dir),
            "cwd": cwd,
            "title": "",
            "model": None,
            "mtime": stat.st_mtime,
            "size_bytes": stat.st_size,
        })

        if len(sessions) >= 1000:
            break

    return sessions
