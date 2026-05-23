# Scout uniform signal shape (Phase 4c)

Scout — and every continuous SessionFS agent that ingests external
sources — converges on a single canonical envelope before the
reasoning loop runs. Source adapters live in n8n (or wherever the
fetch happens); the SessionFS platform never sees a raw upstream
payload. This document is the contract those adapters must satisfy.

Companion docs:
- [`scout-n8n.md`](scout-n8n.md) — the v0.10.21 Scout v4 continuity
  contract (single-source). This shape spec is Phase 4c on top of
  v4 — same continuity loop, multiple sources behind a Merge node.
- [`n8n-source-adapters/`](n8n-source-adapters/) — reference
  Code-node templates for HN Algolia, GitHub Releases, Reddit, and
  generic RSS. Each ships with a test fixture so future sources can
  be added with confidence.

This is a docs-only deliverable. **No new SessionFS endpoint exists
for signal ingestion** — adapters run inside n8n, emit the canonical
shape, then feed straight into the existing `POST /entries/add` write
path defined by the v4 contract. AC #6 of the parent ticket
(`tk_918073e8aa4c4478`) honored.

---

## 1. The envelope

Every source emits items conforming to this exact shape:

```jsonc
{
  "source": "hn",                       // string, required, ASCII slug
  "source_id": "hn:48235065",           // string, required, max 200 chars
  "title": "Show HN: my sessionfs killer",  // string, required, max 240 chars
  "url": "https://news.ycombinator.com/item?id=48235065",  // string, required, https only
  "content": "<longform body...>",      // string, required (may be ""), max 4000 chars
  "posted_at": "2026-05-22T20:14:33Z",  // string, required, ISO-8601 UTC
  "author": "pg",                       // string, required (may be ""), max 80 chars
  "signal_strength": 0.62,              // number, required, [0.0, 1.0]
  "raw": { ... }                        // object, optional, source-native debug payload
}
```

### Field rules

| Field | Type | Required | Max | Notes |
|-------|------|----------|-----|-------|
| `source` | string | yes | 32 chars | ASCII slug. Lowercase, no spaces. Stable per upstream (`"hn"`, `"gh_release"`, `"reddit"`, `"rss"`, `"discord"`). |
| `source_id` | string | yes | 200 chars | **Globally unique when prefixed by `source`.** Format: `"<source>:<native-id>"`. Examples: `"hn:48235065"`, `"gh:openai/openai-python@v2.4.0"`, `"reddit:abc1234"`. This IS the dedup key. |
| `title` | string | yes | 240 chars | Truncate longer titles with a trailing `"…"` so the downstream LLM still sees a clean headline. |
| `url` | string | yes | 2000 chars | **MUST start with `https://`.** Reject `http://`, `javascript:`, `data:`, etc. at normalizer time. |
| `content` | string | yes (may be `""`) | 4000 chars | Body text, description, changelog, or excerpt. Plain text. HTML stripped. Truncate from the END with a trailing `"\n\n[truncated]"` marker when the source exceeds the cap. |
| `posted_at` | string | yes | — | ISO-8601 with `Z` (UTC). If the upstream has no timestamp, use `$now.toISO()`. |
| `author` | string | yes (may be `""`) | 80 chars | Best-effort display name. **NEVER PII** — see §5. |
| `signal_strength` | number | yes | — | `[0.0, 1.0]`. Source-specific heuristic (see §3 + each adapter). |
| `raw` | object | no | — | Original source payload, untruncated. For debugging + future reprocessing. Stripped before forwarding to the LLM (token cost). |

### Validation discipline

The normalizer Code node is the validation site. Reject (drop or
log + skip) any item that:

1. Has a non-`https://` URL.
2. Has an empty `title` after trim.
3. Has a `source_id` that doesn't start with `<source>:`.
4. Has a `signal_strength` outside `[0.0, 1.0]`.
5. Has a `posted_at` that won't parse as ISO-8601.

Don't try to repair invalid items — drop them and emit a single
warning per execution. Scout's job is to find real signals; one
malformed item from a flaky upstream is not a signal worth saving.

---

## 2. `source_id` is the dedup key

The Scout v4 contract (`scout-n8n.md` §3.1) defines `source_context`
on KB writes as
`scout:n8n:<workflow_id>:<exec_id>:<signal_id>`. In multi-source
v5, `<signal_id>` MUST be the canonical `source_id` from §1, so:

```
source_context = "scout:n8n:wf_3QvgDheP:<exec_id>:hn:48235065"
                 └──────┬──────┘ └─────┬─────┘ └──────┬──────┘
                  scope+workflow   execution      source_id
```

Two runs of the same workflow processing the same HN story produce
DIFFERENT `source_context` values (different `exec_id`) but the
`scout:` portion of the suffix matches — the **pre-write dedupe**
step (`scout-n8n.md` §4.2 option 1) queries
`GET /entries?source_filter=:hn:48235065&persona_name=scout` and
finds the prior write. Without a stable `source_id`, the same story
gets persisted twice on every retry.

This is why HN-by-URL is not enough: the same story can have
multiple URLs (mobile vs desktop, with vs without `#`, after
URL-shortener resolution). Always use the upstream's native id.

---

## 3. `signal_strength` is within-source

Each source defines its own `signal_strength` heuristic. They are
NOT comparable across sources:

| Source | Heuristic | Rationale |
|--------|-----------|-----------|
| HN | `min(1.0, (points + comments * 0.5) / 100)` | HN's signal is community attention; points + half-weighted comments approximate engagement. Cap at 1.0. |
| GH releases | `1.0` if final + not draft, `0.5` if prerelease, `0.0` if draft (and drop drafts entirely) | Releases are binary — shipped or not. Prereleases are weaker signal. |
| Reddit | `min(1.0, (score + 2 * num_comments) / 200)` | Reddit's signal is upvotes + discussion. Comments weighted higher than upvotes. |
| RSS | `0.5` (flat — RSS has no native engagement metric) | RSS feeds are notification streams. The downstream LLM ranks them. |
| Discord (deferred) | `min(1.0, reactions / 10)` | Reactions over time are the closest analogue to upvotes. |

**Cross-source comparison is the LLM's job**, not the heuristic's.
The Scout reasoning loop reads all items at once and decides which
ones matter regardless of `signal_strength`. The heuristic is for:

1. Sort within a single source's stream (so the LLM sees the top
   10 HN items, not a random 10).
2. Per-source filtering thresholds (e.g. "drop HN items below
   0.2 unless they mention SessionFS by name").
3. Downstream observability (a sudden zero average from HN's
   normalizer means HN's adapter is broken).

If a normalizer cannot compute a meaningful score, pass `0.5`
(neutral) and document why in the adapter file. Do not synthesize
fake numbers to make the dashboard look full.

---

## 4. Examples (one canonical item per source)

### HN Algolia

Upstream: `https://hn.algolia.com/api/v1/search?tags=story&...`

```json
{
  "source": "hn",
  "source_id": "hn:48235065",
  "title": "Show HN: SessionFS — portable AI coding sessions across 9 tools",
  "url": "https://news.ycombinator.com/item?id=48235065",
  "content": "Posted a 2-line story title with a link out to sessionfs.dev — full body lives on the linked page, not on HN.",
  "posted_at": "2026-05-22T15:00:00Z",
  "author": "sessionfs",
  "signal_strength": 0.42,
  "raw": {
    "objectID": "48235065",
    "points": 38,
    "num_comments": 12,
    "story_text": null,
    "url": "https://sessionfs.dev"
  }
}
```

### GitHub Releases

Upstream: `https://api.github.com/repos/{owner}/{repo}/releases?per_page=10`

```json
{
  "source": "gh_release",
  "source_id": "gh:openai/openai-python@v2.4.0",
  "title": "openai/openai-python v2.4.0 — Responses API hardening",
  "url": "https://github.com/openai/openai-python/releases/tag/v2.4.0",
  "content": "## What's Changed\n* Add streaming for Responses API\n* Fix retry on 429 (...)",
  "posted_at": "2026-05-21T19:42:11Z",
  "author": "openai",
  "signal_strength": 1.0,
  "raw": {
    "id": 218492045,
    "tag_name": "v2.4.0",
    "prerelease": false,
    "draft": false,
    "html_url": "https://github.com/openai/openai-python/releases/tag/v2.4.0",
    "body": "<full release body>"
  }
}
```

### Reddit

Upstream: `https://www.reddit.com/r/{subreddit}/new.json?limit=25`

```json
{
  "source": "reddit",
  "source_id": "reddit:abc1234",
  "title": "Is anyone using SessionFS in production?",
  "url": "https://www.reddit.com/r/LocalLLaMA/comments/abc1234/...",
  "content": "Question about whether anyone's running it as a daemon with cloud sync. [PII scrubbed.]",
  "posted_at": "2026-05-22T10:14:00Z",
  "author": "u/some_user",
  "signal_strength": 0.28,
  "raw": {
    "id": "abc1234",
    "score": 24,
    "num_comments": 8,
    "subreddit": "LocalLLaMA",
    "permalink": "/r/LocalLLaMA/comments/abc1234/..."
  }
}
```

### Generic RSS / Atom

Upstream: any RSS 2.0 or Atom 1.0 feed.

```json
{
  "source": "rss",
  "source_id": "rss:https://anthropic.com/news/feed.xml@2026-05-22-announcing",
  "title": "Announcing Claude Opus 4.7",
  "url": "https://anthropic.com/news/announcing-claude-opus-4-7",
  "content": "We're releasing Claude Opus 4.7 with improved reasoning and a 1M-token context window. (...)",
  "posted_at": "2026-05-22T14:00:00Z",
  "author": "Anthropic",
  "signal_strength": 0.5,
  "raw": {
    "feed_url": "https://anthropic.com/news/feed.xml",
    "guid": "announcing-claude-opus-4-7",
    "description": "<full description>"
  }
}
```

### Generic webhook (catch-all for sources with no list endpoint)

For sources without a pollable list endpoint (status pages, Stripe
webhooks, Sentry alerts), wire the upstream's webhook directly to
an n8n Webhook node, then run a normalizer that conforms the body
to this same shape. Choose `source` as a free slug (`"stripe"`,
`"sentry"`, etc.). `source_id` should embed enough of the upstream
event ID to be globally unique. `signal_strength` defaults to
`0.5` unless the upstream gives a useful priority field.

---

## 5. PII scrubbing — hard rules

Two sources expose user-generated content where naive forwarding
would surface PII into SessionFS's audit trail:

### Reddit

Reddit usernames are public but Scout should **NOT** include them
in `content`. Use them in `author` only (where the downstream LLM
can ignore them) and ALWAYS prefix with `u/` so the
provenance-rendering layer can recognize and redact in dashboards.

When body text contains an `@-mention` of another user, the
normalizer MUST replace it with `[user]` before assignment to
`content`. Same rule for direct quotes that name a specific user.

Subreddit names are fine — they're public metadata.

### Discord (deferred — guidance for whenever it lands)

Discord messages include user IDs (numeric snowflakes), display
names, and often DM-style content. The normalizer MUST:

1. Strip user IDs from `content` (replace with `[user]`).
2. Use the channel name in `source_id`, NOT the user.
3. Never forward a DM (channel type 1 / GROUP_DM type 3). Only
   public guild channels (GUILD_TEXT type 0, GUILD_ANNOUNCEMENT
   type 5).
4. Drop attachments — Scout reads text only.

These rules are enforced at the normalizer; the SessionFS server
does NOT re-scrub Scout's writes. If the normalizer is wrong,
the PII leaks into the KB.

### Why these rules and not platform DLP

SessionFS has org-side DLP (the v0.9.x scrubber), but it covers
secrets + PHI patterns (SSNs, credit cards, API keys), not
public-internet usernames. Adapter-side scrubbing is the right
boundary because the adapter knows source semantics; DLP doesn't.

---

## 6. Truncation discipline

The 4000-char cap on `content` exists because:

1. Token cost — a multi-source run with 30 signals at 4000 chars
   each is 120k chars (~30k tokens) before the LLM reasoning step.
2. KB write cost — agent-authored entries should be dense, not
   verbose. The 4000-char ceiling forces the normalizer to
   extract the lede.
3. PostgreSQL TEXT performance — gin_trgm indexes (Phase 4a's
   `idx_knowledge_persona_recent` neighbor) degrade on huge
   blobs.

Truncation rules:

1. Truncate from the END, not the beginning. The lede contains
   the headline + first paragraph; readers + LLMs both get
   strongest signal from the top.
2. Append a literal `"\n\n[truncated]"` marker when truncation
   happens, so downstream consumers know the content is partial.
3. If the source has a native "summary" field (RSS
   `<description>`, GH release `body` first paragraph), prefer
   that over a hard truncation of the full body.
4. For HN: the `story_text` field is usually empty (HN stories
   are link-out); use the linked page's `<meta name="description">`
   if you have it, otherwise pass `""`.

---

## 7. Filter rules per source

The normalizer's other job (besides shape) is **upfront filtering**
so the Scout reasoning loop only sees signals worth reasoning
about. Each adapter defines its own filter heuristic, but the
shared principle: **shipped > announced > rumored**.

| Source | Drop if | Keep if |
|--------|---------|---------|
| HN | `points < 5` AND no `sessionfs` substring in title | story has clear product-launch / new-tool / model-release shape |
| GH releases | `draft == true` OR `prerelease == true` (unless explicitly tracked) | `prerelease == false` AND has release notes |
| Reddit | `score < 3` AND subreddit not in core list | subreddit is `LocalLLaMA`, `programming`, `MachineLearning`, `agi`, `SessionFS`, etc. |
| RSS | `posted_at` older than `(now - 7 days)` | recent + in tracked feed |

The filter MUST run **before** the dedup query — otherwise Scout
wastes one `GET /entries?source_filter=...` per junk item.

---

## 8. The n8n merge-and-iterate pattern

The v5 workflow is:

```
Schedule
  ↓
Config (n8n Set node — workflow id, project id, persona, dedup keys)
  ↓
Build trigger_ref (per scout-n8n.md §2.2 — durable composite)
  ↓
Verify Scout Persona (per scout-n8n.md §2.1 — preflight, fail-fast on 404)
  ↓
Create AgentRun (per scout-n8n.md §2.2 — queued)
  ↓
Fetch Prior Findings (per scout-n8n.md §2.3 — limit=30, no claim_class filter)
  ↓
┌─────────────────────────────────── parallel ────────────────────────────────┐
│ HTTP Request: HN          → Code: normalize-hn          →                    │
│ HTTP Request: GH releases → Code: normalize-gh-releases →                    │
│ HTTP Request: Reddit      → Code: normalize-reddit      →                    │
│ HTTP Request: RSS         → Code: normalize-rss-generic →                    │
└──────────────────────────────────────────────────────────────────────────────┘
  ↓ (Merge: append)
SplitOut (one item per signal)
  ↓
Code: pre-write dedup (GET /entries?source_filter=<source_id>&limit=1)
  ↓
IF (not already persisted)
  ↓
HTTP Request: LLM reasoning (one call per signal OR batched — workflow choice)
  ↓
Code: parse + enforce MAX_KB_WRITES_PER_RUN=20
  ↓
IF (not noise)
  ↓
HTTP Request: POST /entries/add (persona_name=scout, author_class=agent,
                                  source_context=scout:n8n:<wf>:<exec>:<source_id>)
  ↓
Aggregate Summary (count writes, count tickets, count drops)
  ↓
HTTP Request: POST /agent-runs/{run_id}/complete (status=passed | errored)
```

**Adding source N+1 = three nodes:**
1. New `HTTP Request: <Source>` for fetch.
2. New `Code: normalize-<source>` for canonicalization.
3. Wire to the existing `Merge` node's next input pin.

The reasoning, dedup, write, and complete-AgentRun steps never
change. This is the abstraction the Phase 4c deliverable
unlocks.

### Merge node configuration

n8n's `Merge` node in "Append" mode concatenates all input streams
into one. Each input pin is a parallel source branch; the Merge
node emits one combined stream when ALL inputs have run. This
gives the dedup step a single uniform list, and a single LLM call
can reason across sources holistically.

### Failure isolation

If one source fails (HN Algolia returns 503), the Merge node still
fires once the other sources complete — Scout doesn't lose the
whole run for one flaky upstream. Wire each source's HTTP node
with a "Continue on Fail" setting and let the normalizer's
validation rules (§1) drop empty/malformed items.

The AgentRun severity matrix (`scout-n8n.md` §5) should be updated
for v5 to use:

| Failure class | Severity |
|---------------|----------|
| ALL sources failed (no signals from any branch) | `high` |
| One source failed, others succeeded | `low` (transient — next run probably recovers) |
| LLM reasoning crashed mid-stream | `medium` |
| `POST /entries/add` returned 422 (persona typo, schema drift) | `high` |
| `POST /entries/add` returned 5xx (server outage) | `medium` |

---

## 9. Out of scope (Phase 4d+ future work)

The following deliberately are NOT in this contract — they're
follow-ups when Scout v5 actually hits them:

- **Cross-source `signal_strength` normalization**: today each
  source's heuristic is within-source. If Scout v5 grows a
  "top 10 across all sources" output, normalize then.
- **Per-source rate limiting / caching**: the Reddit API has a
  rate ceiling that single-workflow polls won't hit. If Scout v6
  adds a second Reddit-watching agent (Ledger or Relay), then
  introduce a shared cache.
- **Twitter / X normalizer**: API + ToS friction is real. Defer
  until either the platform's free tier supports listening or
  there's a specific competitor whose Twitter feed Scout MUST
  watch.
- **Discord normalizer**: included as a "deferred — guidance"
  entry in §5 above. Build when there's a specific Discord
  server worth watching for Scout's purposes.
- **Pricing-page differ**: a normalizer that compares competitor
  pricing-page snapshots from one run to the next, emitting
  signals only on diff. Distinct enough from event-stream
  adapters that it deserves a separate doc.
- **Adapter unit tests inside the SessionFS repo**: today the
  test fixtures live alongside the .js templates in
  `docs/integrations/n8n-source-adapters/`. If we add a `tests/`
  harness for them (Node-side runner), that's separate.

---

## 10. Reference: the four shipped templates

Each template is a self-contained n8n Code node body. Copy the
file content into a Code node, leave the node's `mode` as
`"runOnceForEachItem"` (the canonical input is one HTTP Request
response item per fetch), and wire it downstream of the matching
HTTP Request node.

| Adapter | File | Upstream auth |
|---------|------|---------------|
| HN Algolia | [`n8n-source-adapters/hn-algolia.js`](n8n-source-adapters/hn-algolia.js) | none (public API) |
| GitHub Releases | [`n8n-source-adapters/gh-releases.js`](n8n-source-adapters/gh-releases.js) | GitHub PAT recommended (raises rate limit from 60/hr to 5000/hr); unauth works for low-volume |
| Reddit JSON | [`n8n-source-adapters/reddit.js`](n8n-source-adapters/reddit.js) | none for `.json` endpoints, but set a descriptive `User-Agent` header per Reddit ToS |
| Generic RSS | [`n8n-source-adapters/rss-generic.js`](n8n-source-adapters/rss-generic.js) | none (public feed) |

Each file ships with an inline `// FIXTURE` block at the bottom: a
canonical upstream sample + the expected normalized output. To
add a new source category, copy `rss-generic.js`, rename, rewrite
the field extraction, update the FIXTURE, and append a row to §3
+ §7 + §10 above.
