<!--
sessionfs-managed: version=2 hash=22003cc67522ac61
Canonical source: sfs rules edit  (do not edit by hand — changes will be overwritten on the next `sfs rules compile`)
-->

## Project Preferences

# SessionFS project preferences

## Commit conventions
- Author: sessionfsbot <bot@sessionfs.dev>
- Never mention AI/Claude/LLM in commit messages
- Commit messages should read as if written by a human developer

## Branch policy
- develop is LOCAL ONLY; never push to origin
- Public releases: merge to main, sanitize, push
- Use the release skill for every release

## Hard constraints
- No WebSockets, no Redis, no real-time sync (HTTP + ETags only)
- No server-side LLM API keys (all LLM calls are client-side)
- Sessions are append-only; never modify messages in place
- All .sfs paths are relative to workspace root
- All GCP infrastructure via Terraform only

## Test and lint gates
- ruff check src/ before every commit
- pytest tests/ -x -q before every release
- helm lint charts/sessionfs before every release
- npm run build in dashboard/ before every release

## Knowledge base
- Call add_knowledge() via MCP when you discover something significant
- Every discovery helps the next agent that works here

## Project Facts (from knowledge base)

- Project fact: Knowledge Base v2 implemented (commit 513413f). Core architectural shift: entries now carry claim_class (evidence/claim/note) and freshness_class (current/aging/stale/superseded). Session extraction defaults to evidence, MCP add_knowledge defaults to note, auto-promote to claim only when quality gates pass (confidence >= 0.8, content >= 50 chars, under claim quota 5/session, under total quota 20/hr, passes near-duplicate). Compiled views serve only active claims (not notes, not evidence, not superseded, not stale). Per-type freshness decay: bug=30d, dependency=60d, pattern=90d, discovery=90d, convention=180d, decision=365d. Conservative auto-supersession: same entity_ref + type + confidence >= 0.8 + overlap > 0.9 (creates contradicts link for moderate overlap 0.5-0.9). Budget default changed from 8000 to 2000 words. Migration 027 adds 10 new columns + 2 indexes. Dashboard shows claim badges, freshness dots, stale review queue, filter controls, provenance blocks, rebuild button. _(decision)_
- Project fact: Knowledge base architecture direction (2026-04-12, Codex analysis + CEO alignment): The current KB optimizes for capture volume, not active truth per token. Agreed direction: three-layer split — (1) evidence: immutable session-derived facts, (2) claims: normalized facts with provenance, confidence, freshness, entity_ref, superseded_by, claim_class (evidence/claim/note), (3) views: context doc + section pages + concept pages as disposable projections compiled only from active non-superseded claims. Priority order: claim model upgrade (migration) → agent writeback gate (default to "note", require high confidence for "claim") → contradiction detection at compile time → serving-layer rebuild → evaluation harness. Core anti-pattern to avoid: building an impressive automatic historian instead of a useful memory system. This session contributed 68 entries; retrospectively ~20 should have been claims and ~48 notes. The current system accepted all 68 without meaningful quality pushback beyond rate limits and similarity gates. _(decision)_
- Project fact: v0.9.8.5 released (2026-04-12). Largest patch since v0.9.8: 4 HIGH + 4 MEDIUM security fixes (GitLab webhook binding, GitHub claim IDOR+race, effective-tier leak, pr_comments scoped index, Security Scan rebuilt, Dockerfile USER 10001, Helm chart hardened, 4 CVE patches), knowledge lifecycle fully closed (LLM budget, semantic dedup all paths, concept prune page_type fix, health stale count, bulk dismiss confidence guard), dashboard Help page, admin org endpoints, 93 new tests (1091 backend + 76 dashboard = 1167 total), 4 daemon fixes (Codex null-content, startup order, stop PID fallback, daemon.json cleanup), migration 026 (pr_comments scoped unique), Cloud SQL upgrade, Vercel VITE_API_URL set, 10 Dependabot PRs unblocked. Released via /release skill with Shield-SR approved + Scribe-Site synced. Deploy Dashboard GHA failed (transient Vercel API error) but manual Vercel deploy succeeded and is verified live. _(decision)_
- Project fact: Concept page cleanup now runs unconditionally via _prune_dead_concept_pages() called at the top of auto_generate_concepts(), BEFORE checking for new candidates. Previously, cleanup only ran inside the candidate loop — when the project dropped below the 15-entry clustering threshold, no candidates were returned and dead concept pages lingered indefinitely. The new helper scans all concept/* auto-pages, checks if all linked entries are dismissed, and deletes any orphaned or dead pages. Also: bulk dismiss-stale endpoint at POST /projects/{id}/entries/dismiss-stale atomically sets dismissed=true on all entries matching the stale query (same OR condition as the health counter). Dashboard surfaces it as a "Dismiss N stale" button in the health banner with toast feedback. _(decision)_
- Project fact: Knowledge base semantic dedup applies to ALL entry paths as of this commit. Previously only the manual add_entry (MCP/API) route checked word_overlap > 0.85; the deterministic and LLM extraction paths used exact-string content dedup only. Now both extraction functions run a three-layer filter: (1) exact-match against the session's prior extractions, (2) word_overlap > 0.85 against the 100 most recent project entries (_fetch_recent_project_contents), (3) intra-batch dedup so a single extraction run can't add two near-duplicates. The shared helpers word_overlap() and is_near_duplicate() live in services/knowledge.py and routes/knowledge.py imports from there. Threshold is 0.85 everywhere — set empirically from observing "File modified: src/foo.py" vs "File created/modified: src/foo.py" pairs accumulating across sessions. _(decision)_
