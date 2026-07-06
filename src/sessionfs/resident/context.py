"""Living context hydration for the resident runner (R2).

Assembles a bounded warm digest from durable SessionFS sources before each
review so the reviewer LLM reasons WITH its accumulated project knowledge,
prior findings, and the review playbook — not cold.

Hydration sources (all authed with the resident's service key):
1. Resident's OWN private mind — POST .../residents/{id}/memory/hydrate
   (latest digest + recent reasoning). REQUIRED — the core of the living mind.
2. Persona-filtered KB claims — GET .../entries?persona_name=<name>
   (the resident's prior durable findings). REQUIRED.
3. Review playbook wiki page — GET .../pages/review-playbook
   (best-effort; service keys may not have wiki access yet).
4. Project context — GET /api/v1/projects/{project_id}
   (best-effort; service keys may not have context access yet).
5. Context sections — GET .../context/sections/{slug}
   (best-effort; expand-on-demand).

The assembled warm digest is TOKEN-CAPPED at config.mind_token_budget.
Full context is available on-demand; the digest is a re-expandable cache,
never the system of record (§3.3, §3.5).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from sessionfs.resident.client import _api_request
from sessionfs.resident.config import ResidentConfig

logger = logging.getLogger("sessionfs.resident.context")

# Sections of the compiled project context that are most relevant for
# code review. Fetched on-demand (expand-on-demand), not preloaded.
# The ACTUAL compiler section slugs (server SECTION_MAP → split_context_sections:
# lowercase heading, non-alnum runs → "_"). An earlier guess (architecture/
# conventions/…) 404'd for every section.
_REVIEW_SECTIONS = [
    "key_decisions",
    "patterns_conventions",
    "coding_conventions",
    "known_issues_workarounds",
    "dependencies_integrations",
]


@dataclass
class LivingContext:
    """The assembled warm context fed to the reviewer LLM alongside the
    directive's bounded task delta.

    All fields are strings / simple structures — the LLM adapter formats
    them into the prompt. The total estimated tokens across all fields
    must stay under mind_token_budget.
    """

    # Private mind — from the resident-memory primitive.
    memory_digest: str = ""  # latest compacted digest (may be empty on first wake)
    memory_digest_id: str = ""  # server id of the latest digest (to supersede at compact)
    recent_reasoning: list[str] = field(default_factory=list)
    # Server ids of the hydrated recent reasoning entries — lets a forced compact
    # after a cap hit on RESTART supersede live entries this process didn't write.
    recent_reasoning_ids: list[str] = field(default_factory=list)

    # Durable shared KB — the resident's own prior findings.
    prior_findings: list[str] = field(default_factory=list)

    # Best-effort enrichments.
    review_playbook: str = ""
    project_context_summary: str = ""
    sections: dict[str, str] = field(default_factory=dict)

    # Metadata.
    token_estimate: int = 0

    def enrich_review_context(self, review_context: dict) -> dict:
        """Enrich a ReviewContext-like dict with the living context fields.
        The caller (runner.py) passes this enriched context to the LLM adapter.
        """
        if self.memory_digest:
            review_context["memory_digest"] = self.memory_digest
        if self.recent_reasoning:
            review_context["recent_reasoning"] = self.recent_reasoning
        if self.prior_findings:
            review_context["prior_findings"] = self.prior_findings
        if self.review_playbook:
            review_context["review_playbook"] = self.review_playbook
        if self.project_context_summary:
            review_context["project_context_summary"] = self.project_context_summary
        return review_context


async def hydrate_living_context(
    api_url: str,
    api_key: str,
    config: ResidentConfig,
    skip_digest: bool = False,
) -> LivingContext:
    """Assemble the bounded warm context from all durable sources.

    Called on boot AND before each review. Fetches from:
    1. Resident memory hydrate (required — the core of the living mind).
    2. Persona-filtered KB claims (required — prior durable findings).
    3. Review playbook wiki (best-effort).
    4. Project context (best-effort).
    5. Context sections (best-effort, expand-on-demand).

    The returned LivingContext is token-capped at config.mind_token_budget.
    Sources that fail (403/404/network error) are gracefully skipped.

    R4 --cold: `skip_digest=True` OMITS the compacted digest entirely (not just
    empties it after) so the full mind_token_budget goes to the RAW durable
    sources — recent reasoning, KB findings, playbook, sections — which is what
    a cold rebuild needs to recover from a poisoned/stale digest.
    """
    ctx = LivingContext()
    budget = config.mind_token_budget

    # ── 1. Resident memory hydrate (PRIVATE mind) ──────────────────────
    if config.resident_id and config.org_id:
        memory = await _hydrate_memory(api_url, api_key, config)
        if memory:
            digest = memory.get("digest") or {}
            if isinstance(digest, dict) and digest.get("content"):
                if not skip_digest:
                    content = str(digest["content"])
                    ctx.memory_digest = _fit_to_budget(content, budget)
                    budget -= _estimate_tokens(ctx.memory_digest)
                # Capture the digest's server id REGARDLESS of skip_digest so the
                # runner supersedes it at the NEXT compaction (else each compact
                # leaks a live digest toward the F6 cap). A --cold rebuild omits
                # the digest's CONTENT but must not orphan the old digest.
                if digest.get("id"):
                    ctx.memory_digest_id = str(digest["id"])

            for entry in memory.get("recent_reasoning") or []:
                if isinstance(entry, dict) and entry.get("content"):
                    # Track the server id regardless of budget so a forced
                    # compact after a restart cap-hit can supersede live entries.
                    if entry.get("id"):
                        ctx.recent_reasoning_ids.append(str(entry["id"]))
                    content = str(entry["content"])
                    if budget > 0:
                        clipped = _fit_to_budget(content, budget)
                        if clipped:
                            ctx.recent_reasoning.append(clipped)
                            budget -= _estimate_tokens(clipped)

    # ── 2. Persona-filtered KB claims (SHARED durable findings) ────────
    if config.project and budget > 0:
        entries = await _fetch_persona_entries(api_url, api_key, config)
        for entry_text in entries:
            if budget <= 0:
                break
            clipped = _fit_to_budget(entry_text, budget)
            if clipped:
                ctx.prior_findings.append(clipped)
                budget -= _estimate_tokens(clipped)

    # ── 3. Review playbook wiki (best-effort) ──────────────────────────
    if config.project and budget > 200:
        playbook = await _fetch_wiki_page(
            api_url, api_key, config.project, "review-playbook"
        )
        if playbook:
            clipped = _fit_to_budget(playbook, budget)
            if clipped:
                ctx.review_playbook = clipped
                budget -= _estimate_tokens(clipped)

    # ── 4. Context sections — the service-key-accessible compiled context ──
    # (The full context_document is only reachable via the git-remote catch-all
    # GET /projects/{id}, which is a user-key route and 403s service keys, so
    # residents assemble the compiled context from its SECTIONS instead — the
    # `context/sections/{slug}` route is on the knowledge:read scope.)
    if config.project and budget > 500:
        for slug in _REVIEW_SECTIONS:
            if budget <= 100:
                break
            section = await _fetch_context_section(
                api_url, api_key, config.project, slug
            )
            if section:
                clipped = _fit_to_budget(section, budget)
                if clipped:
                    ctx.sections[slug] = clipped
                    budget -= _estimate_tokens(clipped)

    ctx.token_estimate = config.mind_token_budget - budget
    logger.info(
        "Hydrated living context: %d tokens (budget=%d, digest=%s, "
        "reasoning=%d, findings=%d, playbook=%s, sections=%d)",
        ctx.token_estimate,
        config.mind_token_budget,
        "yes" if ctx.memory_digest else "no",
        len(ctx.recent_reasoning),
        len(ctx.prior_findings),
        "yes" if ctx.review_playbook else "no",
        len(ctx.sections),
    )
    return ctx


# ── Internal helpers ───────────────────────────────────────────────────────


async def _hydrate_memory(
    api_url: str, api_key: str, config: ResidentConfig
) -> dict | None:
    """Call POST .../residents/{id}/memory/hydrate."""
    path = (
        f"/api/v1/orgs/{config.org_id}"
        f"/residents/{config.resident_id}/memory/hydrate"
    )
    resp = await _api_request("POST", api_url, api_key, path)
    if resp.status_code == 200 and isinstance(resp.body, dict):
        return resp.body
    if resp.status_code >= 400:
        logger.debug("Memory hydrate returned %s — skipping.", resp.status_code)
    return None


async def _fetch_persona_entries(
    api_url: str, api_key: str, config: ResidentConfig
) -> list[str]:
    """Fetch the resident's own prior KB findings via persona_name filter.

    Returns the content of the most recent entries, newest first. Filters to the
    resident's OWN persona but NOT to claim_class=claim — the server's quality
    gates/quota may store some writebacks as notes, and those are still the
    resident's own findings that must round-trip on later wakes.
    """
    path = (
        f"/api/v1/projects/{config.project}/entries"
        f"?persona_name={config.persona}"
        f"&dismissed=false"
        f"&limit=20"
    )
    resp = await _api_request("GET", api_url, api_key, path)
    if resp.status_code != 200 or not isinstance(resp.body, list):
        logger.debug(
            "Persona entries fetch returned %s — skipping.", resp.status_code
        )
        return []

    entries: list[str] = []
    for entry in resp.body:
        if isinstance(entry, dict) and entry.get("content"):
            content = str(entry["content"])
            # Keep entries concise — the full set may be large.
            if len(content) > 1000:
                content = content[:997] + "..."
            entries.append(content)
            if len(entries) >= 10:
                break
    return entries


async def _fetch_wiki_page(
    api_url: str, api_key: str, project_id: str, slug: str
) -> str | None:
    """Fetch a wiki page by slug. Best-effort — service keys may get 403."""
    path = f"/api/v1/projects/{project_id}/pages/{slug}"
    resp = await _api_request("GET", api_url, api_key, path)
    if resp.status_code == 200 and isinstance(resp.body, dict):
        content = resp.body.get("content")
        if isinstance(content, str) and content.strip():
            return content
    if resp.status_code >= 400:
        logger.debug(
            "Wiki page '%s' fetch returned %s — skipping.", slug, resp.status_code
        )
    return None


async def _fetch_context_section(
    api_url: str, api_key: str, project_id: str, slug: str
) -> str | None:
    """Fetch one section of the compiled project context. Best-effort."""
    path = f"/api/v1/projects/{project_id}/context/sections/{slug}"
    resp = await _api_request("GET", api_url, api_key, path)
    if resp.status_code == 200 and isinstance(resp.body, dict):
        content = resp.body.get("content")
        if isinstance(content, str) and content.strip():
            # Keep each section bounded.
            if len(content) > 1500:
                content = content[:1497] + "..."
            return content
    if resp.status_code >= 400:
        logger.debug(
            "Context section '%s' fetch returned %s — skipping.",
            slug,
            resp.status_code,
        )
    return None


def _estimate_tokens(text: str) -> int:
    """Conservative token estimate: ~4 chars per token.
    Matches the heuristic used by the server-side token budget.
    """
    return max(1, len(text) // 4)


def _fit_to_budget(text: str, budget: int) -> str:
    """Clip or truncate text to fit within a token budget.
    Returns empty string if budget is insufficient for anything meaningful.
    """
    if budget <= 0:
        return ""
    char_budget = budget * 4
    if len(text) <= char_budget:
        return text
    return text[: char_budget - 3] + "..."
