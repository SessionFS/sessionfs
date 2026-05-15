"""Tests for sfs init command."""

from __future__ import annotations

from pathlib import Path

from sessionfs.cli.cmd_init import (
    ToolInfo,
    _get_tool_definitions,
    detect_tools,
)


class TestToolDetection:
    """Test tool detection logic."""

    def test_detects_tool_when_path_exists(self, tmp_path: Path) -> None:
        """Tool is detected when its config directory exists."""
        tool_dir = tmp_path / ".claude"
        tool_dir.mkdir()

        tools = [
            ToolInfo(name="Claude Code", config_key="claude_code", detect_paths=[tool_dir]),
        ]
        detected, missing = detect_tools(tools)

        assert len(detected) == 1
        assert len(missing) == 0
        assert detected[0].info.name == "Claude Code"
        assert detected[0].found_path == tool_dir

    def test_missing_tool_when_path_absent(self, tmp_path: Path) -> None:
        """Tool is missing when no config directory exists."""
        tools = [
            ToolInfo(
                name="Codex",
                config_key="codex",
                detect_paths=[tmp_path / ".codex"],
            ),
        ]
        detected, missing = detect_tools(tools)

        assert len(detected) == 0
        assert len(missing) == 1
        assert missing[0].name == "Codex"

    def test_detects_first_matching_path(self, tmp_path: Path) -> None:
        """When multiple detect paths exist, the first match is used."""
        primary = tmp_path / ".claude"
        secondary = tmp_path / ".config" / "claude-code"
        primary.mkdir()
        secondary.mkdir(parents=True)

        tools = [
            ToolInfo(
                name="Claude Code",
                config_key="claude_code",
                detect_paths=[primary, secondary],
            ),
        ]
        detected, _ = detect_tools(tools)

        assert len(detected) == 1
        assert detected[0].found_path == primary

    def test_falls_back_to_secondary_path(self, tmp_path: Path) -> None:
        """When primary path is missing, falls back to secondary."""
        secondary = tmp_path / ".config" / "claude-code"
        secondary.mkdir(parents=True)

        tools = [
            ToolInfo(
                name="Claude Code",
                config_key="claude_code",
                detect_paths=[tmp_path / ".claude", secondary],
            ),
        ]
        detected, _ = detect_tools(tools)

        assert len(detected) == 1
        assert detected[0].found_path == secondary

    def test_mixed_detected_and_missing(self, tmp_path: Path) -> None:
        """Some tools detected, some missing."""
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()

        tools = [
            ToolInfo(name="Claude Code", config_key="claude_code", detect_paths=[claude_dir]),
            ToolInfo(name="Codex", config_key="codex", detect_paths=[tmp_path / ".codex"]),
            ToolInfo(name="Gemini CLI", config_key="gemini", detect_paths=[tmp_path / ".gemini"]),
        ]
        detected, missing = detect_tools(tools)

        assert len(detected) == 1
        assert len(missing) == 2
        assert detected[0].info.name == "Claude Code"
        assert {m.name for m in missing} == {"Codex", "Gemini CLI"}

    def test_no_tools_detected(self, tmp_path: Path) -> None:
        """All tools missing returns empty detected list."""
        tools = [
            ToolInfo(name="Codex", config_key="codex", detect_paths=[tmp_path / ".codex"]),
        ]
        detected, missing = detect_tools(tools)

        assert len(detected) == 0
        assert len(missing) == 1

    def test_all_tools_detected(self, tmp_path: Path) -> None:
        """All tools present returns empty missing list."""
        dirs = [tmp_path / ".claude", tmp_path / ".cursor"]
        for d in dirs:
            d.mkdir()

        tools = [
            ToolInfo(name="Claude Code", config_key="claude_code", detect_paths=[dirs[0]]),
            ToolInfo(name="Cursor", config_key="cursor", detect_paths=[dirs[1]]),
        ]
        detected, missing = detect_tools(tools)

        assert len(detected) == 2
        assert len(missing) == 0


class TestToolDefinitions:
    """Test the built-in tool definitions."""

    def test_all_nine_tools_defined(self) -> None:
        """All nine supported tools have definitions."""
        tools = _get_tool_definitions()
        assert len(tools) == 9

        names = {t.name for t in tools}
        assert names == {
            "Claude Code",
            "Cursor",
            "Codex",
            "Gemini CLI",
            "Copilot",
            "Amp",
            "Cline",
            "Roo Code",
            "Kilo Code",
        }

    def test_each_tool_has_detect_paths(self) -> None:
        """Every tool definition has at least one detection path."""
        for tool in _get_tool_definitions():
            assert len(tool.detect_paths) >= 1, f"{tool.name} has no detect paths"

    def test_config_keys_are_unique(self) -> None:
        """Config keys are unique across all tools."""
        tools = _get_tool_definitions()
        keys = [t.config_key for t in tools]
        assert len(keys) == len(set(keys))


class TestInitRegistration:
    """Test that init_cmd is importable and compatible with typer."""

    def test_init_cmd_is_callable(self) -> None:
        """init_cmd can be imported and is callable."""
        from sessionfs.cli.cmd_init import init_cmd

        assert callable(init_cmd)

    def test_init_registered_in_app(self) -> None:
        """init command is registered in the main CLI app."""
        from sessionfs.cli.main import app

        # Collect all registered command names
        command_names = set()
        for cmd_info in app.registered_commands:
            if cmd_info.callback:
                command_names.add(cmd_info.callback.__name__)

        assert "init_cmd" in command_names
