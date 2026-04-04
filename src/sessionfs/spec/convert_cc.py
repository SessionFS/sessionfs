"""Claude Code -> .sfs Converter

Converts Claude Code native session files into the canonical .sfs format.
Uses the Claude Code parser from sessionfs.watchers.claude_code.

Usage:
    python -m sessionfs.spec.convert_cc <session-uuid> --output /path/to/output/
    python -m sessionfs.spec.convert_cc --list
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import subprocess
import sys
import uuid as uuid_mod
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sessionfs.watchers.claude_code import (
    ContentBlock as CCContentBlock,
    Message as CCMessage,
    ParsedSession,
    discover_sessions,
    find_session_path,
    parse_session,
)

# .sfs format version — independent of package version. Only bump when spec changes.
from sessionfs.spec.version import SFS_FORMAT_VERSION as SFS_VERSION
from sessionfs.spec.version import SFS_CONVERTER_VERSION as CONVERTER_VERSION

# Default home dir for CLI usage
_DEFAULT_HOME = Path.home() / ".claude"


def _smart_title(raw_title: str | None, sfs_messages: list[dict[str, Any]]) -> str | None:
    """Apply smart title extraction during conversion.

    Returns a clean title or None if no usable title found.
    """
    from sessionfs.utils.title_utils import extract_smart_title

    result = extract_smart_title(
        messages=sfs_messages or None,
        raw_title=raw_title[:100] if raw_title else None,
        message_count=len(sfs_messages),
    )
    if result.startswith("Untitled session"):
        return None  # Let the server/CLI handle the fallback
    return result


# ---------------------------------------------------------------------------
# Content block conversion
# ---------------------------------------------------------------------------


def _convert_content_block(block: CCContentBlock) -> dict[str, Any]:
    """Convert a CC ContentBlock to an .sfs content block."""
    btype = block.block_type

    if btype == "text":
        return {"type": "text", "text": block.text or ""}

    elif btype == "thinking":
        result: dict[str, Any] = {
            "type": "thinking",
            "text": block.thinking or "",
        }
        if block.signature:
            result["signature"] = block.signature
        return result

    elif btype == "tool_use":
        return {
            "type": "tool_use",
            "tool_use_id": block.tool_use_id or "",
            "name": block.tool_name or "",
            "input": block.tool_input or {},
        }

    elif btype == "tool_result":
        return {
            "type": "tool_result",
            "tool_use_id": block.tool_use_id or "",
            "content": block.tool_result_content or "",
        }

    elif btype == "image":
        return {
            "type": "image",
            "source": {
                "type": "url",
                "data": block.text or "",
            },
        }

    else:
        result = {"type": btype}
        if block.text is not None:
            result["text"] = block.text
        return result


# ---------------------------------------------------------------------------
# Message conversion
# ---------------------------------------------------------------------------


def _convert_message(msg: CCMessage) -> dict[str, Any]:
    """Convert a CC Message to an .sfs message entry."""
    role_map = {
        "user": "user",
        "assistant": "assistant",
        "summary": "system",
    }
    role = role_map.get(msg.role, msg.role)

    content: list[dict[str, Any]] = []

    if role == "system" and msg.role == "summary":
        summary_text = ""
        for block in msg.content_blocks:
            if block.text:
                summary_text += block.text
        content = [{"type": "summary", "text": summary_text}]
    else:
        has_tool_result = any(b.block_type == "tool_result" for b in msg.content_blocks)
        if has_tool_result and role == "user":
            role = "tool"

        for block in msg.content_blocks:
            content.append(_convert_content_block(block))

    entry: dict[str, Any] = {
        "msg_id": msg.uuid or str(uuid_mod.uuid4()),
        "parent_msg_id": msg.parent_uuid,
        "role": role,
        "content": content,
        "timestamp": msg.timestamp or datetime.now(timezone.utc).isoformat(),
    }

    if msg.model:
        entry["model"] = msg.model
        if "claude" in msg.model.lower():
            entry["provider"] = "anthropic"
        elif any(x in msg.model.lower() for x in ["gpt", "o1", "o3", "o4"]):
            entry["provider"] = "openai"

    if msg.stop_reason:
        entry["stop_reason"] = msg.stop_reason

    if msg.usage:
        entry["usage"] = {
            "input_tokens": msg.usage.get("input_tokens", 0),
            "output_tokens": msg.usage.get("output_tokens", 0),
            "cache_read_tokens": msg.usage.get("cache_read_input_tokens", 0),
            "cache_write_tokens": msg.usage.get("cache_creation_input_tokens", 0),
            "reasoning_tokens": 0,
        }

    if msg.is_sidechain:
        entry["is_sidechain"] = True

    if msg.is_meta:
        entry["is_meta"] = True

    metadata: dict[str, Any] = {}
    if msg.request_id:
        metadata["cc_request_id"] = msg.request_id
    if msg.cwd:
        metadata["cc_cwd"] = msg.cwd
    if msg.git_branch:
        metadata["cc_git_branch"] = msg.git_branch
    if metadata:
        entry["metadata"] = metadata

    return entry


# ---------------------------------------------------------------------------
# Context capture
# ---------------------------------------------------------------------------


def capture_git_context(project_path: str | None) -> dict[str, Any] | None:
    """Capture git context from a project directory."""
    if not project_path:
        return None

    project_dir = Path(project_path)
    if not project_dir.is_dir():
        return None

    git_dir = project_dir / ".git"
    if not git_dir.exists():
        return None

    context: dict[str, Any] = {}

    try:
        result = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            capture_output=True, text=True, cwd=project_dir, timeout=5,
        )
        if result.returncode == 0:
            context["remote_url"] = result.stdout.strip()

        result = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True, text=True, cwd=project_dir, timeout=5,
        )
        if result.returncode == 0:
            context["branch"] = result.stdout.strip()

        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True, text=True, cwd=project_dir, timeout=5,
        )
        if result.returncode == 0:
            context["commit_sha"] = result.stdout.strip()

        result = subprocess.run(
            ["git", "log", "-1", "--format=%s"],
            capture_output=True, text=True, cwd=project_dir, timeout=5,
        )
        if result.returncode == 0:
            context["commit_message"] = result.stdout.strip()

        result = subprocess.run(
            ["git", "status", "--porcelain"],
            capture_output=True, text=True, cwd=project_dir, timeout=5,
        )
        if result.returncode == 0:
            context["dirty"] = bool(result.stdout.strip())

    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass

    return context if context else None


def capture_environment() -> dict[str, Any]:
    """Capture runtime environment info."""
    env: dict[str, Any] = {
        "os": platform.system().lower(),
        "os_version": platform.platform(),
        "shell": os.environ.get("SHELL", "").split("/")[-1] or None,
        "languages": {},
    }

    try:
        result = subprocess.run(
            ["python3", "--version"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            env["languages"]["python"] = result.stdout.strip().split()[-1]
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass

    try:
        result = subprocess.run(
            ["node", "--version"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            env["languages"]["node"] = result.stdout.strip().lstrip("v")
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass

    return env


# ---------------------------------------------------------------------------
# File reference extraction
# ---------------------------------------------------------------------------


def extract_file_refs(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Extract file references from tool_use blocks."""
    file_roles: dict[str, str] = {}
    role_priority = {
        "deleted": 5, "created": 4, "edited": 3,
        "written": 2, "read": 1, "referenced": 0,
    }

    for msg in messages:
        for block in msg.get("content", []):
            if not isinstance(block, dict) or block.get("type") != "tool_use":
                continue

            name = block.get("name", "")
            tool_input = block.get("input", {})
            if not isinstance(tool_input, dict):
                continue

            file_path = tool_input.get("file_path") or tool_input.get("path")
            if not file_path or not isinstance(file_path, str):
                continue

            if file_path.startswith("/"):
                continue

            tool_role_map = {
                "Read": "read",
                "Write": "written",
                "Edit": "edited",
                "Glob": "referenced",
                "Grep": "referenced",
            }
            role = tool_role_map.get(name, "referenced")

            current = file_roles.get(file_path, "referenced")
            if role_priority.get(role, 0) > role_priority.get(current, 0):
                file_roles[file_path] = role

    return [
        {"path": path, "role": role}
        for path, role in sorted(file_roles.items())
    ]


# ---------------------------------------------------------------------------
# Session conversion
# ---------------------------------------------------------------------------


def convert_session(
    cc_session: ParsedSession,
    output_dir: Path,
    session_id: str | None = None,
    session_dir: Path | None = None,
) -> Path:
    """Convert a parsed Claude Code session to .sfs format.

    Args:
        cc_session: Parsed session from the CC parser.
        output_dir: Parent directory for the output .sfs session (used when
            session_dir is not provided).
        session_id: Optional session ID (ses_ prefixed). If None, generates one.
        session_dir: Optional pre-allocated output directory. When provided,
            files are written here directly instead of creating a subdirectory
            under output_dir.

    Returns:
        Path to the created .sfs session directory.
    """
    if session_id:
        sid = session_id
    else:
        from sessionfs.session_id import generate_session_id
        sid = generate_session_id()
    if session_dir is None:
        session_dir = output_dir / sid
    session_dir.mkdir(parents=True, exist_ok=True)

    # --- Convert messages ---
    sfs_messages: list[dict[str, Any]] = []
    tool_names: set[str] = set()
    total_input = 0
    total_output = 0

    for msg in cc_session.messages:
        sfs_msg = _convert_message(msg)
        sfs_messages.append(sfs_msg)

        for block in sfs_msg.get("content", []):
            if isinstance(block, dict) and block.get("type") == "tool_use":
                tool_names.add(block.get("name", ""))

        usage = sfs_msg.get("usage")
        if usage:
            total_input += usage.get("input_tokens", 0)
            total_output += usage.get("output_tokens", 0)

    # Include sub-agent messages
    for sub in cc_session.sub_agents:
        for msg in sub.messages:
            sfs_msg = _convert_message(msg)
            sfs_msg["is_sidechain"] = True
            sfs_msg["agent_id"] = sub.agent_id
            sfs_messages.append(sfs_msg)

            for block in sfs_msg.get("content", []):
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    tool_names.add(block.get("name", ""))

    # Fix dangling parent_msg_id references
    msg_ids = {m["msg_id"] for m in sfs_messages}
    for msg in sfs_messages:
        parent = msg.get("parent_msg_id")
        if parent is not None and parent not in msg_ids:
            msg["parent_msg_id"] = None

    # Write messages.jsonl
    messages_path = session_dir / "messages.jsonl"
    with open(messages_path, "w") as f:
        for msg in sfs_messages:
            f.write(json.dumps(msg, separators=(",", ":")) + "\n")

    # --- Detect model ---
    primary_model = None
    primary_provider = None
    for msg in sfs_messages:
        if msg.get("model") and msg.get("role") == "assistant" and not msg.get("is_sidechain"):
            primary_model = msg["model"]
            primary_provider = msg.get("provider")
            break

    # --- Timestamps ---
    timestamps = [m["timestamp"] for m in sfs_messages if m.get("timestamp")]
    created_at = min(timestamps) if timestamps else datetime.now(timezone.utc).isoformat()
    updated_at = max(timestamps) if timestamps else created_at

    # --- Count turns ---
    turn_count = 0
    prev_role = None
    for msg in sfs_messages:
        if not msg.get("is_sidechain"):
            if msg["role"] == "user" and prev_role != "user":
                turn_count += 1
            prev_role = msg["role"]

    # --- Tool use count ---
    tool_use_count = sum(
        1
        for msg in sfs_messages
        for block in msg.get("content", [])
        if isinstance(block, dict) and block.get("type") == "tool_use"
    )

    # --- Duration ---
    duration_ms = None
    if len(timestamps) >= 2:
        try:
            first = datetime.fromisoformat(min(timestamps).replace("Z", "+00:00"))
            last = datetime.fromisoformat(max(timestamps).replace("Z", "+00:00"))
            duration_ms = int((last - first).total_seconds() * 1000)
        except (ValueError, TypeError):
            pass

    # --- Write manifest.json ---
    manifest: dict[str, Any] = {
        "sfs_version": SFS_VERSION,
        "session_id": sid,
        "title": _smart_title(cc_session.first_prompt, sfs_messages),
        "tags": [],
        "created_at": created_at,
        "updated_at": updated_at,
        "source": {
            "tool": "claude-code",
            "tool_version": cc_session.claude_code_version,
            "sfs_converter_version": CONVERTER_VERSION,
            "original_session_id": cc_session.session_id,
            "original_path": cc_session.source_path,
            "interface": "cli",
        },
        "stats": {
            "message_count": len(sfs_messages),
            "turn_count": turn_count,
            "tool_use_count": tool_use_count,
            "total_input_tokens": total_input,
            "total_output_tokens": total_output,
            "duration_ms": duration_ms,
        },
    }

    if primary_model and primary_model not in ("<synthetic>", "synthetic", ""):
        manifest["model"] = {
            "provider": primary_provider or "anthropic",
            "model_id": primary_model,
        }

    if cc_session.sub_agents:
        manifest["sub_agents"] = [
            {
                "agent_id": sa.agent_id,
                "model": sa.model,
                "message_count": len(sa.messages),
            }
            for sa in cc_session.sub_agents
        ]

    manifest_path = session_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2))

    # --- Write workspace.json ---
    workspace: dict[str, Any] = {
        "root_path": cc_session.project_path or "",
    }

    git_context = capture_git_context(cc_session.project_path)
    if git_context:
        workspace["git"] = git_context
    elif cc_session.git_branch:
        workspace["git"] = {"branch": cc_session.git_branch}

    file_refs = extract_file_refs(sfs_messages)
    if file_refs:
        workspace["files"] = file_refs

    workspace["environment"] = capture_environment()

    workspace_path = session_dir / "workspace.json"
    workspace_path.write_text(json.dumps(workspace, indent=2))

    # --- Write tools.json ---
    tools: dict[str, Any] = {}

    if tool_names:
        tools["tools_used"] = sorted(tool_names)

    if cc_session.project_path:
        tools["shell"] = {
            "default_shell": os.environ.get("SHELL", "").split("/")[-1] or "bash",
            "working_directory": cc_session.project_path,
        }

    tools_path = session_dir / "tools.json"
    tools_path.write_text(json.dumps(tools, indent=2))

    return session_dir


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def cmd_list() -> None:
    """List discovered Claude Code sessions."""
    sessions = discover_sessions(_DEFAULT_HOME)
    if not sessions:
        print("No Claude Code sessions found.")
        return

    print(f"\nDiscovered {len(sessions)} session(s):\n")
    for s in sessions:
        size_kb = s["size_bytes"] / 1024
        prompt = s["first_prompt"][:60] + "..." if len(s["first_prompt"]) > 60 else s["first_prompt"]
        print(f"  {s['session_id']}")
        print(f"    Size: {size_kb:.1f} KB | Messages: {s['message_count']}")
        if prompt:
            print(f"    Prompt: {prompt}")
        print()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Convert Claude Code sessions to .sfs format.",
    )
    parser.add_argument(
        "sessions",
        nargs="*",
        help="Session UUID(s) or JSONL path(s) to convert.",
    )
    parser.add_argument(
        "--output", "-o",
        default=".",
        help="Output directory for .sfs session directories.",
    )
    parser.add_argument(
        "--list", "-l",
        action="store_true",
        dest="list_only",
        help="List available Claude Code sessions.",
    )
    parser.add_argument(
        "--validate", "-v",
        action="store_true",
        help="Validate converted sessions after conversion.",
    )

    args = parser.parse_args()

    if args.list_only:
        cmd_list()
        return

    if not args.sessions:
        parser.print_help()
        sys.exit(1)

    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)

    converted: list[Path] = []
    for ident in args.sessions:
        path = find_session_path(_DEFAULT_HOME, ident)
        if not path:
            print(f"Session not found: {ident}", file=sys.stderr)
            continue

        print(f"Parsing: {path}")
        cc_session = parse_session(path)
        print(
            f"  Messages: {cc_session.message_count}, "
            f"Sub-agents: {len(cc_session.sub_agents)}, "
            f"Parse errors: {len(cc_session.parse_errors)}"
        )

        print("Converting to .sfs...")
        session_dir = convert_session(cc_session, output_dir)
        converted.append(session_dir)
        print(f"  Output: {session_dir}")

    print(f"\nConverted {len(converted)} session(s).")

    if args.validate and converted:
        print("\nValidating...")
        from sessionfs.spec.validate import validate_session

        all_valid = True
        for session_dir in converted:
            result = validate_session(session_dir)
            result.print_report()
            if not result.valid:
                all_valid = False

        if all_valid:
            print(f"\nAll {len(converted)} converted session(s) are valid.")
        else:
            print("\nSome sessions have validation errors.", file=sys.stderr)
            sys.exit(1)


if __name__ == "__main__":
    main()
