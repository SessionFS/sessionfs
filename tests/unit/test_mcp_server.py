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

    def test_marketplace_loads_are_audited_as_skill_refs_not_personas(self):
        """R17 P2-4: a package payload records {package_id, version_id,
        content_hash} under skill_packages and never a persona ref."""
        result = {
            "package_id": "spk_1",
            "version_id": "skv_1",
            "content_hash": "sha256:" + "a" * 64,
            "name": "review-helper",
            "listing_id": "sml_1",
            "instruction_bundle": [{"path": "skills/review/SKILL.md", "content": "# x"}],
        }
        refs = collect_returned_refs(result)
        assert refs["skill_packages"] == [
            {"package_id": "spk_1", "version_id": "skv_1", "content_hash": "sha256:" + "a" * 64}
        ]
        assert "persona_names" not in refs
        # A persona payload is still a persona; a bare package_id without identity is not a skill ref.
        assert collect_returned_refs({"name": "atlas"})["persona_names"] == ["atlas"]
        assert "skill_packages" not in collect_returned_refs({"package_id": "spk_1", "name": "x"})
        # Nested under a list (search results) each package is recorded once.
        nested = {"items": [result, dict(result)]}
        assert len(collect_returned_refs(nested)["skill_packages"]) == 1

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
    tools (6) + v0.10.2 AgentRun tools (3) + v0.10.2 ticket-approval +
    session-ops tools (4) + retrieval audit log (44) + v0.10.7
    get_wiki_page_history (45) + v0.10.9 handoff lifecycle tools
    (create/claim/get/list_inbox/list_sent/revoke/decline/comment = 8).
    Total: 53."""

    def test_tool_count_is_83(self):
        from sessionfs.mcp.server import _TOOLS
        assert len(_TOOLS) == 83, (
            "Expected 83 MCP tools after ask_session_helper (session helper), "
            f"got {len(_TOOLS)}"
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
            # v0.10.9 handoff lifecycle (R3 LOW 1)
            "create_handoff",
            "claim_handoff",
            "get_handoff",
            "list_inbox_handoffs",
            "list_sent_handoffs",
            "revoke_handoff",
            "decline_handoff",
            "add_handoff_comment",
            # v0.10.10 ticket comment polling (tk_32f3dacf1c9749bc)
            "list_ticket_comments",
            # v0.10.10 KB write API gap (tk_a253102a20c148a9)
            "update_entry_confidence",
            "promote_entry",
            # v0.10.11 review-state summary (tk_e025375272b84a95)
            "get_ticket_review_state",
            # v0.10.12 bulk KB repair (tk_c64915570f4d4042)
            "promote_eligible_entries",
            # v0.10.23 session rename (tk_cf9f1691091d4e8e)
            "rename_session",
            # v0.10.24 persona MCP parity (tk_32abb6d0d4744c5d / GH #50)
            "update_persona",
            "delete_persona",
            # WQ-P2 work-queue management (tk_3481237f3b0847d6)
            "create_work_queue",
            "get_work_queue",
            "list_work_queues",
            "set_work_queue_status",
            # WQ-P3 step engine (tk_3de50bf7bb73418b)
            "run_work_queue_step",
            "complete_work_queue_step",
            # v0.16 hosted skill/plugin marketplace
            "search_skill_marketplace",
            "get_marketplace_package",
            "diff_marketplace_versions",
            "get_skill_policy",
            "preview_marketplace_skill",
            "load_marketplace_skill",
            "request_skill_install",
            "get_skill_install_request",
            "list_skill_install_requests",
            "cancel_skill_install_request",
            # v0.17.0 agent feedback (structured agent-work outcomes)
            "record_agent_feedback",
            "list_agent_feedback",
            "get_agent_feedback_summary",
            # GitHub #95 — replay an agent run with an edited prompt/ticket/context
            "replay_agent_run",
            # Session helper (Enterprise, BYOK) — non-streaming answer + citations
            "ask_session_helper",
        ):
            assert new_tool in names, f"Missing MCP tool: {new_tool}"
        # A3: approval is dashboard/CLI/API only — never brokered over MCP,
        # otherwise the same principal can preview -> request -> approve -> load.
        assert "approve_skill_install_request" not in names

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

            async def delete(self, url, *, params=None, headers=None):
                # v0.10.24 tk_32abb6d0d4744c5d — delete_persona dispatch
                # needs a DELETE method on the fake client. Most DELETE
                # routes in our API return 204 (no body), so the
                # default response shape is empty + status 204.
                captured["method"] = "DELETE"
                captured["url"] = url
                captured["params"] = params or {}
                captured["headers"] = headers or {}
                return _FakeResponse(204, None)

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

    # ── v0.17.0 agent-feedback dispatch ──

    @pytest.mark.asyncio
    async def test_dispatch_record_agent_feedback(self, fake_resolver, fake_httpx, captured):
        result = await mcp_server._handle_record_agent_feedback({
            "feedback_type": "route",
            "verdict": "negative",
            "label": "wrong_route",
            "ticket_id": "tk_1",
            "final_owner": "sentinel",
            "notes": "misrouted",
        })
        assert captured["method"] == "POST"
        assert captured["url"] == (
            "https://api.test/api/v1/projects/proj_test/agent-feedback"
        )
        assert captured["json"] == {
            "feedback_type": "route",
            "verdict": "negative",
            "label": "wrong_route",
            "ticket_id": "tk_1",
            "final_owner": "sentinel",
            "notes": "misrouted",
        }
        assert "error" not in result

    @pytest.mark.asyncio
    async def test_dispatch_record_agent_feedback_requires_target(
        self, fake_resolver, fake_httpx, captured
    ):
        result = await mcp_server._handle_record_agent_feedback({
            "feedback_type": "route",
            "verdict": "negative",
        })
        # Rejected locally BEFORE any project resolution / network call.
        assert "at least one target" in result["error"]
        assert not captured

    @pytest.mark.asyncio
    async def test_dispatch_record_agent_feedback_rejects_bad_type(
        self, fake_resolver, fake_httpx, captured
    ):
        result = await mcp_server._handle_record_agent_feedback({
            "feedback_type": "bogus",
            "verdict": "negative",
            "ticket_id": "tk_1",
        })
        assert "feedback_type" in result["error"]
        assert not captured

    @pytest.mark.asyncio
    async def test_dispatch_record_agent_feedback_rejects_label_mismatch(
        self, fake_resolver, fake_httpx, captured
    ):
        result = await mcp_server._handle_record_agent_feedback({
            "feedback_type": "duplicate",
            "verdict": "positive",
            "label": "wrong_route",
            "ticket_id": "tk_1",
        })
        assert "not valid for feedback_type" in result["error"]
        assert not captured

    @pytest.mark.asyncio
    async def test_dispatch_list_agent_feedback(self, fake_resolver, fake_httpx, captured):
        result = await mcp_server._handle_list_agent_feedback({
            "feedback_type": "route",
            "verdict": "negative",
            "label": "wrong_route",
            "persona": "sentinel",
            "since": "2026-10-01T00:00:00Z",
            "since_id": "afb_1",
            "limit": 50,
        })
        assert captured["method"] == "GET"
        assert captured["url"] == (
            "https://api.test/api/v1/projects/proj_test/agent-feedback"
        )
        assert captured["params"]["feedback_type"] == "route"
        assert captured["params"]["verdict"] == "negative"
        assert captured["params"]["label"] == "wrong_route"
        assert captured["params"]["persona"] == "sentinel"
        assert captured["params"]["since"] == "2026-10-01T00:00:00Z"
        assert captured["params"]["since_id"] == "afb_1"
        assert captured["params"]["limit"] == "50"
        assert "agent_feedback" in result

    @pytest.mark.asyncio
    async def test_dispatch_replay_agent_run(self, fake_resolver, fake_httpx, captured):
        result = await mcp_server._handle_replay_agent_run({
            "run_id": "run_parent",
            "persona_name": "sentinel",
            "ticket_id": "tk_2",
            "prompt": "edited prompt",
            "fail_on": "severity",
            "trigger_ref": "ci-ref",
            "context_refs": ["a", "b"],
        })
        assert captured["method"] == "POST"
        assert captured["url"] == (
            "https://api.test/api/v1/projects/proj_test"
            "/agent-runs/run_parent/replay"
        )
        assert captured["json"] == {
            "persona_name": "sentinel",
            "ticket_id": "tk_2",
            "prompt": "edited prompt",
            "fail_on": "severity",
            "trigger_ref": "ci-ref",
            "context_refs": ["a", "b"],
        }
        assert "error" not in result

    @pytest.mark.asyncio
    async def test_dispatch_replay_agent_run_requires_run_id(
        self, fake_resolver, fake_httpx, captured
    ):
        result = await mcp_server._handle_replay_agent_run({})
        assert "run_id is required" in result["error"]
        assert not captured

    @pytest.mark.asyncio
    async def test_dispatch_replay_agent_run_passes_prompt_through_and_allows_clearing(
        self, fake_resolver, fake_httpx, captured
    ):
        # Prompt is prose: whitespace is preserved verbatim, not stripped.
        result = await mcp_server._handle_replay_agent_run({
            "run_id": "run_parent",
            "prompt": "  keep leading/trailing  ",
            "context_refs": [],
        })
        assert captured["json"] == {
            "prompt": "  keep leading/trailing  ",
            "context_refs": [],
        }
        assert "error" not in result

        # An explicit empty prompt/context_refs forwards as a clear signal.
        captured.clear()
        result = await mcp_server._handle_replay_agent_run({
            "run_id": "run_parent",
            "prompt": "",
        })
        assert captured["json"] == {"prompt": ""}
        assert "error" not in result

    @pytest.mark.asyncio
    async def test_dispatch_get_agent_feedback_summary(
        self, fake_resolver, fake_httpx, captured
    ):
        await mcp_server._handle_get_agent_feedback_summary({
            "persona": "sentinel",
            "since": "2026-10-01T00:00:00Z",
        })
        assert captured["method"] == "GET"
        assert captured["url"] == (
            "https://api.test/api/v1/projects/proj_test/agent-feedback/summary"
        )
        assert captured["params"]["persona"] == "sentinel"
        assert captured["params"]["since"] == "2026-10-01T00:00:00Z"

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
    async def test_dispatch_rename_session_title_and_alias(self, monkeypatch):
        """rename_session dispatches PATCH /sessions/{id} with title+alias body.
        Sessions are user-scoped — handler uses load_config, not project resolver."""
        import httpx
        from types import SimpleNamespace

        fake_config = SimpleNamespace(
            sync=SimpleNamespace(api_url="https://api.test", api_key="test-key"),
        )
        monkeypatch.setattr(mcp_server, "load_config", lambda: fake_config)

        captured: dict = {}

        class _Resp:
            status_code = 200
            text = "{}"

            def json(self):
                return {"id": "ses_xyz", "title": "New", "alias": "new-alias"}

        class _FakeClient:
            def __init__(self, *a, **k):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def patch(self, url, *, json=None, headers=None):
                captured["url"] = url
                captured["json"] = json
                captured["method"] = "PATCH"
                return _Resp()

            async def delete(self, url, *, headers=None):
                captured.setdefault("delete_url", url)
                return _Resp()

            async def get(self, url, *, headers=None):
                captured["get_url"] = url
                return _Resp()

        monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)

        result = await mcp_server._handle_rename_session({
            "session_id": "ses_xyz",
            "new_title": "New",
            "new_alias": "new-alias",
        })
        assert captured["method"] == "PATCH"
        assert captured["url"] == "https://api.test/api/v1/sessions/ses_xyz"
        assert captured["json"] == {"title": "New", "alias": "new-alias"}
        assert result["title"] == "New"
        assert result["alias"] == "new-alias"

    @pytest.mark.asyncio
    async def test_dispatch_rename_session_empty_alias_clears(self, monkeypatch):
        """Passing new_alias='' routes through DELETE /alias, not PATCH alias=null."""
        import httpx
        from types import SimpleNamespace

        fake_config = SimpleNamespace(
            sync=SimpleNamespace(api_url="https://api.test", api_key="test-key"),
        )
        monkeypatch.setattr(mcp_server, "load_config", lambda: fake_config)

        captured: dict = {}

        class _Resp:
            status_code = 200
            text = "{}"

            def json(self):
                return {"id": "ses_xyz", "alias": None}

        class _FakeClient:
            def __init__(self, *a, **k):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def patch(self, url, *, json=None, headers=None):
                captured["patch_url"] = url
                captured["patch_json"] = json
                return _Resp()

            async def delete(self, url, *, headers=None):
                captured["delete_url"] = url
                return _Resp()

            async def get(self, url, *, headers=None):
                captured["get_url"] = url
                return _Resp()

        monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)

        # Alias-clear only (no title change).
        result = await mcp_server._handle_rename_session({
            "session_id": "ses_xyz",
            "new_alias": "",
        })
        assert captured["delete_url"] == "https://api.test/api/v1/sessions/ses_xyz/alias"
        # PATCH must NOT be called when only clearing alias.
        assert "patch_url" not in captured
        # Codex R1 MEDIUM fix: DELETE response carries the canonical
        # SessionDetail; handler must NOT issue a follow-up GET that
        # could 404 if session_id was the just-cleared alias.
        assert "get_url" not in captured
        # The returned payload is the DELETE response body.
        assert result["id"] == "ses_xyz"

    @pytest.mark.asyncio
    async def test_dispatch_rename_session_alias_clear_via_alias_identifier(self, monkeypatch):
        """Codex R1 MEDIUM regression: caller passes the alias they're about
        to clear as `session_id`. Handler must resolve to canonical id from
        the DELETE response before any follow-up call, and must NOT issue a
        post-DELETE GET/PATCH against the now-unresolvable alias."""
        import httpx
        from types import SimpleNamespace

        fake_config = SimpleNamespace(
            sync=SimpleNamespace(api_url="https://api.test", api_key="test-key"),
        )
        monkeypatch.setattr(mcp_server, "load_config", lambda: fake_config)

        captured: dict[str, list] = {"patch": [], "delete": [], "get": []}

        class _DelResp:
            status_code = 200
            text = "{}"

            def json(self):
                # Server returns the canonical SessionDetail after the clear.
                return {"id": "ses_abc123", "alias": None, "title": "Original"}

        class _FakeClient:
            def __init__(self, *a, **k):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def patch(self, url, *, json=None, headers=None):
                captured["patch"].append(url)
                return _DelResp()

            async def delete(self, url, *, headers=None):
                captured["delete"].append(url)
                return _DelResp()

            async def get(self, url, *, headers=None):
                captured["get"].append(url)
                return _DelResp()

        monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)

        # Caller passes the alias being cleared.
        result = await mcp_server._handle_rename_session({
            "session_id": "old-alias",
            "new_alias": "",
        })
        # DELETE goes to the alias-identified URL (server resolves).
        assert captured["delete"] == ["https://api.test/api/v1/sessions/old-alias/alias"]
        # PATCH and GET must NOT be issued — DELETE response carries everything.
        assert captured["patch"] == []
        assert captured["get"] == []
        # Handler returns the canonical SessionDetail from DELETE.
        assert result["id"] == "ses_abc123"
        assert result["alias"] is None

    @pytest.mark.asyncio
    async def test_dispatch_rename_session_alias_clear_plus_title_uses_canonical_id(self, monkeypatch):
        """Codex R1 MEDIUM regression: when caller asks to BOTH clear alias
        AND set a new title, with session_id being the alias-to-clear, the
        follow-up PATCH must target the canonical id resolved from the
        DELETE response — NOT the stale alias identifier."""
        import httpx
        from types import SimpleNamespace

        fake_config = SimpleNamespace(
            sync=SimpleNamespace(api_url="https://api.test", api_key="test-key"),
        )
        monkeypatch.setattr(mcp_server, "load_config", lambda: fake_config)

        captured: dict[str, str] = {}

        class _DelResp:
            status_code = 200
            text = "{}"

            def json(self):
                return {"id": "ses_abc123", "alias": None, "title": "Original"}

        class _PatchResp:
            status_code = 200
            text = "{}"

            def json(self):
                return {"id": "ses_abc123", "alias": None, "title": "Renamed"}

        class _FakeClient:
            def __init__(self, *a, **k):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def patch(self, url, *, json=None, headers=None):
                captured["patch_url"] = url
                captured["patch_json"] = json
                return _PatchResp()

            async def delete(self, url, *, headers=None):
                captured["delete_url"] = url
                return _DelResp()

            async def get(self, url, *, headers=None):
                captured["get_url"] = url
                return _DelResp()

        monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)

        result = await mcp_server._handle_rename_session({
            "session_id": "old-alias",
            "new_alias": "",
            "new_title": "Renamed",
        })
        # DELETE hits the alias-identified URL.
        assert captured["delete_url"] == "https://api.test/api/v1/sessions/old-alias/alias"
        # PATCH must target the canonical id, NOT the stale alias.
        assert captured["patch_url"] == "https://api.test/api/v1/sessions/ses_abc123"
        assert captured["patch_json"] == {"title": "Renamed"}
        # No GET — the PATCH response is what we return.
        assert "get_url" not in captured
        assert result["title"] == "Renamed"

    @pytest.mark.asyncio
    async def test_dispatch_rename_session_requires_session_id(self):
        result = await mcp_server._handle_rename_session({})
        assert "error" in result
        assert "session_id" in result["error"].lower()

    @pytest.mark.asyncio
    async def test_dispatch_rename_session_requires_at_least_one_field(self):
        result = await mcp_server._handle_rename_session({"session_id": "ses_xyz"})
        assert "error" in result
        assert "new_title" in result["error"] or "new_alias" in result["error"]

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

    # ── tk_a253102a20c148a9 — KB write API gap closure ──

    @pytest.mark.asyncio
    async def test_dispatch_update_entry_confidence(
        self, fake_resolver, fake_httpx, captured
    ):
        await mcp_server._handle_update_entry_confidence({
            "id": 42,
            "confidence": 0.9,
        })
        assert captured["method"] == "PUT"
        assert captured["url"] == (
            "https://api.test/api/v1/projects/proj_test/entries/42/confidence"
        )
        assert captured["json"] == {"confidence": 0.9}

    @pytest.mark.asyncio
    async def test_dispatch_update_entry_confidence_validates_id(
        self, fake_resolver, fake_httpx
    ):
        result = await mcp_server._handle_update_entry_confidence({"confidence": 0.5})
        assert "error" in result and "positive integer" in result["error"]

    @pytest.mark.asyncio
    async def test_dispatch_update_entry_confidence_rejects_out_of_range(
        self, fake_resolver, fake_httpx
    ):
        result = await mcp_server._handle_update_entry_confidence({
            "id": 42,
            "confidence": 1.5,
        })
        assert "error" in result and "0.0" in result["error"]

    @pytest.mark.asyncio
    async def test_dispatch_update_entry_confidence_rejects_non_numeric(
        self, fake_resolver, fake_httpx
    ):
        # Booleans are technically int subclasses in Python — must reject
        # explicitly so an agent passing `True` doesn't get interpreted
        # as confidence=1.0.
        result = await mcp_server._handle_update_entry_confidence({
            "id": 42,
            "confidence": True,
        })
        assert "error" in result

        result = await mcp_server._handle_update_entry_confidence({
            "id": 42,
            "confidence": "high",
        })
        assert "error" in result

    @pytest.mark.asyncio
    async def test_dispatch_promote_entry(
        self, fake_resolver, fake_httpx, captured
    ):
        await mcp_server._handle_promote_entry({"id": 42})
        assert captured["method"] == "PUT"
        assert captured["url"] == (
            "https://api.test/api/v1/projects/proj_test/entries/42/promote"
        )
        # No body required on promote — the server reads the entry and
        # applies quality gates.
        assert captured.get("json") is None

    @pytest.mark.asyncio
    async def test_dispatch_promote_entry_validates_id(
        self, fake_resolver, fake_httpx
    ):
        result = await mcp_server._handle_promote_entry({})
        assert "error" in result and "positive integer" in result["error"]

    # ── tk_97b693793c814f4d — dispatch tests for promote_eligible_entries ──

    @pytest.mark.asyncio
    async def test_dispatch_promote_eligible_entries_default_dry_run(
        self, fake_resolver, fake_httpx, captured
    ):
        """Default args: no min_length / min_confidence / set_confidence
        / entry_type. Body should only carry dry_run=True (the default).
        URL should be the bulk-promote endpoint under the resolved
        project."""
        await mcp_server._handle_promote_eligible_entries({})
        assert captured["method"] == "POST"
        assert captured["url"] == (
            "https://api.test/api/v1/projects/proj_test/entries/bulk-promote"
        )
        assert captured["json"] == {"dry_run": True}

    @pytest.mark.asyncio
    async def test_dispatch_promote_eligible_entries_forwards_all_args(
        self, fake_resolver, fake_httpx, captured
    ):
        await mcp_server._handle_promote_eligible_entries({
            "min_length": 100,
            "min_confidence": 0.9,
            "set_confidence": 0.95,
            "entry_type": "decision",
            "dry_run": False,
        })
        body = captured["json"]
        assert body["min_length"] == 100
        assert body["min_confidence"] == 0.9
        assert body["set_confidence"] == 0.95
        assert body["entry_type"] == "decision"
        assert body["dry_run"] is False

    @pytest.mark.asyncio
    async def test_dispatch_promote_eligible_entries_validates_min_length(
        self, fake_resolver, fake_httpx
    ):
        # Out of range
        result = await mcp_server._handle_promote_eligible_entries({
            "min_length": 0,
        })
        assert "error" in result and "min_length" in result["error"]
        result = await mcp_server._handle_promote_eligible_entries({
            "min_length": 20_000,
        })
        assert "error" in result and "min_length" in result["error"]
        # Non-int
        result = await mcp_server._handle_promote_eligible_entries({
            "min_length": "fifty",
        })
        assert "error" in result and "min_length" in result["error"]

    @pytest.mark.asyncio
    async def test_dispatch_promote_eligible_entries_validates_confidence_range(
        self, fake_resolver, fake_httpx
    ):
        result = await mcp_server._handle_promote_eligible_entries({
            "min_confidence": 1.5,
        })
        assert "error" in result and "min_confidence" in result["error"]
        result = await mcp_server._handle_promote_eligible_entries({
            "set_confidence": -0.1,
        })
        assert "error" in result and "set_confidence" in result["error"]

    @pytest.mark.asyncio
    async def test_dispatch_promote_eligible_entries_rejects_boolean_confidence(
        self, fake_resolver, fake_httpx
    ):
        """Python's bool is an int subclass — without explicit rejection,
        True would be coerced to 1.0 (or set_confidence interpreted as
        'use the max gate'). Reject the type ambiguity at the boundary."""
        for arg in ("min_confidence", "set_confidence"):
            result = await mcp_server._handle_promote_eligible_entries({arg: True})
            assert "error" in result and arg in result["error"], (
                f"boolean {arg} must be rejected"
            )

    @pytest.mark.asyncio
    async def test_dispatch_promote_eligible_entries_rejects_blank_entry_type(
        self, fake_resolver, fake_httpx
    ):
        for bad in ("", "   ", 42):
            result = await mcp_server._handle_promote_eligible_entries({
                "entry_type": bad,
            })
            assert "error" in result and "entry_type" in result["error"]

    @pytest.mark.asyncio
    async def test_dispatch_promote_eligible_entries_validates_dry_run_type(
        self, fake_resolver, fake_httpx
    ):
        result = await mcp_server._handle_promote_eligible_entries({
            "dry_run": "true",  # string, not bool
        })
        assert "error" in result and "dry_run" in result["error"]

    @pytest.mark.asyncio
    async def test_dispatch_promote_eligible_entries_strips_entry_type_whitespace(
        self, fake_resolver, fake_httpx, captured
    ):
        """Leading/trailing whitespace on entry_type would silently miss
        the server-side equality check, so we strip at the boundary."""
        await mcp_server._handle_promote_eligible_entries({
            "entry_type": "  decision  ",
        })
        assert captured["json"]["entry_type"] == "decision"

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

    # ── GH #70 — org-level personas ──

    @pytest.fixture
    def fake_org_config(self, monkeypatch):
        async def _fake(org_id: str):
            return (
                "https://api.test",
                "test-key",
                f"https://api.test/api/v1/orgs/{org_id}/personas",
            )

        monkeypatch.setattr(mcp_server, "_org_persona_api_config", _fake)

    @pytest.mark.asyncio
    async def test_list_personas_includes_org_by_default(
        self, fake_resolver, fake_httpx, captured
    ):
        await mcp_server._handle_list_personas({})
        assert captured["url"] == "https://api.test/api/v1/projects/proj_test/personas"
        assert captured["params"] == {}

    @pytest.mark.asyncio
    async def test_list_personas_include_org_false(
        self, fake_resolver, fake_httpx, captured
    ):
        for falsey in (False, "false", "0", "no"):
            captured.clear()
            await mcp_server._handle_list_personas({"include_org": falsey})
            assert captured["params"] == {"include_org": "false"}, falsey
        # Unknown junk keeps the default (include).
        captured.clear()
        await mcp_server._handle_list_personas({"include_org": "maybe"})
        assert captured["params"] == {}

    @pytest.mark.asyncio
    async def test_create_persona_with_org_id_targets_org_route(
        self, fake_org_config, fake_httpx, captured
    ):
        await mcp_server._handle_create_persona({
            "name": "release-governance",
            "role": "Security sign-off",
            "org_id": "org_1",
        })
        assert captured["method"] == "POST"
        assert captured["url"] == "https://api.test/api/v1/orgs/org_1/personas"
        assert captured["json"]["name"] == "release-governance"

    @pytest.mark.asyncio
    async def test_update_and_delete_persona_with_org_id(
        self, fake_org_config, fake_httpx, captured
    ):
        await mcp_server._handle_update_persona({
            "name": "atlas", "content": "# v2", "org_id": "org_1",
        })
        assert captured["method"] == "PUT"
        assert captured["url"] == "https://api.test/api/v1/orgs/org_1/personas/atlas"
        captured.clear()
        await mcp_server._handle_delete_persona({
            "name": "atlas", "org_id": "org_1", "force": True,
        })
        assert captured["method"] == "DELETE"
        assert captured["url"] == "https://api.test/api/v1/orgs/org_1/personas/atlas"
        assert captured["params"] == {"force": "true"}

    @pytest.mark.asyncio
    async def test_list_and_get_persona_with_org_id_target_org_routes(
        self, fake_org_config, fake_httpx, captured
    ):
        await mcp_server._handle_list_personas({"org_id": "org_1"})
        assert captured["method"] == "GET"
        assert captured["url"] == "https://api.test/api/v1/orgs/org_1/personas"
        captured.clear()
        await mcp_server._handle_get_persona({"name": "atlas", "org_id": "org_1"})
        assert captured["url"] == "https://api.test/api/v1/orgs/org_1/personas/atlas"

    @pytest.mark.asyncio
    async def test_persona_represented_user_is_sent_for_org_personas(
        self, fake_org_config, fake_httpx, captured
    ):
        # GitHub #81
        await mcp_server._handle_create_persona({
            "name": "kelvin", "role": "Platform", "org_id": "org_1",
            "represented_user": " kelvin@example.com ",
        })
        assert captured["json"]["represented_user"] == "kelvin@example.com"
        captured.clear()
        await mcp_server._handle_update_persona({
            "name": "kelvin", "org_id": "org_1", "represented_user": "u_2",
        })
        assert captured["json"] == {"represented_user": "u_2"}
        captured.clear()
        # An empty string clears the link (null on the wire).
        await mcp_server._handle_update_persona({
            "name": "kelvin", "org_id": "org_1", "represented_user": "",
        })
        assert captured["json"] == {"represented_user": None}

    @pytest.mark.asyncio
    async def test_persona_represented_user_needs_org_id(
        self, fake_resolver, fake_httpx, captured
    ):
        for handler, args in (
            (mcp_server._handle_create_persona, {"name": "a", "role": "r", "represented_user": "u"}),
            (mcp_server._handle_update_persona, {"name": "a", "represented_user": "u"}),
        ):
            result = await handler(args)
            assert "org_id" in result["error"]
        assert "method" not in captured

    def test_persona_tool_schemas_expose_org_args(self):
        tools = {t.name: t for t in mcp_server._TOOLS}
        for name in ("create_persona", "update_persona", "delete_persona", "list_personas", "get_persona"):
            assert "org_id" in tools[name].inputSchema["properties"], name
        assert "include_org" in tools["list_personas"].inputSchema["properties"]
        for name in ("create_persona", "update_persona"):
            assert "represented_user" in tools[name].inputSchema["properties"], name

    # ── tk_835a876529de4551 — update_ticket MCP dispatch ──

    @pytest.mark.asyncio
    async def test_update_ticket_requires_ticket_id(self, fake_resolver):
        result = await mcp_server._handle_update_ticket({})
        assert "error" in result
        assert "ticket_id" in result["error"].lower()

    @pytest.mark.asyncio
    async def test_update_ticket_requires_at_least_one_mutable_field(
        self, fake_resolver, fake_httpx, captured
    ):
        result = await mcp_server._handle_update_ticket({"ticket_id": "tk_abc"})
        assert "error" in result
        # Must reject without firing the PUT — server-side no-op would
        # produce confusing 200 with no diff comment.
        assert captured == {} or "method" not in captured

    @pytest.mark.asyncio
    async def test_update_ticket_sends_put_with_only_provided_fields(
        self, fake_resolver, fake_httpx, captured
    ):
        await mcp_server._handle_update_ticket({
            "ticket_id": "tk_abc",
            "title": "Corrected",
            "description": "Revised after Codex review.",
        })
        assert captured["method"] == "PUT"
        assert captured["url"] == (
            "https://api.test/api/v1/projects/proj_test/tickets/tk_abc"
        )
        # Only the two passed fields end up in the body — omitted fields
        # must NOT be sent as None/defaults (server treats omitted-as-
        # no-op; sending defaults would silently overwrite).
        assert captured["json"] == {
            "title": "Corrected",
            "description": "Revised after Codex review.",
        }

    @pytest.mark.asyncio
    async def test_update_ticket_forwards_lease_epoch(
        self, fake_resolver, fake_httpx, captured
    ):
        await mcp_server._handle_update_ticket({
            "ticket_id": "tk_abc",
            "priority": "high",
            "lease_epoch": 7,
        })
        assert captured["json"] == {"priority": "high", "lease_epoch": 7}

    @pytest.mark.asyncio
    async def test_update_ticket_rejects_lease_epoch_only(
        self, fake_resolver, fake_httpx, captured
    ):
        """tk_835a876529de4551 R1 MED #2 — lease_epoch is a fence,
        not a mutation. The MCP handler must reject lease_epoch-only
        locally so the round-trip doesn't burn another worker's
        epoch without writing an audit row."""
        result = await mcp_server._handle_update_ticket({
            "ticket_id": "tk_abc",
            "lease_epoch": 7,
        })
        assert "error" in result
        assert "lease_epoch" in result["error"].lower()
        # No HTTP call should have fired.
        assert captured == {} or "method" not in captured

    @pytest.mark.asyncio
    async def test_update_ticket_tool_schema_excludes_assigned_to(self):
        """tk_835a876529de4551 R1 LOW — `assigned_to` is reserved for
        the dedicated `assign_persona` verb. The update_ticket tool
        spec must not advertise it; the underlying PUT route still
        accepts it for assign_persona back-compat but it's hidden
        from the generic update surface."""
        from sessionfs.mcp.server import _TOOLS

        tool = next(t for t in _TOOLS if t.name == "update_ticket")
        props = tool.inputSchema["properties"]
        assert "assigned_to" not in props
        assert "related_sessions" not in props
        # Required mutable fields still present.
        for key in (
            "title",
            "description",
            "priority",
            "acceptance_criteria",
            "context_refs",
            "file_refs",
            "depends_on",
            "lease_epoch",
        ):
            assert key in props, f"update_ticket tool missing {key}"

    # ── v0.10.24 tk_32abb6d0d4744c5d / GH #50 — persona MCP parity ──

    @pytest.mark.asyncio
    async def test_update_persona_requires_name(self, fake_resolver):
        result = await mcp_server._handle_update_persona({})
        assert "error" in result
        assert "name" in result["error"].lower()

    @pytest.mark.asyncio
    async def test_update_persona_requires_at_least_one_field(
        self, fake_resolver, fake_httpx, captured
    ):
        result = await mcp_server._handle_update_persona({"name": "atlas"})
        # Should reject without firing the PUT — saves a round-trip and
        # avoids server-side no-op confusion.
        assert "error" in result
        assert "role" in result["error"] or "content" in result["error"]
        assert captured == {} or "method" not in captured

    @pytest.mark.asyncio
    async def test_update_persona_sends_put_with_only_provided_fields(
        self, fake_resolver, fake_httpx, captured
    ):
        await mcp_server._handle_update_persona({
            "name": "atlas",
            "content": "# Atlas\n\nRevised after research.",
        })
        assert captured["method"] == "PUT"
        assert captured["url"] == "https://api.test/api/v1/projects/proj_test/personas/atlas"
        # Only `content` was passed — body must not include role or
        # specializations (server treats omitted fields as no-op; sending
        # defaults would silently overwrite existing values).
        assert captured["json"] == {"content": "# Atlas\n\nRevised after research."}

    @pytest.mark.asyncio
    async def test_update_persona_sends_specializations_list(
        self, fake_resolver, fake_httpx, captured
    ):
        await mcp_server._handle_update_persona({
            "name": "atlas",
            "specializations": ["fastapi", "alembic"],
        })
        assert captured["json"] == {"specializations": ["fastapi", "alembic"]}

    @pytest.mark.asyncio
    async def test_delete_persona_requires_name(self, fake_resolver):
        result = await mcp_server._handle_delete_persona({})
        assert "error" in result
        assert "name" in result["error"].lower()

    @pytest.mark.asyncio
    async def test_delete_persona_sends_delete_without_force(
        self, fake_resolver, fake_httpx, captured
    ):
        await mcp_server._handle_delete_persona({"name": "atlas"})
        assert captured["method"] == "DELETE"
        assert captured["url"] == "https://api.test/api/v1/projects/proj_test/personas/atlas"
        # No `force` param by default — server's non-terminal-ticket
        # guard fires unless the caller explicitly opts in.
        assert captured.get("params", {}) == {}

    @pytest.mark.asyncio
    async def test_delete_persona_passes_force_when_true(
        self, fake_resolver, fake_httpx, captured
    ):
        await mcp_server._handle_delete_persona({"name": "atlas", "force": True})
        assert captured["method"] == "DELETE"
        assert captured.get("params", {}) == {"force": "true"}

    @pytest.mark.asyncio
    async def test_delete_persona_rejects_falsey_strings_as_unforced(
        self, fake_resolver, fake_httpx, captured
    ):
        """Codex R1 MEDIUM regression — `bool("false")` is True in
        Python; the destructive force path must not accept truthy-string
        values that mean False. "false", "0", "no" → unforced."""
        for falsey in ("false", "False", "0", "no", "NO", ""):
            captured.clear()
            await mcp_server._handle_delete_persona({
                "name": "atlas",
                "force": falsey,
            })
            assert captured.get("params", {}) == {}, (
                f"force={falsey!r} should NOT pass ?force=true"
            )

    @pytest.mark.asyncio
    async def test_delete_persona_accepts_truthy_strings_as_forced(
        self, fake_resolver, fake_httpx, captured
    ):
        """Strict allowlist for force=true: only literal True or the
        narrow truthy-string set ("true", "1", "yes") opts in."""
        for truthy in ("true", "True", "1", "yes", "YES"):
            captured.clear()
            await mcp_server._handle_delete_persona({
                "name": "atlas",
                "force": truthy,
            })
            assert captured.get("params", {}) == {"force": "true"}, (
                f"force={truthy!r} should pass ?force=true"
            )

    @pytest.mark.asyncio
    async def test_update_persona_surfaces_404_cleanly(self, monkeypatch):
        """Codex R1 LOW regression (AC) — 404 from the server is
        translated to a human-readable error string instead of leaking
        the raw API response text."""
        async def _resolve_stub(_remote=""):
            return ("https://api.test", "test-key", "proj_test")
        monkeypatch.setattr(mcp_server, "_resolve_project_id", _resolve_stub)

        import httpx

        class _Resp404:
            status_code = 404
            text = '{"error": {"code": "404", "message": "not found"}}'

            def json(self):
                return {"error": {"code": "404", "message": "not found"}}

        class _FakeClient:
            def __init__(self, *a, **k):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def put(self, *a, **k):
                return _Resp404()

        monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)

        result = await mcp_server._handle_update_persona({
            "name": "ghost",
            "role": "Backend",
        })
        assert "error" in result
        # Must include the persona name + project context so the agent
        # can recover, not just leak the raw API JSON.
        assert "ghost" in result["error"]
        assert "not found" in result["error"].lower()

    @pytest.mark.asyncio
    async def test_delete_persona_surfaces_409_envelope(self, monkeypatch):
        """Codex R1 LOW regression (AC) — 409 (non-terminal tickets
        reference the persona) surfaces the structured envelope so
        callers can decide whether to retry with force=true."""
        async def _resolve_stub(_remote=""):
            return ("https://api.test", "test-key", "proj_test")
        monkeypatch.setattr(mcp_server, "_resolve_project_id", _resolve_stub)

        import httpx

        envelope = {
            "error": {
                "code": "persona_has_open_tickets",
                "message": "atlas is referenced by 3 non-terminal tickets",
                "details": {"open_ticket_count": 3},
            }
        }

        class _Resp409:
            status_code = 409
            text = json.dumps(envelope)

            def json(self):
                return envelope

        class _FakeClient:
            def __init__(self, *a, **k):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def delete(self, *a, **k):
                return _Resp409()

        monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)

        result = await mcp_server._handle_delete_persona({"name": "atlas"})
        assert result.get("status") == 409
        # Envelope is surfaced intact so the agent can route on
        # error.code + read error.details.open_ticket_count to decide
        # whether to retry with force=true.
        assert result["error"]["error"]["code"] == "persona_has_open_tickets"
        assert result["error"]["error"]["details"]["open_ticket_count"] == 3

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

    @pytest.mark.asyncio
    async def test_assume_and_start_write_canonical_persona_name(
        self, fake_resolver, monkeypatch, tmp_path
    ):
        """GH #70 — bundles record the server's canonical persona name, not
        the caller's spelling or the raw ticket assignment."""
        import httpx

        bundle = tmp_path / "active.json"
        from sessionfs import active_ticket as _at
        monkeypatch.setattr(_at, "bundle_path", lambda: bundle)

        class _Resp:
            def __init__(self, body):
                self.status_code = 200
                self._body = body
                self.text = json.dumps(body)

            def json(self):
                return self._body

        class _FakeClient:
            def __init__(self, *a, **k):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def get(self, url, **kw):
                return _Resp({"name": "Atlas", "scope": "project"})

            async def post(self, url, **kw):
                return _Resp({
                    "ticket": {"id": "tk_1", "assigned_to": "atlas", "lease_epoch": 2},
                    "compiled_context": "...",
                    "retrieval_audit_id": "ra_1",
                    "persona_name": "Atlas",
                })

        monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)

        await mcp_server._handle_assume_persona({"name": "ATLAS"})
        assert json.loads(bundle.read_text())["persona_name"] == "Atlas"

        await mcp_server._handle_start_ticket({"ticket_id": "tk_1"})
        assert json.loads(bundle.read_text())["persona_name"] == "Atlas"

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


class TestListTicketCommentsDispatch:
    """v0.10.10 — list_ticket_comments wraps GET /comments and supports
    since + limit for incremental polling. Project-scoped via the
    standard _resolve_project_id helper."""

    @pytest.mark.asyncio
    async def test_dispatch_routes_to_comments_endpoint(self, monkeypatch):
        captured = {}

        async def _fake_resolve(_git_remote: str = ""):
            return ("https://api.test", "test-key", "proj_x")
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
            async def get(self, url, *, headers=None, params=None):
                captured["url"] = url
                captured["params"] = params
                captured["headers"] = headers or {}
                return _Resp(200, [
                    {"id": "tc_1", "ticket_id": "tk_x", "content": "hi",
                     "author_user_id": "u1", "author_persona": None,
                     "session_id": None, "created_at": "2026-05-17T05:30:00Z"},
                ])

        monkeypatch.setattr(httpx, "AsyncClient", _Client)

        out = await mcp_server._handle_list_ticket_comments({"ticket_id": "tk_x"})
        assert captured["url"] == (
            "https://api.test/api/v1/projects/proj_x/tickets/tk_x/comments"
        )
        assert captured["params"] is None  # no since/limit passed
        assert out["comments"][0]["id"] == "tc_1"

    @pytest.mark.asyncio
    async def test_dispatch_passes_since_and_limit_for_incremental_polling(
        self, monkeypatch
    ):
        captured = {}

        async def _fake_resolve(_git_remote: str = ""):
            return ("https://api.test", "test-key", "proj_x")
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
            async def get(self, url, *, headers=None, params=None):
                captured["params"] = params
                return _Resp(200, [])

        monkeypatch.setattr(httpx, "AsyncClient", _Client)

        await mcp_server._handle_list_ticket_comments({
            "ticket_id": "tk_x",
            "since": "2026-05-17T03:00:00Z",
            "limit": 50,
        })
        assert captured["params"]["since"] == "2026-05-17T03:00:00Z"
        assert captured["params"]["limit"] == 50

    @pytest.mark.asyncio
    async def test_dispatch_requires_ticket_id(self):
        out = await mcp_server._handle_list_ticket_comments({})
        assert "error" in out and "ticket_id" in out["error"]

    @pytest.mark.asyncio
    async def test_dispatch_invalid_limit_returns_error(self):
        out = await mcp_server._handle_list_ticket_comments(
            {"ticket_id": "tk_x", "limit": "not-a-number"}
        )
        assert "error" in out and "limit" in out["error"]

    @pytest.mark.asyncio
    async def test_dispatch_surfaces_404_for_missing_ticket(self, monkeypatch):
        async def _fake_resolve(_git_remote: str = ""):
            return ("https://api.test", "test-key", "proj_x")
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
            async def get(self, *a, **kw):
                return _Resp(404, {"detail": "not found"})

        monkeypatch.setattr(httpx, "AsyncClient", _Client)
        out = await mcp_server._handle_list_ticket_comments(
            {"ticket_id": "tk_missing"}
        )
        assert "error" in out and "tk_missing" in out["error"]


class TestHandoffMcpDispatch:
    """v0.10.9 R3 LOW 1 — MCP handoff tools need URL/body dispatch tests.

    All 8 handoff handlers wrap REST endpoints and use the shared
    `_handoff_api_config()` helper (which is NOT project-scoped — handoffs
    are user-scoped). These tests confirm each handler routes to the
    correct URL with the right method + body shape, and surfaces errors
    cleanly."""

    @staticmethod
    def _install_fakes(monkeypatch, capture: dict, status: int = 200, body=None):
        """Install fake _handoff_api_config + httpx.AsyncClient."""
        body = body if body is not None else {"id": "hnd_x"}

        def _fake_conf():
            return ("https://api.test", "test-key")

        monkeypatch.setattr(mcp_server, "_handoff_api_config", _fake_conf)

        import httpx

        class _Resp:
            def __init__(self, code, b):
                self.status_code = code
                self._b = b
                self.text = json.dumps(b)

            def json(self):
                return self._b

        class _Client:
            def __init__(self, *a, **kw):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def post(self, url, *, headers=None, json=None, params=None):
                capture["method"] = "POST"
                capture["url"] = url
                capture["body"] = json
                capture["headers"] = headers or {}
                return _Resp(status, body)

            async def get(self, url, *, headers=None, params=None):
                capture["method"] = "GET"
                capture["url"] = url
                capture["params"] = params
                capture["headers"] = headers or {}
                return _Resp(status, body)

        monkeypatch.setattr(httpx, "AsyncClient", _Client)

    @pytest.mark.asyncio
    async def test_create_handoff_dispatch(self, monkeypatch):
        cap = {}
        self._install_fakes(monkeypatch, cap, status=201, body={"id": "hnd_abc"})
        out = await mcp_server._handle_create_handoff(
            {
                "session_id": "ses_x",
                "recipient_email": "r@x.com",
                "ticket_id": "tk_1",
                "attachments": [{"kind": "kb_entry", "ref_id": "42"}],
                "expires_in_hours": 24,
            }
        )
        assert cap["url"] == "https://api.test/api/v1/handoffs"
        assert cap["method"] == "POST"
        assert cap["body"]["session_id"] == "ses_x"
        assert cap["body"]["recipient_email"] == "r@x.com"
        assert cap["body"]["ticket_id"] == "tk_1"
        assert cap["body"]["expires_in_hours"] == 24
        assert cap["body"]["attachments"] == [{"kind": "kb_entry", "ref_id": "42"}]
        assert out["id"] == "hnd_abc"

    @pytest.mark.asyncio
    async def test_create_handoff_requires_session_id(self):
        out = await mcp_server._handle_create_handoff({})
        assert "error" in out and "session_id" in out["error"]

    @pytest.mark.asyncio
    async def test_claim_handoff_dispatch(self, monkeypatch):
        cap = {}
        self._install_fakes(monkeypatch, cap, body={"status": "claimed"})
        out = await mcp_server._handle_claim_handoff({"handoff_id": "hnd_q"})
        assert cap["url"] == "https://api.test/api/v1/handoffs/hnd_q/claim"
        assert cap["method"] == "POST"
        assert out["status"] == "claimed"

    @pytest.mark.asyncio
    async def test_get_handoff_dispatch(self, monkeypatch):
        cap = {}
        self._install_fakes(monkeypatch, cap, body={"id": "hnd_q"})
        out = await mcp_server._handle_get_handoff({"handoff_id": "hnd_q"})
        assert cap["url"] == "https://api.test/api/v1/handoffs/hnd_q"
        assert cap["method"] == "GET"
        assert out["id"] == "hnd_q"

    @pytest.mark.asyncio
    async def test_list_inbox_handoffs_dispatch(self, monkeypatch):
        cap = {}
        self._install_fakes(monkeypatch, cap, body={"handoffs": [], "total": 0})
        out = await mcp_server._handle_list_inbox_handoffs({"include_team": False})
        assert cap["url"] == "https://api.test/api/v1/handoffs/inbox"
        assert cap["method"] == "GET"
        assert cap["params"]["include_team"] == "false"
        assert out["total"] == 0

    @pytest.mark.asyncio
    async def test_list_sent_handoffs_dispatch(self, monkeypatch):
        cap = {}
        self._install_fakes(monkeypatch, cap, body={"handoffs": [], "total": 0})
        out = await mcp_server._handle_list_sent_handoffs({})
        assert cap["url"] == "https://api.test/api/v1/handoffs/sent"
        assert cap["method"] == "GET"
        assert out["total"] == 0

    @pytest.mark.asyncio
    async def test_revoke_handoff_dispatch(self, monkeypatch):
        cap = {}
        self._install_fakes(monkeypatch, cap, body={"status": "revoked"})
        out = await mcp_server._handle_revoke_handoff(
            {"handoff_id": "hnd_q", "reason": "wrong recipient"}
        )
        assert cap["url"] == "https://api.test/api/v1/handoffs/hnd_q/revoke"
        assert cap["method"] == "POST"
        assert cap["body"] == {"reason": "wrong recipient"}
        assert out["status"] == "revoked"

    @pytest.mark.asyncio
    async def test_revoke_handoff_requires_reason(self):
        out = await mcp_server._handle_revoke_handoff({"handoff_id": "hnd_q"})
        assert "error" in out and "reason" in out["error"]

    @pytest.mark.asyncio
    async def test_decline_handoff_dispatch(self, monkeypatch):
        cap = {}
        self._install_fakes(monkeypatch, cap, body={"status": "declined"})
        out = await mcp_server._handle_decline_handoff(
            {"handoff_id": "hnd_q", "reason": "not my area"}
        )
        assert cap["url"] == "https://api.test/api/v1/handoffs/hnd_q/decline"
        assert cap["method"] == "POST"
        assert cap["body"] == {"reason": "not my area"}
        assert out["status"] == "declined"

    @pytest.mark.asyncio
    async def test_decline_handoff_no_reason_sends_empty_body(self, monkeypatch):
        cap = {}
        self._install_fakes(monkeypatch, cap, body={"status": "declined"})
        await mcp_server._handle_decline_handoff({"handoff_id": "hnd_q"})
        assert cap["body"] == {}

    @pytest.mark.asyncio
    async def test_add_handoff_comment_dispatch(self, monkeypatch):
        cap = {}
        self._install_fakes(monkeypatch, cap, status=201, body={"id": "hcm_x"})
        out = await mcp_server._handle_add_handoff_comment(
            {"handoff_id": "hnd_q", "content": "Got it, will pick up tomorrow"}
        )
        assert cap["url"] == "https://api.test/api/v1/handoffs/hnd_q/comments"
        assert cap["method"] == "POST"
        assert cap["body"]["content"] == "Got it, will pick up tomorrow"
        assert out["id"] == "hcm_x"

    @pytest.mark.asyncio
    async def test_handoff_handlers_surface_api_errors(self, monkeypatch):
        cap = {}
        self._install_fakes(monkeypatch, cap, status=404, body={"detail": "not found"})
        out = await mcp_server._handle_get_handoff({"handoff_id": "hnd_missing"})
        assert "error" in out and "404" in out["error"]


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

    def test_list_checkpoints_equal_mtimes_order_by_name(self, mcp_env):
        import os

        paths = {}
        for name in ("beta", "alpha", "gamma"):  # created out of name order
            created = mcp_server._handle_checkpoint_session(
                {"session_id": "ses_auth1234abcdef", "name": name}
            )
            paths[name] = created["path"]
        stamp = 1_700_000_000_000_000_000
        for path in paths.values():
            os.utime(path, ns=(stamp, stamp))
        for _ in range(3):
            result = mcp_server._handle_list_checkpoints(
                {"session_id": "ses_auth1234abcdef"}
            )
            assert [cp["name"] for cp in result["checkpoints"]] == [
                "alpha",
                "beta",
                "gamma",
            ]
        # A genuinely older checkpoint still sorts first.
        os.utime(paths["gamma"], ns=(stamp - 10**9, stamp - 10**9))
        result = mcp_server._handle_list_checkpoints(
            {"session_id": "ses_auth1234abcdef"}
        )
        assert [cp["name"] for cp in result["checkpoints"]] == ["gamma", "alpha", "beta"]

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
        # Patch on both the mcp_server namespace AND the source module:
        # _fetch_kb_entries_raw uses `from sessionfs.daemon.config import
        # load_config` inside the function (re-imported each call) so
        # patching only mcp_server.load_config doesn't intercept it.
        # In CI without a real ~/.sessionfs/config.toml, the unpatched
        # path returns an empty api_key and the KB fetch early-returns []
        # — the test passed locally only because the dev's config file
        # has a real key.
        import sessionfs.daemon.config as daemon_cfg

        monkeypatch.setattr(mcp_server, "load_config", lambda: cfg)
        monkeypatch.setattr(daemon_cfg, "load_config", lambda *_args, **_kw: cfg)
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
        # Patch on both modules (see test_returns_kb_and_session_sources
        # for the rationale — `_fetch_kb_entries_raw` re-imports
        # load_config from sessionfs.daemon.config inside the function).
        import sessionfs.daemon.config as daemon_cfg

        monkeypatch.setattr(mcp_server, "load_config", lambda: cfg)
        monkeypatch.setattr(daemon_cfg, "load_config", lambda *_args, **_kw: cfg)
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


class TestAddKnowledgeAttribution:
    """v0.10.21 Phase 4a (tk_8028c79963fe4dc7) — MCP `add_knowledge`
    forwards persona_name + author_class and auto-threads the active
    ticket bundle's persona when project_id matches.
    """

    @pytest.fixture
    def fake_resolver(self, monkeypatch):
        async def _fake(_git_remote: str = ""):
            return ("https://api.test", "test-key", "proj_test")

        monkeypatch.setattr(mcp_server, "_resolve_project_id", _fake)

    @pytest.fixture
    def captured(self):
        return {}

    @pytest.fixture
    def fake_httpx_kb_add(self, monkeypatch, captured):
        """Patch httpx so POST /entries/add returns a Phase-4a-shaped
        201 body with whatever attribution arrived in the payload.
        That lets tests assert both the outgoing payload and the
        message returned to the caller.
        """
        import httpx

        class _FakeResponse:
            def __init__(self, status_code, body):
                self.status_code = status_code
                self._body = body
                self.text = json.dumps(body)

            def json(self):
                return self._body

        class _FakeClient:
            def __init__(self, *a, **k):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def post(self, url, *, json=None, headers=None):
                captured["url"] = url
                captured["json"] = json
                # Echo the attribution back in the response so the
                # handler can format it into the user-facing message.
                # Server-side anti-spoof is independent — covered in
                # tests/server/integration/test_scoped_service_keys.py
                # by test_service_key_cannot_spoof_author_class_human.
                resp_body = {
                    "id": 42,
                    "claim_class": "note",
                    "persona_name": (json or {}).get("persona_name"),
                    "author_class": (json or {}).get("author_class")
                    or "human",
                }
                return _FakeResponse(201, resp_body)

        monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)

    @pytest.mark.asyncio
    async def test_explicit_persona_name_is_forwarded(
        self, fake_resolver, fake_httpx_kb_add, captured
    ):
        """Caller passes persona_name explicitly → payload carries it."""
        result = await mcp_server._handle_add_knowledge({
            "content": "Scout HN: feature flag plateau",
            "entry_type": "discovery",
            "persona_name": "scout",
        })
        assert captured["url"].endswith("/projects/proj_test/entries/add")
        assert captured["json"]["persona_name"] == "scout"
        assert "persona: scout" in result

    @pytest.mark.asyncio
    async def test_explicit_author_class_is_forwarded(
        self, fake_resolver, fake_httpx_kb_add, captured
    ):
        """author_class=agent flows through to the server payload."""
        result = await mcp_server._handle_add_knowledge({
            "content": "Atlas writes on behalf of scout persona",
            "entry_type": "discovery",
            "persona_name": "scout",
            "author_class": "agent",
        })
        assert captured["json"]["author_class"] == "agent"
        assert "author_class: agent" in result

    @pytest.mark.asyncio
    async def test_auto_threads_persona_from_active_ticket_bundle(
        self, fake_resolver, fake_httpx_kb_add, captured, monkeypatch, tmp_path
    ):
        """When persona_name is not supplied AND the active ticket
        bundle's project_id matches the resolved project, the handler
        threads bundle.persona_name into the payload. Mirrors
        update_wiki_page behavior (v0.10.7).

        Codex R1 MEDIUM: bundle auto-thread must ALSO default
        author_class='agent' so the resulting row is retrievable via
        GET /entries?persona_name=...&author_class=agent — without
        this, the server defaults to 'human' and the agent-memory
        loop misses bundle-attributed writes.
        """
        bundle = tmp_path / "active.json"
        bundle.write_text(json.dumps({
            "ticket_id": "tk_active",
            "persona_name": "scout",
            "project_id": "proj_test",
            "started_at": "2026-05-22T00:00:00Z",
        }))
        from sessionfs import active_ticket as _at
        monkeypatch.setattr(_at, "bundle_path", lambda: bundle)

        await mcp_server._handle_add_knowledge({
            "content": "Auto-threaded persona via bundle",
            "entry_type": "discovery",
        })
        assert captured["json"]["persona_name"] == "scout"
        # Codex R1 MEDIUM: bundle path defaults author_class to "agent".
        assert captured["json"]["author_class"] == "agent"

    @pytest.mark.asyncio
    async def test_explicit_author_class_overrides_bundle_agent_default(
        self, fake_resolver, fake_httpx_kb_add, captured, monkeypatch, tmp_path
    ):
        """Even when bundle auto-thread fires, an explicit
        `author_class` from the caller wins over the 'agent' default
        (Codex R1 MEDIUM safety rail — humans can still attribute on
        behalf of a persona)."""
        bundle = tmp_path / "active.json"
        bundle.write_text(json.dumps({
            "ticket_id": "tk_active",
            "persona_name": "scout",
            "project_id": "proj_test",
            "started_at": "2026-05-22T00:00:00Z",
        }))
        from sessionfs import active_ticket as _at
        monkeypatch.setattr(_at, "bundle_path", lambda: bundle)

        await mcp_server._handle_add_knowledge({
            "content": "Human attributing on behalf of the persona",
            "entry_type": "discovery",
            "author_class": "human",
        })
        assert captured["json"]["persona_name"] == "scout"
        assert captured["json"]["author_class"] == "human"

    @pytest.mark.asyncio
    async def test_explicit_persona_overrides_bundle(
        self, fake_resolver, fake_httpx_kb_add, captured, monkeypatch, tmp_path
    ):
        """If both an explicit persona_name and a matching bundle are
        present, the explicit arg wins."""
        bundle = tmp_path / "active.json"
        bundle.write_text(json.dumps({
            "ticket_id": "tk_active",
            "persona_name": "scout",
            "project_id": "proj_test",
            "started_at": "2026-05-22T00:00:00Z",
        }))
        from sessionfs import active_ticket as _at
        monkeypatch.setattr(_at, "bundle_path", lambda: bundle)

        await mcp_server._handle_add_knowledge({
            "content": "Explicit persona wins over the bundle",
            "entry_type": "discovery",
            "persona_name": "atlas",
        })
        assert captured["json"]["persona_name"] == "atlas"

    @pytest.mark.asyncio
    async def test_bundle_ignored_when_project_mismatch(
        self, fake_resolver, fake_httpx_kb_add, captured, monkeypatch, tmp_path
    ):
        """A bundle for a different project must NOT contribute persona
        attribution — provenance is project-scoped."""
        bundle = tmp_path / "active.json"
        bundle.write_text(json.dumps({
            "ticket_id": "tk_other_project",
            "persona_name": "scout",
            "project_id": "proj_OTHER",
            "started_at": "2026-05-22T00:00:00Z",
        }))
        from sessionfs import active_ticket as _at
        monkeypatch.setattr(_at, "bundle_path", lambda: bundle)

        await mcp_server._handle_add_knowledge({
            "content": "Cross-project bundle must not leak persona",
            "entry_type": "discovery",
        })
        assert "persona_name" not in captured["json"]

    @pytest.mark.asyncio
    async def test_no_bundle_preserves_legacy_behavior(
        self, fake_resolver, fake_httpx_kb_add, captured, monkeypatch, tmp_path
    ):
        """When no bundle exists and no persona is supplied, payload
        carries no persona_name (legacy behavior preserved for plain
        manual writes)."""
        from sessionfs import active_ticket as _at
        monkeypatch.setattr(_at, "bundle_path", lambda: tmp_path / "missing.json")

        await mcp_server._handle_add_knowledge({
            "content": "Plain manual entry without persona attribution",
            "entry_type": "discovery",
        })
        assert "persona_name" not in captured["json"]
        assert "author_class" not in captured["json"]

    @pytest.mark.asyncio
    async def test_service_key_author_class_human_is_forwarded_server_overrides(
        self, fake_resolver, captured, monkeypatch
    ):
        """The MCP handler is a plumbing layer — it forwards whatever
        author_class the caller passes. Server-side anti-spoof is
        authoritative: when auth.actor_type='service_key', the server
        forces author_class='agent' regardless of payload. This test
        proves the MCP layer (a) forwards author_class='human' as-is
        and (b) the user-facing response surfaces whatever attribution
        the server actually persisted.

        Server-side enforcement is covered by
        test_service_key_cannot_spoof_author_class_human in
        tests/server/integration/test_scoped_service_keys.py.
        """
        import httpx

        class _FakeResp:
            def __init__(self, body):
                self.status_code = 201
                self._body = body
                self.text = json.dumps(body)

            def json(self):
                return self._body

        class _FakeClient:
            def __init__(self, *a, **k):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def post(self, url, *, json=None, headers=None):
                captured["url"] = url
                captured["json"] = json
                # Simulate the server overriding author_class='agent'
                # for the service-key principal.
                return _FakeResp({
                    "id": 99,
                    "claim_class": "note",
                    "persona_name": (json or {}).get("persona_name"),
                    "author_class": "agent",
                })

        monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)

        result = await mcp_server._handle_add_knowledge({
            "content": "Service-key caller attempts to spoof human",
            "entry_type": "discovery",
            "author_class": "human",
        })
        # Forwarded as-is.
        assert captured["json"]["author_class"] == "human"
        # But the user-facing message reflects what the server persisted.
        assert "author_class: agent" in result

    @pytest.mark.asyncio
    async def test_upsert_flag_is_forwarded(
        self, fake_resolver, captured, monkeypatch
    ):
        """v0.10.23 tk_49db8d2b6c424d35 — explicit upsert opt-in must
        plumb through the MCP handler to the /entries/add payload, and
        the response's upserted_from list must surface in the agent-
        facing message so the caller can audit the roll-forward."""
        import httpx

        class _FakeResponse:
            def __init__(self, status_code, body):
                self.status_code = status_code
                self._body = body
                self.text = json.dumps(body)

            def json(self):
                return self._body

        class _FakeClient:
            def __init__(self, *a, **k):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def post(self, url, *, json=None, headers=None):
                captured["url"] = url
                captured["json"] = json
                return _FakeResponse(201, {
                    "id": 99,
                    "claim_class": "note",
                    "persona_name": (json or {}).get("persona_name"),
                    "author_class": "agent",
                    "upserted_from": [42, 17],
                })

        monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)

        result = await mcp_server._handle_add_knowledge({
            "content": "Scout state cache rolled forward with new signals",
            "entry_type": "discovery",
            "entity_ref": "scout-state:decisions",
            "upsert": True,
        })
        # Payload carries the explicit flag.
        assert captured["json"]["upsert"] is True
        assert captured["json"]["entity_ref"] == "scout-state:decisions"
        # User-facing message surfaces the dismissed IDs so the caller
        # can confirm the roll-forward landed.
        assert "Upsert" in result
        assert "[42, 17]" in result
        assert "scout-state:decisions" in result

    @pytest.mark.asyncio
    async def test_upsert_omitted_does_not_send_flag(
        self, fake_resolver, fake_httpx_kb_add, captured
    ):
        """When upsert is omitted, the MCP handler must NOT inject it
        into the payload — preserves the v0.10.22 default behavior
        where entity_ref does not silently auto-supersede."""
        await mcp_server._handle_add_knowledge({
            "content": "Distinct discovery about src/db.py with concrete file refs to clear gates",
            "entry_type": "discovery",
            "entity_ref": "src/db.py",
        })
        assert "upsert" not in captured["json"]


_PKG = "spk_" + "0" * 24
_VER = "skv_" + "1" * 24
_REQ = "sir_" + "2" * 24
_REQ2 = "sir_" + "3" * 24


class TestMarketplaceMcpTools:
    @pytest.fixture
    def marketplace_http(self, monkeypatch):
        import httpx

        calls: list[dict] = []

        async def _resolve(_remote: str = ""):
            return ("https://api.test", "test-key", "proj_test")

        class _Response:
            status_code = 200
            text = '{"ok": true}'

            def json(self):
                return {"ok": True}

        class _Client:
            def __init__(self, *args, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            async def get(self, url, *, headers=None, params=None):
                calls.append({"method": "GET", "url": url, "params": params or {}})
                return _Response()

            async def post(self, url, *, headers=None, json=None):
                calls.append({"method": "POST", "url": url, "json": json})
                return _Response()

        monkeypatch.setattr(mcp_server, "_resolve_project_id", _resolve)
        monkeypatch.setattr(
            mcp_server,
            "_marketplace_api_config",
            lambda: ("https://api.test", "test-key"),
        )
        monkeypatch.setattr(httpx, "AsyncClient", _Client)
        return calls

    @pytest.mark.asyncio
    async def test_discovery_and_preview_dispatch(self, marketplace_http):
        result = await mcp_server._handle_search_skill_marketplace(
            {"query": "review", "tool": "codex", "limit": 5}
        )
        assert result == {"ok": True}
        assert marketplace_http[-1] == {
            "method": "GET",
            "url": "https://api.test/api/v1/marketplace/packages",
            "params": {"q": "review", "tool": "codex", "limit": 5},
        }

        # P2-1: the opaque page cursor is passed through untouched; garbage is refused locally.
        await mcp_server._handle_search_skill_marketplace({"query": "review", "cursor": "eyJwIjoi"})
        assert marketplace_http[-1]["params"] == {"q": "review", "cursor": "eyJwIjoi"}
        assert "error" in await mcp_server._handle_search_skill_marketplace({"cursor": 5})
        assert "error" in await mcp_server._handle_search_skill_marketplace({"cursor": "x" * 600})

        # Marketplace v2 §9 discovery filters pass through as query params.
        await mcp_server._handle_search_skill_marketplace(
            {"query": "lint", "category": "review", "publisher": "acme", "verified": True,
             "attested": False, "license": "MIT", "max_severity": "low", "capability": "scripts",
             "works_in": "codex", "updated_within": 30, "include_deprecated": False, "sort": "relevance"}
        )
        assert marketplace_http[-1]["params"] == {
            "q": "lint", "category": "review", "publisher": "acme", "verified": "true",
            "attested": "false", "license": "MIT", "max_severity": "low", "capability": "scripts",
            "works_in": "codex", "updated_within": 30, "include_deprecated": "false", "sort": "relevance",
        }
        assert "error" in await mcp_server._handle_search_skill_marketplace({"verified": "yes"})
        assert "error" in await mcp_server._handle_search_skill_marketplace({"updated_within": True})
        assert "error" in await mcp_server._handle_search_skill_marketplace({"category": ""})

        await mcp_server._handle_get_marketplace_package({"package_id": _PKG})
        assert marketplace_http[-1]["url"].endswith(f"/marketplace/packages/{_PKG}")
        await mcp_server._handle_preview_marketplace_skill({"version_id": _VER})
        assert marketplace_http[-1]["url"].endswith(f"/marketplace/versions/{_VER}/preview")

    @pytest.mark.asyncio
    async def test_approval_gated_load_and_request_lifecycle_dispatch(self, marketplace_http):
        await mcp_server._handle_request_skill_install(
            {
                "version_id": _VER,
                "content_hash": "sha256:abc",
                "target_scope": "device",
                "target_device_id": "runner-1",
                "target_tools": ["codex"],
            }
        )
        call = marketplace_http[-1]
        assert call["url"].endswith("/projects/proj_test/skill-install-requests")
        assert call["json"]["content_hash"] == "sha256:abc"
        assert call["json"]["requested_capabilities"] == ["instructions"]
        # A3.2: the server refuses self-approval of MCP-originated requests.
        assert call["json"]["requested_via"] == "mcp"

        await mcp_server._handle_load_marketplace_skill(
            {"version_id": _VER, "tool": "codex", "device_id": "runner-1"}
        )
        call = marketplace_http[-1]
        assert call["url"].endswith(f"/marketplace/versions/{_VER}/instructions")
        assert call["params"] == {
            "project_id": "proj_test",
            "tool": "codex",
            "device_id": "runner-1",
        }
        # A3.3: loaded bundles are third-party data, never user instructions.
        loaded = await mcp_server._handle_load_marketplace_skill({"version_id": _VER, "tool": "codex"})
        assert loaded["untrusted"] is True
        assert "UNTRUSTED THIRD-PARTY CONTENT" in loaded["untrusted_preamble"]
        previewed = await mcp_server._handle_preview_marketplace_skill({"version_id": _VER})
        assert previewed["untrusted"] is True

        await mcp_server._handle_get_skill_install_request({"request_id": _REQ})
        assert marketplace_http[-1]["url"].endswith(f"/skill-install-requests/{_REQ}")
        await mcp_server._handle_list_skill_install_requests({"status": "pending"})
        assert marketplace_http[-1]["params"] == {"status": "pending"}
        await mcp_server._handle_cancel_skill_install_request({"request_id": _REQ2})
        assert marketplace_http[-1]["url"].endswith(f"/skill-install-requests/{_REQ2}/cancel")
        # A3: approval is never brokered over MCP.
        assert not hasattr(mcp_server, "_handle_approve_skill_install_request")
        before = len(marketplace_http)
        denied = await mcp_server._handle_install_request_action({"request_id": _REQ}, "approve")
        assert "error" in denied
        assert len(marketplace_http) == before

    @pytest.mark.asyncio
    async def test_marketplace_validation_happens_before_network(self, marketplace_http):
        assert "error" in await mcp_server._handle_get_marketplace_package({})
        assert "error" in await mcp_server._handle_preview_marketplace_skill({})
        assert "error" in await mcp_server._handle_load_marketplace_skill(
            {"version_id": _VER}
        )
        assert "error" in await mcp_server._handle_request_skill_install(
            {"version_id": _VER}
        )
        assert marketplace_http == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "traversal",
        [
            "../../admin/users",
            "spk_1/../../admin/users",
            "skv_" + "a" * 24 + "/../../admin/users",
            "%2e%2e%2fadmin",
            "spk_" + "A" * 24,
            "",
        ],
    )
    async def test_marketplace_path_ids_are_validated_locally(self, marketplace_http, traversal):
        """A5: caller ids are interpolated into authenticated URL paths — a
        traversal value must be rejected locally with NO HTTP call made."""
        handlers = [
            (mcp_server._handle_get_marketplace_package, {"package_id": traversal}),
            (mcp_server._handle_preview_marketplace_skill, {"version_id": traversal}),
            (mcp_server._handle_load_marketplace_skill, {"version_id": traversal, "tool": "codex"}),
            (mcp_server._handle_request_skill_install, {"version_id": traversal, "content_hash": "sha256:a"}),
            (mcp_server._handle_get_skill_install_request, {"request_id": traversal}),
            (mcp_server._handle_cancel_skill_install_request, {"request_id": traversal}),
        ]
        for handler, args in handlers:
            result = await handler(args)
            assert "error" in result, handler.__name__
        assert marketplace_http == []

    @pytest.mark.asyncio
    async def test_marketplace_path_segments_are_percent_encoded(self, marketplace_http, monkeypatch):
        """Even a validated id is encoded, and the resolved project id is too."""
        async def _resolve(_remote: str = ""):
            return ("https://api.test", "test-key", "proj/../x")

        monkeypatch.setattr(mcp_server, "_resolve_project_id", _resolve)
        await mcp_server._handle_get_skill_install_request({"request_id": _REQ})
        assert "/projects/proj%2F..%2Fx/skill-install-requests/" in marketplace_http[-1]["url"]
        # Device ids follow the server's rule (printable, no control chars):
        # Unicode and spaces are fine, control characters are rejected locally.
        assert "error" in await mcp_server._handle_load_marketplace_skill(
            {"version_id": _VER, "tool": "codex", "device_id": "bad\x00id"}
        )
        unicode_ok = await mcp_server._handle_load_marketplace_skill(
            {"version_id": _VER, "tool": "codex", "device_id": "Ola's MacBook ✓ runner 1"}
        )
        assert "error" not in unicode_ok
        assert marketplace_http[-1]["params"]["device_id"] == "Ola's MacBook ✓ runner 1"
        assert "error" in await mcp_server._handle_list_skill_install_requests(
            {"status": "pending/../../admin"}
        )



class TestProjectResolverRemoteValidation:
    """P1-1 (round 6): `git_remote` reaches the shared `_resolve_project_id`
    used by EVERY project-scoped tool; it must be a strict repository id."""

    @pytest.mark.parametrize(
        "remote, expected",
        [
            # shapes normalize_git_remote's own tests cover
            ("https://github.com/Acme/Skills.git", "acme/skills"),
            ("  https://github.com/org/repo.git  ", "org/repo"),
            ("https://github.com/acme/skills/", "acme/skills"),
            ("git@github.com:SessionFS/sessionfs", "sessionfs/sessionfs"),
            ("git@github.com:SessionFS/sessionfs.git", "sessionfs/sessionfs"),
            ("git@gitlab.com:team/project.git", "team/project"),
            ("https://gitlab.com/team/project", "team/project"),
            ("ssh://git@github.com/acme/skills", None),  # server keeps ssh:// raw; key follows it
            ("acme/skills", "acme/skills"),
            ("https://gitlab.example.com:8443/team.x/repo-1", "team.x/repo-1"),
            # scheme-less form existing users have stored (project fixtures use it)
            ("github.com/acme/repo", "github.com/acme/repo"),
            ("github.com/a/legacy-remote", "github.com/a/legacy-remote"),
            # GitLab subgroups / self-hosted nested paths
            ("https://gitlab.com/group/subgroup/repo.git", "group/subgroup/repo"),
            ("https://git.corp.example/a/b/c/d/repo", "a/b/c/d/repo"),
            ("ssh://git@host/a/b/c.git", None),
            ("git@host:a/b/c.git", "a/b/c"),
            ("http://localhost:3000/owner/repo.git", "owner/repo"),
            ("Owner_1/Repo.Name", "owner_1/repo.name"),
            # r11 P2-2: never narrower than the server's normalization
            ("https://github.com/acme/repo+tools.git", None),  # key == server normalization, '+' encoded
            ("https://git.example/~user/repo", "~user/repo"),
            ("https://git.example/a%20b/repo%2Bx.git", None),
            ("https://[::1]:8443/a/b.git", "a/b"),
            ("git@[fe80::1]:owner/repo.git", None),
        ],
    )
    def test_accepts_repository_identifiers(self, remote, expected):
        """P2-6: for every accepted shape the lookup key is exactly the
        server-side normalize_git_remote() result (percent-encoded per segment)."""
        from urllib.parse import quote

        from sessionfs.server.github_app import normalize_git_remote

        key = mcp_server._strict_repository_identifier(remote)
        # The server stores normalize_git_remote() of the raw remote (only
        # surrounding whitespace is dropped; a trailing "/" is preserved), so the
        # lookup key must decode to exactly that.
        server_key = normalize_git_remote(remote.strip())
        assert key == "/".join(quote(segment, safe="") for segment in server_key.split("/"))
        if expected is not None:
            assert key == expected

    @pytest.mark.parametrize(
        "remote",
        [
            "../admin/users",
            "acme/../admin",
            "https://github.com/acme/skills/../../admin/users",
            "https://github.com/acme/skills?x=1",
            "https://github.com/acme/skills#frag",
            "git@github.com:../admin",
            "a/./b",
            "..",
            ".",
            "a b/c",
            "a%2F../b",
            "%2e%2e/b",
            "a\\..\\b",
            "acme/repo\x01",
            "",
        ],
    )
    def test_rejects_traversal_and_malformed_remotes(self, remote):
        with pytest.raises(Exception, match="Could not parse git remote"):
            mcp_server._strict_repository_identifier(remote)

    @pytest.mark.asyncio
    async def test_traversal_remote_never_reaches_the_network(self, monkeypatch):
        """A traversal remote is refused locally: no HTTP request is issued by
        any project-scoped tool that resolves a caller-supplied remote."""
        import httpx

        async def explode(*args, **kwargs):
            raise AssertionError("must not be called")

        async def no_workspace():
            return ""

        monkeypatch.setattr(httpx.AsyncClient, "get", explode)
        monkeypatch.setattr(httpx.AsyncClient, "post", explode)
        monkeypatch.setattr(mcp_server, "_resolve_workspace_git_remote", no_workspace)
        for bad in (
            "../admin/users",
            "..%2fadmin%2fusers",
            "https://h/a/../../admin",
            "github.com/a/b?x=1",
            "github.com/a/b#f",
        ):
            with pytest.raises(Exception, match="Could not parse git remote URL"):
                mcp_server._strict_repository_identifier(bad)
            if bad.startswith("../"):
                # Path-shaped: never used as a lookup key; an unresolvable
                # path is refused locally.
                with pytest.raises(Exception, match="is a path but not a git repository"):
                    await mcp_server._resolve_project_id(bad)
            else:
                with pytest.raises(Exception, match="Could not parse git remote URL"):
                    await mcp_server._resolve_project_id(bad)

    @pytest.mark.asyncio
    async def test_marketplace_tools_refuse_traversal_remote(self, monkeypatch):
        import httpx

        calls: list[str] = []

        class _Client:
            def __init__(self, *a, **k):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def get(self, url, **kwargs):
                calls.append(url)
                raise AssertionError("must not be called")

        async def no_workspace():
            return ""

        monkeypatch.setattr(httpx, "AsyncClient", _Client)
        monkeypatch.setattr(mcp_server, "_resolve_workspace_git_remote", no_workspace)
        for handler, args in (
            (mcp_server._handle_list_skill_install_requests, {"git_remote": "../admin/users"}),
            (mcp_server._handle_load_marketplace_skill, {"version_id": _VER, "tool": "codex", "git_remote": "../admin/users"}),
            (mcp_server._handle_request_skill_install, {"version_id": _VER, "content_hash": "sha256:a", "git_remote": "../admin/users"}),
            (mcp_server._handle_get_skill_install_request, {"request_id": _REQ, "git_remote": "../admin/users"}),
            (mcp_server._handle_cancel_skill_install_request, {"request_id": _REQ, "git_remote": "../admin/users"}),
        ):
            result = await handler(args)
            assert "error" in result and "is a path but not a git repository" in result["error"]
        with pytest.raises(Exception, match="is a path but not a git repository"):
            await mcp_server._resolve_project_id("../admin/users")
        with pytest.raises(Exception, match="Could not parse git remote"):
            await mcp_server._resolve_project_id("acme/../admin/users")
        assert calls == []

    @pytest.mark.asyncio
    async def test_resolver_percent_encodes_segments(self, monkeypatch):
        import httpx

        calls: list[str] = []

        class _Resp:
            status_code = 200

            def json(self):
                return {"id": "prj_1"}

        class _Client:
            def __init__(self, *a, **k):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def get(self, url, **kwargs):
                calls.append(url)
                return _Resp()

        class _Cfg:
            class sync:
                api_key = "k"
                api_url = "https://api.test/"

        monkeypatch.setattr(httpx, "AsyncClient", _Client)
        monkeypatch.setattr(mcp_server, "load_config", lambda: _Cfg)
        api_url, _, project_id = await mcp_server._resolve_project_id("https://github.com/Acme/Skills.git")
        assert project_id == "prj_1"
        assert calls == ["https://api.test/api/v1/projects/acme/skills"]
        calls.clear()
        await mcp_server._resolve_project_id("github.com/Acme/Repo")
        assert calls == ["https://api.test/api/v1/projects/github.com/acme/repo"]
        calls.clear()
        await mcp_server._resolve_project_id("https://gitlab.com/group/sub/repo.git")
        assert calls == ["https://api.test/api/v1/projects/group/sub/repo"]


def test_strict_repository_identifier_accepts_userless_scp_remotes():
    """R19 P3-1: `github.com:owner/repo.git` is valid git syntax the server
    stores verbatim; the strict validator must accept it (and only it)."""
    from urllib.parse import quote

    from sessionfs.server.github_app import normalize_git_remote

    remote = "github.com:owner/repo.git"
    expected = "/".join(quote(seg, safe="") for seg in normalize_git_remote(remote).split("/"))
    assert mcp_server._strict_repository_identifier(remote) == expected
    assert mcp_server._strict_repository_identifier("gitlab.example.com:group/sub/repo") == "/".join(
        quote(seg, safe="") for seg in normalize_git_remote("gitlab.example.com:group/sub/repo").split("/")
    )
    for bad in ("github.com:owner/../repo", "github.com:owner/%2e%2e/repo", "github.com:owner/repo?x=1"):
        with pytest.raises(Exception, match="Could not parse git remote URL"):
            mcp_server._strict_repository_identifier(bad)


class TestPathSegmentValidation:
    """Shield-SR v0.15.1: the git_remote fix left the same request-forgery
    class open on every OTHER caller-supplied id interpolated into an API path
    (ticket_id, persona name, wiki slug, handoff id, work-queue id, run id,
    entry id, session id). `_path_segment` closes it."""

    @pytest.mark.parametrize(
        "value, allow_slash, expected",
        [
            ("tk_0123abcd", False, "tk_0123abcd"),
            ("atlas", False, "atlas"),
            (42, False, "42"),
            ("x?y", False, "x%3Fy"),
            ("x#frag", False, "x%23frag"),
            ("a b", False, "a%20b"),
            ("concepts/auth-flow", True, "concepts/auth-flow"),
        ],
    )
    def test_accepts_and_encodes(self, value, allow_slash, expected):
        assert mcp_server._path_segment(value, allow_slash=allow_slash) == expected

    @pytest.mark.parametrize(
        "value, allow_slash",
        [
            ("../../../admin/users", False),
            ("../../../admin/users", True),
            ("..", False),
            (".", True),
            ("%2e%2e", False),
            ("%2E%2E", True),
            ("a/b", False),
            ("a%2Fb", False),
            ("concepts/../x", True),
            ("concepts//x", True),
            ("a\\..\\b", True),
            ("tk_1\n", False),
            ("", False),
            (None, False),
        ],
    )
    def test_rejects_traversal(self, value, allow_slash):
        with pytest.raises(ValueError):
            mcp_server._path_segment(value, allow_slash=allow_slash)

    @pytest.mark.asyncio
    async def test_get_ticket_cannot_reach_other_endpoint(self, monkeypatch):
        """The exploit: get_ticket(ticket_id='../../../admin/users') used to
        issue GET /api/v1/admin/users with the user's key."""
        async def fake_resolve(git_remote=""):
            return "https://api.example", "sk_sfs_test", "proj_1"

        monkeypatch.setattr(mcp_server, "_resolve_project_id", fake_resolve)
        requested: list[str] = []

        class _Client:
            def __init__(self, *a, **k):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def get(self, url, **kwargs):
                requested.append(url)
                raise AssertionError("request must not be sent")

        import httpx

        monkeypatch.setattr(httpx, "AsyncClient", _Client)
        with pytest.raises(ValueError):
            await mcp_server._handle_get_ticket({"ticket_id": "../../../admin/users"})
        assert requested == []

    def test_ssh_host_cannot_inject_a_dot_segment(self):
        """normalize_git_remote drops the host of URL remotes, so a dot-segment
        host never reaches the key."""
        for remote, key in (("ssh://../admin", "admin"), ("ssh://./admin", "admin"),
                            ("git://../x/y", "x/y")):
            assert mcp_server._strict_repository_identifier(remote) == key
        for remote in ("ssh://host/../admin", "git://host/x/./y"):
            with pytest.raises(ValueError):
                mcp_server._strict_repository_identifier(remote)

    @pytest.mark.parametrize(
        "remote",
        ["git://github.com/o/r.git", "git+ssh://git@h.example/o/r.git", "HTTPS://github.com/o/r"],
    )
    def test_other_git_schemes_still_resolve(self, remote):
        """Reject-only: schemes the server stores verbatim keep working."""
        from urllib.parse import quote

        from sessionfs.server.github_app import normalize_git_remote

        key = mcp_server._strict_repository_identifier(remote)
        assert key == "/".join(quote(s, safe="") for s in normalize_git_remote(remote).split("/"))


class TestRemoteMcpPathSafety:
    """The hosted (remote) MCP server had the same unvalidated interpolation."""

    def test_cloud_client_encodes_session_id(self):
        import inspect

        from sessionfs.mcp import cloud_client

        src = inspect.getsource(cloud_client.CloudAPIClient)
        assert "/api/v1/sessions/{session_id}" not in src
        assert "_path_segment(session_id)" in src

    def test_remote_server_uses_strict_validator(self):
        import inspect

        from sessionfs.mcp import remote_server

        src = inspect.getsource(remote_server)
        assert "_strict_repository_identifier(git_remote)" in src


class TestRepositoryKeyParityWithServer:
    """The lookup key must be EXACTLY what the server stored for the project
    (`normalize_git_remote` of the raw remote), or every project-scoped MCP
    tool reports an existing project as missing."""

    @pytest.mark.parametrize(
        "remote",
        [
            "https://github.com/org/repo",
            "https://github.com/org/repo/",
            "https://github.com/org/repo.git",
            "git@github.com:org/repo.git",
            "git@github.com:org/repo.git/",
            "ssh://git@host/a/b/c.git",
            "git://host.example/team/project.git",
            "HTTPS://GitHub.com/Org/Repo",
            "https://gitlab.com/group/subgroup/repo.git",
            "github.com:owner/repo.git",
            "github.com/acme/repo",
            "file:///srv/repos/foo.git",
            "file:///srv/repos/foo",
            "  https://github.com/org/repo  ",
            "git@example.com:/srv/git/project.git",
            "user@host:/abs/path/repo.git",
            "/srv/git/project.git",
            "/home/user/repos/thing",
            "file://localhost/srv/repos/foo.git",
            "ssh://git@host:2222/org/repo.git",
            "git+ssh://git@host/org/repo.git",
            "git@host:~user/repo.git",
            "~/repos/mine.git",
        ],
    )
    def test_key_decodes_to_the_server_normalization(self, remote):
        from urllib.parse import unquote

        from sessionfs.mcp.path_safety import _strict_repository_identifier
        from sessionfs.server.github_app import normalize_git_remote

        key = _strict_repository_identifier(remote)
        assert unquote(key) == normalize_git_remote(remote.strip())

    def test_file_scheme_alone_may_omit_the_host(self):
        from sessionfs.mcp.path_safety import _strict_repository_identifier

        _strict_repository_identifier("file:///srv/repos/foo.git")
        for bad in ("file:///srv/../admin", "file:///srv/%2e%2e/admin"):
            with pytest.raises(ValueError):
                _strict_repository_identifier(bad)


    def test_relative_and_doubled_separators_stay_rejected(self):
        """Relative local remotes carry traversal segments in the very key the
        server stored, so they can never be requested safely. (Empty segments,
        e.g. doubled separators, are harmless: they cannot collapse or leave the
        /api/v1/projects/ prefix, so they are accepted with server parity.)"""
        from sessionfs.mcp.path_safety import _strict_repository_identifier

        for bad in ("./relative/repo", "../sibling/repo", "/srv/../admin", "/srv/%2e%2e/admin"):
            with pytest.raises(ValueError):
                _strict_repository_identifier(bad)


def test_remote_validator_fuzz_matches_the_server_exactly():
    """Generative spec of the validator, guarding against it narrowing what the
    server accepts (four compatibility regressions were found one shape at a
    time before the validator was redefined over the requested key).

    For every generated git-legal remote: accepted iff no segment of the key
    the server stores is `.`/`..` (raw or percent-decoded) or decodes to a
    separator/control character; when accepted, the lookup key decodes to
    exactly that stored key.
    """
    import random
    from urllib.parse import unquote

    from sessionfs.mcp.path_safety import _strict_repository_identifier
    from sessionfs.server.github_app import normalize_git_remote

    def unsafe(stored: str) -> bool:
        for seg in stored.split("/"):
            dec = unquote(seg)
            if seg in {".", ".."} or dec in {".", ".."} or "/" in dec or "\\" in dec:
                return True
            if any(ord(c) < 32 or ord(c) == 127 for c in dec):
                return True
        return False

    rng = random.Random(20261006)
    seg_alphabet = "abcXYZ019-_.~+@:%"
    prefixes = ["", "/", "https://h.example/", "ssh://git@h.example/", "git@h.example:",
                "git@h.example:/", "h.example:", "file:///", "git://h/", "user@host:/"]

    def segment():
        if rng.random() < 0.08:
            return rng.choice([".", "..", "%2e", "%2E%2e", "%2f", "%2F..%2F", "%01"])
        return "".join(rng.choice(seg_alphabet) for _ in range(rng.randint(1, 6)))

    accepted = rejected = 0
    for _ in range(5000):
        remote = rng.choice(prefixes) + "/".join(segment() for _ in range(rng.randint(1, 4)))
        if rng.random() < 0.2:
            remote += ".git"
        if rng.random() < 0.1:
            remote += "/"
        stored = normalize_git_remote(remote)
        if not stored:
            continue
        if unsafe(stored):
            with pytest.raises(ValueError):
                _strict_repository_identifier(remote)
            rejected += 1
            continue
        key = _strict_repository_identifier(remote)
        assert unquote(key) == stored, f"{remote!r}: key {key!r} != stored {stored!r}"
        accepted += 1
    assert accepted > 2000 and rejected > 100


class TestGitRemotePathArgument:
    """tk_a68849d2ade844b5: agents pass the repo (or worktree) directory as
    `git_remote`. A path resolves through the repo's `origin`; a path that is
    not a repo with an origin is an error (never a silent workspace fallback,
    which could send a write to the wrong project)."""

    ORIGIN = "https://github.com/Acme/Widgets.git"
    WORKSPACE = "https://github.com/acme/workspace.git"

    @staticmethod
    def _git(*args: str) -> None:
        import subprocess

        subprocess.run(["git", *args], check=True, capture_output=True, text=True)

    @pytest.fixture
    def repo(self, tmp_path: Path) -> Path:
        repo = tmp_path / "Repo" / "Widgets"
        repo.mkdir(parents=True)
        self._git("init", "-q", str(repo))
        self._git("-C", str(repo), "config", "user.name", "t")
        self._git("-C", str(repo), "config", "user.email", "t@example.com")
        self._git("-C", str(repo), "remote", "add", "origin", self.ORIGIN)
        self._git(
            "-C", str(repo), "-c", "commit.gpgsign=false",
            "commit", "-q", "--allow-empty", "-m", "init",
        )
        return repo

    @pytest.fixture
    def lookups(self, monkeypatch) -> list[str]:
        """Record project lookups; workspace detection returns WORKSPACE."""
        import httpx

        calls: list[str] = []

        class _Resp:
            status_code = 200

            def json(self):
                return {"id": "prj_1"}

        class _Client:
            def __init__(self, *a, **k):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def get(self, url, **kwargs):
                calls.append(url)
                return _Resp()

        class _Cfg:
            class sync:
                api_key = "k"
                api_url = "https://api.test"

        async def workspace():
            return self.WORKSPACE

        monkeypatch.setattr(httpx, "AsyncClient", _Client)
        monkeypatch.setattr(mcp_server, "load_config", lambda: _Cfg)
        # Some handlers import load_config inside the function body.
        monkeypatch.setattr("sessionfs.daemon.config.load_config", lambda: _Cfg)
        monkeypatch.setattr(mcp_server, "_resolve_workspace_git_remote", workspace)
        return calls

    @pytest.mark.asyncio
    async def test_repo_path_resolves_origin(self, repo, lookups):
        await mcp_server._resolve_project_id(str(repo))
        assert lookups == ["https://api.test/api/v1/projects/acme/widgets"]

    @pytest.mark.asyncio
    async def test_worktree_path_resolves_origin(self, repo, tmp_path, lookups):
        worktree = tmp_path / "Worktrees" / "Feature"
        self._git("-C", str(repo), "worktree", "add", "-q", "-b", "feature", str(worktree))
        await mcp_server._resolve_project_id(str(worktree))
        assert lookups == ["https://api.test/api/v1/projects/acme/widgets"]

    @pytest.mark.asyncio
    async def test_tilde_path_resolves_origin(self, repo, tmp_path, monkeypatch, lookups):
        monkeypatch.setenv("HOME", str(tmp_path))
        await mcp_server._resolve_project_id("~/Repo/Widgets")
        assert lookups == ["https://api.test/api/v1/projects/acme/widgets"]

    @pytest.mark.asyncio
    async def test_non_repo_path_is_an_error(self, tmp_path, lookups):
        plain = tmp_path / "Plain"
        plain.mkdir()
        with pytest.raises(Exception, match="is a path but not a git repository"):
            await mcp_server._resolve_project_id(str(plain))
        assert lookups == []

    @pytest.mark.asyncio
    async def test_repo_without_origin_is_an_error(self, tmp_path, lookups):
        no_origin = tmp_path / "NoOrigin"
        self._git("init", "-q", str(no_origin))
        with pytest.raises(Exception, match="is a path but not a git repository"):
            await mcp_server._resolve_project_id(str(no_origin))
        assert lookups == []

    @pytest.mark.asyncio
    async def test_nonexistent_path_is_an_error(self, tmp_path, lookups):
        missing = str(tmp_path / "Missing" / "Repo")
        with pytest.raises(Exception, match="omit git_remote to use the current workspace") as exc:
            await mcp_server._resolve_project_id(missing)
        assert missing in str(exc.value)
        assert lookups == []

    @pytest.mark.asyncio
    async def test_directory_named_like_remote_is_still_a_remote(self, tmp_path, monkeypatch, lookups):
        """Only explicit path forms are paths: `acme/repo` matching a local
        directory is looked up as a remote."""
        (tmp_path / "acme" / "repo").mkdir(parents=True)
        monkeypatch.chdir(tmp_path)
        await mcp_server._resolve_project_id("acme/repo")
        assert lookups == ["https://api.test/api/v1/projects/acme/repo"]

    @pytest.mark.asyncio
    async def test_remote_url_unchanged(self, lookups):
        await mcp_server._resolve_project_id("github.com/Acme/Repo")
        await mcp_server._resolve_project_id("git@github.com:Acme/Repo.git")
        assert lookups == [
            "https://api.test/api/v1/projects/github.com/acme/repo",
            "https://api.test/api/v1/projects/acme/repo",
        ]

    @pytest.mark.asyncio
    async def test_inline_resolvers_share_path_handling(self, repo, lookups):
        """Tools that look the project up without `_resolve_project_id`
        resolve a path the same way."""
        await mcp_server._handle_get_project_context({"git_remote": str(repo)})
        await mcp_server._handle_search_knowledge(
            {"query": "widgets", "git_remote": str(repo)}
        )
        assert lookups[0] == "https://api.test/api/v1/projects/acme/widgets"
        assert lookups[1] == "https://api.test/api/v1/projects/acme/widgets"

    @pytest.mark.asyncio
    async def test_inline_resolvers_report_unresolvable_path(self, tmp_path, lookups):
        missing = str(tmp_path / "Missing")
        for result in (
            await mcp_server._handle_get_project_context({"git_remote": missing}),
            await mcp_server._handle_search_knowledge({"query": "widgets", "git_remote": missing}),
        ):
            assert "is a path but not a git repository" in result
        assert lookups == []

    @pytest.mark.asyncio
    async def test_fetch_kb_entries_raw_resolves_repo_path(self, repo, tmp_path, lookups):
        await mcp_server._fetch_kb_entries_raw({"query": "widgets", "git_remote": str(repo)})
        assert lookups[0] == "https://api.test/api/v1/projects/acme/widgets"
        lookups.clear()
        entries = await mcp_server._fetch_kb_entries_raw(
            {"query": "widgets", "git_remote": str(tmp_path / "Missing")}
        )
        assert entries == []
        assert lookups == []

    @pytest.mark.asyncio
    async def test_no_project_error_explains_git_remote(self, monkeypatch):
        import httpx

        class _Resp:
            status_code = 404

        class _Client:
            def __init__(self, *a, **k):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def get(self, url, **kwargs):
                return _Resp()

        class _Cfg:
            class sync:
                api_key = "k"
                api_url = "https://api.test"

        monkeypatch.setattr(httpx, "AsyncClient", _Client)
        monkeypatch.setattr(mcp_server, "load_config", lambda: _Cfg)
        with pytest.raises(Exception, match="remote URL like github.com/org/repo") as exc:
            await mcp_server._resolve_project_id("github.com/acme/none")
        assert "omitted to use the current workspace" in str(exc.value)

    def test_git_remote_descriptions_are_shared(self):
        described = [
            tool.name
            for tool in mcp_server._TOOLS
            if "git_remote" in tool.inputSchema.get("properties", {})
        ]
        assert described
        for tool in mcp_server._TOOLS:
            prop = tool.inputSchema.get("properties", {}).get("git_remote")
            if prop is not None:
                assert prop.get("description") == mcp_server._GIT_REMOTE_DESCRIPTION, tool.name


class TestAskSessionHelper:
    """ask_session_helper routes to POST /api/v1/helper/turns (JSON mode).

    The handler authenticates with the user key from load_config() and does
    NOT resolve a git remote, so tests patch load_config + httpx.AsyncClient
    directly.
    """

    class _Cfg:
        class sync:
            api_key = "test-key"
            api_url = "https://api.test"

    @pytest.fixture
    def fake_client(self, monkeypatch):
        import httpx

        captured = {}

        class _FakeResponse:
            def __init__(self, status_code, body, text=None):
                self.status_code = status_code
                self._body = body
                self.text = text if text is not None else json.dumps(body)

            def json(self):
                return self._body

        class _FakeAsyncClient:
            def __init__(self, *a, **k):
                captured["timeout"] = k.get("timeout")

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def post(self, url, *, json=None, headers=None):
                captured["url"] = url
                captured["json"] = json
                captured["headers"] = headers or {}
                return captured["response"]()

        monkeypatch.setattr(httpx, "AsyncClient", _FakeAsyncClient)
        monkeypatch.setattr(mcp_server, "load_config", lambda: self._Cfg)
        return captured

    @pytest.mark.asyncio
    async def test_posts_turn_and_returns_answer(self, fake_client):
        fake_client["response"] = lambda: type(
            "R", (), {
                "status_code": 200,
                "text": "",
                "json": lambda self: {
                    "conversation_id": "hc_1",
                    "answer": "It was the auth middleware.",
                    "citations": [{"session_id": "ses_1", "message_index": 2, "title": "Auth"}],
                    "tool_calls": [],
                    "usage": {"input_tokens": 10, "output_tokens": 5},
                    "stop_reason": "stop",
                    "key_source": "user",
                },
            },
        )()
        result = await mcp_server._handle_ask_session_helper({"question": "what failed?"})
        assert result["answer"] == "It was the auth middleware."
        assert result["citations"][0]["session_id"] == "ses_1"
        assert fake_client["url"] == "https://api.test/api/v1/helper/turns"
        assert fake_client["json"] == {"message": "what failed?", "stream": False}
        assert fake_client["headers"]["Authorization"] == "Bearer test-key"

    @pytest.mark.asyncio
    async def test_scopes_project_and_sessions(self, fake_client):
        fake_client["response"] = lambda: type(
            "R", (), {"status_code": 200, "text": "{}", "json": lambda self: {}},
        )()
        await mcp_server._handle_ask_session_helper({
            "question": "summarize",
            "project": "proj_1",
            "session_ids": ["ses_1", "ses_2"],
            "conversation_id": "hc_1",
        })
        assert fake_client["json"] == {
            "message": "summarize",
            "stream": False,
            "conversation_id": "hc_1",
            "scope": {"project_id": "proj_1", "session_ids": ["ses_1", "ses_2"]},
        }

    @pytest.mark.asyncio
    async def test_requires_question(self, fake_client):
        result = await mcp_server._handle_ask_session_helper({"question": "   "})
        assert result == {"error": "A question is required."}

    @pytest.mark.asyncio
    async def test_uses_600_second_timeout(self, fake_client):
        fake_client["response"] = lambda: type(
            "R", (), {"status_code": 200, "text": "{}", "json": lambda self: {}},
        )()
        await mcp_server._handle_ask_session_helper({"question": "hi"})
        assert fake_client["timeout"] == 600.0

    @pytest.mark.asyncio
    async def test_rejects_non_string_question(self, fake_client):
        result = await mcp_server._handle_ask_session_helper({"question": 42})
        assert result == {"error": "question must be a string."}

    @pytest.mark.asyncio
    async def test_rejects_question_over_max_chars(self, fake_client):
        result = await mcp_server._handle_ask_session_helper({"question": "x" * 20_001})
        assert result == {"error": "The question must be at most 20000 characters."}

    @pytest.mark.asyncio
    async def test_accepts_question_at_max_chars(self, fake_client):
        fake_client["response"] = lambda: type(
            "R", (), {"status_code": 200, "text": "{}", "json": lambda self: {}},
        )()
        question = "x" * 20_000
        await mcp_server._handle_ask_session_helper({"question": question})
        assert fake_client["json"]["message"] == question

    @pytest.mark.asyncio
    async def test_rejects_non_string_project(self, fake_client):
        result = await mcp_server._handle_ask_session_helper(
            {"question": "hi", "project": 42},
        )
        assert result == {"error": "project must be a string."}

    @pytest.mark.asyncio
    async def test_rejects_non_string_conversation_id(self, fake_client):
        result = await mcp_server._handle_ask_session_helper(
            {"question": "hi", "conversation_id": 42},
        )
        assert result == {"error": "conversation_id must be a string."}

    @pytest.mark.asyncio
    async def test_rejects_non_list_session_ids(self, fake_client):
        result = await mcp_server._handle_ask_session_helper(
            {"question": "hi", "session_ids": 42},
        )
        assert result == {"error": "session_ids must be a list of strings."}

    @pytest.mark.asyncio
    async def test_rejects_non_string_session_id(self, fake_client):
        # None must be rejected, not coerced to the string "None".
        result = await mcp_server._handle_ask_session_helper(
            {"question": "hi", "session_ids": ["ses_1", None]},
        )
        assert result == {"error": "session_ids must be a list of strings."}

    @pytest.mark.asyncio
    async def test_rejects_too_many_session_ids(self, fake_client):
        result = await mcp_server._handle_ask_session_helper(
            {"question": "hi", "session_ids": [f"ses_{i}" for i in range(101)]},
        )
        assert result == {"error": "session_ids accepts at most 100 IDs."}

    @pytest.mark.asyncio
    async def test_filters_blank_session_ids(self, fake_client):
        fake_client["response"] = lambda: type(
            "R", (), {"status_code": 200, "text": "{}", "json": lambda self: {}},
        )()
        await mcp_server._handle_ask_session_helper(
            {"question": "hi", "session_ids": ["ses_1", "", "  "]},
        )
        assert fake_client["json"]["scope"] == {"session_ids": ["ses_1"]}

    @pytest.mark.asyncio
    async def test_error_extracts_detail_message(self, fake_client):
        fake_client["response"] = lambda: type(
            "R", (), {
                "status_code": 409,
                "text": '{"detail": {"code": "helper_key_required", "message": "No LLM key"}}',
                "json": lambda self: {
                    "detail": {"code": "helper_key_required", "message": "No LLM key"}
                },
            },
        )()
        result = await mcp_server._handle_ask_session_helper({"question": "hi"})
        assert result["error"] == "API error 409: No LLM key"

    @pytest.mark.asyncio
    async def test_not_authenticated(self, monkeypatch):
        class _NoKeyCfg:
            class sync:
                api_key = ""
                api_url = "https://api.test"

        monkeypatch.setattr(mcp_server, "load_config", lambda: _NoKeyCfg)
        result = await mcp_server._handle_ask_session_helper({"question": "hi"})
        assert result == {"error": "Not authenticated. Run 'sfs auth login' first."}
