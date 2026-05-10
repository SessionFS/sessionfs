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


# ---------------------------------------------------------------------------
# v0.9.9.6 — Tier A read-side MCP tools (7 new tools)
# ---------------------------------------------------------------------------


class TestToolRegistryV0996:
    """Tier A read tools (v0.9.9.6) + dismiss_knowledge_entry (v0.9.9.7).
    Tool count totals 22."""

    def test_tool_count_is_22(self):
        from sessionfs.mcp.server import _TOOLS
        assert len(_TOOLS) == 22, (
            f"Expected 22 MCP tools after v0.9.9.7 dismiss tool, got {len(_TOOLS)}"
        )

    def test_new_tools_registered(self):
        from sessionfs.mcp.server import _TOOLS

        names = {t.name for t in _TOOLS}
        for new_tool in (
            # v0.9.9.6 Tier A
            "get_knowledge_entry",
            "list_knowledge_entries",
            "get_wiki_page",
            "get_knowledge_health",
            "get_context_section",
            "get_session_provenance",
            "compile_knowledge_base",
            # v0.9.9.7 audited write
            "dismiss_knowledge_entry",
        ):
            assert new_tool in names, f"Missing MCP tool: {new_tool}"

    def test_new_tool_descriptions_include_mcp_over_cli_guidance(self):
        """Brief mandates each new tool's description must steer agents
        away from `sfs ...` shell-outs."""
        from sessionfs.mcp.server import _TOOLS

        new_tool_names = {
            "get_knowledge_entry",
            "list_knowledge_entries",
            "get_wiki_page",
            "get_knowledge_health",
            "get_context_section",
            "get_session_provenance",
            "compile_knowledge_base",
            "dismiss_knowledge_entry",
        }
        for tool in _TOOLS:
            if tool.name in new_tool_names:
                assert "instead of running" in tool.description, (
                    f"{tool.name} description missing MCP-over-CLI guidance"
                )

    def test_list_knowledge_entries_filter_schema(self):
        """list_knowledge_entries must expose all 4 new filters + sort + pagination."""
        from sessionfs.mcp.server import _TOOLS

        tool = next(t for t in _TOOLS if t.name == "list_knowledge_entries")
        props = tool.inputSchema["properties"]
        for field in (
            "claim_class",
            "freshness_class",
            "dismissed",
            "session_id",
            "sort",
            "page",
            "limit",
        ):
            assert field in props, f"list_knowledge_entries missing param: {field}"


class TestNewToolDispatch:
    """Each new tool routes to the right URL with the right params.

    We don't run the API — we monkeypatch httpx.AsyncClient and
    _resolve_project_id so the tests are pure unit-level. This is how
    the brief asks us to verify dispatch.
    """

    @pytest.fixture
    def fake_resolver(self, monkeypatch):
        """Patch _resolve_project_id so handlers don't try to hit the network."""
        async def _fake(_git_remote: str = ""):
            return ("https://api.test", "test-key", "proj_test")

        monkeypatch.setattr(mcp_server, "_resolve_project_id", _fake)

    @pytest.fixture
    def captured(self):
        """Holds the URL + params the handler hit."""
        return {}

    @pytest.fixture
    def fake_httpx(self, monkeypatch, captured):
        """Patch httpx.AsyncClient so every request is captured + faked."""
        import httpx

        class _FakeResponse:
            def __init__(self, status_code: int, body, headers=None):
                self.status_code = status_code
                self._body = body
                self.text = json.dumps(body) if not isinstance(body, str) else body
                self.headers = headers or {}

            def json(self):
                if isinstance(self._body, str):
                    return json.loads(self._body)
                return self._body

        class _FakeAsyncClient:
            def __init__(self, *args, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            async def get(self, url, *, params=None, headers=None):
                captured["method"] = "GET"
                captured["url"] = url
                captured["params"] = params or {}
                captured["headers"] = headers or {}
                # Default body shaped like the API response
                if "/entries/" in url and url.rsplit("/", 1)[-1].isdigit():
                    return _FakeResponse(200, {"id": 42, "content": "..."})
                if url.endswith("/entries"):
                    return _FakeResponse(200, [])
                if "/pages/" in url:
                    return _FakeResponse(200, {"slug": "x", "content": "..."})
                if url.endswith("/health"):
                    return _FakeResponse(200, {"pending_entries": 0})
                if "/context/sections/" in url:
                    return _FakeResponse(200, {"slug": "x", "title": "X", "content": "..."})
                if "/provenance" in url:
                    return _FakeResponse(200, {
                        "session_id": "ses_x", "rules_version": None,
                        "rules_hash": None, "rules_source": None,
                        "instruction_artifacts": [],
                    })
                return _FakeResponse(200, {})

            async def post(self, url, *, json=None, headers=None):
                captured["method"] = "POST"
                captured["url"] = url
                captured["json"] = json
                captured["headers"] = headers or {}
                return _FakeResponse(200, {
                    "id": 1, "project_id": "proj_test", "user_id": "u",
                    "entries_compiled": 0, "compiled_at": "2026-05-10T00:00:00Z",
                    "context_words_before": 0, "context_words_after": 0,
                    "section_pages_updated": 0, "concept_pages_updated": 0,
                })

            async def put(self, url, *, json=None, headers=None):
                captured["method"] = "PUT"
                captured["url"] = url
                captured["json"] = json
                captured["headers"] = headers or {}
                # Shape mirrors KnowledgeEntryResponse including the
                # v0.9.9.7 audit triple.
                return _FakeResponse(200, {
                    "id": 42,
                    "project_id": "proj_test",
                    "session_id": "ses_x",
                    "user_id": "u",
                    "entry_type": "decision",
                    "content": "...",
                    "confidence": 0.8,
                    "created_at": "2026-05-10T00:00:00Z",
                    "dismissed": True,
                    "dismissed_at": "2026-05-10T00:00:00Z",
                    "dismissed_by": "u",
                    "dismissed_reason": "stale",
                })

        monkeypatch.setattr(httpx, "AsyncClient", _FakeAsyncClient)

    @pytest.mark.asyncio
    async def test_dispatch_get_knowledge_entry(self, fake_resolver, fake_httpx, captured):
        result = await mcp_server._handle_get_knowledge_entry({"id": 42})
        assert captured["url"] == "https://api.test/api/v1/projects/proj_test/entries/42"
        assert captured["method"] == "GET"
        assert result.get("id") == 42

    @pytest.mark.asyncio
    async def test_dispatch_list_knowledge_entries(self, fake_resolver, fake_httpx, captured):
        result = await mcp_server._handle_list_knowledge_entries({
            "entry_type": "decision",
            "claim_class": "claim",
            "freshness_class": "current",
            "dismissed": False,
            "session_id": "ses_abc",
            "sort": "confidence_desc",
            "page": 2,
            "limit": 25,
        })
        assert captured["url"] == "https://api.test/api/v1/projects/proj_test/entries"
        # The handler maps entry_type → type for the existing route param.
        assert captured["params"]["type"] == "decision"
        assert captured["params"]["claim_class"] == "claim"
        assert captured["params"]["freshness_class"] == "current"
        assert captured["params"]["dismissed"] == "false"
        assert captured["params"]["session_id"] == "ses_abc"
        assert captured["params"]["sort"] == "confidence_desc"
        assert captured["params"]["page"] == "2"
        assert captured["params"]["limit"] == "25"
        assert result["page"] == 2
        assert result["limit"] == 25
        assert result["sort"] == "confidence_desc"

    @pytest.mark.asyncio
    async def test_dispatch_get_wiki_page(self, fake_resolver, fake_httpx, captured):
        await mcp_server._handle_get_wiki_page({"slug": "architecture"})
        assert captured["url"] == "https://api.test/api/v1/projects/proj_test/pages/architecture"

    @pytest.mark.asyncio
    async def test_dispatch_get_wiki_page_requires_slug(self, fake_resolver, fake_httpx):
        result = await mcp_server._handle_get_wiki_page({})
        assert "error" in result
        assert "slug" in result["error"].lower()

    @pytest.mark.asyncio
    async def test_dispatch_get_knowledge_health(self, fake_resolver, fake_httpx, captured):
        await mcp_server._handle_get_knowledge_health({})
        assert captured["url"] == "https://api.test/api/v1/projects/proj_test/health"

    @pytest.mark.asyncio
    async def test_dispatch_get_context_section(self, fake_resolver, fake_httpx, captured):
        await mcp_server._handle_get_context_section({"slug": "team_workflow"})
        assert captured["url"] == (
            "https://api.test/api/v1/projects/proj_test/context/sections/team_workflow"
        )

    @pytest.mark.asyncio
    async def test_dispatch_get_session_provenance(self, monkeypatch, captured):
        """Provenance handler resolves auth from config, not project_id —
        sessions are user-scoped, not project-scoped."""
        import httpx
        from types import SimpleNamespace

        fake_config = SimpleNamespace(
            sync=SimpleNamespace(api_url="https://api.test", api_key="test-key"),
        )
        monkeypatch.setattr(mcp_server, "load_config", lambda: fake_config)

        class _Resp:
            status_code = 200
            text = "{}"

            def json(self):
                return {
                    "session_id": "ses_xyz",
                    "rules_version": 7,
                    "rules_hash": "abc123",
                    "rules_source": "canonical",
                    "instruction_artifacts": [],
                }

        class _FakeClient:
            def __init__(self, *a, **k):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def get(self, url, *, headers=None, params=None):
                captured["url"] = url
                return _Resp()

        monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)

        result = await mcp_server._handle_get_session_provenance({"session_id": "ses_xyz"})
        assert captured["url"] == "https://api.test/api/v1/sessions/ses_xyz/provenance"
        assert result["rules_version"] == 7

    @pytest.mark.asyncio
    async def test_dispatch_compile_knowledge_base(self, fake_resolver, fake_httpx, captured):
        result = await mcp_server._handle_compile_knowledge_base({})
        assert captured["url"] == "https://api.test/api/v1/projects/proj_test/compile"
        assert captured["method"] == "POST"
        # Compile returns the structured fields agents need to surface diffs
        assert "entries_compiled" in result
        assert "context_words_before" in result
        assert "context_words_after" in result
        assert "section_pages_updated" in result
        assert "concept_pages_updated" in result

    @pytest.mark.asyncio
    async def test_dispatch_dismiss_knowledge_entry(self, fake_resolver, fake_httpx, captured):
        result = await mcp_server._handle_dismiss_knowledge_entry({
            "id": 42,
            "reason": "stale",
        })
        assert captured["method"] == "PUT"
        assert captured["url"] == "https://api.test/api/v1/projects/proj_test/entries/42"
        assert captured["json"] == {"dismissed": True, "reason": "stale"}
        # Audit triple is surfaced in the response so agents can confirm
        # what was recorded — Codex round 1 finding (v0.9.9.7).
        assert result["dismissed"] is True
        assert result["dismissed_by"] == "u"
        assert result["dismissed_reason"] == "stale"
        assert result["dismissed_at"]

    @pytest.mark.asyncio
    async def test_dispatch_dismiss_knowledge_entry_undismiss(
        self, fake_resolver, fake_httpx, captured
    ):
        await mcp_server._handle_dismiss_knowledge_entry({
            "id": 42,
            "undismiss": True,
        })
        # When undismiss=True, body must send dismissed=False (no reason).
        assert captured["json"] == {"dismissed": False}

    @pytest.mark.asyncio
    async def test_dispatch_dismiss_knowledge_entry_validates_id(
        self, fake_resolver, fake_httpx
    ):
        result = await mcp_server._handle_dismiss_knowledge_entry({})
        assert "error" in result
        assert "positive integer" in result["error"]

    @pytest.mark.asyncio
    async def test_dispatch_dismiss_knowledge_entry_strips_blank_reason(
        self, fake_resolver, fake_httpx, captured
    ):
        """A whitespace-only reason should not land in the request body —
        the audit field would be useless."""
        await mcp_server._handle_dismiss_knowledge_entry({
            "id": 42,
            "reason": "   ",
        })
        assert "reason" not in captured["json"]
