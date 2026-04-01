"""Skill/slash-command detection for AI coding tool sessions.

Detects /command patterns in user messages (e.g., /commit, /review-pr, /release)
while filtering out file paths like /Users/ola/file.py or /api/v1/sessions.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any


# Match /command at the start of text, but NOT file paths.
# Command names: 2-50 chars, alphanumeric + hyphens, no nested slashes.
_SLASH_CMD_RE = re.compile(r"(?:^|(?<=\s))/([a-zA-Z][a-zA-Z0-9\-]{1,49})(?=\s|$)")


@dataclass
class DetectedSkill:
    """A slash command detected in a user message."""

    name: str
    message_index: int
    source: str
    raw_text: str


def _is_file_path(text: str, match_start: int) -> bool:
    """Check if the match is part of a file path (has a / after the command)."""
    # Get the full token containing the match
    end = match_start
    while end < len(text) and not text[end].isspace():
        end += 1
    token = text[match_start:end]
    # If the token contains a second slash, it's a file path
    return "/" in token[1:]


def detect_skills(
    messages: list[dict[str, Any]],
    source_tool: str,
) -> list[DetectedSkill]:
    """Scan user messages for /command patterns.

    Args:
        messages: List of .sfs-format messages with role and content.
        source_tool: Name of the originating tool (e.g., "claude-code").

    Returns:
        List of DetectedSkill instances found in user messages.
    """
    skills: list[DetectedSkill] = []

    for idx, msg in enumerate(messages):
        if msg.get("role") != "user":
            continue

        content = msg.get("content", [])
        if isinstance(content, str):
            texts = [content]
        elif isinstance(content, list):
            texts = []
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    texts.append(block.get("text", ""))
                elif isinstance(block, str):
                    texts.append(block)
        else:
            continue

        for text in texts:
            for match in _SLASH_CMD_RE.finditer(text):
                cmd_name = match.group(1)
                # Verify this isn't a file path by checking the full token
                if _is_file_path(text, match.start()):
                    continue
                skills.append(DetectedSkill(
                    name=cmd_name,
                    message_index=idx,
                    source=source_tool,
                    raw_text=match.group(0),
                ))

    return skills


def skills_to_tools_json(skills: list[DetectedSkill]) -> list[dict[str, Any]]:
    """Convert detected skills to custom_tools format for tools.json.

    Deduplicates by name and includes invocation_count.

    Returns:
        List of dicts with name, type, source, and invocation_count.
    """
    counts: dict[str, int] = {}
    sources: dict[str, str] = {}
    for skill in skills:
        counts[skill.name] = counts.get(skill.name, 0) + 1
        if skill.name not in sources:
            sources[skill.name] = skill.source

    return [
        {
            "name": name,
            "type": "slash_command",
            "source": sources[name],
            "invocation_count": count,
        }
        for name, count in counts.items()
    ]
