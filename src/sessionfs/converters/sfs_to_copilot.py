""".sfs -> Copilot CLI converter.

Converts a canonical .sfs session directory into a Copilot CLI native
events.jsonl format and workspace.yaml.

Copilot CLI events.jsonl format:
  Each line is {"type": "event.type", "data": {...}, "id": "uuid",
                "timestamp": "ISO-8601", "parentId": "uuid|null"}

Event types:
  - user.message → data.content has user text
  - assistant.message → assistant response
  - tool.execution_start → tool invocation
  - tool.execution_result → tool output
"""

from __future__ import annotations

import json
import logging
import uuid as uuid_mod
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger("sessionfs.converters.sfs_to_copilot")


def convert_sfs_to_copilot(
    sfs_dir: Path,
    output_path: Path | None = None,
    cwd: str | None = None,
) -> dict[str, Any]:
    """Convert a canonical .sfs session to Copilot CLI native format.

    Args:
        sfs_dir: Path to the .sfs session directory.
        output_path: Where to write the events.jsonl file. If None, generates
            a path in /tmp.
        cwd: Working directory override. If None, reads from workspace.json.

    Returns:
        Dict with keys: copilot_session_id, events_path, workspace_yaml_path,
                         message_count
    """
    manifest = json.loads((sfs_dir / "manifest.json").read_text())
    messages = _read_messages(sfs_dir / "messages.jsonl")

    # Read workspace for cwd
    workspace_path = sfs_dir / "workspace.json"
    workspace: dict[str, Any] = {}
    if workspace_path.exists():
        workspace = json.loads(workspace_path.read_text())

    effective_cwd = cwd or workspace.get("root_path") or "/tmp"

    # Generate Copilot session ID
    copilot_session_id = str(uuid_mod.uuid4())

    # Determine output path
    if output_path is None:
        output_path = Path(f"/tmp/copilot-{copilot_session_id}/events.jsonl")

    output_path.parent.mkdir(parents=True, exist_ok=True)

    # Build the events.jsonl lines
    events: list[dict[str, Any]] = []
    model_info = manifest.get("model") or {}
    created_at = manifest.get("created_at", datetime.now(timezone.utc).isoformat())

    main_messages = [m for m in messages if not m.get("is_sidechain")]

    msg_count = 0
    current_user_id: str | None = None

    for msg in main_messages:
        role = msg.get("role", "user")
        content = msg.get("content", [])
        ts = msg.get("timestamp", created_at)
        model = msg.get("model")

        if role == "user":
            user_text = _extract_text_content(content)
            event_id = str(uuid_mod.uuid4())
            current_user_id = event_id

            events.append({
                "type": "user.message",
                "data": {"content": user_text},
                "id": event_id,
                "timestamp": ts,
                "parentId": None,
            })
            msg_count += 1

        elif role == "assistant":
            for block in (content if isinstance(content, list) else []):
                if not isinstance(block, dict):
                    continue
                btype = block.get("type", "")

                if btype == "text":
                    event_id = str(uuid_mod.uuid4())
                    events.append({
                        "type": "assistant.message",
                        "data": {
                            "content": block.get("text", ""),
                            "model": model or model_info.get("model_id"),
                        },
                        "id": event_id,
                        "timestamp": ts,
                        "parentId": current_user_id,
                    })
                    msg_count += 1

                elif btype == "tool_use":
                    tool_name = block.get("name", "unknown")
                    tool_input = block.get("input", {})
                    event_id = str(uuid_mod.uuid4())

                    events.append({
                        "type": "tool.execution_start",
                        "data": {
                            "tool": tool_name,
                            "input": tool_input if isinstance(tool_input, dict) else {"command": str(tool_input)},
                        },
                        "id": event_id,
                        "timestamp": ts,
                        "parentId": current_user_id,
                    })

                elif btype == "thinking":
                    # Copilot CLI doesn't have a native thinking type;
                    # emit as assistant.message with a prefix
                    event_id = str(uuid_mod.uuid4())
                    thinking_text = block.get("text", "")
                    if thinking_text:
                        events.append({
                            "type": "assistant.message",
                            "data": {
                                "content": thinking_text,
                                "model": model or model_info.get("model_id"),
                            },
                            "id": event_id,
                            "timestamp": ts,
                            "parentId": current_user_id,
                        })

        elif role == "tool":
            for block in (content if isinstance(content, list) else []):
                if not isinstance(block, dict):
                    continue
                btype = block.get("type", "")

                if btype == "tool_result":
                    output = block.get("content", "") or block.get("output", "")
                    if isinstance(output, list):
                        output = "\n".join(
                            b.get("text", "") for b in output if isinstance(b, dict)
                        )
                    parent = block.get("tool_use_id") or current_user_id
                    event_id = str(uuid_mod.uuid4())

                    events.append({
                        "type": "tool.execution_result",
                        "data": {"output": str(output)},
                        "id": event_id,
                        "timestamp": ts,
                        "parentId": parent,
                    })

        elif role in ("system", "developer"):
            # Map system/developer to user.message (context injection)
            dev_text = _extract_text_content(content)
            if dev_text:
                event_id = str(uuid_mod.uuid4())
                events.append({
                    "type": "user.message",
                    "data": {"content": dev_text},
                    "id": event_id,
                    "timestamp": ts,
                    "parentId": None,
                })
                msg_count += 1

    # Write events.jsonl
    with open(output_path, "w") as f:
        for event in events:
            f.write(json.dumps(event, separators=(",", ":")) + "\n")

    # Write workspace.yaml
    workspace_yaml_path = output_path.parent / "workspace.yaml"
    _write_workspace_yaml(workspace_yaml_path, effective_cwd, model_info)

    return {
        "copilot_session_id": copilot_session_id,
        "events_path": str(output_path),
        "workspace_yaml_path": str(workspace_yaml_path),
        "message_count": msg_count,
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


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


def _extract_text_content(content: Any) -> str:
    """Extract plain text from .sfs content blocks."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts = []
    for block in content:
        if isinstance(block, str):
            parts.append(block)
        elif isinstance(block, dict):
            if block.get("type") in ("text", "input_text", "output_text"):
                parts.append(block.get("text", ""))
    return "\n".join(parts)


def _write_workspace_yaml(path: Path, cwd: str, model_info: dict) -> None:
    """Write a workspace.yaml file for the Copilot session."""
    lines = [
        f"cwd: {cwd}",
    ]
    model_id = model_info.get("model_id")
    if model_id:
        lines.append(f"model: {model_id}")
    provider = model_info.get("provider")
    if provider:
        lines.append(f"model_provider: {provider}")

    path.write_text("\n".join(lines) + "\n")
