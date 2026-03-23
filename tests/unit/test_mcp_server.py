"""Tests for MCP server tool implementations."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from sessionfs.mcp import server as mcp_server
from sessionfs.mcp.search import SessionSearchIndex
from sessionfs.store.local import LocalStore


@pytest.fixture
def mcp_env(tmp_path: Path):
    """Set up a store with sessions and initialize the MCP server state."""
    store_dir = tmp_path / ".sessionfs"
    store = LocalStore(store_dir)
    store.initialize()

    # Create two sessions
    for sid, title, tool, text in [
        ("ses_auth1234abcdef", "Debug auth flow", "claude-code", "The /api/users returns 401"),
        ("ses_dbmigrate1234ab", "DB migration", "codex", "ALTER TABLE users ADD COLUMN role"),
    ]:
        d = store.allocate_session_dir(sid)
        manifest = {
            "sfs_version": "0.1.0", "session_id": sid, "title": title,
            "created_at": "2026-03-20T10:00:00Z", "updated_at": "2026-03-20T10:05:00Z",
            "source": {"tool": tool}, "model": {"model_id": "claude-opus-4-6"},
            "stats": {"message_count": 2},
        }
        (d / "manifest.json").write_text(json.dumps(manifest))
        with open(d / "messages.jsonl", "w") as f:
            f.write(json.dumps({"role": "user", "content": [{"type": "text", "text": text}]}) + "\n")
            f.write(json.dumps({"role": "assistant", "content": [{"type": "text", "text": "I'll fix it."}]}) + "\n")
        store.upsert_session_metadata(sid, manifest, str(d))

    # Initialize search index
    search = SessionSearchIndex(store_dir / "search.db")
    search.initialize()
    search.reindex_all(store_dir)

    # Wire into MCP server module
    mcp_server._store = store
    mcp_server._search = search

    yield store, search

    store.close()
    search.close()
    mcp_server._store = None
    mcp_server._search = None


class TestSearchSessions:
    def test_search_returns_results(self, mcp_env):
        result = mcp_server._handle_search({"query": "401 auth"})
        assert result["count"] >= 1
        assert result["results"][0]["session_id"] == "ses_auth1234abcdef"

    def test_search_with_tool_filter(self, mcp_env):
        result = mcp_server._handle_search({"query": "users", "tool_filter": "codex"})
        assert all(r["source_tool"] == "codex" for r in result["results"])

    def test_search_empty(self, mcp_env):
        result = mcp_server._handle_search({"query": "kubernetes"})
        assert result["count"] == 0


class TestGetContext:
    def test_get_full_context(self, mcp_env):
        result = mcp_server._handle_get_context({"session_id": "ses_auth1234abcdef"})
        assert result["session_id"] == "ses_auth1234abcdef"
        assert result["title"] == "Debug auth flow"
        assert len(result["messages"]) == 2

    def test_get_summary_only(self, mcp_env):
        result = mcp_server._handle_get_context({
            "session_id": "ses_auth1234abcdef", "summary_only": True
        })
        assert "messages" not in result
        assert result["title"] == "Debug auth flow"

    def test_get_not_found(self, mcp_env):
        result = mcp_server._handle_get_context({"session_id": "ses_nonexistent1234"})
        assert "error" in result


class TestListRecent:
    def test_list_all(self, mcp_env):
        result = mcp_server._handle_list_recent({})
        assert result["count"] == 2

    def test_list_with_tool_filter(self, mcp_env):
        result = mcp_server._handle_list_recent({"tool_filter": "codex"})
        assert result["count"] == 1
        assert result["sessions"][0]["source_tool"] == "codex"

    def test_list_with_limit(self, mcp_env):
        result = mcp_server._handle_list_recent({"limit": 1})
        assert result["count"] == 1


class TestFindRelated:
    def test_find_by_error(self, mcp_env):
        result = mcp_server._handle_find_related({"error_text": "401"})
        assert result["count"] >= 1

    def test_find_requires_input(self, mcp_env):
        result = mcp_server._handle_find_related({})
        assert "error" in result
