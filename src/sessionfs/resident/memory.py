"""Durable writeback + compaction for the resident runner (R2).

After each review the resident writes:
1. Rolling reasoning — POST .../residents/{id}/memory (kind='reasoning').
   A short note of what it concluded. Bounded; respects the server F6 cap.
2. Durable shared writeback — add_knowledge (persona_name='codex-reviewer',
   author_class forced to 'agent' by server) when the review surfaces
   something durable (recurring anti-pattern, convention, fix pattern).
   De-duplicated against active claims FIRST.
3. Wiki updates — update_wiki_page for substantial findings.
   Best-effort (service keys may not have wiki write access yet).

Compaction:
Every N wakes (config.compact_every_wakes), the resident summarizes its
recent reasoning entries client-side and calls POST .../memory/compact
with a digest + superseded_entry_ids. The server stores the digest +
marks those entries superseded.

NO-OP DISCIPLINE (hard rule):
A wake that learns nothing durable writes NOTHING to KB/wiki.
The verdict comment itself is NOT a writeback — it stays on the settle-path.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from sessionfs.resident.client import _api_request
from sessionfs.resident.config import ResidentConfig

logger = logging.getLogger("sessionfs.resident.memory")

# Maximum length for a single reasoning entry (bounded per the design).
_MAX_REASONING_LENGTH = 2000

# Maximum length for a compact digest.
_MAX_DIGEST_LENGTH = 32000


async def write_reasoning(
    api_url: str,
    api_key: str,
    config: ResidentConfig,
    ticket_id: str,
    conclusion: str,
) -> str | None:
    """Write a rolling reasoning entry to the resident's private memory.

    Returns the created entry's server id on success (so the runner can later
    supersede it during compaction — otherwise the F6 cap never frees), or None
    if capped/error. On 429 (F6 cap exceeded), logs a warning and returns None —
    the caller should trigger a compact.
    """
    if not config.resident_id or not config.org_id:
        logger.debug("Skipping reasoning write — no resident_id/org_id configured.")
        return None

    if len(conclusion) > _MAX_REASONING_LENGTH:
        conclusion = conclusion[:_MAX_REASONING_LENGTH - 3] + "..."

    path = (
        f"/api/v1/orgs/{config.org_id}"
        f"/residents/{config.resident_id}/memory"
    )
    body = {
        "kind": "reasoning",
        "content": conclusion,
        "token_estimate": max(1, len(conclusion) // 4),
    }
    resp = await _api_request("POST", api_url, api_key, path, json_data=body)

    if resp.status_code == 429:
        logger.warning(
            "Memory cap exceeded — compact before writing more reasoning. "
            "Server says: %s",
            resp.body.get("detail", {}).get("message", str(resp.body))
            if isinstance(resp.body, dict)
            else str(resp.body),
        )
        return None
    if resp.status_code in (200, 201):
        logger.debug("Reasoning written for ticket=%s", ticket_id)
        return resp.body.get("id") if isinstance(resp.body, dict) else None

    logger.debug(
        "Reasoning write failed (HTTP %s) for ticket=%s",
        resp.status_code,
        ticket_id,
    )
    return None


async def writeback_durable_knowledge(
    api_url: str,
    api_key: str,
    config: ResidentConfig,
    findings: list[str],
) -> int:
    """Write durable KB findings that the review surfaced.

    Each finding is de-duplicated against active claims before writing.
    Uses add_knowledge (POST .../entries/add) with persona_name='codex-reviewer'.
    Service keys are server-forced to author_class='agent'.

    Returns the number of entries actually written (after de-dup).
    A return of 0 means nothing new was written (no-op discipline honored).

    Each finding should be a self-contained discovery like:
    "Recurring pattern: all handlers must validate tier before accessing org resources"
    """
    if not config.project or not findings:
        return 0

    # De-dup: fetch existing claims for this persona and check overlap.
    existing = await _fetch_existing_claim_texts(api_url, api_key, config)
    written = 0

    for finding in findings:
        finding = finding.strip()
        if not finding or len(finding) < 20:
            continue
        if _is_duplicate(finding, existing):
            logger.debug("Skipping duplicate finding: %s", finding[:80])
            continue

        success = await _add_knowledge_entry(api_url, api_key, config, finding)
        if success:
            existing.append(finding)  # prevent double-write in same batch
            written += 1

    if written > 0:
        logger.info("Wrote %d durable KB finding(s) to project %s.", written, config.project)
    return written


async def writeback_wiki_page(
    api_url: str,
    api_key: str,
    config: ResidentConfig,
    slug: str,
    content: str,
    title: str | None = None,
) -> bool:
    """Create or update a wiki page. Best-effort — service keys may get 403.
    Only used for substantial findings (post-mortems, playbook updates).
    """
    if not config.project:
        return False

    path = f"/api/v1/projects/{config.project}/pages/{slug}"
    body: dict = {"content": content}
    if title:
        body["title"] = title

    resp = await _api_request("PUT", api_url, api_key, path, json_data=body)
    if resp.status_code in (200, 201):
        logger.info("Wiki page '%s' updated.", slug)
        return True

    logger.debug(
        "Wiki page '%s' write failed (HTTP %s) — skipping.",
        slug,
        resp.status_code,
    )
    return False


async def compact_memory(
    api_url: str,
    api_key: str,
    config: ResidentConfig,
    digest_content: str,
    superseded_entry_ids: list[str],
    digest_token_estimate: int = 0,
) -> bool:
    """Compact the resident's private memory.

    Sends a client-summarized digest + the superseded_entry_ids to the
    server. The server stores the digest + marks those entries superseded.

    Called periodically (every compact_every_wakes) or when a 429 from
    a memory write signals the cap is near.
    """
    if not config.resident_id or not config.org_id:
        return False

    if len(digest_content) > _MAX_DIGEST_LENGTH:
        digest_content = digest_content[:_MAX_DIGEST_LENGTH - 3] + "..."

    path = (
        f"/api/v1/orgs/{config.org_id}"
        f"/residents/{config.resident_id}/memory/compact"
    )
    body = {
        "digest_content": digest_content,
        "digest_token_estimate": digest_token_estimate or max(1, len(digest_content) // 4),
        "superseded_entry_ids": superseded_entry_ids[:200],  # server cap
    }
    resp = await _api_request("POST", api_url, api_key, path, json_data=body)

    if resp.status_code in (200, 201):
        logger.info(
            "Compacted %d entries into new digest (token_estimate=%d).",
            len(superseded_entry_ids),
            body["digest_token_estimate"],
        )
        return True
    if resp.status_code == 429:
        logger.warning(
            "Compact rejected (memory cap): %s",
            resp.body.get("detail", {}).get("message", str(resp.body))
            if isinstance(resp.body, dict)
            else str(resp.body),
        )
    else:
        logger.error("Compact failed (HTTP %s).", resp.status_code)
    return False


def summarize_for_digest(
    reasoning_entries: list[dict],
    prior_digest: str = "",
) -> str:
    """Produce a client-side digest summary from recent reasoning entries.

    This is a DETERMINISTIC summarizer — it does NOT call an LLM.
    The digest is a re-expandable cache; the durable sources (KB/wiki/
    compiled context) are the system of record (§3.6.4).

    The summarizer extracts the key conclusions from each reasoning entry,
    deduplicates similar lines, and prepends the prior digest for continuity.
    """
    lines: list[str] = []
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    lines.append(f"# Resident memory digest — compacted at {now}")

    if prior_digest:
        # Carry forward the prior digest's key points (first 500 chars).
        prior_summary = prior_digest[:500]
        first_newline = prior_summary.find("\n", 1)
        if first_newline > 1:
            prior_summary = prior_summary[:first_newline]
        lines.append(f"Prior: {prior_summary}")

    seen: set[str] = set()
    for entry in reasoning_entries:
        content = entry.get("content", "") if isinstance(entry, dict) else ""
        if not content:
            continue
        # Extract the first sentence or line as the key point.
        point = content.split("\n")[0].strip()
        if len(point) > 200:
            point = point[:197] + "..."
        normalized = point.lower().strip()
        if normalized and normalized not in seen:
            seen.add(normalized)
            lines.append(f"- {point}")

    if not lines[1:]:  # no reasoning entries to summarize
        lines.append("- (no new reasoning since last compaction)")

    return "\n".join(lines)


# ── Internal helpers ───────────────────────────────────────────────────────


async def _fetch_existing_claim_texts(
    api_url: str, api_key: str, config: ResidentConfig
) -> list[str]:
    """Fetch existing entries by this persona for de-dup (claims AND notes —
    the server may store some writebacks as notes, and we must not re-write
    something already recorded in either class)."""
    path = (
        f"/api/v1/projects/{config.project}/entries"
        f"?persona_name={config.persona}"
        f"&dismissed=false"
        f"&limit=50"
    )
    resp = await _api_request("GET", api_url, api_key, path)
    if resp.status_code != 200 or not isinstance(resp.body, list):
        return []

    return [
        str(e["content"])
        for e in resp.body
        if isinstance(e, dict) and e.get("content")
    ]


def _is_duplicate(finding: str, existing: list[str]) -> bool:
    """Check if a finding is materially similar to any existing claim.
    Uses simple word-overlap heuristic — not an LLM call.
    A finding is considered duplicate if >70% of its significant words
    appear in any existing entry.
    """
    if not existing:
        return False

    words = set(finding.lower().split())
    if len(words) < 4:
        return False  # too short to de-dup meaningfully

    for entry in existing:
        entry_words = set(entry.lower().split())
        if not entry_words:
            continue
        overlap = len(words & entry_words) / len(words)
        if overlap > 0.7:
            return True
    return False


async def _add_knowledge_entry(
    api_url: str, api_key: str, config: ResidentConfig, content: str
) -> bool:
    """POST a single knowledge entry to the project."""
    path = f"/api/v1/projects/{config.project}/entries/add"
    body = {
        "content": content,
        "entry_type": "discovery",
        # The resident's REGISTERED persona (server validates persona_name
        # against project personas — a hardcoded literal would 422 for residents
        # under a different persona, and break the persona-filtered round-trip).
        "persona_name": config.persona,
        # Prefer a claim (higher confidence + force_claim); hydration/de-dup no
        # longer require claim_class=claim, so findings the server stores as
        # notes still round-trip.
        "confidence": 0.85,
        "force_claim": True,
    }
    resp = await _api_request("POST", api_url, api_key, path, json_data=body)
    if resp.status_code in (200, 201):
        return True
    logger.debug(
        "add_knowledge failed (HTTP %s): %s",
        resp.status_code,
        resp.body if isinstance(resp.body, dict) else "",
    )
    return False
