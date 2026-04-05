"""Tests for MCP knowledge instruction injection and removal."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from sessionfs.cli.cmd_mcp import (
    KNOWLEDGE_INSTRUCTIONS,
    SECTION_MARKER_END,
    SECTION_MARKER_START,
    inject_agent_instructions,
    remove_agent_instructions,
)


def test_inject_creates_new_file(tmp_path: Path) -> None:
    """inject_agent_instructions creates the instruction file if it doesn't exist."""
    with patch("sessionfs.cli.cmd_mcp.Path.cwd", return_value=tmp_path):
        inject_agent_instructions("cursor")

    path = tmp_path / ".cursorrules"
    assert path.exists()
    content = path.read_text()
    assert SECTION_MARKER_START in content
    assert SECTION_MARKER_END in content
    assert "SessionFS Knowledge Base" in content


def test_inject_appends_to_existing_file(tmp_path: Path) -> None:
    """inject_agent_instructions appends to an existing file without markers."""
    path = tmp_path / ".cursorrules"
    path.write_text("# Existing rules\nDo not delete this.\n")

    with patch("sessionfs.cli.cmd_mcp.Path.cwd", return_value=tmp_path):
        inject_agent_instructions("cursor")

    content = path.read_text()
    assert content.startswith("# Existing rules\nDo not delete this.")
    assert SECTION_MARKER_START in content
    assert SECTION_MARKER_END in content


def test_inject_updates_existing_section_idempotent(tmp_path: Path) -> None:
    """inject_agent_instructions replaces existing section in-place."""
    path = tmp_path / ".cursorrules"
    original_before = "# My rules\n\n"
    original_after = "\n\n# Other stuff\n"
    path.write_text(
        original_before
        + SECTION_MARKER_START
        + "\nold content\n"
        + SECTION_MARKER_END
        + original_after
    )

    with patch("sessionfs.cli.cmd_mcp.Path.cwd", return_value=tmp_path):
        inject_agent_instructions("cursor")

    content = path.read_text()
    # Old content replaced
    assert "old content" not in content
    # New content present
    assert "SessionFS Knowledge Base" in content
    # Surrounding content preserved
    assert "# My rules" in content
    assert "# Other stuff" in content
    # Only one marker pair
    assert content.count(SECTION_MARKER_START) == 1
    assert content.count(SECTION_MARKER_END) == 1


def test_remove_cleans_section_from_file(tmp_path: Path) -> None:
    """remove_agent_instructions removes the section, keeps remaining content."""
    path = tmp_path / ".cursorrules"
    path.write_text(
        "# My rules\n\n"
        + SECTION_MARKER_START
        + "\n"
        + KNOWLEDGE_INSTRUCTIONS
        + "\n"
        + SECTION_MARKER_END
        + "\n"
    )

    with patch("sessionfs.cli.cmd_mcp.Path.cwd", return_value=tmp_path):
        remove_agent_instructions("cursor")

    content = path.read_text()
    assert SECTION_MARKER_START not in content
    assert SECTION_MARKER_END not in content
    assert "# My rules" in content


def test_remove_deletes_empty_file(tmp_path: Path) -> None:
    """remove_agent_instructions deletes the file if only the section was present."""
    path = tmp_path / ".cursorrules"
    path.write_text(
        SECTION_MARKER_START
        + "\n"
        + KNOWLEDGE_INSTRUCTIONS
        + "\n"
        + SECTION_MARKER_END
        + "\n"
    )

    with patch("sessionfs.cli.cmd_mcp.Path.cwd", return_value=tmp_path):
        remove_agent_instructions("cursor")

    assert not path.exists()


def test_tools_without_instruction_support_log_message(tmp_path: Path, capsys) -> None:
    """Tools without instruction files print an informational message."""
    with patch("sessionfs.cli.cmd_mcp.Path.cwd", return_value=tmp_path):
        inject_agent_instructions("copilot")

    # No files should be created
    assert list(tmp_path.iterdir()) == []


def test_remove_noop_when_no_file(tmp_path: Path) -> None:
    """remove_agent_instructions is a no-op when the file doesn't exist."""
    with patch("sessionfs.cli.cmd_mcp.Path.cwd", return_value=tmp_path):
        remove_agent_instructions("cursor")  # Should not raise


def test_remove_noop_when_no_markers(tmp_path: Path) -> None:
    """remove_agent_instructions is a no-op when no markers in file."""
    path = tmp_path / ".cursorrules"
    original = "# My rules\nKeep this.\n"
    path.write_text(original)

    with patch("sessionfs.cli.cmd_mcp.Path.cwd", return_value=tmp_path):
        remove_agent_instructions("cursor")

    assert path.read_text() == original


def test_inject_claude_code_creates_claude_md(tmp_path: Path) -> None:
    """inject for claude-code targets CLAUDE.md."""
    with patch("sessionfs.cli.cmd_mcp.Path.cwd", return_value=tmp_path):
        inject_agent_instructions("claude-code")

    assert (tmp_path / "CLAUDE.md").exists()


def test_inject_codex_creates_codex_md(tmp_path: Path) -> None:
    """inject for codex targets codex.md."""
    with patch("sessionfs.cli.cmd_mcp.Path.cwd", return_value=tmp_path):
        inject_agent_instructions("codex")

    assert (tmp_path / "codex.md").exists()


def test_inject_gemini_creates_gemini_md(tmp_path: Path) -> None:
    """inject for gemini targets GEMINI.md."""
    with patch("sessionfs.cli.cmd_mcp.Path.cwd", return_value=tmp_path):
        inject_agent_instructions("gemini")

    assert (tmp_path / "GEMINI.md").exists()
