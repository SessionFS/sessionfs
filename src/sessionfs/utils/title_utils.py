"""Shared smart title extraction and sanitization.

Used by the converter (at capture time), the CLI (at display time), and the
server (during metadata extraction) to produce clean, meaningful, secret-free
session titles.

This module has no dependencies on CLI, server, or daemon code — only on the
security secrets module for pattern-based redaction.
"""

from __future__ import annotations

import re
from typing import Any

from sessionfs.security.secrets import SECRET_PATTERNS, ALLOWLIST

# ---------------------------------------------------------------------------
# Junk detection — lines that aren't natural language
# ---------------------------------------------------------------------------

_JUNK_PREFIXES = (
    "#",           # Markdown headings (agent persona preambles)
    "<",           # XML/HTML tags, tool markup
    "[",           # System messages like [Request interrupt...]
    "(",           # Agent persona load instructions
    "---",         # YAML frontmatter / horizontal rules
    "```",         # Code fences
    "<!--",        # HTML comments
    "IMPORTANT:",  # System instructions
    "Note:",       # System notes
    "WARNING:",    # System warnings
    "Set ",        # CLI mode toggles: "Set Fast mode to ON"
)

_JUNK_PATTERNS = re.compile(
    r"^(?:"
    r"<[a-z_-]+|"                  # XML-like tags
    r"\[(?:Request|System|Tool)|"  # Bracketed system messages
    r"---\s*$|"                    # Horizontal rules
    r"#+\s+Agent:|"               # Agent persona headings
    r"Co-Authored-By:|"           # Git commit trailers
    r"(?:CLAUDE|README)\.md|"     # File references as first line
    r"Implement the following plan:|"
    r"(?:Load|Using) (?:full |both )?personas?\b"
    r")",
    re.IGNORECASE,
)

# Title-specific secret patterns (broader than M10 code-assignment patterns)
_TITLE_SECRET_PATTERNS: list[re.Pattern[str]] = [
    re.compile(
        r"(?:password|passwd|pwd|secret|token|api.?key)\s+(?:is|was|=|:)\s*"
        r'["\'](?P<secret>[^"\']{4,})["\']',
        re.IGNORECASE,
    ),
    re.compile(r"://[^:]+:(?P<secret>[^@\s]{4,})@"),
]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def extract_smart_title(
    messages: list[dict[str, Any]] | None = None,
    raw_title: str | None = None,
    message_count: int = 0,
    max_length: int = 80,
) -> str:
    """Extract a meaningful session title.

    Priority:
    1. Use raw_title if it looks like natural language (not junk)
    2. Find the first user message that starts with natural language
    3. Fall back to "Untitled session (N messages)"

    Works on both .sfs message dicts (from messages.jsonl) and raw manifest
    title strings. Truncates at sentence/word boundary and redacts secrets.
    """
    # Priority 1: existing title, if usable
    if raw_title and is_usable_title(raw_title):
        return _finalize(raw_title, max_length)

    # Priority 2: first user message with natural language
    if messages:
        for msg in messages:
            if msg.get("role") != "user":
                continue
            if msg.get("is_sidechain"):
                continue

            text = _extract_text(msg)
            if not text:
                continue

            for line in _iter_usable_lines(text):
                if is_usable_title(line):
                    return _finalize(line, max_length)

    # Fallback
    if message_count > 0:
        return f"Untitled session ({message_count} messages)"
    return "Untitled session"


def is_usable_title(text: str) -> bool:
    """Check if text looks like natural language suitable for a title."""
    text = text.strip()
    if not text or len(text) < 3:
        return False
    for prefix in _JUNK_PREFIXES:
        if text.startswith(prefix):
            return False
    if _JUNK_PATTERNS.match(text):
        return False
    return True


def sanitize_secrets(title: str) -> str:
    """Remove detected secrets from a title string."""
    if any(p.search(title) for p in ALLOWLIST):
        return title

    for pattern in SECRET_PATTERNS.values():
        if pattern.search(title):
            title = pattern.sub("[redacted]", title)

    for pattern in _TITLE_SECRET_PATTERNS:
        if pattern.search(title):
            title = pattern.sub("[redacted]", title)

    return title


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _finalize(text: str, max_length: int) -> str:
    text = text.strip()
    text = _extract_first_sentence(text, max_length)
    text = _truncate_at_word(text, max_length)
    text = sanitize_secrets(text)
    return text


def _extract_text(msg: dict[str, Any]) -> str:
    content = msg.get("content", [])
    if isinstance(content, str):
        return content
    parts = []
    for block in content:
        if isinstance(block, str):
            parts.append(block)
        elif isinstance(block, dict) and block.get("type") == "text":
            parts.append(block.get("text", ""))
    return "\n".join(parts)


def _iter_usable_lines(text: str):
    in_fence = False
    in_frontmatter = False
    frontmatter_seen = False

    for i, line in enumerate(text.split("\n")):
        stripped = line.strip()

        if stripped == "---":
            if i == 0 and not frontmatter_seen:
                in_frontmatter = True
                frontmatter_seen = True
                continue
            elif in_frontmatter:
                in_frontmatter = False
                continue

        if in_frontmatter:
            continue
        if stripped.startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        if stripped:
            yield stripped


def _extract_first_sentence(text: str, max_length: int) -> str:
    for i, ch in enumerate(text):
        if ch in ".!?" and i < max_length - 1:
            candidate = text[: i + 1].strip()
            if len(candidate) >= 10:
                return candidate
    return text


def _truncate_at_word(text: str, max_length: int) -> str:
    if len(text) <= max_length:
        return text
    truncated = text[:max_length]
    last_space = truncated.rfind(" ")
    if last_space > max_length // 2:
        truncated = truncated[:last_space]
    return truncated.rstrip() + "\u2026"
