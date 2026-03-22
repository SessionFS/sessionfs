""".sfs -> Gemini CLI converter.

Converts a canonical .sfs session to Gemini CLI native format (single JSON file).

Gemini sessions are simple: flat message array, user content as [{"text": "..."}],
model content as plain string. No structured tool calls — tool interactions are
part of the assistant text.
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid as uuid_mod
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger("sessionfs.converters.sfs_to_gemini")


def convert_sfs_to_gemini(
    sfs_dir: Path,
    output_path: Path | None = None,
    project_path: str | None = None,
) -> dict[str, Any]:
    """Convert a canonical .sfs session to Gemini CLI native format.

    Args:
        sfs_dir: Path to the .sfs session directory.
        output_path: Where to write the Gemini JSON file.
        project_path: Project path for the session.

    Returns:
        Dict with: gemini_session_id, json_path, message_count
    """
    manifest = json.loads((sfs_dir / "manifest.json").read_text())
    messages = _read_messages(sfs_dir / "messages.jsonl")

    workspace_path = sfs_dir / "workspace.json"
    workspace: dict[str, Any] = {}
    if workspace_path.exists():
        workspace = json.loads(workspace_path.read_text())

    effective_project = project_path or workspace.get("root_path") or "/tmp"

    gemini_session_id = str(uuid_mod.uuid4())
    project_hash = hashlib.sha256(effective_project.encode()).hexdigest()
    created_at = manifest.get("created_at", datetime.now(timezone.utc).isoformat())
    updated_at = manifest.get("updated_at", created_at)

    if output_path is None:
        ts = created_at[:19].replace(":", "-")
        output_path = Path(f"/tmp/session-{ts}-{gemini_session_id[:8]}.json")
    output_path.parent.mkdir(parents=True, exist_ok=True)

    # Convert messages
    gemini_messages: list[dict[str, Any]] = []
    main_messages = [m for m in messages if not m.get("is_sidechain")]
    msg_count = 0

    for msg in main_messages:
        role = msg.get("role", "user")
        content = msg.get("content", [])
        timestamp = msg.get("timestamp", created_at)
        msg_id = str(uuid_mod.uuid4())

        if role == "user":
            # Convert .sfs content blocks to Gemini user parts
            parts = []
            for block in (content if isinstance(content, list) else []):
                if not isinstance(block, dict):
                    continue
                btype = block.get("type", "")
                if btype == "text":
                    parts.append({"text": block.get("text", "")})
                elif btype == "image":
                    source = block.get("source", {})
                    if source.get("type") == "base64":
                        parts.append({
                            "inlineData": {
                                "mimeType": source.get("media_type", "image/png"),
                                "data": source.get("data", ""),
                            }
                        })

            if parts:
                gemini_messages.append({
                    "id": msg_id,
                    "timestamp": timestamp,
                    "type": "user",
                    "content": parts,
                })
                msg_count += 1

        elif role == "assistant":
            # Combine all content blocks into a single text string
            text_parts = []
            for block in (content if isinstance(content, list) else []):
                if not isinstance(block, dict):
                    continue
                btype = block.get("type", "")
                if btype == "text":
                    text_parts.append(block.get("text", ""))
                elif btype == "thinking":
                    # Gemini doesn't have thinking blocks — skip
                    pass
                elif btype == "tool_use":
                    name = block.get("name", "tool")
                    inp = block.get("input", {})
                    if name in ("Bash", "bash", "shell"):
                        cmd = inp.get("command", "") if isinstance(inp, dict) else str(inp)
                        text_parts.append(f"I'll run: `{cmd}`")
                    else:
                        text_parts.append(f"I'll use {name}.")
                elif btype == "tool_result":
                    result = block.get("content", "")
                    if isinstance(result, list):
                        result = "\n".join(b.get("text", "") for b in result if isinstance(b, dict))
                    if result:
                        text_parts.append(f"Result:\n```\n{result}\n```")

            text = "\n\n".join(text_parts)
            if text:
                gemini_messages.append({
                    "id": msg_id,
                    "timestamp": timestamp,
                    "type": "gemini",
                    "content": text,
                })
                msg_count += 1

        elif role in ("system", "developer"):
            # Map to info type
            text = _extract_text(content)
            if text:
                gemini_messages.append({
                    "id": msg_id,
                    "timestamp": timestamp,
                    "type": "info",
                    "content": text,
                })

        elif role == "tool":
            # Tool results become part of the conversation as info
            text = _extract_text(content)
            if text:
                gemini_messages.append({
                    "id": msg_id,
                    "timestamp": timestamp,
                    "type": "info",
                    "content": f"Tool output:\n{text}",
                })

    # Build session JSON
    session_data = {
        "sessionId": gemini_session_id,
        "projectHash": project_hash,
        "startTime": created_at,
        "lastUpdated": updated_at,
        "messages": gemini_messages,
    }

    # Add summary from manifest title
    title = manifest.get("title")
    if title:
        session_data["summary"] = title

    output_path.write_text(json.dumps(session_data, indent=2))

    return {
        "gemini_session_id": gemini_session_id,
        "json_path": str(output_path),
        "message_count": msg_count,
        "project_hash": project_hash,
    }


def _read_messages(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    messages = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                messages.append(json.loads(line))
    return messages


def _extract_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts = []
    for block in content:
        if isinstance(block, str):
            parts.append(block)
        elif isinstance(block, dict):
            if block.get("type") in ("text", "tool_result"):
                parts.append(block.get("text", "") or block.get("content", ""))
    return "\n".join(parts)
