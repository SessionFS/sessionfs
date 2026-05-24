// Generic RSS / Atom → Scout uniform signal shape (Phase 4c).
//
// Upstream: any RSS 2.0 or Atom 1.0 feed.
// Auth:     none.
// Cadence:  every 60 min is plenty for most news/blog feeds.
//
// n8n setup: use the built-in "RSS Read" node to pull items, then
// pipe to this Code node in "runOnceForEachItem" mode. The RSS Read
// node already normalizes most field names to a flat shape
// (`title`, `link`, `pubDate`, `contentSnippet`, `creator`, `guid`).
//
// Signal_strength: flat 0.5 — RSS has no native engagement metric.
// The Scout LLM ranks RSS items against each other in the
// reasoning loop, not at normalizer time.

const item = $json;

if (!item || !item.title) {
  return [];
}

// 1. URL — RSS Read maps `<link>` (RSS) and `<link href="...">`
//    (Atom) to `link`. Require https.
const url = item.link;
if (!url || typeof url !== 'string' || !url.startsWith('https://')) {
  return [];
}

// 2. Title.
let title = String(item.title).trim();
if (!title) return [];
if (title.length > 240) {
  title = title.slice(0, 239) + '…';
}

// 3. Content — prefer `contentSnippet` (text-only plaintext) over
//    `content` (HTML). Strip tags as a fallback if only HTML is
//    present.
let content = '';
const snippet = item.contentSnippet || item['content:encodedSnippet'];
if (snippet && typeof snippet === 'string') {
  content = snippet.trim();
} else if (item.content && typeof item.content === 'string') {
  content = item.content
    .replace(/<[^>]+>/g, '')
    .replace(/&amp;/g, '&')
    .replace(/&lt;/g, '<')
    .replace(/&gt;/g, '>')
    .replace(/&quot;/g, '"')
    .replace(/&#x27;/g, "'")
    .replace(/\s+/g, ' ')
    .trim();
}
if (content.length > 4000) {
  content = content.slice(0, 4000 - 14) + '\n\n[truncated]';
}

// 4. posted_at — RSS Read maps `<pubDate>` / `<published>` to
//    `pubDate` or `isoDate`. Prefer isoDate (already ISO-8601).
const tsRaw = item.isoDate || item.pubDate;
if (!tsRaw) {
  return [];
}
const d = new Date(tsRaw);
if (isNaN(d.getTime())) {
  return [];
}
const posted_at = d.toISOString();

// 5. Drop items older than 7 days — RSS readers can sometimes
//    backfill long histories on first read; Scout only cares
//    about new signal.
const SEVEN_DAYS_MS = 7 * 24 * 60 * 60 * 1000;
if (Date.now() - d.getTime() > SEVEN_DAYS_MS) {
  return [];
}

// 6. Author — RSS `<author>` or `<dc:creator>`; Atom `<author><name>`.
const author = (item.creator || item.author || '').toString().slice(0, 80);

// 7. source_id — Atom feeds usually have `guid`; RSS may have `guid`
//    or just rely on `link`. Use guid if present; else hash the
//    link with feed scope to keep cross-feed collisions out.
let nativeId;
if (item.guid && typeof item.guid === 'string' && item.guid.trim()) {
  nativeId = item.guid.trim();
} else {
  nativeId = url;
}
// Bound source_id length: signal-shape §1 says max 200 chars,
// "rss:" prefix is 4, leave 196 for the native part.
if (nativeId.length > 196) {
  nativeId = nativeId.slice(0, 196);
}
const source_id = `rss:${nativeId}`;

return [{
  json: {
    source: 'rss',
    source_id,
    title,
    url,
    content,
    posted_at,
    author,
    signal_strength: 0.5,
    raw: item,
  },
}];

// FIXTURE.
//
// INPUT (one item emitted by n8n's RSS Read node after parsing
// https://anthropic.com/news/feed.xml):
//
//   {
//     "title": "Announcing Claude Opus 4.7",
//     "link": "https://anthropic.com/news/announcing-claude-opus-4-7",
//     "pubDate": "Thu, 22 May 2026 14:00:00 GMT",
//     "isoDate": "2026-05-22T14:00:00.000Z",
//     "creator": "Anthropic",
//     "contentSnippet": "We're releasing Claude Opus 4.7 with improved reasoning and a 1M-token context window.",
//     "content": "<p>We're releasing Claude Opus 4.7 with improved reasoning and a 1M-token context window.</p>",
//     "guid": "announcing-claude-opus-4-7"
//   }
//
// EXPECTED OUTPUT:
//
//   {
//     "source": "rss",
//     "source_id": "rss:announcing-claude-opus-4-7",
//     "title": "Announcing Claude Opus 4.7",
//     "url": "https://anthropic.com/news/announcing-claude-opus-4-7",
//     "content": "We're releasing Claude Opus 4.7 with improved reasoning and a 1M-token context window.",
//     "posted_at": "2026-05-22T14:00:00.000Z",
//     "author": "Anthropic",
//     "signal_strength": 0.5,
//     "raw": { /* the input above, untouched */ }
//   }
