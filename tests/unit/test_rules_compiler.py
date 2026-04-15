"""Unit tests for the rules compilers (no DB, no network)."""

from __future__ import annotations

import pytest

from sessionfs.server.services.rules_compiler import (
    COMPILERS,
    SUPPORTED_TOOLS,
    CompileContext,
    KnowledgeClaim,
    aggregate_hash,
    get_compiler,
    is_managed_content,
    parse_managed_marker,
)


@pytest.fixture
def basic_ctx() -> CompileContext:
    return CompileContext(
        static_rules="Use tabs not spaces.\nBranch off develop.",
        knowledge_claims=[
            KnowledgeClaim(
                entry_type="convention",
                content="API routes live in src/server/routes/",
                entity_ref="src/server/routes/",
            ),
            KnowledgeClaim(
                entry_type="decision",
                content="We chose PostgreSQL over MySQL for JSON support",
            ),
        ],
        context_sections={
            "overview": "Session portability tool for AI coding agents.",
            "architecture": "FastAPI + PostgreSQL + S3.",
        },
        version=1,
    )


class TestDeterminism:
    def test_same_inputs_produce_same_hash(self, basic_ctx: CompileContext):
        for tool in SUPPORTED_TOOLS:
            compiler = get_compiler(tool)
            r1 = compiler.compile(basic_ctx)
            r2 = compiler.compile(basic_ctx)
            assert r1.content == r2.content, f"{tool} non-deterministic content"
            assert r1.content_hash == r2.content_hash, f"{tool} non-deterministic hash"

    def test_different_inputs_produce_different_hashes(self, basic_ctx: CompileContext):
        compiler = get_compiler("claude-code")
        r1 = compiler.compile(basic_ctx)
        ctx2 = CompileContext(
            static_rules=basic_ctx.static_rules + "\nExtra line.",
            knowledge_claims=basic_ctx.knowledge_claims,
            context_sections=basic_ctx.context_sections,
            version=basic_ctx.version,
        )
        r2 = compiler.compile(ctx2)
        assert r1.content_hash != r2.content_hash

    def test_aggregate_hash_stable(self, basic_ctx: CompileContext):
        results_a = [COMPILERS[t].compile(basic_ctx) for t in SUPPORTED_TOOLS]
        results_b = [COMPILERS[t].compile(basic_ctx) for t in reversed(SUPPORTED_TOOLS)]
        assert aggregate_hash(results_a) == aggregate_hash(results_b)


class TestManagedMarker:
    def test_output_contains_managed_marker(self, basic_ctx: CompileContext):
        for tool in SUPPORTED_TOOLS:
            r = get_compiler(tool).compile(basic_ctx)
            assert is_managed_content(r.content), f"{tool} missing marker"
            parsed = parse_managed_marker(r.content)
            assert parsed is not None
            version, _hash = parsed
            assert version == 1

    def test_marker_reflects_version(self, basic_ctx: CompileContext):
        basic_ctx.version = 5
        r = get_compiler("claude-code").compile(basic_ctx)
        parsed = parse_managed_marker(r.content)
        assert parsed is not None
        assert parsed[0] == 5

    def test_unmanaged_content_detected(self):
        assert not is_managed_content("# Plain markdown file")
        assert not is_managed_content("")
        assert not is_managed_content(None)


class TestFilenames:
    def test_expected_filenames(self):
        expected = {
            "claude-code": "CLAUDE.md",
            "codex": "codex.md",
            "cursor": ".cursorrules",
            "copilot": ".github/copilot-instructions.md",
            "gemini": "GEMINI.md",
        }
        for tool, fname in expected.items():
            assert get_compiler(tool).filename == fname


class TestKnowledgeFraming:
    def test_claims_rendered_descriptive(self, basic_ctx: CompileContext):
        r = get_compiler("claude-code").compile(basic_ctx)
        # "Project fact:" framing per design rule #5
        assert "Project fact:" in r.content
        assert "API routes live in src/server/routes/" in r.content


class TestContextInjection:
    def test_context_sections_present(self, basic_ctx: CompileContext):
        r = get_compiler("claude-code").compile(basic_ctx)
        assert "Session portability" in r.content
        assert "FastAPI" in r.content


class TestTokenCondensation:
    def test_cursor_smaller_than_claude(self, basic_ctx: CompileContext):
        # Large knowledge payload
        big_claims = [
            KnowledgeClaim(entry_type="convention", content=f"Fact {i}: " + "x" * 200)
            for i in range(100)
        ]
        ctx = CompileContext(
            static_rules="x" * 500,
            knowledge_claims=big_claims,
            context_sections={"overview": "y" * 5000, "architecture": "z" * 5000},
            version=1,
        )
        cursor_out = get_compiler("cursor").compile(ctx)
        claude_out = get_compiler("claude-code").compile(ctx)
        # Cursor has the tightest ceiling — must produce smaller output
        assert len(cursor_out.content) < len(claude_out.content)

    def test_empty_inputs_still_produce_valid_output(self):
        ctx = CompileContext(static_rules="", version=1)
        for tool in SUPPORTED_TOOLS:
            r = get_compiler(tool).compile(ctx)
            assert is_managed_content(r.content)
            assert r.token_count >= 1


class TestToolOverrides:
    def test_tool_override_extra_is_injected(self, basic_ctx: CompileContext):
        basic_ctx.tool_overrides = {
            "cursor": {"extra": "Cursor-only: keep responses terse."}
        }
        cursor_out = get_compiler("cursor").compile(basic_ctx)
        claude_out = get_compiler("claude-code").compile(basic_ctx)
        assert "keep responses terse" in cursor_out.content
        assert "keep responses terse" not in claude_out.content
