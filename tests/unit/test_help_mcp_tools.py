"""The dashboard Help page lists every MCP tool from dashboard/src/help/mcpTools.json.

This test keeps that file in step with the tools the MCP server registers, so
adding or removing a tool fails here until the Help page is updated too.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

from sessionfs.mcp.server import _TOOLS

HELP_TOOLS_JSON = (
    Path(__file__).resolve().parents[2] / "dashboard" / "src" / "help" / "mcpTools.json"
)


def _help_entries() -> list[dict]:
    data = json.loads(HELP_TOOLS_JSON.read_text(encoding="utf-8"))
    entries: list[dict] = []
    for group in data["groups"]:
        assert group.get("id") and group.get("label"), f"group missing id/label: {group}"
        assert group.get("tools"), f"group {group['id']} has no tools"
        entries.extend(group["tools"])
    return entries


def test_help_page_lists_exactly_the_registered_mcp_tools():
    listed = [entry["name"] for entry in _help_entries()]
    registered = {tool.name for tool in _TOOLS}

    missing = sorted(registered - set(listed))
    extra = sorted(set(listed) - registered)
    assert not missing, (
        f"MCP tools missing from {HELP_TOOLS_JSON.name} (add them to the Help page): {missing}"
    )
    assert not extra, (
        f"{HELP_TOOLS_JSON.name} lists tools the MCP server no longer registers: {extra}"
    )


def test_help_page_lists_each_tool_once():
    dupes = [name for name, n in Counter(e["name"] for e in _help_entries()).items() if n > 1]
    assert not dupes, f"tools listed more than once on the Help page: {dupes}"


def test_every_help_entry_has_a_short_description():
    for entry in _help_entries():
        desc = entry.get("desc", "")
        assert desc.strip(), f"{entry['name']} has no description"
        assert len(desc) <= 100, f"{entry['name']} description is too long for the Help page"


def test_help_tool_group_ids_are_unique():
    groups = json.loads(HELP_TOOLS_JSON.read_text(encoding="utf-8"))["groups"]
    ids = [group["id"] for group in groups]
    assert len(ids) == len(set(ids)), ids
