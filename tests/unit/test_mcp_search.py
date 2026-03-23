"""Tests for MCP search index."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from sessionfs.mcp.search import SessionSearchIndex


@pytest.fixture
def search_index(tmp_path: Path) -> SessionSearchIndex:
    idx = SessionSearchIndex(tmp_path / "search.db")
    idx.initialize()
    return idx


@pytest.fixture
def sample_session(tmp_path: Path) -> Path:
    """Create a sample .sfs session for indexing."""
    d = tmp_path / "sessions" / "ses_test1234abcdef.sfs"
    d.mkdir(parents=True)

    manifest = {
        "sfs_version": "0.1.0",
        "session_id": "ses_test1234abcdef",
        "title": "Debug auth middleware",
        "created_at": "2026-03-20T10:00:00Z",
        "source": {"tool": "claude-code"},
        "model": {"model_id": "claude-opus-4-6"},
        "stats": {"message_count": 4},
    }
    (d / "manifest.json").write_text(json.dumps(manifest))

    messages = [
        {"role": "user", "content": [{"type": "text", "text": "The /api/users endpoint returns 401 unauthorized"}]},
        {"role": "assistant", "content": [{"type": "text", "text": "I'll check the auth middleware in src/middleware/auth.ts"}]},
        {"role": "assistant", "content": [{"type": "tool_use", "name": "Bash", "input": {"command": "cat src/middleware/auth.ts"}}]},
        {"role": "assistant", "content": [{"type": "text", "text": "The JWT token expiry check has an off-by-one error on line 42."}]},
    ]
    with open(d / "messages.jsonl", "w") as f:
        for m in messages:
            f.write(json.dumps(m) + "\n")

    return d


@pytest.fixture
def second_session(tmp_path: Path) -> Path:
    """Create a second session about a different topic."""
    d = tmp_path / "sessions" / "ses_db1234migration.sfs"
    d.mkdir(parents=True)

    (d / "manifest.json").write_text(json.dumps({
        "sfs_version": "0.1.0",
        "session_id": "ses_db1234migration",
        "title": "Database migration for users table",
        "created_at": "2026-03-19T08:00:00Z",
        "source": {"tool": "codex"},
        "model": {"model_id": "gpt-4.1"},
        "stats": {"message_count": 3},
    }))

    messages = [
        {"role": "user", "content": [{"type": "text", "text": "Add a new column 'role' to the users table"}]},
        {"role": "assistant", "content": [{"type": "text", "text": "I'll create an alembic migration for /src/db/migrations/add_role.py"}]},
        {"role": "assistant", "content": [{"type": "tool_result", "content": "Error: relation \"users\" does not exist"}]},
    ]
    with open(d / "messages.jsonl", "w") as f:
        for m in messages:
            f.write(json.dumps(m) + "\n")

    return d


class TestIndexing:
    def test_index_session(self, search_index: SessionSearchIndex, sample_session: Path):
        search_index.index_session("ses_test1234abcdef", sample_session)
        assert search_index.is_indexed("ses_test1234abcdef")

    def test_not_indexed_by_default(self, search_index: SessionSearchIndex):
        assert not search_index.is_indexed("ses_nonexistent")

    def test_reindex_all(self, search_index: SessionSearchIndex, sample_session: Path, tmp_path: Path):
        count = search_index.reindex_all(tmp_path)
        assert count == 1
        assert search_index.is_indexed("ses_test1234abcdef")


class TestSearch:
    def test_keyword_search(self, search_index: SessionSearchIndex, sample_session: Path):
        search_index.index_session("ses_test1234abcdef", sample_session)
        results = search_index.search("401 unauthorized")
        assert len(results) >= 1
        assert results[0]["session_id"] == "ses_test1234abcdef"

    def test_search_by_title(self, search_index: SessionSearchIndex, sample_session: Path):
        search_index.index_session("ses_test1234abcdef", sample_session)
        results = search_index.search("auth middleware")
        assert len(results) >= 1

    def test_search_returns_excerpt(self, search_index: SessionSearchIndex, sample_session: Path):
        search_index.index_session("ses_test1234abcdef", sample_session)
        results = search_index.search("JWT token")
        assert len(results) >= 1
        assert results[0]["excerpt"]  # Should have a text snippet

    def test_search_no_results(self, search_index: SessionSearchIndex, sample_session: Path):
        search_index.index_session("ses_test1234abcdef", sample_session)
        results = search_index.search("kubernetes deployment")
        assert len(results) == 0

    def test_search_tool_filter(
        self, search_index: SessionSearchIndex, sample_session: Path, second_session: Path
    ):
        search_index.index_session("ses_test1234abcdef", sample_session)
        search_index.index_session("ses_db1234migration", second_session)

        # Search for "users" with tool filter
        all_results = search_index.search("users")
        codex_results = search_index.search("users", tool_filter="codex")

        assert len(all_results) >= 2
        assert all(r["source_tool"] == "codex" for r in codex_results)

    def test_search_max_results(self, search_index: SessionSearchIndex, sample_session: Path):
        search_index.index_session("ses_test1234abcdef", sample_session)
        results = search_index.search("auth", limit=1)
        assert len(results) <= 1

    def test_empty_query(self, search_index: SessionSearchIndex):
        results = search_index.search("")
        assert results == []


class TestFindByFile:
    def test_find_by_file_path(self, search_index: SessionSearchIndex, sample_session: Path):
        search_index.index_session("ses_test1234abcdef", sample_session)
        results = search_index.find_by_file("auth.ts")
        assert len(results) >= 1
        assert results[0]["session_id"] == "ses_test1234abcdef"

    def test_find_by_file_no_match(self, search_index: SessionSearchIndex, sample_session: Path):
        search_index.index_session("ses_test1234abcdef", sample_session)
        results = search_index.find_by_file("kubernetes.yaml")
        assert len(results) == 0


class TestFindByError:
    def test_find_by_error(self, search_index: SessionSearchIndex, second_session: Path):
        search_index.index_session("ses_db1234migration", second_session)
        results = search_index.find_by_error("relation does not exist")
        assert len(results) >= 1

    def test_find_by_error_no_match(self, search_index: SessionSearchIndex, second_session: Path):
        search_index.index_session("ses_db1234migration", second_session)
        results = search_index.find_by_error("segmentation fault")
        assert len(results) == 0
