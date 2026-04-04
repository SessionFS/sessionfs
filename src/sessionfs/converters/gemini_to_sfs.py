"""Gemini CLI -> .sfs converter.

Converts Gemini CLI native session files (JSON) into the canonical .sfs format.

Gemini sessions are single JSON files with a flat messages array.
Message types: user (array of parts), gemini (plain string), error, info.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sessionfs.spec.version import SFS_FORMAT_VERSION, SFS_CONVERTER_VERSION

logger = logging.getLogger("sessionfs.converters.gemini_to_sfs")


def _extract_tool_result_text(result: list[dict[str, Any]] | Any) -> str:
    """Extract a text string from a Gemini toolCalls result array.

    Each entry in the result array is a dict with a ``functionResponse`` key
    containing ``response.output``.  We concatenate all output strings.
    """
    if not isinstance(result, list):
        return str(result) if result else ""

    parts: list[str] = []
    for entry in result:
        if not isinstance(entry, dict):
            continue
        fr = entry.get("functionResponse", {})
        resp = fr.get("response", {})
        output = resp.get("output", "")
        if output:
            parts.append(str(output))
    return "\n".join(parts) if parts else ""


@dataclass
class GeminiParsedSession:
    """Intermediate representation of a parsed Gemini CLI session."""

    session_id: str
    project_hash: str | None = None
    project_path: str | None = None
    source_path: str | None = None
    start_time: str | None = None
    last_updated: str | None = None
    summary: str | None = None
    model_id: str | None = None
    messages: list[dict[str, Any]] = field(default_factory=list)
    message_count: int = 0
    turn_count: int = 0
    tool_use_count: int = 0
    parse_errors: list[str] = field(default_factory=list)


def parse_gemini_session(session_path: Path) -> GeminiParsedSession:
    """Parse a Gemini CLI session JSON file."""
    data = json.loads(session_path.read_text())

    session = GeminiParsedSession(
        session_id=data.get("sessionId", session_path.stem),
        project_hash=data.get("projectHash"),
        source_path=str(session_path),
        start_time=data.get("startTime"),
        last_updated=data.get("lastUpdated"),
        summary=data.get("summary"),
    )

    sfs_messages: list[dict[str, Any]] = []
    turn_count = 0
    tool_use_count = 0
    prev_role = None

    for msg in data.get("messages", []):
        msg_type = msg.get("type", "")
        msg_id = msg.get("id", f"msg_{len(sfs_messages):04d}")
        timestamp = msg.get("timestamp")
        content_raw = msg.get("content")

        if msg_type == "user":
            # Count turns (user message after non-user)
            if prev_role != "user":
                turn_count += 1

            # Content is array of parts: [{"text": "..."}]
            sfs_content = []
            if isinstance(content_raw, list):
                for part in content_raw:
                    if isinstance(part, dict):
                        if "text" in part:
                            sfs_content.append({"type": "text", "text": part["text"]})
                        elif "inlineData" in part:
                            sfs_content.append({
                                "type": "image",
                                "source": {
                                    "type": "base64",
                                    "media_type": part["inlineData"].get("mimeType", "image/png"),
                                    "data": part["inlineData"].get("data", ""),
                                },
                            })
            elif isinstance(content_raw, str):
                sfs_content.append({"type": "text", "text": content_raw})

            if sfs_content:
                sfs_messages.append({
                    "msg_id": msg_id,
                    "role": "user",
                    "content": sfs_content,
                    "timestamp": timestamp,
                })
            prev_role = "user"

        elif msg_type == "gemini":
            # Content is a plain string
            text = str(content_raw) if content_raw else ""
            tool_calls = msg.get("toolCalls", [])

            if tool_calls:
                # Gemini CLI stores tool calls as a `toolCalls` array on
                # assistant messages. Each entry has: id, name, args, result,
                # status, timestamp, displayName, description.
                #
                # We emit the assistant text + tool_use blocks in one message,
                # then a separate tool_result message for each call's response.
                asst_content: list[dict[str, Any]] = []
                if text:
                    asst_content.append({"type": "text", "text": text})

                for tc in tool_calls:
                    tc_id = tc.get("id", f"tc_{len(sfs_messages):04d}")
                    tc_name = tc.get("displayName") or tc.get("name", "unknown")
                    tc_args = tc.get("args", {})

                    asst_content.append({
                        "type": "tool_use",
                        "id": tc_id,
                        "name": tc_name,
                        "input": tc_args if isinstance(tc_args, dict) else {},
                    })
                    tool_use_count += 1

                if asst_content:
                    sfs_messages.append({
                        "msg_id": msg_id,
                        "role": "assistant",
                        "content": asst_content,
                        "timestamp": timestamp,
                    })

                # Emit tool_result messages
                for tc in tool_calls:
                    tc_id = tc.get("id", "")
                    tc_result = tc.get("result", [])
                    tc_status = tc.get("status", "")
                    result_text = _extract_tool_result_text(tc_result)
                    is_error = tc_status == "error"

                    result_msg_id = f"{msg_id}_result_{tc_id}"
                    tc_timestamp = tc.get("timestamp", timestamp)
                    sfs_messages.append({
                        "msg_id": result_msg_id,
                        "role": "tool",
                        "content": [{
                            "type": "tool_result",
                            "tool_use_id": tc_id,
                            "content": result_text,
                            "is_error": is_error,
                        }],
                        "timestamp": tc_timestamp,
                    })

            elif text:
                sfs_messages.append({
                    "msg_id": msg_id,
                    "role": "assistant",
                    "content": [{"type": "text", "text": text}],
                    "timestamp": timestamp,
                })
            prev_role = "assistant"

        elif msg_type == "error":
            # Map to system message
            text = str(content_raw) if content_raw else ""
            sfs_messages.append({
                "msg_id": msg_id,
                "role": "system",
                "content": [{"type": "text", "text": f"[Error] {text}"}],
                "timestamp": timestamp,
                "is_meta": True,
            })
            prev_role = "system"

        elif msg_type == "info":
            text = str(content_raw) if content_raw else ""
            sfs_messages.append({
                "msg_id": msg_id,
                "role": "system",
                "content": [{"type": "text", "text": f"[Info] {text}"}],
                "timestamp": timestamp,
                "is_meta": True,
            })
            prev_role = "system"

    session.messages = sfs_messages
    session.message_count = len(sfs_messages)
    session.turn_count = turn_count
    session.tool_use_count = tool_use_count
    return session


def convert_gemini_to_sfs(
    gemini_session: GeminiParsedSession,
    session_dir: Path,
    session_id: str | None = None,
) -> Path:
    """Convert a parsed Gemini session to .sfs format."""
    from sessionfs.session_id import session_id_from_native
    from sessionfs.utils.title_utils import extract_smart_title

    sid = session_id or session_id_from_native(gemini_session.session_id)
    session_dir.mkdir(parents=True, exist_ok=True)

    messages = gemini_session.messages
    created_at = gemini_session.start_time or datetime.now(timezone.utc).isoformat()
    updated_at = gemini_session.last_updated or created_at

    # Duration
    duration_ms = None
    try:
        start = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
        end = datetime.fromisoformat(updated_at.replace("Z", "+00:00"))
        duration_ms = int((end - start).total_seconds() * 1000)
    except (ValueError, TypeError):
        pass

    # Title: use summary if available, else extract from messages
    title = extract_smart_title(
        messages=messages or None,
        raw_title=gemini_session.summary,
        message_count=gemini_session.message_count,
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
            "tool": "gemini-cli",
            "tool_version": None,
            "sfs_converter_version": SFS_CONVERTER_VERSION,
            "original_session_id": gemini_session.session_id,
            "original_path": gemini_session.source_path,
            "interface": "cli",
        },
        "model": {
            "provider": "google",
            "model_id": None if gemini_session.model_id in ("<synthetic>", "synthetic", "") else gemini_session.model_id,
        },
        "stats": {
            "message_count": gemini_session.message_count,
            "turn_count": gemini_session.turn_count,
            "tool_use_count": gemini_session.tool_use_count,
            "total_input_tokens": 0,
            "total_output_tokens": 0,
            "duration_ms": duration_ms,
        },
    }

    (session_dir / "manifest.json").write_text(json.dumps(manifest, indent=2))

    with open(session_dir / "messages.jsonl", "w") as f:
        for msg in messages:
            f.write(json.dumps(msg, separators=(",", ":")) + "\n")

    # Workspace
    if gemini_session.project_path:
        workspace = {"root_path": gemini_session.project_path, "git": {}}
        (session_dir / "workspace.json").write_text(json.dumps(workspace, indent=2))

    # Detect slash-command skills in user messages
    from sessionfs.converters.skill_detector import detect_skills, skills_to_tools_json

    detected = detect_skills(messages, "gemini-cli")
    if detected:
        tools_data = {"custom_tools": skills_to_tools_json(detected)}
        (session_dir / "tools.json").write_text(json.dumps(tools_data, indent=2))

    return session_dir


def _extract_model_from_logs(project_dir: Path, session_id: str) -> str | None:
    """Extract the model name from logs.json for a given session.

    Gemini CLI logs the --model flag as the first user message (messageId 0)
    in logs.json with the format: "—model <model-name>"
    """
    logs_path = project_dir / "logs.json"
    if not logs_path.exists():
        return None
    try:
        entries = json.loads(logs_path.read_text())
        for entry in entries:
            if entry.get("sessionId", "").startswith(session_id[:8]):
                msg = entry.get("message", "")
                # Match both em-dash (—) and double-dash (--)
                for prefix in ("\u2014model ", "--model "):
                    if msg.startswith(prefix):
                        return msg[len(prefix):].strip()
    except (json.JSONDecodeError, OSError):
        pass
    return None


def discover_gemini_sessions(home_dir: Path) -> list[dict[str, Any]]:
    """Discover all Gemini CLI sessions.

    Scans ~/.gemini/tmp/*/chats/*.json and resolves project paths
    from projects.json.
    """
    sessions: list[dict[str, Any]] = []

    # Load project mappings
    project_map: dict[str, str] = {}  # hash -> project_path
    projects_file = home_dir / "projects.json"
    if projects_file.exists():
        try:
            projects = json.loads(projects_file.read_text()).get("projects", {})
            for path, name in projects.items():
                h = hashlib.sha256(path.encode()).hexdigest()
                project_map[h] = path
                # Also map by name directory
                project_map[name] = path
        except (json.JSONDecodeError, AttributeError):
            pass

    # Also check history dirs for project roots
    history_dir = home_dir / "history"
    if history_dir.is_dir():
        for d in history_dir.iterdir():
            root_file = d / ".project_root"
            if root_file.exists():
                project_map[d.name] = root_file.read_text().strip()

    tmp_dir = home_dir / "tmp"
    if not tmp_dir.is_dir():
        return sessions

    for project_dir in tmp_dir.iterdir():
        if not project_dir.is_dir():
            continue

        chats_dir = project_dir / "chats"
        if not chats_dir.is_dir():
            continue

        project_path = project_map.get(project_dir.name, "")

        for session_file in sorted(chats_dir.glob("session-*.json"), reverse=True):
            stat = session_file.stat()
            # Extract session ID from filename: session-YYYY-MM-DDThh-mm-{uuid8}.json
            stem = session_file.stem
            parts = stem.split("-")
            uuid_part = parts[-1] if len(parts) > 4 else stem

            sessions.append({
                "session_id": uuid_part,
                "path": str(session_file),
                "project_dir": str(project_dir),
                "project_path": project_path,
                "mtime": stat.st_mtime,
                "size_bytes": stat.st_size,
            })

    return sessions
