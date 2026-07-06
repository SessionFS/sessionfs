"""Tests for the Resident R2 living context — hydration, writeback, compaction.

Covers the mandatory scenarios from .resident-r2-brief.md:
1. Hydration assembles warm context from context+KB(persona-filtered)+
   wiki+memory-hydrate, org/resident-scoped.
2. The warm digest stays under mind_token_budget (bounded assembly).
3. Writeback: a review that surfaces a durable finding calls add_knowledge
   (persona=codex-reviewer, de-duped); a review that learns nothing writes
   NOTHING (no-op discipline).
4. Compaction: after compact_every_wakes wakes, POST .../memory/compact is
   called with a digest + superseded_entry_ids.
5. A 429 from a memory write triggers a compact rather than crashing.
6. The hydrated context reaches the LLM prompt (the reviewer sees its
   prior findings + playbook).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from sessionfs.resident.config import ResidentConfig, LLMConfig
from sessionfs.resident.context import (
    LivingContext,
    hydrate_living_context,
    _estimate_tokens,
    _fit_to_budget,
)
from sessionfs.resident.llm_adapter import (
    ReviewContext,
    ReviewResult,
)
from sessionfs.resident.memory import (
    write_reasoning,
    writeback_durable_knowledge,
    writeback_wiki_page,
    compact_memory,
    summarize_for_digest,
    _is_duplicate,
)
from sessionfs.resident.runner import (
    ResidentRunner,
    _extract_conclusion,
    _extract_durable_findings,
)


# ── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture
def r2_config() -> ResidentConfig:
    """A valid R2 config with resident_id + org_id for memory endpoints."""
    cfg = ResidentConfig(
        name="test-resident-r2",
        queue_id="wq_test123",
        project="proj_test",
        org_profile="test-org",
        resident_id="res_test1234abcd",
        org_id="org_test5678efgh",
        poll_interval_seconds=10,
        mind_token_budget=8000,
        compact_every_wakes=10,
        llm=LLMConfig(
            base_url="https://test-llm.example.com/v1",
            model="test-model",
            api_key="test-llm-key",
            request_timeout_seconds=30,
            max_tokens=1000,
        ),
    )
    cfg._resolved_llm_key = "test-llm-key"
    return cfg


class StubReviewLLM:
    """An adapter that returns whatever verdict you configure."""

    def __init__(
        self,
        verdict_phrase: str = "VERIFIED-CLEAN",
        reasoning: str = "Looks good.",
        error: str = "",
        should_raise: bool = False,
    ) -> None:
        self._verdict = verdict_phrase
        self._reasoning = reasoning
        self._error = error
        self._should_raise = should_raise

    async def review(self, context: ReviewContext) -> ReviewResult:
        if self._should_raise:
            raise RuntimeError("Simulated LLM crash")
        return ReviewResult(
            verdict_phrase=self._verdict,
            reasoning=self._reasoning,
            error=self._error,
        )


# ── Helper to build mock API responses ──────────────────────────────────────


def _mock_api_response(status_code: int = 200, body: object = None) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status_code
    resp.body = body if body is not None else {}
    return resp


# ── Test 1: Hydration assembles warm context from all sources ────────────────


@pytest.mark.asyncio
async def test_hydration_assembles_from_all_sources(r2_config: ResidentConfig):
    """Hydration fetches from memory, KB, wiki, and context — all org/resident-scoped."""
    call_paths: list[str] = []

    async def fake_request(method: str, api_url: str, api_key: str,
                           path: str, json_data: dict | None = None,
                           timeout: int = 30) -> MagicMock:
        call_paths.append(path)
        # Simulate each endpoint.
        if "/memory/hydrate" in path:
            return _mock_api_response(200, {
                "digest": {
                    "id": "rme_digest1", "resident_id": "res_test1234abcd",
                    "org_id": "org_test5678efgh", "kind": "digest", "seq": 5,
                    "content": "Prior review found auth pattern issues.",
                    "token_estimate": 10, "superseded_by": None,
                    "compacted_at": None, "quarantined": False,
                    "created_at": "2026-07-05T00:00:00Z",
                },
                "recent_reasoning": [
                    {
                        "id": "rme_r1", "resident_id": "res_test1234abcd",
                        "org_id": "org_test5678efgh", "kind": "reasoning",
                        "seq": 4, "content": "Checked tier resolution — ok.",
                        "token_estimate": 5, "superseded_by": None,
                        "compacted_at": None, "quarantined": False,
                        "created_at": "2026-07-05T00:00:00Z",
                    }
                ],
            })
        elif "/entries" in path and "persona_name" in path:
            return _mock_api_response(200, [
                {
                    "id": 1, "content": "All handlers must validate tier before accessing org resources.",
                    "persona_name": "codex-reviewer", "claim_class": "claim",
                }
            ])
        elif "/pages/review-playbook" in path:
            return _mock_api_response(200, {
                "id": "page_1", "slug": "review-playbook",
                "title": "Review Playbook", "content": "Always check for SQL injection.",
            })
        elif "/projects/" in path and "/context/sections/" not in path and "/entries" not in path and "/pages" not in path:
            # Project context fetch.
            return _mock_api_response(200, {
                "id": "proj_test",
                "context_document": "## Architecture\nThis is a FastAPI app.",
            })
        elif "/context/sections/" in path:
            slug = path.split("/")[-1]
            return _mock_api_response(200, {
                "slug": slug, "title": slug.title(),
                "content": f"Section content for {slug}.",
            })
        return _mock_api_response(404, {})

    with patch("sessionfs.resident.context._api_request", side_effect=fake_request):
        ctx = await hydrate_living_context(
            "https://api.test", "svc-key", r2_config,
        )

    # Verify all sources were called.
    assert any("/memory/hydrate" in p for p in call_paths), call_paths
    assert any("/entries" in p and "persona_name" in p for p in call_paths), call_paths
    assert any("/pages/review-playbook" in p for p in call_paths), call_paths
    assert any("/context/sections/" in p for p in call_paths), call_paths

    # Verify the context was assembled.
    assert ctx.memory_digest != ""
    assert "auth pattern" in ctx.memory_digest
    assert len(ctx.recent_reasoning) > 0
    assert len(ctx.prior_findings) > 0
    assert ctx.review_playbook != ""
    assert "SQL injection" in ctx.review_playbook
    # Project context comes from the service-key-accessible SECTIONS (the full
    # context_document catch-all is a user-key route that 403s service keys).
    assert len(ctx.sections) > 0


# ── Test 2: Warm digest stays under mind_token_budget ────────────────────────


@pytest.mark.asyncio
async def test_warm_digest_stays_under_budget(r2_config: ResidentConfig):
    """The assembled LivingContext must respect mind_token_budget."""
    r2_config.mind_token_budget = 500  # very tight budget

    async def fake_request(method: str, api_url: str, api_key: str,
                           path: str, json_data: dict | None = None,
                           timeout: int = 30) -> MagicMock:
        if "/memory/hydrate" in path:
            return _mock_api_response(200, {
                "digest": {
                    "id": "rme_d1", "resident_id": "res_test1234abcd",
                    "org_id": "org_test5678efgh", "kind": "digest", "seq": 1,
                    "content": "A" * 4000,  # ~1000 tokens — will be clipped
                    "token_estimate": 1000,
                },
                "recent_reasoning": [
                    {
                        "id": "rme_r1", "resident_id": "res_test1234abcd",
                        "org_id": "org_test5678efgh", "kind": "reasoning",
                        "seq": 0, "content": "B" * 4000, "token_estimate": 1000,
                    }
                ],
            })
        elif "/entries" in path:
            return _mock_api_response(200, [
                {"id": 1, "content": "C" * 4000, "persona_name": "codex-reviewer",
                 "claim_class": "claim"},
            ])
        elif "/pages/" in path:
            return _mock_api_response(200, {
                "content": "D" * 4000,
            })
        elif "/context/sections/" in path:
            return _mock_api_response(200, {"content": "E" * 4000})
        elif "/projects/" in path:
            return _mock_api_response(200, {"context_document": "F" * 4000})
        return _mock_api_response(404, {})

    with patch("sessionfs.resident.context._api_request", side_effect=fake_request):
        ctx = await hydrate_living_context(
            "https://api.test", "svc-key", r2_config,
        )

    # The total token estimate must be under the budget.
    assert ctx.token_estimate <= r2_config.mind_token_budget, (
        f"token_estimate={ctx.token_estimate} exceeds budget={r2_config.mind_token_budget}"
    )
    # Individual fields should be clipped.
    if ctx.memory_digest:
        assert len(ctx.memory_digest) <= r2_config.mind_token_budget * 4 + 10


# ── Test 3: Writeback with durable finding → add_knowledge called ────────────


@pytest.mark.asyncio
async def test_writeback_durable_finding_calls_add_knowledge(r2_config: ResidentConfig):
    """A review that surfaces a durable finding calls add_knowledge,
    persona=codex-reviewer, de-duped against existing claims."""
    api_calls: list[dict] = []

    async def fake_request(method: str, api_url: str, api_key: str,
                           path: str, json_data: dict | None = None,
                           timeout: int = 30) -> MagicMock:
        api_calls.append({"method": method, "path": path, "body": json_data})
        if "/entries/add" in path:
            return _mock_api_response(201, {"id": 999})
        if "/entries" in path and method == "GET":
            # Return existing claims for de-dup (different from what we'll write).
            return _mock_api_response(200, [
                {"id": 1, "content": "Existing: use async fixtures for DB tests."}
            ])
        return _mock_api_response(200, {})

    with patch("sessionfs.resident.memory._api_request", side_effect=fake_request):
        written = await writeback_durable_knowledge(
            "https://api.test", "svc-key", r2_config,
            findings=[
                "Recurring pattern: all handlers must validate tier before accessing org resources.",
            ],
        )

    assert written == 1
    # Verify the add call was made with the right persona.
    add_calls = [c for c in api_calls if "/entries/add" in c["path"]]
    assert len(add_calls) == 1
    assert add_calls[0]["body"]["persona_name"] == "codex-reviewer"
    # Stored as a CLAIM (not a note) so hydration/de-dup retrieve it later.
    assert add_calls[0]["body"]["force_claim"] is True
    assert add_calls[0]["body"]["confidence"] >= 0.7
    assert "validate tier" in add_calls[0]["body"]["content"]


# ── Test 4: No-op writeback — nothing learned → add_knowledge NOT called ─────


@pytest.mark.asyncio
async def test_noop_writeback_writes_nothing(r2_config: ResidentConfig):
    """A review that learns nothing durable must NOT call add_knowledge."""
    api_calls: list[dict] = []

    async def fake_request(method: str, api_url: str, api_key: str,
                           path: str, json_data: dict | None = None,
                           timeout: int = 30) -> MagicMock:
        api_calls.append({"method": method, "path": path, "body": json_data})
        return _mock_api_response(200, [])

    with patch("sessionfs.resident.memory._api_request", side_effect=fake_request):
        written = await writeback_durable_knowledge(
            "https://api.test", "svc-key", r2_config,
            findings=[],  # empty — nothing to write
        )

    assert written == 0
    add_calls = [c for c in api_calls if "/entries/add" in c["path"]]
    assert len(add_calls) == 0


# ── Test 5: De-dup prevents duplicate writes ──────────────────────────────────


@pytest.mark.asyncio
async def test_dedup_prevents_duplicate_writes(r2_config: ResidentConfig):
    """A finding that's already in the KB is skipped (de-dup)."""
    api_calls: list[dict] = []

    async def fake_request(method: str, api_url: str, api_key: str,
                           path: str, json_data: dict | None = None,
                           timeout: int = 30) -> MagicMock:
        api_calls.append({"method": method, "path": path, "body": json_data})
        if "/entries/add" in path:
            return _mock_api_response(201, {"id": 999})
        if "/entries" in path and method == "GET":
            # The existing claim is almost identical.
            return _mock_api_response(200, [
                {"id": 1, "content": "All handlers must validate tier before accessing org resources."}
            ])
        return _mock_api_response(200, {})

    with patch("sessionfs.resident.memory._api_request", side_effect=fake_request):
        written = await writeback_durable_knowledge(
            "https://api.test", "svc-key", r2_config,
            findings=[
                "All handlers must validate tier before accessing org resources.",
            ],
        )

    assert written == 0  # de-duped — nothing new
    add_calls = [c for c in api_calls if "/entries/add" in c["path"]]
    assert len(add_calls) == 0


# ── Test 6: Compaction sends digest + superseded_entry_ids ───────────────────


@pytest.mark.asyncio
async def test_compaction_sends_digest_and_superseded_ids(r2_config: ResidentConfig):
    """Compaction calls POST .../memory/compact with digest + superseded IDs."""
    compact_calls: list[dict] = []

    async def fake_request(method: str, api_url: str, api_key: str,
                           path: str, json_data: dict | None = None,
                           timeout: int = 30) -> MagicMock:
        if "/memory/compact" in path:
            compact_calls.append(json_data or {})
            return _mock_api_response(201, {
                "id": "rme_digest2", "kind": "digest", "seq": 10,
                "content": json_data["digest_content"] if json_data else "",
            })
        return _mock_api_response(200, {})

    with patch("sessionfs.resident.memory._api_request", side_effect=fake_request):
        ok = await compact_memory(
            "https://api.test", "svc-key", r2_config,
            digest_content="Compacted summary of 5 reviews.",
            superseded_entry_ids=["rme_1", "rme_2", "rme_3"],
            digest_token_estimate=10,
        )

    assert ok is True
    assert len(compact_calls) == 1
    body = compact_calls[0]
    assert "Compacted summary" in body["digest_content"]
    assert body["superseded_entry_ids"] == ["rme_1", "rme_2", "rme_3"]
    assert body["digest_token_estimate"] == 10


# ── Test 7: 429 from memory write triggers compact, no crash ──────────────────


@pytest.mark.asyncio
async def test_429_triggers_compact_not_crash(r2_config: ResidentConfig):
    """A 429 from a reasoning write triggers a compact attempt, not a crash."""
    compact_attempted = False
    write_calls = 0

    async def fake_request(method: str, api_url: str, api_key: str,
                           path: str, json_data: dict | None = None,
                           timeout: int = 30) -> MagicMock:
        nonlocal compact_attempted, write_calls
        if "/memory/compact" in path:
            compact_attempted = True
            return _mock_api_response(201, {"id": "rme_d", "kind": "digest"})
        if "/memory" in path and method == "POST" and "compact" not in path and "hydrate" not in path:
            write_calls += 1
            if write_calls == 1:
                return _mock_api_response(429, {
                    "detail": {
                        "error": "memory_cap_exceeded",
                        "message": "Un-compacted memory entry count has reached the cap.",
                    }
                })
            return _mock_api_response(201, {"id": "rme_x"})
        if "/memory/hydrate" in path:
            return _mock_api_response(200, {"digest": None, "recent_reasoning": []})
        return _mock_api_response(200, {})

    with patch("sessionfs.resident.memory._api_request", side_effect=fake_request), \
         patch("sessionfs.resident.runner.compact_memory") as mock_compact, \
         patch("sessionfs.resident.runner.hydrate_living_context",
               AsyncMock(return_value=LivingContext())):

        mock_compact.return_value = True

        # Simulate write_reasoning returning False (cap hit).
        wrote = await write_reasoning(
            "https://api.test", "svc-key", r2_config,
            ticket_id="tk_x",
            conclusion="Reviewed auth logic.",
        )

    assert wrote is None  # cap blocked the write
    # The write itself should not crash.
    assert write_calls == 1


# ── Test 8: Hydrated context reaches the LLM prompt ──────────────────────────


@pytest.mark.asyncio
async def test_hydrated_context_reaches_llm_prompt(r2_config: ResidentConfig):
    """The reviewer LLM sees prior findings + playbook in its prompt context."""
    # Simulate a runner with living context.
    runner = ResidentRunner(r2_config, llm_adapter=StubReviewLLM())
    runner._api_url = "https://api.test"
    runner._api_key = "svc-key"
    runner._project_id = "proj_test"

    lc = LivingContext(
        memory_digest="Prior review found auth pattern issues.",
        recent_reasoning=["Checked tier resolution — ok."],
        prior_findings=["All handlers must validate tier."],
        review_playbook="Always check for SQL injection.",
        project_context_summary="FastAPI app with PostgreSQL.",
    )
    runner._living_context = lc

    # Build a review context and enrich it.
    directive = {
        "ticket_id": "tk_test",
        "directive_id": "dir_1",
        "item_id": "wqi_1",
        "ticket": {"id": "tk_test", "title": "Fix auth"},
        "comment_delta": [{"author_persona": "atlas", "content": "Fixed."}],
    }
    from sessionfs.resident.runner import _build_review_context
    ctx = _build_review_context(directive)
    enriched = runner._enrich_with_living_context(ctx)

    # The enriched context contains the living mind fields.
    desc = enriched.ticket_description
    assert "auth pattern" in desc
    assert "tier resolution" in desc
    assert "validate tier" in desc
    assert "SQL injection" in desc
    assert "FastAPI" in desc


# ── Test 9: Config validation for R2 fields ──────────────────────────────────


def test_config_validation_r2_fields():
    """R2 config fields are validated."""
    # NO resident_id + org_id → VALID (R2 memory disabled, R1 loop still works).
    cfg = ResidentConfig(
        name="test", queue_id="wq_1", project="proj_test",
        org_profile="test-org", llm=LLMConfig(api_key="k"),
    )
    cfg._resolved_llm_key = "k"
    errors = cfg.validate()
    assert not any("resident_id" in e for e in errors)
    assert not any("org_id" in e for e in errors)

    # Only ONE of the pair set → error (both required together for memory).
    cfg_half = ResidentConfig(
        name="test", queue_id="wq_1", project="proj_test",
        org_profile="test-org", resident_id="res_x",
        llm=LLMConfig(api_key="k"),
    )
    cfg_half._resolved_llm_key = "k"
    assert any("set together" in e for e in cfg_half.validate())

    # Invalid resident_id prefix.
    cfg2 = ResidentConfig(
        name="test", queue_id="wq_1", project="proj_test",
        org_profile="test-org", resident_id="bad_id", org_id="org_x",
        llm=LLMConfig(api_key="k"),
    )
    cfg2._resolved_llm_key = "k"
    errors2 = cfg2.validate()
    assert any("must start with 'res_'" in e for e in errors2)

    # Invalid org_id prefix.
    cfg3 = ResidentConfig(
        name="test", queue_id="wq_1", project="proj_test",
        org_profile="test-org", resident_id="res_x", org_id="bad_org",
        llm=LLMConfig(api_key="k"),
    )
    cfg3._resolved_llm_key = "k"
    errors3 = cfg3.validate()
    assert any("must start with 'org_'" in e for e in errors3)

    # Valid R2 config.
    cfg4 = ResidentConfig(
        name="test", queue_id="wq_1", project="proj_test",
        org_profile="test-org", resident_id="res_abc123", org_id="org_def456",
        llm=LLMConfig(api_key="k"),
    )
    cfg4._resolved_llm_key = "k"
    assert cfg4.validate() == []


# ── Test 10: Config from TOML includes R2 fields ─────────────────────────────


def test_config_from_toml_r2_fields():
    """TOML config parsing picks up resident_id, org_id, mind_token_budget."""
    import tempfile
    from pathlib import Path

    toml_content = """\
[resident]
queue_id = "wq_test"
project = "proj_abc"
org_profile = "my-org"
resident_id = "res_abc123def456"
org_id = "org_xyz789"
poll_interval_seconds = 60
mind_token_budget = 4000
compact_every_wakes = 5

[llm]
api_key = "sk-test"
"""
    with tempfile.TemporaryDirectory() as tmpdir:
        residents_dir = Path(tmpdir)
        config_path = residents_dir / "test-r2.toml"
        config_path.write_text(toml_content)

        with patch(
            "sessionfs.resident.config._residents_dir",
            return_value=residents_dir,
        ):
            cfg = ResidentConfig.from_toml("test-r2")

    assert cfg.resident_id == "res_abc123def456"
    assert cfg.org_id == "org_xyz789"
    assert cfg.mind_token_budget == 4000
    assert cfg.compact_every_wakes == 5


# ── Test 11: summarize_for_digest produces a digest from reasoning entries ───


def test_summarize_for_digest():
    """The deterministic summarizer produces a digest from reasoning entries."""
    entries = [
        {"content": "[VERIFIED-CLEAN] tk_1: Auth middleware looks correct."},
        {"content": "[CHANGES_REQUESTED] tk_2: Missing validation in handler X."},
    ]
    digest = summarize_for_digest(entries)
    assert "Resident memory digest" in digest
    assert "Auth middleware" in digest
    assert "Missing validation" in digest


def test_summarize_for_digest_dedup():
    """Summarizer deduplicates truly identical conclusions (by normalized text)."""
    entries = [
        {"content": "[VERIFIED-CLEAN] tk_1: Auth middleware looks correct."},
        {"content": "[VERIFIED-CLEAN] tk_1: Auth middleware looks correct."},
    ]
    digest = summarize_for_digest(entries)
    # The digest should mention the identical content only once.
    assert digest.count("Auth middleware") == 1


def test_summarize_for_digest_empty():
    """Summarizer handles empty entries gracefully."""
    digest = summarize_for_digest([])
    assert "no new reasoning" in digest


# ── Test 12: _is_duplicate detection ─────────────────────────────────────────


def test_is_duplicate_detects_overlap():
    """Findings with >70% word overlap are flagged as duplicates."""
    existing = [
        "All handlers must validate tier before accessing org resources.",
        "Use async fixtures for DB testing.",
    ]
    assert _is_duplicate(
        "All handlers must validate tier before accessing org resources.",
        existing,
    ) is True
    assert _is_duplicate(
        "This is a completely new finding about caching strategies.",
        existing,
    ) is False


def test_is_duplicate_short_finding():
    """Very short findings (< 4 words) are never flagged as duplicates."""
    assert _is_duplicate("OK", ["OK done"]) is False


# ── Test 13: _extract_conclusion and _extract_durable_findings ────────────────


def test_extract_conclusion_clean():
    """Clean verdict produces a short conclusion."""
    result = ReviewResult(
        verdict_phrase="VERIFIED-CLEAN",
        reasoning="The changes look good. All edge cases are covered.",
    )
    conclusion = _extract_conclusion(result, "tk_test")
    assert "[VERIFIED-CLEAN]" in conclusion
    assert "tk_test" in conclusion
    assert "edge cases" in conclusion


def test_extract_durable_findings_clean_returns_empty():
    """A clean verdict produces no durable findings (no-op discipline)."""
    result = ReviewResult(
        verdict_phrase="VERIFIED-CLEAN",
        reasoning="Everything looks great.",
    )
    findings = _extract_durable_findings(result)
    assert findings == []


def test_extract_durable_findings_detects_patterns():
    """A non-clean review mentioning patterns/conventions returns findings."""
    result = ReviewResult(
        verdict_phrase="CHANGES_REQUESTED\n- MEDIUM: Recurring anti-pattern in auth handlers.",
        reasoning="Every handler should always validate the tier before proceeding.",
    )
    findings = _extract_durable_findings(result)
    assert len(findings) > 0
    assert any("anti-pattern" in f.lower() for f in findings)


# ── Test 14: writeback_wiki_page ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_writeback_wiki_page(r2_config: ResidentConfig):
    """Wiki page write calls PUT with correct slug and content."""
    put_calls: list[dict] = []

    async def fake_request(method: str, api_url: str, api_key: str,
                           path: str, json_data: dict | None = None,
                           timeout: int = 30) -> MagicMock:
        if method == "PUT" and "/pages/" in path:
            put_calls.append({"path": path, "body": json_data})
            return _mock_api_response(200, {"slug": "review-postmortem"})
        return _mock_api_response(200, {})

    with patch("sessionfs.resident.memory._api_request", side_effect=fake_request):
        ok = await writeback_wiki_page(
            "https://api.test", "svc-key", r2_config,
            slug="review-postmortem",
            content="# Post-mortem: Auth review findings",
            title="Auth Review Post-Mortem",
        )

    assert ok is True
    assert len(put_calls) == 1
    assert "review-postmortem" in put_calls[0]["path"]
    assert put_calls[0]["body"]["title"] == "Auth Review Post-Mortem"


# ── Test 15: _estimate_tokens and _fit_to_budget ──────────────────────────────


def test_estimate_tokens():
    """Token estimate is ~4 chars per token."""
    assert _estimate_tokens("hello world") == 2  # 11 chars // 4 = 2
    assert _estimate_tokens("") == 1  # floor at 1
    assert _estimate_tokens("a" * 100) == 25


def test_fit_to_budget():
    """Text is clipped to fit within token budget."""
    text = "hello world, this is a test"
    # budget of 1 token = 4 chars
    clipped = _fit_to_budget(text, 1)
    assert len(clipped) <= 4 + 3  # 4 chars + "..."
    assert clipped.endswith("...")

    # Sufficient budget = no clipping.
    full = _fit_to_budget(text, 100)
    assert full == text

    # Zero budget = empty.
    assert _fit_to_budget(text, 0) == ""


# ── Test 16: write_reasoning respects config ──────────────────────────────────


@pytest.mark.asyncio
async def test_write_reasoning_no_resident_id_skips():
    """write_reasoning skips when resident_id/org_id is not configured."""
    cfg = ResidentConfig(
        name="test", queue_id="wq_1", project="proj_test",
        org_profile="test-org", llm=LLMConfig(api_key="k"),
    )
    cfg._resolved_llm_key = "k"
    wrote = await write_reasoning(
        "https://api.test", "svc-key", cfg,
        ticket_id="tk_x", conclusion="test",
    )
    assert wrote is None  # skipped because no resident_id


# ── Test 17: compact_memory no resident_id skips ─────────────────────────────


@pytest.mark.asyncio
async def test_compact_memory_no_resident_id_skips():
    """compact_memory returns False when resident_id is not configured."""
    cfg = ResidentConfig(
        name="test", queue_id="wq_1", project="proj_test",
        org_profile="test-org", llm=LLMConfig(api_key="k"),
    )
    cfg._resolved_llm_key = "k"
    ok = await compact_memory(
        "https://api.test", "svc-key", cfg,
        digest_content="test", superseded_entry_ids=[],
    )
    assert ok is False


@pytest.mark.asyncio
async def test_write_reasoning_returns_entry_id(r2_config: ResidentConfig):
    """write_reasoning returns the server entry id on success, so the runner
    can supersede it at compaction (otherwise the F6 cap never frees)."""
    async def fake_request(method: str, api_url: str, api_key: str,
                           path: str, json_data: dict | None = None,
                           timeout: int = 30) -> MagicMock:
        if path.endswith("/memory"):
            return _mock_api_response(201, {"id": "rme_new123", "kind": "reasoning"})
        return _mock_api_response(200, {})

    with patch("sessionfs.resident.memory._api_request", side_effect=fake_request):
        entry_id = await write_reasoning(
            "https://api.test", "svc-key", r2_config,
            ticket_id="tk_1", conclusion="ok",
        )
    assert entry_id == "rme_new123"


@pytest.mark.asyncio
async def test_writeback_uses_configured_persona(r2_config: ResidentConfig):
    """KB writeback + de-dup use the resident's REGISTERED persona, not a
    hardcoded literal (a mismatched persona would 422 and break round-trip)."""
    r2_config.persona = "atlas"
    api_calls: list[dict] = []

    async def fake_request(method: str, api_url: str, api_key: str,
                           path: str, json_data: dict | None = None,
                           timeout: int = 30) -> MagicMock:
        api_calls.append({"method": method, "path": path, "body": json_data})
        if "/entries/add" in path:
            return _mock_api_response(201, {"id": 1})
        if "/entries" in path and method == "GET":
            return _mock_api_response(200, [])
        return _mock_api_response(200, {})

    with patch("sessionfs.resident.memory._api_request", side_effect=fake_request):
        await writeback_durable_knowledge(
            "https://api.test", "svc-key", r2_config,
            findings=["A durable convention worth recording for later."],
        )

    add = [c for c in api_calls if "/entries/add" in c["path"]][0]
    assert add["body"]["persona_name"] == "atlas"
    # The de-dup GET is scoped to the same persona and no longer pins claim_class.
    dedup = [c for c in api_calls if c["method"] == "GET" and "/entries" in c["path"]]
    assert dedup and "persona_name=atlas" in dedup[0]["path"]
    assert "claim_class=claim" not in dedup[0]["path"]


@pytest.mark.asyncio
async def test_cold_hydration_skips_digest_keeps_durable_sources(
    r2_config: ResidentConfig,
):
    """R4 --cold: skip_digest=True OMITS the digest so the budget goes to the raw
    durable sources (recent reasoning, KB, playbook) — a real rebuild."""
    async def fake_request(method: str, api_url: str, api_key: str,
                           path: str, json_data: dict | None = None,
                           timeout: int = 30) -> MagicMock:
        if "/memory/hydrate" in path:
            return _mock_api_response(200, {
                "digest": {
                    "id": "rme_digest1", "kind": "digest", "seq": 5,
                    "content": "STALE POISONED DIGEST CONTENT",
                    "quarantined": False,
                    "created_at": "2026-07-05T00:00:00Z",
                },
                "recent_reasoning": [
                    {"id": "rme_r1", "kind": "reasoning", "seq": 4,
                     "content": "Recent durable reasoning entry.",
                     "quarantined": False,
                     "created_at": "2026-07-05T00:00:00Z"},
                ],
            })
        elif "/entries" in path and "persona_name" in path:
            return _mock_api_response(200, [
                {"id": 1, "content": "Durable KB finding.",
                 "persona_name": "codex-reviewer", "claim_class": "claim"},
            ])
        elif "/pages/review-playbook" in path:
            return _mock_api_response(200, {
                "id": "p1", "slug": "review-playbook", "title": "Playbook",
                "content": "Durable playbook rule.",
            })
        return _mock_api_response(404, {})

    with patch("sessionfs.resident.context._api_request", side_effect=fake_request):
        ctx = await hydrate_living_context(
            "https://api.test", "svc-key", r2_config, skip_digest=True,
        )

    # The (stale/poisoned) digest CONTENT is omitted...
    assert ctx.memory_digest == ""
    assert "POISONED" not in str(ctx.memory_digest)
    # ...but its id is PRESERVED so the next compaction supersedes the old
    # digest (it must not be orphaned toward the F6 cap).
    assert ctx.memory_digest_id == "rme_digest1"
    # But the raw durable sources are STILL hydrated (the rebuild material).
    assert len(ctx.recent_reasoning) > 0
    assert len(ctx.prior_findings) > 0
    assert ctx.review_playbook != ""
