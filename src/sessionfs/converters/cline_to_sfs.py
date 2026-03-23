"""Cline / Roo Code -> .sfs converter.

Shared parser for both Cline and Roo Code VS Code extensions. Both store
conversations in the Anthropic MessageParam format, making them nearly
identical to parse. Roo Code is a Cline fork with minor storage differences:

- Cline: tasks/{task-id}/api_conversation_history.json, state/taskHistory.json
- Roo Code: tasks/{task-id}/api_conversation_history.json, tasks/_index.json

Both are capture-only — write-back is not supported due to the complexity
of reconstructing task history state for automated injection.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sessionfs.spec.version import SFS_FORMAT_VERSION, SFS_CONVERTER_VERSION

logger = logging.getLogger("sessionfs.converters.cline_to_sfs")

# Platform-specific default storage paths
_CLINE_STORAGE = {
    "darwin": (
        Path.home() / "Library" / "Application Support"
        / "Code" / "User" / "globalStorage" / "saoudrizwan.claude-dev"
    ),
    "linux": (
        Path.home() / ".config" / "Code" / "User"
        / "globalStorage" / "saoudrizwan.claude-dev"
    ),
}

_ROO_CODE_STORAGE = {
    "darwin": (
        Path.home() / "Library" / "Application Support"
        / "Code" / "User" / "globalStorage" / "rooveterinaryinc.roo-cline"
    ),
    "linux": (
        Path.home() / ".config" / "Code" / "User"
        / "globalStorage" / "rooveterinaryinc.roo-cline"
    ),
}


def _get_platform() -> str:
    import platform
    return platform.system().lower()


def _get_default_storage(tool: str) -> Path | None:
    """Return the default storage path for a given tool."""
    plat = _get_platform()
    paths = _ROO_CODE_STORAGE if tool == "roo-code" else _CLINE_STORAGE
    return paths.get(plat)


# ---------------------------------------------------------------------------
# Parsed session dataclass
# ---------------------------------------------------------------------------


@dataclass
class ClineParsedSession:
    """Intermediate representation of a parsed Cline/Roo Code session."""

    session_id: str
    tool: str = "cline"  # "cline" or "roo-code"
    task_dir: str | None = None
    task_label: str | None = None
    workspace_folder: str | None = None
    created_at: str | None = None
    last_updated_at: str | None = None
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


def _extract_text_from_content(content: Any) -> str:
    """Extract plain text from Anthropic content (string or content blocks)."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict):
                if block.get("type") == "text":
                    parts.append(block.get("text", ""))
        return "\n".join(parts)
    return ""


def _parse_api_messages(raw_messages: list[dict]) -> tuple[
    list[dict[str, Any]], int, int, int
]:
    """Parse Anthropic MessageParam array into .sfs messages.

    Returns (sfs_messages, turn_count, tool_use_count, token_estimate).
    """
    sfs_messages: list[dict[str, Any]] = []
    turn_count = 0
    tool_use_count = 0
    prev_role: str | None = None

    for idx, msg in enumerate(raw_messages):
        role = msg.get("role", "")
        content = msg.get("content", "")

        if role == "user":
            # User content can be a string or array of content blocks
            if isinstance(content, str):
                if not content.strip():
                    continue
                if prev_role != "user":
                    turn_count += 1
                sfs_messages.append({
                    "msg_id": f"msg_{len(sfs_messages):04d}",
                    "role": "user",
                    "content": [{"type": "text", "text": content}],
                })
                prev_role = "user"

            elif isinstance(content, list):
                # Could contain text blocks and/or tool_result blocks
                text_blocks: list[dict] = []
                tool_results: list[dict] = []

                for block in content:
                    if not isinstance(block, dict):
                        continue
                    btype = block.get("type", "")

                    if btype == "text":
                        text_blocks.append({"type": "text", "text": block.get("text", "")})
                    elif btype == "tool_result":
                        # Map to tool role message
                        result_content = block.get("content", "")
                        if isinstance(result_content, list):
                            # Extract text from content blocks
                            result_text = _extract_text_from_content(result_content)
                        else:
                            result_text = str(result_content)
                        tool_results.append({
                            "msg_id": f"msg_{len(sfs_messages) + len(text_blocks) + len(tool_results):04d}",
                            "role": "tool",
                            "content": [{
                                "type": "tool_result",
                                "tool_use_id": block.get("tool_use_id", ""),
                                "content": result_text,
                                "is_error": block.get("is_error", False),
                            }],
                        })
                    elif btype == "image":
                        text_blocks.append({
                            "type": "image",
                            "source": block.get("source", {}),
                        })

                # Emit text blocks as user message
                if text_blocks:
                    if prev_role != "user":
                        turn_count += 1
                    sfs_messages.append({
                        "msg_id": f"msg_{len(sfs_messages):04d}",
                        "role": "user",
                        "content": text_blocks,
                    })
                    prev_role = "user"

                # Emit tool results as tool messages
                for tr in tool_results:
                    tr["msg_id"] = f"msg_{len(sfs_messages):04d}"
                    sfs_messages.append(tr)
                    prev_role = "tool"

        elif role == "assistant":
            sfs_content: list[dict] = []

            if isinstance(content, str):
                if not content.strip():
                    continue
                sfs_content.append({"type": "text", "text": content})

            elif isinstance(content, list):
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    btype = block.get("type", "")

                    if btype == "text":
                        text = block.get("text", "")
                        if text.strip():
                            sfs_content.append({"type": "text", "text": text})
                    elif btype == "thinking":
                        sfs_content.append({
                            "type": "thinking",
                            "text": block.get("thinking", block.get("text", "")),
                        })
                    elif btype == "tool_use":
                        sfs_content.append({
                            "type": "tool_use",
                            "tool_use_id": block.get("id", ""),
                            "name": block.get("name", "unknown"),
                            "input": block.get("input", {}),
                        })
                        tool_use_count += 1

            if sfs_content:
                sfs_messages.append({
                    "msg_id": f"msg_{len(sfs_messages):04d}",
                    "role": "assistant",
                    "content": sfs_content,
                })
                prev_role = "assistant"

    return sfs_messages, turn_count, tool_use_count, 0


def _parse_ui_messages(raw_messages: list[dict]) -> tuple[
    list[dict[str, Any]], int, int
]:
    """Fallback: parse Cline ui_messages.json into .sfs messages.

    Less reliable than api_conversation_history.json but usable as fallback.
    Returns (sfs_messages, turn_count, tool_use_count).
    """
    sfs_messages: list[dict[str, Any]] = []
    turn_count = 0
    tool_use_count = 0
    prev_role: str | None = None

    for msg in raw_messages:
        msg_type = msg.get("type", "")  # "ask" or "say"
        say_type = msg.get("say", "")
        ask_type = msg.get("ask", "")
        text = msg.get("text", "")
        ts = msg.get("ts")

        timestamp = None
        if ts:
            try:
                timestamp = datetime.fromtimestamp(ts / 1000, tz=timezone.utc).isoformat()
            except (TypeError, ValueError, OSError):
                pass

        if msg_type == "ask" and ask_type == "followup":
            # User follow-up question
            if text.strip():
                if prev_role != "user":
                    turn_count += 1
                sfs_messages.append({
                    "msg_id": f"msg_{len(sfs_messages):04d}",
                    "role": "user",
                    "content": [{"type": "text", "text": text}],
                    "timestamp": timestamp,
                })
                prev_role = "user"

        elif msg_type == "say" and say_type == "text":
            # Assistant text response
            if text.strip():
                sfs_messages.append({
                    "msg_id": f"msg_{len(sfs_messages):04d}",
                    "role": "assistant",
                    "content": [{"type": "text", "text": text}],
                    "timestamp": timestamp,
                })
                prev_role = "assistant"

        elif msg_type == "say" and say_type == "tool":
            # Tool use
            tool_use_count += 1
            tool_name = "unknown"
            tool_input: dict = {}
            try:
                tool_data = json.loads(text) if text else {}
                tool_name = tool_data.get("tool", "unknown")
                tool_input = tool_data
            except json.JSONDecodeError:
                pass

            sfs_messages.append({
                "msg_id": f"msg_{len(sfs_messages):04d}",
                "role": "assistant",
                "content": [{
                    "type": "tool_use",
                    "tool_use_id": f"ui_{len(sfs_messages):04d}",
                    "name": tool_name,
                    "input": tool_input,
                }],
                "timestamp": timestamp,
            })
            prev_role = "assistant"

        elif msg_type == "say" and say_type == "user_feedback":
            # User typed something
            if text.strip():
                if prev_role != "user":
                    turn_count += 1
                sfs_messages.append({
                    "msg_id": f"msg_{len(sfs_messages):04d}",
                    "role": "user",
                    "content": [{"type": "text", "text": text}],
                    "timestamp": timestamp,
                })
                prev_role = "user"

    return sfs_messages, turn_count, tool_use_count


def parse_cline_session(
    task_dir: Path, tool: str = "cline",
) -> ClineParsedSession:
    """Parse a Cline/Roo Code task directory into a ClineParsedSession.

    Reads api_conversation_history.json as primary source, falls back to
    ui_messages.json if the API history is missing or empty.
    """
    task_id = task_dir.name
    session = ClineParsedSession(
        session_id=task_id,
        tool=tool,
        task_dir=str(task_dir),
    )

    if not task_dir.is_dir():
        session.parse_errors.append(f"Task directory not found: {task_dir}")
        return session

    # Try api_conversation_history.json first (primary source)
    api_path = task_dir / "api_conversation_history.json"
    ui_path = task_dir / "ui_messages.json"

    used_api = False
    if api_path.exists():
        try:
            raw = json.loads(api_path.read_text())
            if isinstance(raw, list) and len(raw) > 0:
                messages, turns, tools, _ = _parse_api_messages(raw)
                session.messages = messages
                session.message_count = len(messages)
                session.turn_count = turns
                session.tool_use_count = tools
                used_api = True
        except (json.JSONDecodeError, OSError) as exc:
            session.parse_errors.append(f"Failed to read api_conversation_history: {exc}")

    # Fall back to ui_messages.json
    if not used_api and ui_path.exists():
        try:
            raw = json.loads(ui_path.read_text())
            if isinstance(raw, list) and len(raw) > 0:
                messages, turns, tools = _parse_ui_messages(raw)
                session.messages = messages
                session.message_count = len(messages)
                session.turn_count = turns
                session.tool_use_count = tools
        except (json.JSONDecodeError, OSError) as exc:
            session.parse_errors.append(f"Failed to read ui_messages: {exc}")

    # Read per-task metadata (Roo Code stores history_item.json per task)
    history_item_path = task_dir / "history_item.json"
    if history_item_path.exists():
        try:
            meta = json.loads(history_item_path.read_text())
            session.task_label = meta.get("task", "")
            session.workspace_folder = meta.get("workspace", "")
            ts = meta.get("ts")
            if ts:
                session.created_at = datetime.fromtimestamp(
                    ts / 1000, tz=timezone.utc
                ).isoformat()
            total_cost = meta.get("totalCost")
            if isinstance(total_cost, dict):
                session.total_input_tokens = total_cost.get("inputTokens", 0)
                session.total_output_tokens = total_cost.get("outputTokens", 0)
        except (json.JSONDecodeError, OSError):
            pass

    return session


# ---------------------------------------------------------------------------
# Converter
# ---------------------------------------------------------------------------


def convert_cline_to_sfs(
    cline_session: ClineParsedSession,
    session_dir: Path,
    session_id: str | None = None,
) -> Path:
    """Convert a parsed Cline/Roo Code session to .sfs format."""
    from sessionfs.session_id import session_id_from_native
    from sessionfs.utils.title_utils import extract_smart_title

    sid = session_id or session_id_from_native(cline_session.session_id)
    session_dir.mkdir(parents=True, exist_ok=True)

    messages = cline_session.messages
    now_iso = datetime.now(timezone.utc).isoformat()
    created_at = cline_session.created_at or now_iso
    updated_at = cline_session.last_updated_at or now_iso

    title = extract_smart_title(
        messages=messages or None,
        raw_title=cline_session.task_label or None,
        message_count=cline_session.message_count,
    )
    if title.startswith("Untitled session"):
        title = None

    tool_name = cline_session.tool
    manifest = {
        "sfs_version": SFS_FORMAT_VERSION,
        "session_id": sid,
        "title": title,
        "tags": [],
        "created_at": created_at,
        "updated_at": updated_at,
        "source": {
            "tool": tool_name,
            "tool_version": None,
            "sfs_converter_version": SFS_CONVERTER_VERSION,
            "original_session_id": cline_session.session_id,
            "interface": "ide",
        },
        "stats": {
            "message_count": cline_session.message_count,
            "turn_count": cline_session.turn_count,
            "tool_use_count": cline_session.tool_use_count,
            "total_input_tokens": cline_session.total_input_tokens,
            "total_output_tokens": cline_session.total_output_tokens,
        },
    }

    (session_dir / "manifest.json").write_text(json.dumps(manifest, indent=2))

    with open(session_dir / "messages.jsonl", "w") as f:
        for msg in messages:
            f.write(json.dumps(msg, separators=(",", ":")) + "\n")

    if cline_session.workspace_folder:
        workspace = {"root_path": cline_session.workspace_folder, "git": {}}
        (session_dir / "workspace.json").write_text(json.dumps(workspace, indent=2))

    return session_dir


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


def discover_cline_sessions(
    storage_dir: Path, tool: str = "cline",
) -> list[dict[str, Any]]:
    """Discover Cline/Roo Code sessions from the globalStorage directory.

    For Cline: reads state/taskHistory.json
    For Roo Code: reads tasks/_index.json or scans task dirs
    """
    sessions: list[dict[str, Any]] = []
    tasks_dir = storage_dir / "tasks"

    if not tasks_dir.is_dir():
        return sessions

    # Try structured index first
    if tool == "roo-code":
        # Roo Code stores an index at tasks/_index.json
        index_path = tasks_dir / "_index.json"
        if index_path.exists():
            try:
                index_data = json.loads(index_path.read_text())
                if isinstance(index_data, list):
                    for entry in index_data:
                        task_id = entry.get("id", "")
                        if not task_id:
                            continue
                        task_path = tasks_dir / task_id
                        if task_path.is_dir():
                            api_file = task_path / "api_conversation_history.json"
                            mtime = api_file.stat().st_mtime if api_file.exists() else 0.0
                            size = api_file.stat().st_size if api_file.exists() else 0
                            sessions.append({
                                "session_id": task_id,
                                "path": str(task_path),
                                "task_label": entry.get("task", ""),
                                "mtime": mtime,
                                "size_bytes": size,
                            })
                    return sessions
            except (json.JSONDecodeError, OSError):
                pass
    else:
        # Cline: state/taskHistory.json
        history_path = storage_dir / "state" / "taskHistory.json"
        if history_path.exists():
            try:
                history = json.loads(history_path.read_text())
                if isinstance(history, list):
                    for entry in history:
                        task_id = entry.get("id", "")
                        if not task_id:
                            continue
                        task_path = tasks_dir / task_id
                        if task_path.is_dir():
                            api_file = task_path / "api_conversation_history.json"
                            mtime = api_file.stat().st_mtime if api_file.exists() else 0.0
                            size = api_file.stat().st_size if api_file.exists() else 0
                            sessions.append({
                                "session_id": task_id,
                                "path": str(task_path),
                                "task_label": entry.get("task", ""),
                                "mtime": mtime,
                                "size_bytes": size,
                            })
                    return sessions
            except (json.JSONDecodeError, OSError):
                pass

    # Fallback: scan task directories
    for task_path in sorted(tasks_dir.iterdir(), reverse=True):
        if not task_path.is_dir():
            continue
        if task_path.name.startswith("_"):
            continue  # Skip _index.json etc.

        api_file = task_path / "api_conversation_history.json"
        ui_file = task_path / "ui_messages.json"
        has_data = api_file.exists() or ui_file.exists()
        if not has_data:
            continue

        ref_file = api_file if api_file.exists() else ui_file
        stat = ref_file.stat()

        # Try to get task label from history_item.json
        task_label = ""
        hi_path = task_path / "history_item.json"
        if hi_path.exists():
            try:
                hi = json.loads(hi_path.read_text())
                task_label = hi.get("task", "")
            except (json.JSONDecodeError, OSError):
                pass

        sessions.append({
            "session_id": task_path.name,
            "path": str(task_path),
            "task_label": task_label,
            "mtime": stat.st_mtime,
            "size_bytes": stat.st_size,
        })

        if len(sessions) >= 1000:
            break

    return sessions
