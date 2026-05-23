// GitHub Releases → Scout uniform signal shape (Phase 4c).
//
// Upstream: https://api.github.com/repos/{owner}/{repo}/releases?per_page=10
// Auth:     unauthenticated works for low volume (60 req/hr per IP);
//           an n8n Header Auth credential with `Authorization: Bearer <PAT>`
//           raises to 5000 req/hr per token.
// Cadence:  every 60 min is fine. GitHub release cadence is days, not minutes.
//
// n8n Code node mode: "runOnceForEachItem" — one Release row per fire.
//
// Filter (scout-signal-shape.md §7):
//   - Drop drafts entirely (security: drafts can leak unreleased
//     work; never persist into the KB).
//   - Keep prereleases but mark them signal_strength=0.5.
//
// Signal_strength:
//   1.0 if !prerelease and !draft
//   0.5 if prerelease
//   (drafts are dropped above so 0.0 case never emits)

const rel = $json;

if (!rel || !rel.id || !rel.tag_name) {
  return [];
}

// 1. Drop drafts.
if (rel.draft === true) {
  return [];
}

// 2. owner/repo recovery — html_url is the canonical form.
//    Pattern: https://github.com/{owner}/{repo}/releases/tag/{tag}
const url = rel.html_url || '';
if (!url.startsWith('https://github.com/')) {
  return [];
}
const m = url.match(/^https:\/\/github\.com\/([^/]+)\/([^/]+)\/releases\/tag\/(.+)$/);
if (!m) {
  return [];
}
const [, owner, repo] = m;
const tag = rel.tag_name;

// 3. Title — prefer `name` (human-readable), fall back to tag.
const niceName = (rel.name && String(rel.name).trim()) || tag;
let title = `${owner}/${repo} ${tag}`;
if (niceName !== tag) {
  title = `${owner}/${repo} ${tag} — ${niceName}`;
}
if (title.length > 240) {
  title = title.slice(0, 239) + '…';
}

// 4. Content — release body is markdown. Keep as-is; truncate at 4000.
let content = (rel.body && typeof rel.body === 'string') ? rel.body : '';
if (content.length > 4000) {
  content = content.slice(0, 4000 - 14) + '\n\n[truncated]';
}

// 5. posted_at — prefer `published_at`; fall back to `created_at`.
const tsRaw = rel.published_at || rel.created_at;
if (!tsRaw) {
  return [];
}
const d = new Date(tsRaw);
if (isNaN(d.getTime())) {
  return [];
}
const posted_at = d.toISOString();

// 6. Author — release publisher. `author.login` is the GitHub
//    username; fall back to owner (the repo itself).
const author = (rel.author && rel.author.login)
  ? String(rel.author.login).slice(0, 80)
  : owner;

// 7. Signal.
const signal_strength = rel.prerelease ? 0.5 : 1.0;

return [{
  json: {
    source: 'gh_release',
    source_id: `gh:${owner}/${repo}@${tag}`,
    title,
    url,
    content,
    posted_at,
    author,
    signal_strength,
    raw: rel,
  },
}];

// FIXTURE.
//
// INPUT (one item from GET /repos/openai/openai-python/releases?per_page=1):
//
//   {
//     "id": 218492045,
//     "tag_name": "v2.4.0",
//     "name": "Responses API hardening",
//     "draft": false,
//     "prerelease": false,
//     "html_url": "https://github.com/openai/openai-python/releases/tag/v2.4.0",
//     "body": "## What's Changed\n* Add streaming for Responses API\n* Fix retry on 429",
//     "published_at": "2026-05-21T19:42:11Z",
//     "created_at":   "2026-05-21T19:30:00Z",
//     "author": { "login": "openai-bot", "id": 1, "type": "User" }
//   }
//
// EXPECTED OUTPUT:
//
//   {
//     "source": "gh_release",
//     "source_id": "gh:openai/openai-python@v2.4.0",
//     "title": "openai/openai-python v2.4.0 — Responses API hardening",
//     "url": "https://github.com/openai/openai-python/releases/tag/v2.4.0",
//     "content": "## What's Changed\n* Add streaming for Responses API\n* Fix retry on 429",
//     "posted_at": "2026-05-21T19:42:11.000Z",
//     "author": "openai-bot",
//     "signal_strength": 1.0,
//     "raw": { /* the input above, untouched */ }
//   }
