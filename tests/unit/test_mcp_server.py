"""Tests for MCP server tool implementations."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from sessionfs.mcp import server as mcp_server
from sessionfs.mcp.search import SessionSearchIndex
from sessionfs.retrieval_audit import (
    collect_returned_refs,
    record_retrieval,
    sanitize_arguments,
)
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


class TestRetrievalAudit:
    @pytest.mark.asyncio
    async def test_records_and_reads_retrieval_log(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        await mcp_server._record_retrieval_for_tool(
            "get_context_section",
            {"slug": "architecture", "audit_session_id": "ses_audit"},
            {
                "slug": "architecture",
                "source_entries": [{"kb_entry_id": 42}],
            },
        )

        result = await mcp_server._handle_get_session_retrieval_log({
            "session_id": "ses_audit"
        })
        # tk_b3ee62c732c44594 Finding B: response shape must match
        # RetrievalAuditLogResponse on BOTH server-success and local-
        # fallback paths. This test exercises the local-fallback path
        # (no API key configured in mcp_env).
        assert result["count"] == 1
        assert set(result.keys()) == {
            "session_id", "retrieval_audit_id", "events", "count",
        }
        row = result["events"][0]
        assert row["tool_name"] == "get_context_section"
        assert row["arguments"]["slug"] == "architecture"
        assert row["returned_refs"]["slugs"] == ["architecture"]
        assert row["returned_refs"]["kb_entry_ids"] == ["42"]
        # Local-fallback rows carry source="local" + null context_id so
        # consumers can tell server vs local apart.
        assert row["source"] == "local"
        assert row["context_id"] == ""
        assert row["id"] is None
        assert row["session_id"] == "ses_audit"

    @pytest.mark.asyncio
    async def test_retrieval_log_shape_matches_server_response(
        self, tmp_path, monkeypatch
    ):
        """Server-success path and local-fallback path must return the
        same top-level keys so agents can parse one schema regardless of
        which path fired. tk_b3ee62c732c44594 Finding B."""
        monkeypatch.setenv("HOME", str(tmp_path))

        # Make the API call route into the server branch by configuring
        # a fake api_key + url, then mock httpx to return the server
        # shape.
        from sessionfs.cli.common import load_config
        cfg = load_config()
        monkeypatch.setattr(cfg.sync, "api_key", "test-key", raising=False)
        monkeypatch.setattr(cfg.sync, "api_url", "https://api.test", raising=False)
        monkeypatch.setattr(mcp_server, "load_config", lambda: cfg)

        import httpx

        class _Resp:
            status_code = 200
            def json(self):
                return {
                    "session_id": "ses_x",
                    "retrieval_audit_id": "ra_abc",
                    "events": [{
                        "id": 1, "context_id": "ra_abc", "project_id": "p",
                        "session_id": "ses_x", "tool_name": "get_persona",
                        "arguments": {}, "returned_refs": {},
                        "source": "mcp", "caller_user_id": "u",
                        "created_at": "2026-05-15T00:00:00Z",
                    }],
                    "count": 1,
                }

        class _Client:
            def __init__(self, *a, **kw): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return False
            async def get(self, *a, **kw): return _Resp()

        monkeypatch.setattr(httpx, "AsyncClient", _Client)

        server_result = await mcp_server._handle_get_session_retrieval_log(
            {"session_id": "ses_x"}
        )
        server_keys = set(server_result.keys())

        # Now force the local-fallback path: empty api_key. Reset config.
        monkeypatch.setattr(cfg.sync, "api_key", "", raising=False)
        local_result = await mcp_server._handle_get_session_retrieval_log(
            {"session_id": "ses_x"}
        )
        local_keys = set(local_result.keys())

        # Top-level keys must match. The actual key set is
        # {session_id, retrieval_audit_id, events, count}.
        assert server_keys == local_keys == {
            "session_id", "retrieval_audit_id", "events", "count",
        }

    @pytest.mark.asyncio
    async def test_retrieval_log_rejects_path_traversal_session_id(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        result = await mcp_server._handle_get_session_retrieval_log({
            "session_id": "../../../etc/passwd"
        })
        assert "error" in result
        assert not (tmp_path / ".sessionfs" / "retrieval_logs").exists()

    def test_record_retrieval_rejects_unsafe_ids(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        for value in ("..", "../x", "/tmp/x", "bad/id", ""):
            assert not record_retrieval(
                tool_name="get_context_section",
                args={"audit_session_id": value},
                result={"slug": "architecture"},
            )
        assert not (tmp_path / ".sessionfs" / "retrieval_logs").exists()

    def test_returned_refs_are_structural_only_and_depth_limited(self):
        result = {
            "content": "This page mentions KB 42 and Entry 7, but they are prose.",
            "source_entries": [{"kb_entry_id": 99}],
        }
        refs = collect_returned_refs(result)
        assert refs["kb_entry_ids"] == ["99"]

        nested: object = {"kb_entry_id": "too_deep"}
        for _ in range(60):
            nested = {"child": nested}
        assert collect_returned_refs(nested) == {}

    def test_sanitize_arguments_strips_secret_shaped_keys(self):
        sanitized = sanitize_arguments({
            "query": "rate limit",
            "git_remote": "github.com/acme/repo",
            "api_key": "k",
            "github_token": "t",
            "password": "p",
            "auth_header": "a",
            "credential_id": "c",
            "secret_name": "s",
        })
        assert sanitized == {"query": "rate limit"}


# ---------------------------------------------------------------------------
# v0.9.9.6 — Tier A read-side MCP tools (7 new tools)
# ---------------------------------------------------------------------------


class TestToolRegistryV0996:
    """Tier A read tools (v0.9.9.6) + dismiss_knowledge_entry (v0.9.9.7)
    + v0.10.1 Phase 4 persona/ticket tools (8) + Phase 8 agent workflow
    tools (6) + v0.10.2 AgentRun tools (3: create_agent_run,
    complete_agent_run, list_agent_runs) + v0.10.2 ticket-approval +
    session-ops tools (4: approve_ticket, checkpoint_session,
    list_checkpoints, fork_session) + retrieval audit log. Total: 44."""

    def test_tool_count_is_45(self):
        from sessionfs.mcp.server import _TOOLS
        assert len(_TOOLS) == 45, (
            f"Expected 45 MCP tools after get_wiki_page_history, got {len(_TOOLS)}"
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
            # v0.10.2 ticket approval + session ops
            "approve_ticket",
            "checkpoint_session",
            "list_checkpoints",
            "fork_session",
            "get_session_retrieval_log",
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

    @pytest.mark.asyncio
    async def test_complete_ticket_only_unlinks_own_bundle(
        self, fake_resolver, fake_httpx, monkeypatch, tmp_path
    ):
        """KB 332 LOW: complete_ticket must only remove the local
        active_ticket.json bundle if it points at the ticket we just
        completed. If another tool started a different ticket since, its
        bundle must survive.
        """
        bundle_path = tmp_path / "active_ticket.json"
        bundle_path.write_text(json.dumps({
            "ticket_id": "tk_OTHER",
            "persona_name": "atlas",
            "project_id": "proj_test",
            "started_at": "2026-05-13T00:00:00Z",
        }))
        from sessionfs import active_ticket as _at
        monkeypatch.setattr(_at, "bundle_path", lambda: bundle_path)

        result = await mcp_server._handle_complete_ticket({
            "ticket_id": "tk_ME",
            "notes": "done",
        })
        assert "error" not in result
        # Bundle for the OTHER ticket must still exist.
        assert bundle_path.exists()
        assert json.loads(bundle_path.read_text())["ticket_id"] == "tk_OTHER"

    @pytest.mark.asyncio
    async def test_complete_ticket_unlinks_own_bundle(
        self, fake_resolver, fake_httpx, monkeypatch, tmp_path
    ):
        """When the bundle does point at this ticket, complete_ticket
        removes it so subsequent sessions are no longer attributed."""
        bundle_path = tmp_path / "active_ticket.json"
        bundle_path.write_text(json.dumps({
            "ticket_id": "tk_ME",
            "persona_name": "atlas",
            "project_id": "proj_test",
            "started_at": "2026-05-13T00:00:00Z",
        }))
        from sessionfs import active_ticket as _at
        monkeypatch.setattr(_at, "bundle_path", lambda: bundle_path)

        result = await mcp_server._handle_complete_ticket({
            "ticket_id": "tk_ME",
            "notes": "done",
        })
        assert "error" not in result
        assert not bundle_path.exists()

    @pytest.mark.asyncio
    async def test_complete_ticket_preserves_bundle_from_other_project(
        self, fake_resolver, fake_httpx, monkeypatch, tmp_path
    ):
        """Same ticket_id in a different project must not trigger unlink.
        Belt + suspenders against id collisions across projects.
        """
        bundle_path = tmp_path / "active_ticket.json"
        bundle_path.write_text(json.dumps({
            "ticket_id": "tk_ME",
            "persona_name": "atlas",
            "project_id": "proj_OTHER",  # different project
            "started_at": "2026-05-13T00:00:00Z",
        }))
        from sessionfs import active_ticket as _at
        monkeypatch.setattr(_at, "bundle_path", lambda: bundle_path)

        await mcp_server._handle_complete_ticket({
            "ticket_id": "tk_ME",
            "notes": "done",
        })
        assert bundle_path.exists()

    @pytest.mark.asyncio
    async def test_start_ticket_surfaces_bundle_write_failure(
        self, fake_resolver, monkeypatch, tmp_path
    ):
        """KB 339 LOW: when write_bundle returns False, start_ticket's
        payload must include a `provenance_warning` so the agent knows
        the daemon won't tag subsequent sessions."""
        import httpx

        # Fake start_ticket API response.
        class _Resp:
            status_code = 200
            text = "{}"
            def json(self):
                return {
                    "ticket": {"id": "tk_x", "assigned_to": "atlas"},
                    "compiled_context": "...",
                }

        class _FakeClient:
            def __init__(self, *a, **k): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return False
            async def post(self, url, *, headers=None, params=None, json=None):
                return _Resp()

        monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)

        # Force write_bundle to fail by pointing at an unwritable path.
        from sessionfs import active_ticket as _at
        bogus = tmp_path / "blocker" / "active.json"
        (tmp_path / "blocker").write_text("not a dir")
        monkeypatch.setattr(_at, "bundle_path", lambda: bogus)

        result = await mcp_server._handle_start_ticket({"ticket_id": "tk_x"})
        assert "provenance_warning" in result
        assert "Could not write" in result["provenance_warning"]

    # ── v0.10.1 Phase 8 — agent workflow handlers ──

    @pytest.mark.asyncio
    async def test_create_persona_requires_name_and_role(self, fake_resolver):
        assert "error" in await mcp_server._handle_create_persona({})
        assert "error" in await mcp_server._handle_create_persona({"name": "atlas"})
        assert "error" in await mcp_server._handle_create_persona({"role": "Backend"})

    @pytest.mark.asyncio
    async def test_create_persona_posts_payload(
        self, fake_resolver, fake_httpx, captured
    ):
        await mcp_server._handle_create_persona({
            "name": "atlas",
            "role": "Backend Architect",
            "content": "# Atlas\n\nBackend focus.",
            "specializations": ["backend", "api"],
        })
        assert captured["method"] == "POST"
        assert captured["url"] == "https://api.test/api/v1/projects/proj_test/personas"
        assert captured["json"]["name"] == "atlas"
        assert captured["json"]["role"] == "Backend Architect"
        assert captured["json"]["specializations"] == ["backend", "api"]

    @pytest.mark.asyncio
    async def test_assign_persona_sends_put(self, fake_resolver, fake_httpx, captured):
        await mcp_server._handle_assign_persona({
            "ticket_id": "tk_42",
            "persona_name": "atlas",
        })
        assert captured["method"] == "PUT"
        assert captured["url"] == "https://api.test/api/v1/projects/proj_test/tickets/tk_42"
        assert captured["json"] == {"assigned_to": "atlas"}

    @pytest.mark.asyncio
    async def test_assume_persona_writes_persona_only_bundle(
        self, fake_resolver, fake_httpx, monkeypatch, tmp_path
    ):
        """KB Phase 8: assume_persona writes the bundle with ticket_id=None
        so the daemon tags subsequent sessions with persona only."""
        bundle = tmp_path / "active.json"
        from sessionfs import active_ticket as _at
        monkeypatch.setattr(_at, "bundle_path", lambda: bundle)

        result = await mcp_server._handle_assume_persona({"name": "atlas"})
        assert "error" not in result
        assert bundle.exists()
        data = json.loads(bundle.read_text())
        assert data["ticket_id"] is None
        assert data["persona_name"] == "atlas"
        assert data["project_id"] == "proj_test"

    def test_forget_persona_clears_bundle(self, monkeypatch, tmp_path):
        bundle = tmp_path / "active.json"
        bundle.write_text(json.dumps({
            "ticket_id": None,
            "persona_name": "atlas",
            "project_id": "proj_test",
            "started_at": "2026-05-13T00:00:00Z",
        }))
        from sessionfs import active_ticket as _at
        monkeypatch.setattr(_at, "bundle_path", lambda: bundle)

        result = mcp_server._handle_forget_persona({})
        assert result["cleared"] is True
        assert not bundle.exists()

    def test_forget_persona_when_no_bundle(self, monkeypatch, tmp_path):
        from sessionfs import active_ticket as _at
        monkeypatch.setattr(_at, "bundle_path", lambda: tmp_path / "missing.json")
        result = mcp_server._handle_forget_persona({})
        assert result["cleared"] is False

    @pytest.mark.asyncio
    async def test_resolve_ticket_hits_accept_endpoint(
        self, fake_resolver, fake_httpx, captured
    ):
        await mcp_server._handle_resolve_ticket({"ticket_id": "tk_42"})
        assert captured["method"] == "POST"
        assert captured["url"] == "https://api.test/api/v1/projects/proj_test/tickets/tk_42/accept"

    @pytest.mark.asyncio
    async def test_escalate_ticket_bumps_priority(self, fake_resolver, monkeypatch):
        """escalate_ticket reads current priority then PUTs the next level."""
        import httpx

        seen: list[tuple[str, str, dict | None]] = []

        class _Resp:
            def __init__(self, body, status=200):
                self.status_code = status
                self._body = body
                self.text = json.dumps(body)
            def json(self):
                return self._body

        class _FakeClient:
            def __init__(self, *a, **k): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return False
            async def get(self, url, *, headers=None, params=None):
                seen.append(("GET", url, None))
                return _Resp({"id": "tk_42", "priority": "medium"})
            async def put(self, url, *, json=None, headers=None):
                seen.append(("PUT", url, json))
                return _Resp({"id": "tk_42", "priority": json["priority"]})
            async def post(self, url, *, json=None, headers=None):
                seen.append(("POST", url, json))
                return _Resp({"id": "c"})

        monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)

        result = await mcp_server._handle_escalate_ticket({
            "ticket_id": "tk_42",
            "reason": "Customer-facing outage",
        })
        # First GET to read current priority.
        assert seen[0][0] == "GET"
        # Second call is PUT with the bumped priority.
        assert seen[1][0] == "PUT"
        assert seen[1][2] == {"priority": "high"}
        # Third call is the audit comment.
        assert seen[2][0] == "POST"
        assert "Escalated medium → high" in seen[2][2]["content"]
        assert result["escalated_from"] == "medium"
        assert result["escalated_to"] == "high"

    @pytest.mark.asyncio
    async def test_escalate_ticket_noop_when_critical(self, fake_resolver, monkeypatch):
        """KB 352 LOW — already-critical ticket returns a non-error no-op
        payload that matches the tool description and the CLI's exit-0
        semantics."""
        import httpx

        class _Resp:
            status_code = 200
            text = "{}"
            def json(self): return {"id": "tk_42", "priority": "critical"}

        class _FakeClient:
            def __init__(self, *a, **k): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return False
            async def get(self, url, *, headers=None, params=None): return _Resp()

        monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)
        result = await mcp_server._handle_escalate_ticket({"ticket_id": "tk_42"})
        assert "error" not in result
        assert result["escalated"] is False
        assert result["priority"] == "critical"
        assert result["ticket_id"] == "tk_42"

    @pytest.mark.asyncio
    async def test_escalate_ticket_audit_comment_failure_surfaces(
        self, fake_resolver, monkeypatch
    ):
        """KB 352 LOW — when the audit-comment POST returns 4xx/5xx, the
        priority bump stands but the response carries `comment_warning`
        so the caller knows the rationale wasn't recorded."""
        import httpx

        class _Resp:
            def __init__(self, body, status=200):
                self.status_code = status
                self._body = body
                self.text = json.dumps(body) if not isinstance(body, str) else body
            def json(self):
                if isinstance(self._body, str):
                    return json.loads(self._body)
                return self._body

        class _FakeClient:
            def __init__(self, *a, **k): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return False
            async def get(self, url, *, headers=None, params=None):
                return _Resp({"id": "tk_42", "priority": "medium"})
            async def put(self, url, *, json=None, headers=None):
                return _Resp({"id": "tk_42", "priority": json["priority"]})
            async def post(self, url, *, json=None, headers=None):
                return _Resp("comment rejected", status=500)

        monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)

        result = await mcp_server._handle_escalate_ticket({
            "ticket_id": "tk_42",
            "reason": "P0 incident",
        })
        assert result["escalated"] is True
        assert result["escalated_to"] == "high"
        assert "comment_warning" in result
        assert "500" in result["comment_warning"]

    def test_forget_persona_refuses_to_clear_ticket_bundle(
        self, monkeypatch, tmp_path
    ):
        """KB 352 MEDIUM — `forget_persona` must NOT clear a ticket-tagged
        bundle. The right path for that is `complete_ticket` (ownership
        check). The user/agent gets a structured refusal pointing them at
        the correct tool."""
        bundle = tmp_path / "active.json"
        bundle.write_text(json.dumps({
            "ticket_id": "tk_42",
            "persona_name": "atlas",
            "project_id": "proj_test",
            "started_at": "2026-05-13T00:00:00Z",
        }))
        from sessionfs import active_ticket as _at
        monkeypatch.setattr(_at, "bundle_path", lambda: bundle)

        result = mcp_server._handle_forget_persona({})
        assert result["cleared"] is False
        assert "complete_ticket" in result.get("error", "")
        # Bundle survives.
        assert bundle.exists()


# ---------------------------------------------------------------------------
# v0.10.2 — ticket approval + local session ops
# ---------------------------------------------------------------------------


class TestApproveTicketDispatch:
    """approve_ticket must POST to the /approve endpoint and surface
    409 (wrong-state) as a readable error."""

    @pytest.mark.asyncio
    async def test_dispatch_approve_ticket_posts_to_approve_endpoint(self, monkeypatch):
        captured = {}

        async def _fake_resolve(_git_remote: str = ""):
            return ("https://api.test", "test-key", "proj_test")
        monkeypatch.setattr(mcp_server, "_resolve_project_id", _fake_resolve)

        import httpx

        class _Resp:
            def __init__(self, code, body):
                self.status_code = code
                self._body = body
                self.text = json.dumps(body)
            def json(self):
                return self._body

        class _Client:
            def __init__(self, *a, **kw): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return False
            async def post(self, url, *, headers=None, json=None):
                captured["url"] = url
                captured["method"] = "POST"
                captured["headers"] = headers or {}
                return _Resp(200, {"id": "tk_x", "status": "open"})

        monkeypatch.setattr(httpx, "AsyncClient", _Client)

        result = await mcp_server._handle_approve_ticket({"ticket_id": "tk_x"})
        assert captured["url"] == "https://api.test/api/v1/projects/proj_test/tickets/tk_x/approve"
        assert captured["method"] == "POST"
        assert result["status"] == "open"

    @pytest.mark.asyncio
    async def test_approve_ticket_requires_ticket_id(self):
        result = await mcp_server._handle_approve_ticket({})
        assert "error" in result and "ticket_id" in result["error"]

    @pytest.mark.asyncio
    async def test_approve_ticket_409_surfaces_status_conflict(self, monkeypatch):
        async def _fake_resolve(_git_remote: str = ""):
            return ("https://api.test", "test-key", "proj_test")
        monkeypatch.setattr(mcp_server, "_resolve_project_id", _fake_resolve)

        import httpx

        class _Resp:
            def __init__(self, code, body):
                self.status_code = code
                self._body = body
                self.text = json.dumps(body)
            def json(self): return self._body

        class _Client:
            def __init__(self, *a, **kw): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return False
            async def post(self, url, *, headers=None, json=None):
                return _Resp(409, {"detail": "Cannot move from in_progress to open"})

        monkeypatch.setattr(httpx, "AsyncClient", _Client)
        result = await mcp_server._handle_approve_ticket({"ticket_id": "tk_x"})
        assert "error" in result and "suggested" in result["error"]


class TestCheckpointAndForkHandlers:
    """Local session-op MCP handlers operate on ~/.sessionfs via the
    shared `session_ops` helpers. Uses the `mcp_env` fixture which
    spins up a temp store with two real sessions."""

    def test_checkpoint_creates_named_snapshot(self, mcp_env):
        result = mcp_server._handle_checkpoint_session(
            {"session_id": "ses_auth1234abcdef", "name": "before-fix"}
        )
        assert result["name"] == "before-fix"
        assert result["session_id"] == "ses_auth1234abcdef"
        assert Path(result["path"]).is_dir()
        # Manifest copied; messages copied
        assert (Path(result["path"]) / "manifest.json").exists()
        assert (Path(result["path"]) / "messages.jsonl").exists()
        assert result["has_messages"] is True

    def test_checkpoint_rejects_invalid_name(self, mcp_env):
        result = mcp_server._handle_checkpoint_session(
            {"session_id": "ses_auth1234abcdef", "name": "../escape"}
        )
        assert "error" in result and "Checkpoint name" in result["error"]

    def test_checkpoint_rejects_duplicate_name(self, mcp_env):
        mcp_server._handle_checkpoint_session(
            {"session_id": "ses_auth1234abcdef", "name": "v1"}
        )
        result = mcp_server._handle_checkpoint_session(
            {"session_id": "ses_auth1234abcdef", "name": "v1"}
        )
        assert "error" in result and "already exists" in result["error"]

    def test_checkpoint_unknown_session(self, mcp_env):
        result = mcp_server._handle_checkpoint_session(
            {"session_id": "ses_doesnotexist000", "name": "v1"}
        )
        assert "error" in result and "not found" in result["error"]

    def test_list_checkpoints_returns_recent_snapshots(self, mcp_env):
        mcp_server._handle_checkpoint_session(
            {"session_id": "ses_auth1234abcdef", "name": "alpha"}
        )
        mcp_server._handle_checkpoint_session(
            {"session_id": "ses_auth1234abcdef", "name": "beta"}
        )
        result = mcp_server._handle_list_checkpoints(
            {"session_id": "ses_auth1234abcdef"}
        )
        assert result["session_id"] == "ses_auth1234abcdef"
        names = [cp["name"] for cp in result["checkpoints"]]
        assert names == ["alpha", "beta"]  # ascending by mtime
        # message_count reflects the seeded jsonl with 2 messages
        assert all(cp["message_count"] == 2 for cp in result["checkpoints"])

    def test_list_checkpoints_empty(self, mcp_env):
        result = mcp_server._handle_list_checkpoints(
            {"session_id": "ses_dbmigrate1234ab"}
        )
        assert result["checkpoints"] == []

    def test_fork_session_creates_new_session(self, mcp_env):
        result = mcp_server._handle_fork_session(
            {"session_id": "ses_auth1234abcdef", "name": "Auth retry path"}
        )
        new_id = result["session_id"]
        assert new_id != "ses_auth1234abcdef"
        assert result["parent_session_id"] == "ses_auth1234abcdef"
        assert result["forked_from_checkpoint"] is None
        # Messages were copied
        assert (Path(result["path"]) / "messages.jsonl").exists()
        # Manifest reflects new id + title + parent linkage
        manifest = json.loads((Path(result["path"]) / "manifest.json").read_text())
        assert manifest["session_id"] == new_id
        assert manifest["title"] == "Auth retry path"
        assert manifest["parent_session_id"] == "ses_auth1234abcdef"
        # Indexed
        store, _ = mcp_env
        assert store.get_session_metadata(new_id) is not None

    def test_fork_from_checkpoint(self, mcp_env):
        mcp_server._handle_checkpoint_session(
            {"session_id": "ses_auth1234abcdef", "name": "snapshot1"}
        )
        result = mcp_server._handle_fork_session({
            "session_id": "ses_auth1234abcdef",
            "name": "After snapshot1",
            "from_checkpoint": "snapshot1",
        })
        assert result["forked_from_checkpoint"] == "snapshot1"
        manifest = json.loads((Path(result["path"]) / "manifest.json").read_text())
        assert manifest["forked_from_checkpoint"] == "snapshot1"

    def test_fork_rejects_missing_checkpoint(self, mcp_env):
        result = mcp_server._handle_fork_session({
            "session_id": "ses_auth1234abcdef",
            "name": "x",
            "from_checkpoint": "nope",
        })
        assert "error" in result and "not found" in result["error"]

    def test_fork_requires_name(self, mcp_env):
        result = mcp_server._handle_fork_session(
            {"session_id": "ses_auth1234abcdef"}
        )
        assert "error" in result and "name" in result["error"]

    def test_fork_accepts_session_id_prefix(self, mcp_env):
        result = mcp_server._handle_fork_session(
            {"session_id": "ses_auth1234", "name": "via prefix"}
        )
        assert result["parent_session_id"] == "ses_auth1234abcdef"


class TestAskProjectSourcesCited:
    """v0.10.7 — ask_project returns structured sources_cited so SoD /
    audit callers can trace which entities shaped the assembled research
    material. KB entries come from the search step; session IDs come
    from the local session-index match."""

    @pytest.mark.asyncio
    async def test_returns_kb_and_session_sources(self, tmp_path, monkeypatch):
        """KB entries returned by search → {type:kb,id:int}, local
        session matches → {type:session,id:str}. No regex-extraction
        from the markdown — IDs come from the structured fetch path."""
        monkeypatch.setenv("HOME", str(tmp_path))

        from sessionfs.cli.common import load_config

        cfg = load_config()
        monkeypatch.setattr(cfg.sync, "api_key", "test-key", raising=False)
        monkeypatch.setattr(cfg.sync, "api_url", "https://api.test", raising=False)
        monkeypatch.setattr(mcp_server, "load_config", lambda: cfg)
        monkeypatch.setattr(
            mcp_server, "_resolve_workspace_git_remote",
            lambda: _async_value("git@github.com:acme/repo.git"),
        )

        # Mock httpx so the project lookup + entries search return our
        # fixture KB entries. Two project-scoped current-claim entries.
        import httpx

        class _ProjResp:
            status_code = 200
            def json(self):
                return {"id": "proj_abc", "name": "acme"}

        class _EntriesResp:
            status_code = 200
            def json(self):
                return [
                    {
                        "id": 42, "content": "auth uses JWT", "entry_type": "pattern",
                        "claim_class": "claim", "freshness_class": "current",
                        "superseded_by": None, "dismissed": False,
                        "session_id": "manual", "created_at": "2026-05-15T00:00:00Z",
                        "confidence": 0.8,
                    },
                    {
                        "id": 89, "content": "PBKDF2 share-link passwords", "entry_type": "decision",
                        "claim_class": "claim", "freshness_class": "current",
                        "superseded_by": None, "dismissed": False,
                        "session_id": "manual", "created_at": "2026-05-15T00:00:00Z",
                        "confidence": 0.9,
                    },
                ]

        class _Client:
            def __init__(self, *a, **kw): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return False
            async def get(self, url, *a, **kw):
                if "/entries" in url:
                    return _EntriesResp()
                return _ProjResp()

        monkeypatch.setattr(httpx, "AsyncClient", _Client)

        # Also short-circuit get_project_context so it doesn't make
        # additional HTTP calls — return a constant string.
        async def _fake_ctx(_args):
            return "Project context: acme."
        monkeypatch.setattr(mcp_server, "_handle_get_project_context", _fake_ctx)

        # Wire a local session index so the session source path fires.
        store_dir = tmp_path / ".sessionfs"
        store = LocalStore(store_dir)
        store.initialize()
        d = store.allocate_session_dir("ses_local12345678")
        manifest = {
            "sfs_version": "0.1.0", "session_id": "ses_local12345678",
            "title": "Auth debug", "created_at": "2026-05-15T00:00:00Z",
            "updated_at": "2026-05-15T00:00:00Z",
            "source": {"tool": "claude-code"}, "model": {"model_id": "claude-opus-4-6"},
            "stats": {"message_count": 1},
        }
        (d / "manifest.json").write_text(json.dumps(manifest))
        with open(d / "messages.jsonl", "w") as f:
            f.write(json.dumps({"role": "user", "content": [{"type": "text", "text": "auth JWT debugging"}]}) + "\n")
        store.upsert_session_metadata("ses_local12345678", manifest, str(d))
        search = SessionSearchIndex(store_dir / "search.db")
        search.initialize()
        search.reindex_all(store_dir)
        mcp_server._store = store
        mcp_server._search = search

        try:
            result = await mcp_server._handle_ask_project(
                {"question": "auth JWT"}
            )
        finally:
            store.close()
            search.close()
            mcp_server._store = None
            mcp_server._search = None

        assert isinstance(result, dict)
        assert "markdown" in result and "sources_cited" in result
        assert isinstance(result["sources_cited"], list)
        kb_sources = [s for s in result["sources_cited"] if s["type"] == "kb"]
        session_sources = [s for s in result["sources_cited"] if s["type"] == "session"]
        assert {s["id"] for s in kb_sources} == {42, 89}
        assert "ses_local12345678" in {s["id"] for s in session_sources}

    @pytest.mark.asyncio
    async def test_empty_question_returns_empty_sources(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        result = await mcp_server._handle_ask_project({"question": ""})
        assert result["sources_cited"] == []

    @pytest.mark.asyncio
    async def test_no_auth_degrades_gracefully(self, tmp_path, monkeypatch):
        """Without an API key, KB fetch returns []. ask_project still
        succeeds (degraded) with empty sources_cited rather than raising."""
        monkeypatch.setenv("HOME", str(tmp_path))

        from sessionfs.cli.common import load_config

        cfg = load_config()
        monkeypatch.setattr(cfg.sync, "api_key", "", raising=False)
        monkeypatch.setattr(mcp_server, "load_config", lambda: cfg)
        monkeypatch.setattr(
            mcp_server, "_resolve_workspace_git_remote",
            lambda: _async_value("git@github.com:acme/repo.git"),
        )

        async def _fake_ctx(_args):
            return "ctx"
        monkeypatch.setattr(mcp_server, "_handle_get_project_context", _fake_ctx)

        mcp_server._search = None  # No local index → no session sources

        result = await mcp_server._handle_ask_project(
            {"question": "any question"}
        )
        assert isinstance(result, dict)
        assert result["sources_cited"] == []


async def _async_value(value):
    """Helper: coerce a sync value into an awaitable used by
    monkeypatched async functions."""
    return value
