"""Amp -> .sfs converter.

Converts Amp native thread files (JSON) into the canonical .sfs format.

Amp threads are single JSON files with a flat messages array.
Messages have role (user/assistant) and content blocks [{type, text}].
Model info is in env.initial.tags. Token usage is in usageLedger.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sessionfs.spec.version import SFS_FORMAT_VERSION, SFS_CONVERTER_VERSION

logger = logging.getLogger("sessionfs.converters.amp_to_sfs")


@dataclass
class AmpParsedSession:
    """Intermediate representation of a parsed Amp thread."""

    session_id: str
    title: str | None = None
    source_path: str | None = None
    start_time: str | None = None
    last_updated: str | None = None
    model: str | None = None
    messages: list[dict[str, Any]] = field(default_factory=list)
    message_count: int = 0
    turn_count: int = 0
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    parse_errors: list[str] = field(default_factory=list)


def _extract_model_from_tags(env: dict[str, Any] | None) -> str | None:
    """Extract model identifier from env.initial.tags list.

    Tags look like ["model:claude-sonnet-4", ...].
    """
    if not env:
        return None
    initial = env.get("initial", {})
    tags = initial.get("tags", [])
    for tag in tags:
        if isinstance(tag, str) and tag.startswith("model:"):
            return tag[len("model:"):]
    return None


def _extract_token_usage(usage_ledger: dict[str, Any] | None) -> tuple[int, int]:
    """Extract total input/output tokens from usageLedger.

    Returns (input_tokens, output_tokens).
    """
    if not usage_ledger:
        return 0, 0

    input_tokens = 0
    output_tokens = 0
    for event in usage_ledger.get("events", []):
        input_tokens += event.get("inputTokens", 0) or event.get("input_tokens", 0)
        output_tokens += event.get("outputTokens", 0) or event.get("output_tokens", 0)
    return input_tokens, output_tokens


def _ms_to_iso(ms: int | float | None) -> str | None:
    """Convert milliseconds-since-epoch to ISO 8601 string."""
    if ms is None:
        return None
    try:
        return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat()
    except (ValueError, TypeError, OSError):
        return None


def parse_amp_session(thread_path: Path) -> AmpParsedSession:
    """Parse an Amp thread JSON file."""
    data = json.loads(thread_path.read_text())

    thread_id = data.get("id", thread_path.stem)
    created_ms = data.get("created")
    start_time = _ms_to_iso(created_ms)

    # Model from env tags
    model = _extract_model_from_tags(data.get("env"))

    # Token usage
    input_tokens, output_tokens = _extract_token_usage(data.get("usageLedger"))

    session = AmpParsedSession(
        session_id=thread_id,
        title=data.get("title"),
        source_path=str(thread_path),
        start_time=start_time,
        model=model,
        total_input_tokens=input_tokens,
        total_output_tokens=output_tokens,
    )

    sfs_messages: list[dict[str, Any]] = []
    turn_count = 0
    prev_role = None
    last_timestamp = start_time

    for msg in data.get("messages", []):
        role = msg.get("role", "")
        if role not in ("user", "assistant"):
            continue

        msg_id = str(msg.get("messageId", f"msg_{len(sfs_messages):04d}"))

        # Content blocks: [{type, text}]
        sfs_content = []
        content_raw = msg.get("content", [])
        if isinstance(content_raw, list):
            for part in content_raw:
                if isinstance(part, dict) and part.get("type") == "text":
                    text = part.get("text", "")
                    if text:
                        sfs_content.append({"type": "text", "text": text})
        elif isinstance(content_raw, str):
            sfs_content.append({"type": "text", "text": content_raw})

        if not sfs_content:
            continue

        # Map roles: user -> user, assistant -> assistant
        sfs_role = role

        # Count turns (user message after non-user)
        if role == "user" and prev_role != "user":
            turn_count += 1

        sfs_messages.append({
            "msg_id": msg_id,
            "role": sfs_role,
            "content": sfs_content,
            "timestamp": last_timestamp,
        })
        prev_role = role

    # Determine last_updated from last message or created time
    session.last_updated = last_timestamp
    session.messages = sfs_messages
    session.message_count = len(sfs_messages)
    session.turn_count = turn_count
    return session


def convert_amp_to_sfs(
    thread_path: Path,
    output_dir: Path,
    session_id: str | None = None,
) -> Path:
    """Convert an Amp thread to .sfs format.

    Args:
        thread_path: Path to the Amp thread JSON file.
        output_dir: Directory to write .sfs files into.
        session_id: Optional override for the session ID.

    Returns:
        Path to the output directory.
    """
    from sessionfs.session_id import session_id_from_native
    from sessionfs.utils.title_utils import extract_smart_title

    amp_session = parse_amp_session(thread_path)
    sid = session_id or session_id_from_native(amp_session.session_id)
    output_dir.mkdir(parents=True, exist_ok=True)

    messages = amp_session.messages
    created_at = amp_session.start_time or datetime.now(timezone.utc).isoformat()
    updated_at = amp_session.last_updated or created_at

    # Duration
    duration_ms = None
    try:
        start = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
        end = datetime.fromisoformat(updated_at.replace("Z", "+00:00"))
        duration_ms = int((end - start).total_seconds() * 1000)
    except (ValueError, TypeError):
        pass

    # Title: use thread title if available, else extract from messages
    title = extract_smart_title(
        messages=messages or None,
        raw_title=amp_session.title,
        message_count=amp_session.message_count,
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
            "tool": "amp",
            "tool_version": None,
            "sfs_converter_version": SFS_CONVERTER_VERSION,
            "original_session_id": amp_session.session_id,
            "original_path": amp_session.source_path,
            "interface": "cli",
        },
        "model": {
            "provider": _infer_provider(amp_session.model),
            "model_id": amp_session.model,
        } if amp_session.model else None,
        "stats": {
            "message_count": amp_session.message_count,
            "turn_count": amp_session.turn_count,
            "tool_use_count": 0,
            "total_input_tokens": amp_session.total_input_tokens,
            "total_output_tokens": amp_session.total_output_tokens,
            "duration_ms": duration_ms,
        },
    }

    (output_dir / "manifest.json").write_text(json.dumps(manifest, indent=2))

    with open(output_dir / "messages.jsonl", "w") as f:
        for msg in messages:
            f.write(json.dumps(msg, separators=(",", ":")) + "\n")

    return output_dir


def _infer_provider(model_id: str | None) -> str | None:
    """Infer model provider from model ID string."""
    if not model_id:
        return None
    lower = model_id.lower()
    if "claude" in lower or "sonnet" in lower or "opus" in lower or "haiku" in lower:
        return "anthropic"
    if "gpt" in lower or "o1" in lower or "o3" in lower:
        return "openai"
    if "gemini" in lower:
        return "google"
    return None


def discover_amp_sessions(data_dir: Path) -> list[dict[str, Any]]:
    """Discover all Amp thread files.

    Scans the Amp threads directory for JSON files.

    Args:
        data_dir: Path to Amp data directory (e.g., ~/.local/share/amp).

    Returns:
        List of dicts with session_id, path, mtime, size_bytes.
    """
    sessions: list[dict[str, Any]] = []

    threads_dir = data_dir / "threads"
    if not threads_dir.is_dir():
        return sessions

    for thread_file in sorted(threads_dir.glob("*.json"), reverse=True):
        if not thread_file.is_file():
            continue

        stat = thread_file.stat()
        sessions.append({
            "session_id": thread_file.stem,
            "path": str(thread_file),
            "mtime": stat.st_mtime,
            "size_bytes": stat.st_size,
        })

    return sessions
