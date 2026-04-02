"""Reverse converter: .sfs → Claude Code JSONL.

Converts a SessionFS .sfs session back into Claude Code's native JSONL format.
Supports two modes:
  - Resume: writes JSONL to CC storage, updates sessions-index.json
  - Export: writes JSONL to a specified output path

Port of proven patterns from spike_2a_cc_writeback.py.
"""

from __future__ import annotations

import json
import logging
import uuid as uuid_mod
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_writeback_logger = logging.getLogger("sfs.writeback")

# M6: Known safe content block types
_KNOWN_BLOCK_TYPES = frozenset({"text", "thinking", "tool_use", "tool_result", "image", "summary"})


# ---------------------------------------------------------------------------
# Path encoding
# ---------------------------------------------------------------------------


def encode_project_path(project_path: str) -> str:
    """Encode an absolute project path to CC's directory name format.

    CC replaces '/' with '-': /path/to/project → -path-to-project
    """
    return project_path.replace("/", "-")


def get_cc_project_dir(project_path: str, cc_home: Path | None = None) -> Path:
    """Get the CC project directory for a given project path."""
    home = cc_home or Path.home() / ".claude"
    encoded = encode_project_path(project_path)
    return home / "projects" / encoded


# ---------------------------------------------------------------------------
# Session index management
# ---------------------------------------------------------------------------


def read_sessions_index(project_dir: Path) -> dict[str, Any]:
    """Read sessions-index.json for a CC project directory."""
    index_path = project_dir / "sessions-index.json"
    if index_path.exists():
        return json.loads(index_path.read_text())
    return {"version": 1, "entries": []}


def write_sessions_index(project_dir: Path, index: dict[str, Any]) -> None:
    """Write sessions-index.json for a CC project directory."""
    index_path = project_dir / "sessions-index.json"
    index_path.write_text(json.dumps(index, indent=2))


def add_index_entry(
    project_dir: Path,
    session_id: str,
    project_path: str,
    first_prompt: str,
    message_count: int,
    git_branch: str = "",
) -> None:
    """Add/update a session entry in sessions-index.json."""
    index = read_sessions_index(project_dir)
    now = datetime.now(timezone.utc)
    jsonl_path = project_dir / f"{session_id}.jsonl"

    entry = {
        "sessionId": session_id,
        "fullPath": str(jsonl_path),
        "fileMtime": int(now.timestamp() * 1000),
        "firstPrompt": first_prompt[:200],
        "messageCount": message_count,
        "created": now.isoformat().replace("+00:00", "Z"),
        "modified": now.isoformat().replace("+00:00", "Z"),
        "gitBranch": git_branch,
        "projectPath": project_path,
        "isSidechain": False,
    }

    # Remove existing entry for this session
    index["entries"] = [
        e for e in index["entries"] if e.get("sessionId") != session_id
    ]
    index["entries"].append(entry)
    write_sessions_index(project_dir, index)


# ---------------------------------------------------------------------------
# Content block conversion (inverse of convert_cc._convert_content_block)
# ---------------------------------------------------------------------------


def _reverse_content_block(block: dict[str, Any]) -> dict[str, Any]:
    """Convert an .sfs content block back to CC format."""
    btype = block.get("type")

    if btype == "text":
        return {"type": "text", "text": block.get("text", "")}

    elif btype == "thinking":
        result: dict[str, Any] = {
            "type": "thinking",
            "thinking": block.get("text", ""),
        }
        if block.get("signature"):
            result["signature"] = block["signature"]
        return result

    elif btype == "tool_use":
        return {
            "type": "tool_use",
            "id": block.get("tool_use_id", ""),
            "name": block.get("name", ""),
            "input": block.get("input", {}),
        }

    elif btype == "tool_result":
        result = {
            "type": "tool_result",
            "tool_use_id": block.get("tool_use_id", ""),
            "content": block.get("content", ""),
        }
        if block.get("is_error"):
            result["is_error"] = True
        return result

    elif btype == "summary":
        return {"type": "text", "text": block.get("text", "")}

    else:
        # M6: Unknown block type — drop with warning, don't pass through
        _writeback_logger.warning(
            "Dropping unknown block type during write-back: %s", btype
        )
        return {"type": "text", "text": f"[SessionFS: unsupported block type '{btype}']"}


def _make_user_content(blocks: list[dict[str, Any]]) -> str | list[dict[str, Any]]:
    """Build CC user message content.

    CC uses a bare string for text-only user messages, or a list
    when tool_result blocks are present.
    """
    has_tool_result = any(b.get("type") == "tool_result" for b in blocks)
    cc_blocks = [_reverse_content_block(b) for b in blocks]

    if has_tool_result:
        return cc_blocks

    # Simple text — join all text blocks
    texts = [b.get("text", "") for b in cc_blocks if b.get("type") == "text"]
    return "\n".join(texts) if texts else ""


def _make_assistant_content(blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Build CC assistant message content — always a list."""
    return [_reverse_content_block(b) for b in blocks]


# ---------------------------------------------------------------------------
# Message conversion (inverse of convert_cc._convert_message)
# ---------------------------------------------------------------------------


def _reverse_message(
    msg: dict[str, Any],
    cc_session_id: str,
    cwd: str,
    git_branch: str,
    version: str,
    slug: str,
) -> dict[str, Any] | None:
    """Convert an .sfs message back to CC JSONL entry.

    Returns None for messages that should be skipped (unknown roles, sidechains).
    """
    role = msg.get("role")
    content_blocks = msg.get("content", [])
    metadata = msg.get("metadata", {})
    now = msg.get("timestamp") or datetime.now(timezone.utc).isoformat()

    # Use preserved CC metadata if available
    msg_cwd = metadata.get("cc_cwd", cwd)
    msg_git_branch = metadata.get("cc_git_branch", git_branch)

    # Check for summary blocks → CC "summary" type
    has_summary = any(b.get("type") == "summary" for b in content_blocks)
    if role == "system" and has_summary:
        summary_text = " ".join(
            b.get("text", "") for b in content_blocks if b.get("type") == "summary"
        )
        leaf_uuid = msg.get("parent_msg_id")
        return {
            "type": "summary",
            "summary": summary_text,
            "leafUuid": leaf_uuid,
        }

    if role == "user":
        content = _make_user_content(content_blocks)
        entry: dict[str, Any] = {
            "parentUuid": msg.get("parent_msg_id"),
            "isSidechain": False,
            "userType": "external",
            "cwd": msg_cwd,
            "sessionId": cc_session_id,
            "version": version,
            "gitBranch": msg_git_branch,
            "type": "user",
            "message": {
                "role": "user",
                "content": content,
            },
            "uuid": msg.get("msg_id", str(uuid_mod.uuid4())),
            "timestamp": now,
        }
        if slug:
            entry["slug"] = slug
        if msg.get("is_meta"):
            entry["isMeta"] = True
        return entry

    elif role == "tool":
        # Tool result messages are CC "user" messages with tool_result content
        content = _make_user_content(content_blocks)
        entry = {
            "parentUuid": msg.get("parent_msg_id"),
            "isSidechain": False,
            "userType": "external",
            "cwd": msg_cwd,
            "sessionId": cc_session_id,
            "version": version,
            "gitBranch": msg_git_branch,
            "type": "user",
            "message": {
                "role": "user",
                "content": content,
            },
            "uuid": msg.get("msg_id", str(uuid_mod.uuid4())),
            "timestamp": now,
        }
        if slug:
            entry["slug"] = slug
        return entry

    elif role == "developer":
        content = _make_user_content(content_blocks)
        entry = {
            "parentUuid": msg.get("parent_msg_id"),
            "isSidechain": False,
            "userType": "external",
            "cwd": msg_cwd,
            "sessionId": cc_session_id,
            "version": version,
            "gitBranch": msg_git_branch,
            "type": "user",
            "message": {
                "role": "user",
                "content": content,
            },
            "uuid": msg.get("msg_id", str(uuid_mod.uuid4())),
            "timestamp": now,
            "isMeta": True,
        }
        if slug:
            entry["slug"] = slug
        return entry

    elif role == "assistant":
        content = _make_assistant_content(content_blocks)
        model = msg.get("model")
        stop_reason = msg.get("stop_reason", "end_turn")

        message: dict[str, Any] = {
            "role": "assistant",
            "content": content,
            "stop_reason": stop_reason,
            "stop_sequence": None,
        }
        if model:
            message["model"] = model
            message["id"] = f"msg_{uuid_mod.uuid4().hex[:24]}"
            message["type"] = "message"

        # Map usage back to CC format
        usage = msg.get("usage")
        if usage:
            message["usage"] = {
                "input_tokens": usage.get("input_tokens", 0),
                "output_tokens": usage.get("output_tokens", 0),
                "cache_creation_input_tokens": usage.get("cache_write_tokens", 0),
                "cache_read_input_tokens": usage.get("cache_read_tokens", 0),
            }

        entry = {
            "parentUuid": msg.get("parent_msg_id"),
            "isSidechain": False,
            "userType": "external",
            "cwd": msg_cwd,
            "sessionId": cc_session_id,
            "version": version,
            "gitBranch": msg_git_branch,
            "type": "assistant",
            "message": message,
            "uuid": msg.get("msg_id", str(uuid_mod.uuid4())),
            "timestamp": now,
        }
        if slug:
            entry["slug"] = slug

        request_id = metadata.get("cc_request_id")
        if request_id:
            entry["requestId"] = request_id

        return entry

    # Unknown role — skip
    return None


# ---------------------------------------------------------------------------
# Session-level conversion
# ---------------------------------------------------------------------------


def reverse_convert_session(
    session_dir: Path,
    manifest: dict[str, Any] | None = None,
    target_project_path: str | None = None,
    output_path: Path | None = None,
    cc_home: Path | None = None,
) -> dict[str, Any]:
    """Convert an .sfs session back to CC JSONL.

    Args:
        session_dir: Path to the .sfs session directory.
        manifest: Pre-loaded manifest. If None, reads from session_dir.
        target_project_path: For resume mode — writes to CC storage.
        output_path: For export mode — writes JSONL to this path.
        cc_home: Override CC home directory (default ~/.claude).

    Returns:
        Dict with cc_session_id, jsonl_path, message_count, and
        project_dir (resume mode only).
    """
    if manifest is None:
        manifest = json.loads((session_dir / "manifest.json").read_text())

    # Read messages
    messages_path = session_dir / "messages.jsonl"
    messages: list[dict[str, Any]] = []
    if messages_path.exists():
        with open(messages_path) as f:
            for line in f:
                line = line.strip()
                if line:
                    messages.append(json.loads(line))

    # Filter out sidechain messages
    all_main_messages = [m for m in messages if not m.get("is_sidechain")]

    # For cross-tool resume: limit to last 50 messages to avoid context overflow.
    # Claude Code compresses old messages — with 1000+ turns, the AI loses context.
    # The handoff summary (appended later) provides the full session context.
    MAX_RESUME_MESSAGES = 50
    if len(all_main_messages) > MAX_RESUME_MESSAGES:
        _writeback_logger.info(
            "Trimming %d messages to last %d for cross-tool resume",
            len(all_main_messages), MAX_RESUME_MESSAGES,
        )
    main_messages = all_main_messages[-MAX_RESUME_MESSAGES:]

    # Read workspace for project path context
    workspace_path = session_dir / "workspace.json"
    workspace: dict[str, Any] = {}
    if workspace_path.exists():
        workspace = json.loads(workspace_path.read_text())

    project_path = target_project_path or workspace.get("root_path", "")
    git_branch = ""
    git_info = workspace.get("git", {})
    if git_info:
        git_branch = git_info.get("branch", "")

    # Generate new CC session ID to avoid collisions
    cc_session_id = str(uuid_mod.uuid4())
    slug = ""
    version = "2.1.59"

    # Try to recover version from message metadata
    for msg in main_messages:
        meta = msg.get("metadata", {})
        if meta:
            break

    # Extract first user prompt for index entry
    first_prompt = ""
    for msg in all_main_messages:
        if msg.get("role") == "user":
            for block in msg.get("content", []):
                if block.get("type") == "text" and block.get("text"):
                    first_prompt = block["text"][:200]
                    break
            if first_prompt:
                break

    # Convert messages
    cc_lines: list[dict[str, Any]] = []
    for msg in main_messages:
        cc_entry = _reverse_message(
            msg,
            cc_session_id=cc_session_id,
            cwd=project_path,
            git_branch=git_branch,
            version=version,
            slug=slug,
        )
        if cc_entry is not None:
            cc_lines.append(cc_entry)

    # Add a context handoff message at the end so Claude has context after compaction.
    # With 1000+ messages, Claude compacts old turns — this summary survives.
    source_tool = manifest.get("source", {}).get("tool", "another tool")
    stats = manifest.get("stats", {})
    msg_count = stats.get("message_count", len(all_main_messages))
    tool_count = stats.get("tool_use_count", 0)
    title = manifest.get("title", "Untitled session")

    # Build a concise context summary from the last few assistant messages
    last_assistant_texts: list[str] = []
    for msg in reversed(main_messages):
        if msg.get("role") == "assistant":
            for block in msg.get("content", []):
                if block.get("type") == "text" and block.get("text"):
                    last_assistant_texts.append(block["text"][:300])
                    break
            if len(last_assistant_texts) >= 3:
                break

    trimmed = len(all_main_messages) > MAX_RESUME_MESSAGES
    context_lines = [
        f"[SessionFS Resume] This session was transferred from {source_tool}.",
        f"Original session: {manifest.get('session_id', 'unknown')}",
        f"Title: {title}",
        f"Full session: {msg_count} messages, {tool_count} tool calls.",
    ]
    if trimmed:
        context_lines.append(
            f"Note: Only the last {MAX_RESUME_MESSAGES} messages were included. "
            f"The earlier {len(all_main_messages) - MAX_RESUME_MESSAGES} messages were trimmed to fit context."
        )
    context_lines.extend([
        "",
        "Last activity in the prior session:",
    ])
    for txt in reversed(last_assistant_texts):
        context_lines.append(f"  - {txt[:200]}")
    context_lines.append("")
    context_lines.append("Continue from where you left off.")

    last_uuid = cc_lines[-1]["uuid"] if cc_lines else str(uuid_mod.uuid4())
    handoff_uuid = str(uuid_mod.uuid4())
    now_ts = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    handoff_entry: dict[str, Any] = {
        "parentUuid": last_uuid,
        "isSidechain": False,
        "userType": "external",
        "cwd": project_path,
        "sessionId": cc_session_id,
        "version": version,
        "gitBranch": git_branch,
        "type": "user",
        "message": {
            "role": "user",
            "content": "\n".join(context_lines),
        },
        "uuid": handoff_uuid,
        "timestamp": now_ts,
    }
    cc_lines.append(handoff_entry)

    # Determine output path
    if target_project_path:
        # Resume mode — write to CC storage
        project_dir = get_cc_project_dir(target_project_path, cc_home)
        project_dir.mkdir(parents=True, exist_ok=True)
        jsonl_path = project_dir / f"{cc_session_id}.jsonl"
    elif output_path:
        # Export mode — write to specified path
        jsonl_path = output_path
        project_dir = None
    else:
        raise ValueError("Either target_project_path or output_path must be provided.")

    # Write JSONL
    with open(jsonl_path, "w") as f:
        for entry in cc_lines:
            f.write(json.dumps(entry, separators=(",", ":")) + "\n")

    # Update sessions index (resume mode only)
    if target_project_path and project_dir:
        add_index_entry(
            project_dir,
            session_id=cc_session_id,
            project_path=target_project_path,
            first_prompt=first_prompt or "[Resumed from SessionFS]",
            message_count=len(cc_lines),
            git_branch=git_branch,
        )

    result: dict[str, Any] = {
        "cc_session_id": cc_session_id,
        "jsonl_path": str(jsonl_path),
        "message_count": len(cc_lines),
        "sfs_session_id": manifest.get("session_id"),
    }
    if project_dir:
        result["project_dir"] = str(project_dir)

    return result
