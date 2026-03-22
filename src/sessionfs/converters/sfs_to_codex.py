""".sfs -> Codex CLI converter.

Converts a canonical .sfs session directory into a Codex CLI native JSONL
rollout file. Handles role mapping, content block conversion, tree flattening,
and the tagged-union structure that Codex expects.

Codex JSONL format:
  Each line is {"timestamp": "...", "type": "<type>", "payload": {...}}
  Types: session_meta, response_item, event_msg, turn_context

Key differences from CC:
  - Linear turns (no tree), delimited by task_started / task_complete events
  - Developer role (not system) for injected context
  - Content uses input_text/output_text (not text)
  - Tool calls use function_call / function_call_output / local_shell_call
  - Reasoning blocks for thinking
"""

from __future__ import annotations

import json
import logging
import uuid as uuid_mod
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger("sessionfs.converters.sfs_to_codex")


def convert_sfs_to_codex(
    sfs_dir: Path,
    output_path: Path | None = None,
    cwd: str | None = None,
) -> dict[str, Any]:
    """Convert a canonical .sfs session to Codex CLI native format.

    Args:
        sfs_dir: Path to the .sfs session directory.
        output_path: Where to write the Codex JSONL file. If None, generates
            a path in /tmp.
        cwd: Working directory override. If None, reads from workspace.json.

    Returns:
        Dict with keys: codex_session_id, jsonl_path, message_count
    """
    manifest = json.loads((sfs_dir / "manifest.json").read_text())
    messages = _read_messages(sfs_dir / "messages.jsonl")

    # Read workspace for cwd
    workspace_path = sfs_dir / "workspace.json"
    workspace: dict[str, Any] = {}
    if workspace_path.exists():
        workspace = json.loads(workspace_path.read_text())

    effective_cwd = cwd or workspace.get("root_path") or "/tmp"

    # Generate Codex session ID (UUIDv7-like, but UUID4 is fine for injection)
    codex_session_id = str(uuid_mod.uuid4())

    # Determine output path
    if output_path is None:
        now = datetime.now(timezone.utc)
        date_dir = now.strftime("%Y/%m/%d")
        ts_str = now.strftime("%Y-%m-%dT%H-%M-%S")
        output_path = Path(f"/tmp/rollout-{ts_str}-{codex_session_id}.jsonl")

    output_path.parent.mkdir(parents=True, exist_ok=True)

    # Build the Codex JSONL lines
    lines: list[dict[str, Any]] = []
    source = manifest.get("source", {})
    model_info = manifest.get("model") or {}
    git_info = workspace.get("git") or {}
    created_at = manifest.get("created_at", datetime.now(timezone.utc).isoformat())

    # 1. session_meta (first line)
    lines.append({
        "timestamp": created_at,
        "type": "session_meta",
        "payload": {
            "id": codex_session_id,
            "timestamp": created_at,
            "cwd": effective_cwd,
            "originator": "sessionfs_import",
            "cli_version": "0.116.0",
            "source": "custom",
            "model_provider": model_info.get("provider", "openai"),
            "base_instructions": None,
            "git": {
                "commit_hash": git_info.get("commit_sha"),
                "branch": git_info.get("branch"),
                "repository_url": git_info.get("remote_url"),
            } if git_info else None,
            "forked_from_id": None,
            "agent_nickname": None,
            "agent_role": None,
            "memory_mode": None,
            "dynamic_tools": None,
        },
    })

    # 2. Convert messages into turns
    # Flatten tree structure: just take messages in order, skip sidechains
    main_messages = [m for m in messages if not m.get("is_sidechain")]

    turn_id: str | None = None
    turn_count = 0
    msg_count = 0

    for msg in main_messages:
        role = msg.get("role", "user")
        content = msg.get("content", [])
        ts = msg.get("timestamp", created_at)
        model = msg.get("model")

        if role == "user":
            # Start a new turn
            if turn_id is not None:
                # Close previous turn
                lines.append(_event("task_complete", ts, turn_id=turn_id))

            turn_count += 1
            turn_id = str(uuid_mod.uuid4())

            # turn_context
            lines.append({
                "timestamp": ts,
                "type": "turn_context",
                "payload": {
                    "turn_id": turn_id,
                    "cwd": effective_cwd,
                    "current_date": ts[:10] if ts else None,
                    "timezone": "UTC",
                    "approval_policy": "never",
                    "sandbox_policy": {"type": "read-only"},
                    "model": model or model_info.get("model_id", "gpt-4.1"),
                    "personality": "pragmatic",
                    "collaboration_mode": {"mode": "default", "settings": {}},
                    "realtime_active": False,
                    "summary": "auto",
                    "truncation_policy": {"mode": "bytes", "limit": 10000},
                },
            })

            # task_started event
            lines.append(_event("task_started", ts, turn_id=turn_id))

            # user_message event
            user_text = _extract_text_content(content)
            lines.append(_event("user_message", ts, message=user_text, images=[]))

            # user response_item message
            lines.append(_response_message(
                ts, "user", [{"type": "input_text", "text": user_text}],
            ))
            msg_count += 1

        elif role == "assistant":
            if turn_id is None:
                turn_id = str(uuid_mod.uuid4())
                lines.append(_event("task_started", ts, turn_id=turn_id))

            # Convert content blocks
            for block in (content if isinstance(content, list) else []):
                if not isinstance(block, dict):
                    continue
                btype = block.get("type", "")

                if btype == "text":
                    lines.append(_response_message(
                        ts, "assistant",
                        [{"type": "output_text", "text": block.get("text", "")}],
                        end_turn=True, phase="final_answer",
                    ))
                    msg_count += 1

                elif btype == "thinking":
                    lines.append({
                        "timestamp": ts,
                        "type": "response_item",
                        "payload": {
                            "type": "reasoning",
                            "id": f"rs_{uuid_mod.uuid4().hex[:24]}",
                            "summary": [{"type": "summary_text", "text": (block.get("text", "") or "")[:200]}],
                            "content": [{"type": "text", "text": block.get("text", "")}] if block.get("text") else [],
                            "encrypted_content": None,
                        },
                    })

                elif btype == "tool_use":
                    tool_name = block.get("name", "unknown")
                    tool_input = block.get("input", {})
                    call_id = f"call_{uuid_mod.uuid4().hex[:24]}"

                    # Shell tools → local_shell_call, others → function_call
                    if tool_name in ("Bash", "bash", "shell", "execute_command"):
                        cmd = tool_input.get("command", "") if isinstance(tool_input, dict) else str(tool_input)
                        lines.append({
                            "timestamp": ts,
                            "type": "response_item",
                            "payload": {
                                "type": "local_shell_call",
                                "id": f"fc_{uuid_mod.uuid4().hex[:24]}",
                                "call_id": call_id,
                                "status": "completed",
                                "action": {
                                    "type": "exec",
                                    "command": ["bash", "-c", cmd],
                                    "timeout_ms": 30000,
                                    "working_directory": effective_cwd,
                                    "env": {},
                                    "user": None,
                                },
                            },
                        })
                    else:
                        args = json.dumps(tool_input) if isinstance(tool_input, dict) else str(tool_input)
                        lines.append({
                            "timestamp": ts,
                            "type": "response_item",
                            "payload": {
                                "type": "function_call",
                                "id": f"fc_{uuid_mod.uuid4().hex[:24]}",
                                "name": tool_name,
                                "namespace": None,
                                "arguments": args,
                                "call_id": call_id,
                            },
                        })

                elif btype == "tool_result":
                    output_text = block.get("content", "") or block.get("output", "")
                    if isinstance(output_text, list):
                        output_text = "\n".join(
                            b.get("text", "") for b in output_text if isinstance(b, dict)
                        )
                    lines.append({
                        "timestamp": ts,
                        "type": "response_item",
                        "payload": {
                            "type": "function_call_output",
                            "call_id": block.get("tool_use_id", f"call_{uuid_mod.uuid4().hex[:24]}"),
                            "output": {
                                "text": str(output_text),
                                "metadata": None,
                            },
                        },
                    })

                elif btype == "image":
                    logger.info("Dropping image block (not supported in Codex import)")

                elif btype == "summary":
                    # Map to agent_message event
                    lines.append(_event(
                        "agent_message", ts,
                        message=block.get("text", ""),
                        phase="commentary",
                    ))

        elif role in ("system", "developer"):
            # Map to developer response_item message
            dev_text = _extract_text_content(content)
            if dev_text:
                lines.append(_response_message(
                    ts, "developer",
                    [{"type": "input_text", "text": dev_text}],
                ))
                msg_count += 1

        elif role == "tool":
            # Standalone tool results
            for block in (content if isinstance(content, list) else []):
                if not isinstance(block, dict):
                    continue
                btype = block.get("type", "")
                if btype == "tool_result":
                    output_text = block.get("content", "") or block.get("output", "")
                    if isinstance(output_text, list):
                        output_text = "\n".join(
                            b.get("text", "") for b in output_text if isinstance(b, dict)
                        )
                    lines.append({
                        "timestamp": ts,
                        "type": "response_item",
                        "payload": {
                            "type": "function_call_output",
                            "call_id": block.get("tool_use_id", f"call_{uuid_mod.uuid4().hex[:24]}"),
                            "output": {"text": str(output_text), "metadata": None},
                        },
                    })
                elif btype == "text":
                    text = block.get("text", "")
                    if text:
                        lines.append({
                            "timestamp": ts,
                            "type": "response_item",
                            "payload": {
                                "type": "function_call_output",
                                "call_id": f"call_{uuid_mod.uuid4().hex[:24]}",
                                "output": {"text": text, "metadata": None},
                            },
                        })

    # Close the last turn
    if turn_id is not None:
        last_ts = main_messages[-1].get("timestamp", created_at) if main_messages else created_at
        lines.append(_event("task_complete", last_ts, turn_id=turn_id))

    # Write output
    with open(output_path, "w") as f:
        for line in lines:
            f.write(json.dumps(line, separators=(",", ":")) + "\n")

    return {
        "codex_session_id": codex_session_id,
        "jsonl_path": str(output_path),
        "message_count": msg_count,
        "turn_count": turn_count,
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


def _response_message(
    ts: str,
    role: str,
    content: list[dict],
    end_turn: bool = False,
    phase: str = "final_answer",
) -> dict[str, Any]:
    return {
        "timestamp": ts,
        "type": "response_item",
        "payload": {
            "type": "message",
            "id": f"msg_{uuid_mod.uuid4().hex[:24]}",
            "role": role,
            "content": content,
            "end_turn": end_turn,
            "phase": phase,
        },
    }


def _event(event_type: str, ts: str, **payload_fields: Any) -> dict[str, Any]:
    return {
        "timestamp": ts,
        "type": "event_msg",
        "payload": {"type": event_type, **payload_fields},
    }
