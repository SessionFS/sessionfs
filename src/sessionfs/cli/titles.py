"""Smart session title extraction, sanitization, and display formatting.

Used by the CLI (at display time) to produce clean, readable titles.
Core title logic lives in sessionfs.utils.title_utils (shared with server).
This module re-exports the core functions and adds CLI-specific display helpers.
"""

from __future__ import annotations

from typing import Any

# Re-export core title functions from shared module
from sessionfs.utils.title_utils import (
    extract_smart_title,
    is_usable_title,
    sanitize_secrets as sanitize_title_secrets,
    _truncate_at_word,
    _iter_usable_lines,
    _extract_text,
)

# Aliases for backward compatibility with existing callers
_is_usable_title = is_usable_title


def extract_title(
    manifest: dict[str, Any] | None = None,
    messages: list[dict[str, Any]] | None = None,
    max_length: int = 80,
) -> str:
    """Extract a clean session title. Delegates to shared title_utils."""
    raw_title = manifest.get("title") if manifest else None
    msg_count = 0
    if manifest:
        stats = manifest.get("stats", {})
        msg_count = stats.get("message_count", 0)

    return extract_smart_title(
        messages=messages,
        raw_title=raw_title,
        message_count=msg_count,
        max_length=max_length,
    )


# ---------------------------------------------------------------------------
# Display formatting helpers
# ---------------------------------------------------------------------------

_MODEL_ABBREVIATIONS: dict[str, str] = {
    "claude-opus-4-6": "opus-4.6",
    "claude-sonnet-4-6": "sonnet-4.6",
    "claude-haiku-4-5": "haiku-4.5",
    "claude-3-5-sonnet-20241022": "sonnet-3.5",
    "claude-3-5-haiku-20241022": "haiku-3.5",
    "claude-3-opus-20240229": "opus-3",
    "claude-3-sonnet-20240229": "sonnet-3",
    "claude-3-haiku-20240307": "haiku-3",
    "gpt-4o": "gpt-4o",
    "gpt-4o-mini": "gpt-4o-mini",
    "gpt-4-turbo": "gpt-4t",
    "o1-preview": "o1-preview",
    "o1-mini": "o1-mini",
    "o3-mini": "o3-mini",
}


_TOOL_ABBREVIATIONS: dict[str, str] = {
    "claude-code": "CC",
    "codex": "CDX",
    "cursor": "CUR",
    "gemini-cli": "GEM",
    "aider": "AID",
    "windsurf": "WS",
    "copilot": "COP",
}


def abbreviate_tool(tool_name: str | None) -> str:
    """Abbreviate a source tool name for compact table display."""
    if not tool_name:
        return ""
    lower = tool_name.lower()
    for full, short in _TOOL_ABBREVIATIONS.items():
        if lower.startswith(full):
            return short
    if len(tool_name) > 6:
        return tool_name[:5] + "\u2026"
    return tool_name


def abbreviate_model(model_id: str | None) -> str:
    """Abbreviate a model ID for table display."""
    if not model_id:
        return ""
    # Exact match
    if model_id in _MODEL_ABBREVIATIONS:
        return _MODEL_ABBREVIATIONS[model_id]
    # Try stripping date suffix (e.g., claude-opus-4-6-20260301)
    for full, short in _MODEL_ABBREVIATIONS.items():
        if model_id.startswith(full):
            return short
    # Unknown: truncate
    if len(model_id) > 12:
        return model_id[:11] + "\u2026"
    return model_id


def format_relative_time(iso_timestamp: str | None) -> str:
    """Format an ISO timestamp as relative time (e.g., '2m ago', '3h ago')."""
    if not iso_timestamp:
        return ""

    from datetime import datetime, timezone

    try:
        ts = datetime.fromisoformat(iso_timestamp.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return iso_timestamp[:10] if iso_timestamp else ""

    now = datetime.now(timezone.utc)
    delta = now - ts

    seconds = int(delta.total_seconds())
    if seconds < 0:
        return "just now"
    if seconds < 60:
        return f"{seconds}s ago"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m ago"
    hours = minutes // 60
    if hours < 24:
        return f"{hours}h ago"
    days = hours // 24
    if days < 7:
        return f"{days}d ago"
    if days < 365:
        return ts.strftime("%b %d")
    return ts.strftime("%b %Y")


def format_token_count(tokens: int) -> str:
    """Format a token count in human-readable form (e.g., '48.2k', '1.2M')."""
    if tokens == 0:
        return "0"
    if tokens < 1000:
        return str(tokens)
    if tokens < 1_000_000:
        k = tokens / 1000
        if k >= 100:
            return f"{k:.0f}k"
        if k >= 10:
            return f"{k:.1f}k"
        return f"{k:.1f}k"
    m = tokens / 1_000_000
    if m >= 10:
        return f"{m:.0f}M"
    return f"{m:.1f}M"
