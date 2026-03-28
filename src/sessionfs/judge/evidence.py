"""Gather evidence from tool calls in session messages."""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass
class Evidence:
    message_index: int
    tool_name: str
    input_summary: str
    output_summary: str
    exit_code: int | None
    file_path: str | None


_EXIT_CODE_RE = re.compile(r"(?:exit code|exitcode|returned?)\s*[=:]?\s*(\d+)", re.IGNORECASE)
_FILE_PATH_RE = re.compile(r"(?:^|[\s\"'`(])(/[\w./-]{2,})(?:[\s\"'`):,]|$)", re.MULTILINE)


def _extract_exit_code(text: str) -> int | None:
    """Extract exit code from tool output text."""
    match = _EXIT_CODE_RE.search(text)
    if match:
        return int(match.group(1))
    return None


def _extract_file_path_from_input(tool_name: str, inp: dict) -> str | None:
    """Extract file path from tool input based on tool name."""
    # Common patterns across tools
    for key in ("file_path", "path", "filename", "file"):
        if key in inp and isinstance(inp[key], str):
            return inp[key]

    # Bash commands may reference file paths
    if tool_name.lower() in ("bash", "execute", "terminal"):
        cmd = inp.get("command", "")
        if isinstance(cmd, str):
            match = _FILE_PATH_RE.search(cmd)
            if match:
                return match.group(1)

    return None


def _summarise_text(text: str, max_len: int = 2000) -> str:
    """Truncate text to a summary length. Preserves full context for evidence."""
    text = text.strip()
    if len(text) <= max_len:
        return text
    return text[:max_len] + "...[truncated]"


def _extract_text_from_content(content) -> str:
    """Extract plain text from a content field (string or list of blocks)."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict):
                btype = block.get("type", "")
                if btype == "text":
                    parts.append(block.get("text", ""))
                elif btype == "tool_result":
                    inner = block.get("content", "")
                    parts.append(
                        inner if isinstance(inner, str) else _extract_text_from_content(inner)
                    )
        return "\n".join(parts)
    return ""


def _build_input_summary(tool_name: str, inp: dict) -> str:
    """Build a human-readable summary of tool input."""
    if tool_name.lower() in ("bash", "execute", "terminal"):
        cmd = inp.get("command", "")
        return _summarise_text(str(cmd))
    if "file_path" in inp:
        return f"file: {inp['file_path']}"
    if "path" in inp:
        return f"path: {inp['path']}"
    if "query" in inp:
        return f"query: {_summarise_text(str(inp['query']), 100)}"
    # Fallback: show first key-value pair
    for k, v in inp.items():
        return f"{k}: {_summarise_text(str(v), 100)}"
    return ""


def gather_evidence(messages: list[dict]) -> list[Evidence]:
    """Gather evidence from tool_use and tool_result content blocks.

    Processes messages to extract tool invocations and their results,
    capturing exit codes from Bash results and file paths from
    Read/Edit/Write tools.
    """
    evidence: list[Evidence] = []

    # Build a map of tool_use_id -> (message_index, tool_name, input)
    tool_uses: dict[str, tuple[int, str, dict]] = {}

    for idx, msg in enumerate(messages):
        content = msg.get("content", [])
        if isinstance(content, str):
            continue

        for block in content:
            if not isinstance(block, dict):
                continue

            btype = block.get("type", "")

            if btype == "tool_use":
                tool_id = block.get("id", "")
                tool_name = block.get("name", "")
                inp = block.get("input", {})
                if not isinstance(inp, dict):
                    inp = {}
                tool_uses[tool_id] = (idx, tool_name, inp)

            elif btype == "tool_result":
                tool_id = block.get("tool_use_id", "")
                result_content = block.get("content", "")
                result_text = _extract_text_from_content(result_content)

                if tool_id in tool_uses:
                    use_idx, tool_name, inp = tool_uses[tool_id]
                else:
                    use_idx = idx
                    tool_name = "unknown"
                    inp = {}

                exit_code = _extract_exit_code(result_text)
                file_path = _extract_file_path_from_input(tool_name, inp)
                input_summary = _build_input_summary(tool_name, inp)
                output_summary = _summarise_text(result_text)

                evidence.append(
                    Evidence(
                        message_index=idx,
                        tool_name=tool_name,
                        input_summary=input_summary,
                        output_summary=output_summary,
                        exit_code=exit_code,
                        file_path=file_path,
                    )
                )

    # Also handle role=tool messages (alternative format)
    for idx, msg in enumerate(messages):
        if msg.get("role") != "tool":
            continue

        tool_id = msg.get("tool_use_id", "")
        result_text = _extract_text_from_content(msg.get("content", ""))

        if tool_id in tool_uses:
            use_idx, tool_name, inp = tool_uses[tool_id]
        else:
            tool_name = msg.get("name", "unknown")
            inp = {}

        exit_code = _extract_exit_code(result_text)
        file_path = _extract_file_path_from_input(tool_name, inp)
        input_summary = _build_input_summary(tool_name, inp)
        output_summary = _summarise_text(result_text)

        evidence.append(
            Evidence(
                message_index=idx,
                tool_name=tool_name,
                input_summary=input_summary,
                output_summary=output_summary,
                exit_code=exit_code,
                file_path=file_path,
            )
        )

    return evidence
