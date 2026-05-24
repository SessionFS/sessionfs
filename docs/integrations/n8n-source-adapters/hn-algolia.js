// HN Algolia → Scout uniform signal shape (Phase 4c).
//
// Upstream: https://hn.algolia.com/api/v1/search?tags=story&hitsPerPage=50
// Auth:     none (public)
// Cadence:  every 30 min is plenty; HN's signal half-life is hours.
//
// n8n Code node mode: "runOnceForEachItem" — input is one Algolia hit.
// Drops items that fail validation rules in scout-signal-shape.md §1.
//
// Signal_strength heuristic (within-HN):
//   min(1.0, (points + num_comments * 0.5) / 100)
// Rationale in scout-signal-shape.md §3.

const hit = $json;

// 1. Required upstream fields. Drop if missing.
if (!hit || !hit.objectID || !hit.title) {
  return [];
}

// 2. Filter: drop low-signal items unless they name the product.
const title = String(hit.title).trim();
const titleHasProduct = title.toLowerCase().includes('sessionfs');
const points = Number(hit.points) || 0;
if (points < 5 && !titleHasProduct) {
  return [];
}

// 3. URL — HN stories have an optional outbound `url`; fall back to
//    the HN item permalink. Reject non-https outbounds (defensive —
//    HN sometimes carries http:// or ftp:// links).
const itemUrl = `https://news.ycombinator.com/item?id=${hit.objectID}`;
let url = itemUrl;
if (hit.url && typeof hit.url === 'string' && hit.url.startsWith('https://')) {
  url = hit.url;
}

// 4. Content. HN stories rarely carry body text (`story_text` is
//    usually null); when present, it's HTML. Strip tags crudely +
//    truncate per §6.
let content = '';
if (hit.story_text) {
  content = String(hit.story_text)
    .replace(/<[^>]+>/g, '')
    .replace(/&amp;/g, '&')
    .replace(/&lt;/g, '<')
    .replace(/&gt;/g, '>')
    .replace(/&quot;/g, '"')
    .replace(/&#x27;/g, "'")
    .trim();
}
if (content.length > 4000) {
  content = content.slice(0, 4000 - 14) + '\n\n[truncated]';
}

// 5. posted_at — Algolia exposes both `created_at` (ISO) and
//    `created_at_i` (epoch). Prefer ISO; coerce to Z-suffix.
let posted_at;
if (hit.created_at) {
  const d = new Date(hit.created_at);
  if (!isNaN(d.getTime())) {
    posted_at = d.toISOString();
  }
}
if (!posted_at && hit.created_at_i) {
  posted_at = new Date(hit.created_at_i * 1000).toISOString();
}
if (!posted_at) {
  return [];
}

// 6. Author — Algolia exposes `author` as a plain username string.
const author = (hit.author && typeof hit.author === 'string')
  ? hit.author.slice(0, 80)
  : '';

// 7. Signal strength.
const comments = Number(hit.num_comments) || 0;
const signal_strength = Math.min(1.0, (points + comments * 0.5) / 100);

return [{
  json: {
    source: 'hn',
    source_id: `hn:${hit.objectID}`,
    title: title.length > 240 ? title.slice(0, 239) + '…' : title,
    url,
    content,
    posted_at,
    author,
    signal_strength,
    raw: hit,
  },
}];

// FIXTURE — canonical Algolia hit and the canonical normalizer output.
//
// INPUT (one item from `hits` array of
// https://hn.algolia.com/api/v1/search?tags=story&query=sessionfs):
//
//   {
//     "objectID": "48235065",
//     "title": "Show HN: SessionFS — portable AI coding sessions across 9 tools",
//     "url": "https://sessionfs.dev",
//     "points": 38,
//     "num_comments": 12,
//     "story_text": null,
//     "author": "sessionfs",
//     "created_at": "2026-05-22T15:00:00.000Z",
//     "created_at_i": 1779804000,
//     "_tags": ["story", "author_sessionfs", "story_48235065"]
//   }
//
// EXPECTED OUTPUT (after this Code node):
//
//   {
//     "source": "hn",
//     "source_id": "hn:48235065",
//     "title": "Show HN: SessionFS — portable AI coding sessions across 9 tools",
//     "url": "https://sessionfs.dev",
//     "content": "",
//     "posted_at": "2026-05-22T15:00:00.000Z",
//     "author": "sessionfs",
//     "signal_strength": 0.44,
//     "raw": { /* the input above, untouched */ }
//   }
//
// (signal_strength = min(1.0, (38 + 12*0.5) / 100) = 0.44)
